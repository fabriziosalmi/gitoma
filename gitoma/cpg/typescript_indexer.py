"""CPG-lite v0.5-slim — TypeScript AST → Symbol/Reference rows.

Parses ``.ts`` / ``.tsx`` files via ``tree-sitter`` +
``tree-sitter-typescript`` and emits the same Symbol / Reference /
Import row shapes as :mod:`gitoma.cpg.python_indexer`. The on-disk
schema is identical; ``Symbol.language`` carries ``"typescript"``
so callers can filter when needed.

Coverage in v0.5-slim (intentionally narrow — see sprint plan
``project_sprint_cpg_lite_v05_slim.md`` for the deferred list):

* ``function_declaration`` → Symbol(kind=function)
* ``class_declaration`` → Symbol(kind=class)
* ``method_definition`` (in class_body) → Symbol(kind=method)
* ``interface_declaration`` → Symbol(kind=interface)
* ``type_alias_declaration`` → Symbol(kind=type_alias)
* ``lexical_declaration`` (top-level ``const`` / ``let``) →
  Symbol(kind=assignment) for single-Identifier targets only
* ``import_statement`` → Symbol(kind=import) per binding +
  Import row + IMPORT_FROM Reference
* ``call_expression`` → Reference(kind=call)
* ``member_expression`` (load) → Reference(kind=attribute_access)
* ``identifier`` in expression context → Reference(kind=name_load)
* ``extends_clause`` / ``implements_clause`` →
  Reference(kind=inheritance)

Out of scope (v0.5-slim):

* JSX usage as references — components are indexed (they're
  function declarations) but ``<MyComp />`` is not yet a ref.
* Type-level references — a function returning ``User`` does not
  emit a ref to the User interface.
* TypeScript namespace declarations (legacy syntax).
* tsconfig.json path resolution for absolute / aliased imports.

The parser is cached at module level — building a tree-sitter
Parser is non-trivial (~20ms) and we'd otherwise pay it per file.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from gitoma.cpg._base import (
    Reference,
    RefKind,
    Symbol,
    SymbolKind,
    compose_qualified_name,
)
from gitoma.cpg.storage import Storage

__all__ = [
    "index_typescript_file",
    "ts_module_qualified_name_for",
    "TS_LANGUAGE",
]

TS_LANGUAGE = "typescript"


# ── Lazy-cached parsers (one per grammar) ──────────────────────────
# tree-sitter and tree-sitter-typescript are RUNTIME-OPTIONAL deps.
# When they're missing (slim CI image, plugin install gone wrong,
# whatever), we degrade to "no TS support" rather than blow up the
# whole CPG layer. The flag below is set on first import attempt.

_parser_lock = threading.Lock()
_ts_parser: Any = None
_tsx_parser: Any = None
_import_failed = False


def _get_parser(is_tsx: bool):  # noqa: ANN201 — return type depends on optional dep
    """Return a tree-sitter Parser configured for the right grammar.
    Builds once per process; raises if the optional deps aren't
    importable (callers must catch and treat as "no TS support")."""
    global _ts_parser, _tsx_parser, _import_failed
    with _parser_lock:
        if _import_failed:
            raise RuntimeError("tree-sitter-typescript not available")
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_typescript
        except ImportError as exc:  # pragma: no cover — exercised live
            _import_failed = True
            raise RuntimeError(
                f"tree-sitter-typescript unavailable: {exc}"
            ) from exc
        if is_tsx:
            if _tsx_parser is None:
                _tsx_parser = Parser(Language(tree_sitter_typescript.language_tsx()))
            return _tsx_parser
        if _ts_parser is None:
            _ts_parser = Parser(Language(tree_sitter_typescript.language_typescript()))
        return _ts_parser


def ts_module_qualified_name_for(rel_path: str) -> str:
    """Convert a repo-relative path like ``src/components/Button.tsx``
    to a qualified module name ``src.components.Button``. Drops any
    of ``.ts``/``.tsx``/``.d.ts`` suffixes; ``index.ts(x)`` collapses
    to its parent (matching Node module-resolution conventions)."""
    p = rel_path.replace("\\", "/")
    for suffix in (".d.ts", ".tsx", ".ts"):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
            break
    parts = [seg for seg in p.split("/") if seg]
    if parts and parts[-1] == "index":
        parts.pop()
    return ".".join(parts)


def index_typescript_file(
    path: Path, rel_path: str, storage: Storage,
) -> int:
    """Parse ``path`` as TypeScript and emit Symbol + Reference +
    Import rows into ``storage``. Returns Symbol row count.

    Errors (parser unavailable, file unreadable, parse failure) are
    silent → return 0. Same defensive contract as
    :func:`gitoma.cpg.python_indexer.index_python_file`.
    """
    is_tsx = rel_path.endswith(".tsx")
    try:
        parser = _get_parser(is_tsx)
    except RuntimeError:
        return 0
    try:
        src = path.read_bytes()
    except OSError:
        return 0
    if not src:
        # Even empty file gets a module symbol — keeps parity with
        # Python indexer's empty-file behavior.
        return _emit_module_only(rel_path, storage)
    try:
        tree = parser.parse(src)
    except Exception:  # noqa: BLE001 — defensive on grammar bugs
        return 0
    visitor = _TSVisitor(rel_path=rel_path, src=src, storage=storage)
    visitor.visit_program(tree.root_node)
    return visitor.symbol_count


def _emit_module_only(rel_path: str, storage: Storage) -> int:
    qname = ts_module_qualified_name_for(rel_path)
    leaf = qname.rsplit(".", 1)[-1] if qname else ""
    storage.insert_symbol(Symbol(
        id=0, file=rel_path, line=1, col=0,
        kind=SymbolKind.MODULE, name=leaf,
        qualified_name=qname, parent_id=None,
        is_public=not leaf.startswith("_"),
        language=TS_LANGUAGE,
    ))
    return 1


class _TSVisitor:
    """tree-sitter walker emitting CPG rows. Mirrors the shape of
    :class:`gitoma.cpg.python_indexer._Visitor` so future maintainers
    can read both side-by-side."""

    def __init__(self, rel_path: str, src: bytes, storage: Storage) -> None:
        self._rel_path = rel_path
        self._src = src
        self._storage = storage
        self._scope_stack: list[tuple[str, int | None]] = []
        self._class_depth = 0
        self.symbol_count = 0

    # ── Module entry ───────────────────────────────────────────────

    def visit_program(self, node: Any) -> None:
        module_qname = ts_module_qualified_name_for(self._rel_path)
        leaf = module_qname.rsplit(".", 1)[-1] if module_qname else ""
        module_id = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=1, col=0,
            kind=SymbolKind.MODULE, name=leaf,
            qualified_name=module_qname, parent_id=None,
            is_public=not leaf.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1
        self._scope_stack.append((module_qname, module_id))
        for child in node.named_children:
            self._dispatch(child)
        self._scope_stack.pop()

    # ── Dispatch ───────────────────────────────────────────────────

    def _dispatch(self, node: Any) -> None:
        method = getattr(self, f"_visit_{node.type}", None)
        if method is not None:
            method(node)
            return
        # Default: walk into children so nested defs / refs aren't
        # missed when we don't have a specific handler.
        for child in node.named_children:
            self._dispatch(child)

    # ── Helpers ────────────────────────────────────────────────────

    def _text(self, node: Any) -> str:
        return self._src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace",
        )

    def _line_col(self, node: Any) -> tuple[int, int]:
        # tree-sitter rows are 0-based; we keep 1-based to match Python.
        return node.start_point[0] + 1, node.start_point[1]

    def _qualified(self, leaf: str) -> str:
        return compose_qualified_name(
            tuple(part for part, _ in self._scope_stack) + (leaf,),
        )

    def _parent_id(self) -> int | None:
        return self._scope_stack[-1][1] if self._scope_stack else None

    # ── Export statement: peel and recurse ────────────────────────

    def _visit_export_statement(self, node: Any) -> None:
        # Children of export_statement include the actual declaration
        # (function_declaration / class_declaration / lexical_declaration
        # / interface_declaration / type_alias_declaration). Just
        # dispatch each — they emit Symbols in the current scope and
        # are implicitly public when exported.
        for child in node.named_children:
            self._dispatch(child)

    # ── Imports ────────────────────────────────────────────────────

    def _visit_import_statement(self, node: Any) -> None:
        # `import { A, B as C } from 'module'` OR
        # `import D from 'module'` OR
        # `import * as ns from 'module'`
        source_node = node.child_by_field_name("source")
        module_path = ""
        if source_node is not None:
            # source is a `string` node; strip the quotes by reading
            # the inner string_fragment if present, else strip text.
            frag = source_node.named_child(0) if source_node.named_child_count else None
            if frag is not None and frag.type == "string_fragment":
                module_path = self._text(frag)
            else:
                module_path = self._text(source_node).strip("'\"")
        # `import_clause` isn't exposed as a named field in this
        # grammar version — find it via type match on named children.
        clause = next(
            (c for c in node.named_children if c.type == "import_clause"),
            None,
        )
        if clause is None:
            # bare `import 'side-effect'` — just the import_statement
            # with no clause; we don't emit anything for it.
            return
        line, col = self._line_col(node)
        bindings = self._parse_import_clause(clause)
        for imported_name, bound_name, kind in bindings:
            self._storage.insert_import(
                self._rel_path, module_path, bound_name, line,
            )
            qname = self._qualified(bound_name)
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=line, col=col,
                kind=SymbolKind.IMPORT, name=bound_name,
                qualified_name=qname, parent_id=self._parent_id(),
                is_public=not bound_name.startswith("_"),
                language=TS_LANGUAGE,
            ))
            self.symbol_count += 1
            if kind == "named":
                # Record an IMPORT_FROM reference for the original
                # name so callers_of() picks up the binding link
                # the same way Python from-imports do.
                self._storage.insert_reference(Reference(
                    symbol_id=None, raw_name=imported_name,
                    file=self._rel_path, line=line, col=col,
                    kind=RefKind.IMPORT_FROM,
                ))

    def _parse_import_clause(self, clause: Any) -> list[tuple[str, str, str]]:
        """Return a list of ``(imported_name, bound_name, kind)``
        triples. ``kind`` ∈ ``{"default", "named", "namespace"}``."""
        out: list[tuple[str, str, str]] = []
        for child in clause.named_children:
            if child.type == "identifier":
                name = self._text(child)
                out.append(("default", name, "default"))
            elif child.type == "named_imports":
                for spec in child.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name_n = spec.child_by_field_name("name")
                    alias_n = spec.child_by_field_name("alias")
                    if name_n is None:
                        continue
                    imported = self._text(name_n)
                    bound = self._text(alias_n) if alias_n else imported
                    out.append((imported, bound, "named"))
            elif child.type == "namespace_import":
                # `* as fs` — bound name is the identifier; nothing
                # is "imported by name" in the named-import sense.
                ident = next(
                    (c for c in child.named_children if c.type == "identifier"),
                    None,
                )
                if ident is not None:
                    name = self._text(ident)
                    out.append(("*", name, "namespace"))
        return out

    # ── Function declarations ─────────────────────────────────────

    def _visit_function_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.FUNCTION, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=not name.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1
        # Recurse into body to catch nested calls / name loads.
        body = node.child_by_field_name("body")
        self._scope_stack.append((name, sid))
        if body is not None:
            for child in body.named_children:
                self._dispatch(child)
        self._scope_stack.pop()

    # ── Class declarations ────────────────────────────────────────

    def _visit_class_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.CLASS, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=not name.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1
        # Heritage clauses produce inheritance refs in current scope.
        heritage = next(
            (c for c in node.named_children if c.type == "class_heritage"),
            None,
        )
        if heritage is not None:
            self._record_heritage(heritage)
        # Recurse into class_body.
        body = node.child_by_field_name("body")
        self._scope_stack.append((name, sid))
        self._class_depth += 1
        if body is not None:
            for child in body.named_children:
                self._dispatch(child)
        self._class_depth -= 1
        self._scope_stack.pop()

    def _record_heritage(self, heritage: Any) -> None:
        for clause in heritage.named_children:
            if clause.type not in ("extends_clause", "implements_clause"):
                continue
            # The base name(s) are identifier or generic_type within.
            for child in clause.named_children:
                self._record_heritage_target(child)

    def _record_heritage_target(self, node: Any) -> None:
        if node.type in ("identifier", "type_identifier"):
            line, col = self._line_col(node)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=self._text(node),
                file=self._rel_path, line=line, col=col,
                kind=RefKind.INHERITANCE,
            ))
        elif node.type == "generic_type":
            # `Base<T>` — record the head identifier as inheritance.
            head = next(
                (c for c in node.named_children
                 if c.type in ("identifier", "type_identifier")),
                None,
            )
            if head is not None:
                self._record_heritage_target(head)
        else:
            for child in node.named_children:
                self._record_heritage_target(child)

    # ── Method definitions (within class_body) ────────────────────

    def _visit_method_definition(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.METHOD if self._class_depth else SymbolKind.FUNCTION,
            name=name, qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=not name.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1
        body = node.child_by_field_name("body")
        self._scope_stack.append((name, sid))
        if body is not None:
            for child in body.named_children:
                self._dispatch(child)
        self._scope_stack.pop()

    # ── Interface + type alias ────────────────────────────────────

    def _visit_interface_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.INTERFACE, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=not name.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1

    def _visit_type_alias_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.TYPE_ALIAS, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=not name.startswith("_"),
            language=TS_LANGUAGE,
        ))
        self.symbol_count += 1

    # ── const / let — module-level only ────────────────────────────

    def _visit_lexical_declaration(self, node: Any) -> None:
        # Module level only (scope_stack length 1 = just module).
        # Keeps parity with Python indexer's "module-level Name = ..."
        # rule. Inside functions we still dispatch into children so
        # call_expression / identifier refs from initialisers are
        # captured.
        if len(self._scope_stack) == 1:
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_n = declarator.child_by_field_name("name")
                if name_n is None or name_n.type != "identifier":
                    continue
                name = self._text(name_n)
                line, col = self._line_col(name_n)
                self._storage.insert_symbol(Symbol(
                    id=0, file=self._rel_path, line=line, col=col,
                    kind=SymbolKind.ASSIGNMENT, name=name,
                    qualified_name=self._qualified(name),
                    parent_id=self._parent_id(),
                    is_public=not name.startswith("_"),
                    language=TS_LANGUAGE,
                ))
                self.symbol_count += 1
                # Record refs in the value expression too.
                value = declarator.child_by_field_name("value")
                if value is not None:
                    self._dispatch(value)
        else:
            # Inside a function — walk children for refs only.
            for child in node.named_children:
                self._dispatch(child)

    # ── Calls + member access + bare names ─────────────────────────

    def _visit_call_expression(self, node: Any) -> None:
        callee = node.child_by_field_name("function")
        if callee is not None:
            if callee.type == "identifier":
                line, col = self._line_col(callee)
                self._storage.insert_reference(Reference(
                    symbol_id=None, raw_name=self._text(callee),
                    file=self._rel_path, line=line, col=col,
                    kind=RefKind.CALL,
                ))
            elif callee.type == "member_expression":
                prop = callee.child_by_field_name("property")
                if prop is not None and prop.type == "property_identifier":
                    line, col = self._line_col(prop)
                    self._storage.insert_reference(Reference(
                        symbol_id=None, raw_name=self._text(prop),
                        file=self._rel_path, line=line, col=col,
                        kind=RefKind.CALL,
                    ))
                obj = callee.child_by_field_name("object")
                if obj is not None:
                    self._dispatch(obj)
            else:
                self._dispatch(callee)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for child in args.named_children:
                self._dispatch(child)

    def _visit_member_expression(self, node: Any) -> None:
        prop = node.child_by_field_name("property")
        if prop is not None and prop.type == "property_identifier":
            line, col = self._line_col(prop)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=self._text(prop),
                file=self._rel_path, line=line, col=col,
                kind=RefKind.ATTRIBUTE_ACCESS,
            ))
        obj = node.child_by_field_name("object")
        if obj is not None:
            self._dispatch(obj)

    def _visit_identifier(self, node: Any) -> None:
        # Bare identifier in expression context = NAME_LOAD ref.
        # tree-sitter doesn't carry context as cleanly as Python's
        # ast; we accept some over-recording (e.g. parameter names)
        # rather than try to filter precisely.
        line, col = self._line_col(node)
        self._storage.insert_reference(Reference(
            symbol_id=None, raw_name=self._text(node),
            file=self._rel_path, line=line, col=col,
            kind=RefKind.NAME_LOAD,
        ))

    # `new Foo(...)` — `new_expression` wraps a constructor call;
    # tree-sitter exposes the constructor via the `constructor`
    # field. We treat it as a CALL ref on the class name.
    def _visit_new_expression(self, node: Any) -> None:
        ctor = node.child_by_field_name("constructor")
        if ctor is not None:
            if ctor.type in ("identifier", "type_identifier"):
                line, col = self._line_col(ctor)
                self._storage.insert_reference(Reference(
                    symbol_id=None, raw_name=self._text(ctor),
                    file=self._rel_path, line=line, col=col,
                    kind=RefKind.CALL,
                ))
            else:
                self._dispatch(ctor)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for child in args.named_children:
                self._dispatch(child)
