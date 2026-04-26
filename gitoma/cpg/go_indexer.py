"""CPG-lite Go indexer (v0.5-expansion-go).

Go shape diverges from the others on two important points:

* **Visibility by capital letter.** A name starting with an ASCII
  uppercase letter is *exported* (public to other packages); any
  other start (lowercase, underscore) is unexported. There is no
  ``pub`` keyword. v1 uses an ASCII-uppercase check; non-ASCII
  letter starts are treated as unexported (Go spec actually says
  any uppercase Unicode rune exports — documented limitation).

* **Methods declared with receiver functions.** Unlike Python /
  TS / JS / Rust where methods live inside a class/impl/struct
  body, Go declares them OUTSIDE the type block::

      type Repo struct { … }
      func (r *Repo) Find(id int) (int, error) { … }

  v1 walks ``method_declaration`` items separately and chains
  ``parent_id`` to the receiver type's Symbol, looked up in the
  same file (best-effort — methods on types defined in OTHER
  files won't get parent_id chained). Receiver text examples:
  ``(r *Repo)``, ``(s Server)``, ``(r *Repo[T])``. Strip ``*``
  and any generics for the lookup.

Other Go-specific decisions:

* ``type_declaration`` body contains ``type_spec``s. Each spec's
  ``type`` field maps to:
    - ``struct_type`` → SymbolKind.CLASS
    - ``interface_type`` → SymbolKind.INTERFACE
    - anything else → SymbolKind.TYPE_ALIAS
* ``const_declaration`` / ``var_declaration`` (top-level only) →
  SymbolKind.ASSIGNMENT, one per declared name. Both shapes
  flattened — `const ( A = 1; B = 2 )` produces 2 symbols.
* ``import_declaration`` → IMPORT row(s). Path strings stored
  verbatim (with quotes stripped). Aliased imports
  (``alias "path"``) bind the alias; bare imports bind the
  last segment of the path.
* Module qualified name: file path with ``.go`` stripped.
* ``package_clause`` is ignored (we use file paths instead).
* `init()` and `main()` end up `is_public=False` (lowercase
  start) — semantically entry points but the BLAST RADIUS still
  surfaces them when touched.

Out of scope (deferred):
* Embedded fields (anonymous fields in structs) as separate Symbols.
* go.mod / go.work module resolution beyond literal path strings.
* Build tags (``// +build`` / ``//go:build``).
* Generic type-parameter parsing.
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
    "index_go_file",
    "go_module_qualified_name_for",
    "GO_LANGUAGE",
]

GO_LANGUAGE = "go"

_MAX_SIGNATURE_CHARS = 200


_parser_lock = threading.Lock()
_go_parser: Any = None
_import_failed = False


def _get_parser():  # noqa: ANN201
    global _go_parser, _import_failed
    with _parser_lock:
        if _import_failed:
            raise RuntimeError("tree-sitter-go not available")
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_go
        except ImportError as exc:  # pragma: no cover
            _import_failed = True
            raise RuntimeError(f"tree-sitter-go unavailable: {exc}") from exc
        if _go_parser is None:
            _go_parser = Parser(Language(tree_sitter_go.language()))
        return _go_parser


def go_module_qualified_name_for(rel_path: str) -> str:
    """Convert ``internal/handlers/user.go`` → ``internal.handlers.user``.
    ``main.go`` keeps its name (no special collapse like Rust mod.rs)."""
    p = rel_path.replace("\\", "/")
    if p.endswith(".go"):
        p = p[:-3]
    parts = [seg for seg in p.split("/") if seg]
    return ".".join(parts)


def _is_exported(name: str) -> bool:
    """Go visibility convention: name starts with ASCII uppercase
    → exported. Empty / lowercase / underscore → unexported. v1
    uses ASCII-only; Go spec includes any uppercase Unicode rune
    but that's edge-case enough to defer."""
    return bool(name) and "A" <= name[0] <= "Z"


def _receiver_type_name(receiver_text: str) -> str | None:
    """Extract the type name from a receiver expression. Examples:
        ``(r *Repo)``        → ``"Repo"``
        ``(s Server)``       → ``"Server"``
        ``(r *Repo[T])``     → ``"Repo"``
        ``(*Repo)``          → ``"Repo"``  (anonymous receiver)
        ``(_ *Repo)``        → ``"Repo"``  (underscore receiver)
    Returns ``None`` when nothing parseable found."""
    text = receiver_text.strip()
    # Strip outer parens
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    # Drop receiver name (the bit before the type)
    parts = text.split()
    if not parts:
        return None
    type_part = parts[-1]
    # Strip pointer star
    type_part = type_part.lstrip("*").strip()
    # Strip generics: Repo[T] → Repo
    type_part = type_part.split("[", 1)[0]
    return type_part or None


def index_go_file(
    path: Path, rel_path: str, storage: Storage,
) -> int:
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
    except Exception:  # noqa: BLE001
        return 0
    visitor = _GoVisitor(rel_path=rel_path, src=src, storage=storage)
    visitor.visit_source_file(tree.root_node)
    return visitor.symbol_count


def _emit_module_only(rel_path: str, storage: Storage) -> int:
    qname = go_module_qualified_name_for(rel_path)
    leaf = qname.rsplit(".", 1)[-1] if qname else ""
    storage.insert_symbol(Symbol(
        id=0, file=rel_path, line=1, col=0,
        kind=SymbolKind.MODULE, name=leaf,
        qualified_name=qname, parent_id=None,
        is_public=True,
        language=GO_LANGUAGE,
    ))
    return 1


class _GoVisitor:
    def __init__(self, rel_path: str, src: bytes, storage: Storage) -> None:
        self._rel_path = rel_path
        self._src = src
        self._storage = storage
        self._scope_stack: list[tuple[str, int | None]] = []
        self.symbol_count = 0

    def visit_source_file(self, node: Any) -> None:
        module_qname = go_module_qualified_name_for(self._rel_path)
        leaf = module_qname.rsplit(".", 1)[-1] if module_qname else ""
        module_id = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=1, col=0,
            kind=SymbolKind.MODULE, name=leaf,
            qualified_name=module_qname, parent_id=None,
            is_public=True, language=GO_LANGUAGE,
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

    # ── Helpers ────────────────────────────────────────────────────

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
        """Go signature: ``parameters`` field text plus optional
        ``result`` field. Whitespace collapsed; capped."""
        params = node.child_by_field_name("parameters")
        result = node.child_by_field_name("result")
        params_text = self._text(params) if params is not None else "()"
        sig = params_text
        if result is not None:
            sig += f" {self._text(result)}"
        sig = " ".join(sig.split())
        if len(sig) > _MAX_SIGNATURE_CHARS:
            sig = sig[: _MAX_SIGNATURE_CHARS - 3] + "..."
        return sig

    # ── Skip the package_clause — we don't read package names ─────

    def _visit_package_clause(self, node: Any) -> None:
        return  # noqa: RET501 — explicit no-op

    # ── Imports ────────────────────────────────────────────────────

    def _visit_import_declaration(self, node: Any) -> None:
        # Children may be: a list of `import_spec`s directly OR a
        # single `import_spec_list` containing multiple specs.
        for ch in node.named_children:
            if ch.type == "import_spec":
                self._handle_import_spec(ch)
            elif ch.type == "import_spec_list":
                for spec in ch.named_children:
                    if spec.type == "import_spec":
                        self._handle_import_spec(spec)

    def _handle_import_spec(self, spec: Any) -> None:
        path_node = spec.child_by_field_name("path")
        name_node = spec.child_by_field_name("name")
        if path_node is None:
            return
        # path is `"github.com/foo/bar"` — strip quotes
        path_text = self._text(path_node).strip().strip('"').strip("'")
        line, col = self._line_col(spec)
        if name_node is not None:
            bound = self._text(name_node)
        else:
            # Bare import — bound name is the LAST path segment.
            bound = path_text.rstrip("/").rsplit("/", 1)[-1]
        if not bound:
            return
        self._storage.insert_import(
            self._rel_path, path_text, bound, line,
        )
        self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.IMPORT, name=bound,
            qualified_name=self._qualified(bound),
            parent_id=self._parent_id(),
            is_public=False,  # import bindings are package-local
            language=GO_LANGUAGE,
        ))
        self.symbol_count += 1

    # ── Type declarations (struct / interface / alias) ────────────

    def _visit_type_declaration(self, node: Any) -> None:
        # Body may be: list of type_spec children directly OR (older
        # grammar versions) a type_spec_list. Iterate flat.
        for ch in node.named_children:
            if ch.type == "type_spec":
                self._handle_type_spec(ch)
            elif ch.type == "type_spec_list":
                for spec in ch.named_children:
                    if spec.type == "type_spec":
                        self._handle_type_spec(spec)

    def _handle_type_spec(self, spec: Any) -> None:
        name_node = spec.child_by_field_name("name")
        type_node = spec.child_by_field_name("type")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(spec)
        if type_node is not None and type_node.type == "struct_type":
            kind = SymbolKind.CLASS
        elif type_node is not None and type_node.type == "interface_type":
            kind = SymbolKind.INTERFACE
        else:
            kind = SymbolKind.TYPE_ALIAS
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=kind, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=_is_exported(name),
            language=GO_LANGUAGE,
        ))
        self.symbol_count += 1
        # Walk into interface_type body so method-signature children
        # produce METHOD records under the interface's actual id.
        if (type_node is not None
                and type_node.type == "interface_type"):
            for ch in type_node.named_children:
                if ch.type == "method_elem":
                    self._handle_interface_method(
                        ch, parent_id=sid, interface_name=name,
                    )

    def _handle_interface_method(
        self, node: Any, parent_id: int, interface_name: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        # Compose qname with interface name in the chain so
        # `Greeter.Greet` is distinct from a top-level `Greet`.
        qparts = tuple(part for part, _ in self._scope_stack) + (
            interface_name, name,
        )
        self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.METHOD, name=name,
            qualified_name=compose_qualified_name(qparts),
            parent_id=parent_id,
            is_public=_is_exported(name),
            language=GO_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1

    # ── Functions + methods ────────────────────────────────────────

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
            is_public=_is_exported(name),
            language=GO_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1
        # Walk body for refs.
        body = node.child_by_field_name("body")
        if body is not None:
            self._scope_stack.append((name, sid))
            for ch in body.named_children:
                self._dispatch(ch)
            self._scope_stack.pop()

    def _visit_method_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        receiver_node = node.child_by_field_name("receiver")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        # Identify receiver type for parent_id lookup
        parent_id: int | None = None
        receiver_qualifier = ""
        if receiver_node is not None:
            type_name = _receiver_type_name(self._text(receiver_node))
            if type_name:
                receiver_qualifier = type_name
                for sym in self._storage.get_symbols_by_name(type_name):
                    if (sym.file == self._rel_path
                            and sym.kind in (
                                SymbolKind.CLASS,
                                SymbolKind.INTERFACE,
                                SymbolKind.TYPE_ALIAS,
                            )):
                        parent_id = sym.id
                        break
        # Qualified name composes receiver type into the chain so
        # collisions across types stay distinct.
        qparts = tuple(part for part, _ in self._scope_stack)
        if receiver_qualifier:
            qparts = qparts + (receiver_qualifier,)
        qname = compose_qualified_name(qparts + (name,))
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.METHOD, name=name,
            qualified_name=qname,
            parent_id=parent_id if parent_id is not None else self._parent_id(),
            is_public=_is_exported(name),
            language=GO_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1
        # Walk body for refs (use receiver_qualifier as scope-name
        # so qualified refs stay distinct).
        body = node.child_by_field_name("body")
        if body is not None:
            self._scope_stack.append((name, sid))
            for ch in body.named_children:
                self._dispatch(ch)
            self._scope_stack.pop()

    # ── const / var → ASSIGNMENT ──────────────────────────────────

    def _visit_const_declaration(self, node: Any) -> None:
        self._handle_decl_block(node, "const_spec")

    def _visit_var_declaration(self, node: Any) -> None:
        # var declarations may use an inner var_spec_list shape.
        for ch in node.named_children:
            if ch.type == "var_spec":
                self._handle_value_spec(ch)
            elif ch.type == "var_spec_list":
                for spec in ch.named_children:
                    if spec.type == "var_spec":
                        self._handle_value_spec(spec)

    def _handle_decl_block(self, node: Any, spec_type: str) -> None:
        for ch in node.named_children:
            if ch.type == spec_type:
                self._handle_value_spec(ch)
            elif ch.type == f"{spec_type}_list":
                for spec in ch.named_children:
                    if spec.type == spec_type:
                        self._handle_value_spec(spec)

    def _handle_value_spec(self, spec: Any) -> None:
        # const_spec / var_spec can declare multiple names: `const A, B = 1, 2`
        # tree-sitter exposes them via `name` field which, when multiple,
        # appears as repeated named children of type `identifier`.
        # Extract name(s) via field-by-name then fall back to walking
        # identifiers among the spec's children.
        names: list[tuple[str, int, int]] = []
        # The most common case: a single `name` field
        name_node = spec.child_by_field_name("name")
        if name_node is not None and name_node.type == "identifier":
            line, col = self._line_col(name_node)
            names.append((self._text(name_node), line, col))
        else:
            # Multi-name: walk children looking for identifiers in the
            # name position (before `type` and `value` fields).
            for ch in spec.named_children:
                if ch.type == "identifier":
                    line, col = self._line_col(ch)
                    names.append((self._text(ch), line, col))
                else:
                    # Stop at the type/value boundary.
                    break
        for name, line, col in names:
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=line, col=col,
                kind=SymbolKind.ASSIGNMENT, name=name,
                qualified_name=self._qualified(name),
                parent_id=self._parent_id(),
                is_public=_is_exported(name),
                language=GO_LANGUAGE,
            ))
            self.symbol_count += 1

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
            elif callee.type == "selector_expression":
                # `obj.Method(…)` — Method is the called name
                field = callee.child_by_field_name("field")
                if field is not None:
                    line, col = self._line_col(field)
                    self._storage.insert_reference(Reference(
                        symbol_id=None, raw_name=self._text(field),
                        file=self._rel_path, line=line, col=col,
                        kind=RefKind.CALL,
                    ))
                operand = callee.child_by_field_name("operand")
                if operand is not None:
                    self._dispatch(operand)
            else:
                self._dispatch(callee)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for ch in args.named_children:
                self._dispatch(ch)

    def _visit_selector_expression(self, node: Any) -> None:
        field = node.child_by_field_name("field")
        if field is not None:
            line, col = self._line_col(field)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=self._text(field),
                file=self._rel_path, line=line, col=col,
                kind=RefKind.ATTRIBUTE_ACCESS,
            ))
        operand = node.child_by_field_name("operand")
        if operand is not None:
            self._dispatch(operand)

    def _visit_identifier(self, node: Any) -> None:
        line, col = self._line_col(node)
        self._storage.insert_reference(Reference(
            symbol_id=None, raw_name=self._text(node),
            file=self._rel_path, line=line, col=col,
            kind=RefKind.NAME_LOAD,
        ))
