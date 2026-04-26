"""CPG-lite v0.5-expansion — JavaScript AST → Symbol/Reference rows.

Mirror of :mod:`gitoma.cpg.typescript_indexer` minus the
TypeScript-only constructs:

* No INTERFACE / TYPE_ALIAS — those are TS-only declarations.
* No type annotations on signatures — JS captures the parameter
  list verbatim; ``return_type`` field doesn't exist on
  function_declaration / method_definition.
* Same node-type names + named-children shape because
  tree-sitter-typescript inherits from tree-sitter-javascript.

Handles ``.js``, ``.mjs``, ``.cjs``. Strict-mode + classic-script
contexts both parse fine through the same grammar. CommonJS
``require()`` calls show up as CALL refs but do NOT produce
``imports`` rows in v0.5-expansion (deferred — see sprint plan).
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
    "index_javascript_file",
    "js_module_qualified_name_for",
    "JS_LANGUAGE",
]

JS_LANGUAGE = "javascript"

_MAX_SIGNATURE_CHARS = 200


# Lazy-cached parser (single grammar — no JSX/TSX dispatch needed
# at v0.5-expansion; .jsx future-work would use the same grammar).
_parser_lock = threading.Lock()
_js_parser: Any = None
_import_failed = False


def _get_parser():  # noqa: ANN201
    global _js_parser, _import_failed
    with _parser_lock:
        if _import_failed:
            raise RuntimeError("tree-sitter-javascript not available")
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_javascript
        except ImportError as exc:  # pragma: no cover — exercised live
            _import_failed = True
            raise RuntimeError(
                f"tree-sitter-javascript unavailable: {exc}"
            ) from exc
        if _js_parser is None:
            _js_parser = Parser(Language(tree_sitter_javascript.language()))
        return _js_parser


def js_module_qualified_name_for(rel_path: str) -> str:
    """Convert ``src/components/Button.js`` → ``src.components.Button``.
    Drops ``.js`` / ``.mjs`` / ``.cjs`` suffix; collapses ``index``
    to its parent (matching Node module resolution)."""
    p = rel_path.replace("\\", "/")
    for suffix in (".mjs", ".cjs", ".js"):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
            break
    parts = [seg for seg in p.split("/") if seg]
    if parts and parts[-1] == "index":
        parts.pop()
    return ".".join(parts)


def index_javascript_file(
    path: Path, rel_path: str, storage: Storage,
) -> int:
    """Parse ``path`` as JavaScript and emit Symbol+Reference+Import
    rows. Returns Symbol row count. Defensive contract identical to
    the Python / TS indexers."""
    try:
        parser = _get_parser()
    except RuntimeError:
        return 0
    try:
        src = path.read_bytes()
    except OSError:
        return 0
    if not src:
        return _emit_module_only(rel_path, storage)
    try:
        tree = parser.parse(src)
    except Exception:  # noqa: BLE001 — defensive on grammar bugs
        return 0
    visitor = _JSVisitor(rel_path=rel_path, src=src, storage=storage)
    visitor.visit_program(tree.root_node)
    return visitor.symbol_count


def _emit_module_only(rel_path: str, storage: Storage) -> int:
    qname = js_module_qualified_name_for(rel_path)
    leaf = qname.rsplit(".", 1)[-1] if qname else ""
    storage.insert_symbol(Symbol(
        id=0, file=rel_path, line=1, col=0,
        kind=SymbolKind.MODULE, name=leaf,
        qualified_name=qname, parent_id=None,
        is_public=not leaf.startswith("_"),
        language=JS_LANGUAGE,
    ))
    return 1


class _JSVisitor:
    """tree-sitter walker for JavaScript. Same shape as the TS
    visitor — read both side-by-side if extending."""

    def __init__(self, rel_path: str, src: bytes, storage: Storage) -> None:
        self._rel_path = rel_path
        self._src = src
        self._storage = storage
        self._scope_stack: list[tuple[str, int | None]] = []
        self._class_depth = 0
        self.symbol_count = 0

    def visit_program(self, node: Any) -> None:
        module_qname = js_module_qualified_name_for(self._rel_path)
        leaf = module_qname.rsplit(".", 1)[-1] if module_qname else ""
        module_id = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=1, col=0,
            kind=SymbolKind.MODULE, name=leaf,
            qualified_name=module_qname, parent_id=None,
            is_public=not leaf.startswith("_"),
            language=JS_LANGUAGE,
        ))
        self.symbol_count += 1
        self._scope_stack.append((module_qname, module_id))
        for child in node.named_children:
            self._dispatch(child)
        self._scope_stack.pop()

    def _dispatch(self, node: Any) -> None:
        method = getattr(self, f"_visit_{node.type}", None)
        if method is not None:
            method(node)
            return
        for child in node.named_children:
            self._dispatch(child)

    def _text(self, node: Any) -> str:
        return self._src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace",
        )

    def _line_col(self, node: Any) -> tuple[int, int]:
        return node.start_point[0] + 1, node.start_point[1]

    def _qualified(self, leaf: str) -> str:
        return compose_qualified_name(
            tuple(part for part, _ in self._scope_stack) + (leaf,),
        )

    def _parent_id(self) -> int | None:
        return self._scope_stack[-1][1] if self._scope_stack else None

    def _signature_for(self, node: Any) -> str:
        """JS signatures lack return-type annotations; capture the
        parameter list verbatim. ``return_type`` field doesn't exist
        on JS function nodes (it does on TS) so we only extract
        ``parameters``."""
        params = node.child_by_field_name("parameters")
        params_text = self._text(params) if params is not None else "()"
        sig = " ".join(params_text.split())
        if len(sig) > _MAX_SIGNATURE_CHARS:
            sig = sig[: _MAX_SIGNATURE_CHARS - 3] + "..."
        return sig

    # ── Export wrapper: peel + recurse ─────────────────────────────

    def _visit_export_statement(self, node: Any) -> None:
        for child in node.named_children:
            self._dispatch(child)

    # ── Imports ────────────────────────────────────────────────────

    def _visit_import_statement(self, node: Any) -> None:
        source_node = node.child_by_field_name("source")
        module_path = ""
        if source_node is not None:
            frag = source_node.named_child(0) if source_node.named_child_count else None
            if frag is not None and frag.type == "string_fragment":
                module_path = self._text(frag)
            else:
                module_path = self._text(source_node).strip("'\"")
        clause = next(
            (c for c in node.named_children if c.type == "import_clause"),
            None,
        )
        if clause is None:
            return
        line, col = self._line_col(node)
        for imported_name, bound_name, kind in self._parse_import_clause(clause):
            self._storage.insert_import(
                self._rel_path, module_path, bound_name, line,
            )
            qname = self._qualified(bound_name)
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=line, col=col,
                kind=SymbolKind.IMPORT, name=bound_name,
                qualified_name=qname, parent_id=self._parent_id(),
                is_public=not bound_name.startswith("_"),
                language=JS_LANGUAGE,
            ))
            self.symbol_count += 1
            if kind == "named":
                self._storage.insert_reference(Reference(
                    symbol_id=None, raw_name=imported_name,
                    file=self._rel_path, line=line, col=col,
                    kind=RefKind.IMPORT_FROM,
                ))

    def _parse_import_clause(self, clause: Any) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for child in clause.named_children:
            if child.type == "identifier":
                out.append(("default", self._text(child), "default"))
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
                ident = next(
                    (c for c in child.named_children if c.type == "identifier"),
                    None,
                )
                if ident is not None:
                    out.append(("*", self._text(ident), "namespace"))
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
            language=JS_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1
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
            language=JS_LANGUAGE,
        ))
        self.symbol_count += 1
        # `extends Base` — JS uses class_heritage with one expression
        heritage = next(
            (c for c in node.named_children if c.type == "class_heritage"),
            None,
        )
        if heritage is not None:
            for ch in heritage.named_children:
                self._record_heritage_target(ch)
        body = node.child_by_field_name("body")
        self._scope_stack.append((name, sid))
        self._class_depth += 1
        if body is not None:
            for child in body.named_children:
                self._dispatch(child)
        self._class_depth -= 1
        self._scope_stack.pop()

    def _record_heritage_target(self, node: Any) -> None:
        if node.type == "identifier":
            line, col = self._line_col(node)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=self._text(node),
                file=self._rel_path, line=line, col=col,
                kind=RefKind.INHERITANCE,
            ))
        else:
            for ch in node.named_children:
                self._record_heritage_target(ch)

    # ── Method definitions (within class body) ────────────────────

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
            language=JS_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1
        body = node.child_by_field_name("body")
        self._scope_stack.append((name, sid))
        if body is not None:
            for child in body.named_children:
                self._dispatch(child)
        self._scope_stack.pop()

    # ── Module-level lexical (const / let) ────────────────────────

    def _visit_lexical_declaration(self, node: Any) -> None:
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
                    language=JS_LANGUAGE,
                ))
                self.symbol_count += 1
                value = declarator.child_by_field_name("value")
                if value is not None:
                    self._dispatch(value)
        else:
            for child in node.named_children:
                self._dispatch(child)

    # ── References ────────────────────────────────────────────────

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
        line, col = self._line_col(node)
        self._storage.insert_reference(Reference(
            symbol_id=None, raw_name=self._text(node),
            file=self._rel_path, line=line, col=col,
            kind=RefKind.NAME_LOAD,
        ))

    def _visit_new_expression(self, node: Any) -> None:
        ctor = node.child_by_field_name("constructor")
        if ctor is not None:
            if ctor.type == "identifier":
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
