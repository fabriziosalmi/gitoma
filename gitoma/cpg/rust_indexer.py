"""CPG-lite v0.5-expansion — Rust AST → Symbol/Reference rows.

Rust shape diverges from Python/TS/JS:

* No "class" keyword. ``struct_item`` + ``impl_item`` together
  approximate it. v0.5 maps both ``struct_item`` and ``enum_item``
  to :data:`SymbolKind.CLASS` (additive — avoids inventing v0.5
  enum/struct kinds).
* ``trait_item`` is the closest analogue to TS interface →
  :data:`SymbolKind.INTERFACE`.
* ``impl_item`` is NOT a Symbol; its body's ``function_item``
  children are emitted as :data:`SymbolKind.METHOD` with
  ``parent_id`` pointing at the target type's Symbol (best-effort,
  same-file lookup).
* Visibility: presence of ``visibility_modifier`` named child →
  ``is_public=True``. Rust default is private; the leading-
  underscore heuristic is NOT used (Rust convention is different).
* ``use_declaration`` paths use ``::`` separator. v0.5 parses the
  ``argument`` field textually — simple paths
  (``std::collections::HashMap``) and brace-list shapes
  (``crate::types::{User, Repo as DataRepo}``) are supported.
* ``mod_item`` declarations produce no Symbol — the actual file
  gets indexed when the walker reaches it.

Out of scope (deferred):

* Macro expansions (``macro_rules!``, ``#[derive(...)]``).
* Generic bound parsing in impl_item (``impl<T: Send> Foo<T>``).
* Lifetime parameters as separate Symbols.
* Cross-crate ``extern crate`` resolution.
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
    "index_rust_file",
    "rust_module_qualified_name_for",
    "RUST_LANGUAGE",
]

RUST_LANGUAGE = "rust"

_MAX_SIGNATURE_CHARS = 200


_parser_lock = threading.Lock()
_rs_parser: Any = None
_import_failed = False


def _get_parser():  # noqa: ANN201
    global _rs_parser, _import_failed
    with _parser_lock:
        if _import_failed:
            raise RuntimeError("tree-sitter-rust not available")
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_rust
        except ImportError as exc:  # pragma: no cover
            _import_failed = True
            raise RuntimeError(f"tree-sitter-rust unavailable: {exc}") from exc
        if _rs_parser is None:
            _rs_parser = Parser(Language(tree_sitter_rust.language()))
        return _rs_parser


def rust_module_qualified_name_for(rel_path: str) -> str:
    """Convert ``src/handlers/user.rs`` → ``src.handlers.user``.
    ``mod.rs`` collapses to its parent (Rust 2015-style module
    layout)."""
    p = rel_path.replace("\\", "/")
    if p.endswith(".rs"):
        p = p[:-3]
    parts = [seg for seg in p.split("/") if seg]
    if parts and parts[-1] == "mod":
        parts.pop()
    return ".".join(parts)


def index_rust_file(
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
    visitor = _RustVisitor(rel_path=rel_path, src=src, storage=storage)
    visitor.visit_source_file(tree.root_node)
    return visitor.symbol_count


def _emit_module_only(rel_path: str, storage: Storage) -> int:
    qname = rust_module_qualified_name_for(rel_path)
    leaf = qname.rsplit(".", 1)[-1] if qname else ""
    storage.insert_symbol(Symbol(
        id=0, file=rel_path, line=1, col=0,
        kind=SymbolKind.MODULE, name=leaf,
        qualified_name=qname, parent_id=None,
        is_public=True,  # files have no `pub` marker; treat module as public
        language=RUST_LANGUAGE,
    ))
    return 1


def _parse_use_argument(arg_text: str) -> list[tuple[str, str]]:
    """Parse a `use_declaration.argument` text into ``(module, bound)``
    pairs. Examples::

        std::collections::HashMap      → [("std::collections", "HashMap")]
        crate::types::{User, Repo as DataRepo}
                                       → [("crate::types", "User"),
                                          ("crate::types", "DataRepo")]
        crate::handlers::*             → [("crate::handlers", "*")]
        std::io                        → [("std", "io")]
    """
    text = arg_text.strip().strip(";")
    # Brace-enclosed list?
    if "{" in text and text.endswith("}"):
        prefix, _, rest = text.partition("{")
        prefix = prefix.rstrip(":").rstrip()
        # Strip the trailing brace
        items = rest.rstrip("}").strip()
        out: list[tuple[str, str]] = []
        for raw in items.split(","):
            item = raw.strip()
            if not item:
                continue
            # alias: `Foo as Bar`
            if " as " in item:
                _, _, alias = item.partition(" as ")
                bound = alias.strip()
            else:
                bound = item
            out.append((prefix, bound))
        return out
    # Simple path
    if "::" in text:
        prefix, _, leaf = text.rpartition("::")
        return [(prefix, leaf)]
    # Bare identifier (e.g. `use foo;`)
    return [("", text)]


class _RustVisitor:
    def __init__(self, rel_path: str, src: bytes, storage: Storage) -> None:
        self._rel_path = rel_path
        self._src = src
        self._storage = storage
        self._scope_stack: list[tuple[str, int | None]] = []
        self._impl_depth = 0
        self.symbol_count = 0

    def visit_source_file(self, node: Any) -> None:
        module_qname = rust_module_qualified_name_for(self._rel_path)
        leaf = module_qname.rsplit(".", 1)[-1] if module_qname else ""
        module_id = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=1, col=0,
            kind=SymbolKind.MODULE, name=leaf,
            qualified_name=module_qname, parent_id=None,
            is_public=True, language=RUST_LANGUAGE,
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

    def _has_pub(self, node: Any) -> bool:
        for ch in node.named_children:
            if ch.type == "visibility_modifier":
                return True
        return False

    def _signature_for(self, node: Any) -> str:
        """Rust signature: ``parameters`` + ``-> return_type`` (when
        present). Whitespace collapsed; capped."""
        params = node.child_by_field_name("parameters")
        ret = node.child_by_field_name("return_type")
        params_text = self._text(params) if params is not None else "()"
        sig = params_text
        if ret is not None:
            sig += f" -> {self._text(ret)}"
        sig = " ".join(sig.split())
        if len(sig) > _MAX_SIGNATURE_CHARS:
            sig = sig[: _MAX_SIGNATURE_CHARS - 3] + "..."
        return sig

    # ── use declaration ───────────────────────────────────────────

    def _visit_use_declaration(self, node: Any) -> None:
        arg = node.child_by_field_name("argument")
        if arg is None:
            return
        line, col = self._line_col(node)
        for module_path, bound_name in _parse_use_argument(self._text(arg)):
            if not bound_name:
                continue
            self._storage.insert_import(
                self._rel_path, module_path, bound_name, line,
            )
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=line, col=col,
                kind=SymbolKind.IMPORT, name=bound_name,
                qualified_name=self._qualified(bound_name),
                parent_id=self._parent_id(),
                is_public=False,  # use-bindings are private to the module by default
                language=RUST_LANGUAGE,
            ))
            self.symbol_count += 1
            if bound_name != "*":
                # Best-effort: emit a ref so resolver can chain
                self._storage.insert_reference(Reference(
                    symbol_id=None, raw_name=bound_name,
                    file=self._rel_path, line=line, col=col,
                    kind=RefKind.IMPORT_FROM,
                ))

    # ── struct / enum / trait → CLASS / INTERFACE ─────────────────

    def _visit_struct_item(self, node: Any) -> None:
        self._emit_type_decl(node, SymbolKind.CLASS)

    def _visit_enum_item(self, node: Any) -> None:
        self._emit_type_decl(node, SymbolKind.CLASS)

    def _visit_trait_item(self, node: Any) -> None:
        self._emit_type_decl(node, SymbolKind.INTERFACE)

    def _emit_type_decl(self, node: Any, kind: SymbolKind) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        is_pub = self._has_pub(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=kind, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=is_pub,
            language=RUST_LANGUAGE,
        ))
        self.symbol_count += 1
        # Walk the body for any nested function_items (trait method
        # signatures count as methods of the trait).
        body = node.child_by_field_name("body")
        if body is not None:
            self._scope_stack.append((name, sid))
            self._impl_depth += 1
            for ch in body.named_children:
                self._dispatch(ch)
            self._impl_depth -= 1
            self._scope_stack.pop()

    # ── impl block — methods get parent_id = target type ───────────

    def _visit_impl_item(self, node: Any) -> None:
        type_node = node.child_by_field_name("type")
        target_name: str | None = None
        target_id: int | None = None
        if type_node is not None:
            # Strip generics (`Foo<T>` → `Foo`) for lookup.
            target_name = self._text(type_node).split("<", 1)[0].strip()
            for sym in self._storage.get_symbols_by_name(target_name):
                if (sym.file == self._rel_path
                        and sym.kind in (SymbolKind.CLASS, SymbolKind.INTERFACE)):
                    target_id = sym.id
                    break
        # Trait reference for `impl Trait for Type`
        trait_node = node.child_by_field_name("trait")
        if trait_node is not None:
            trait_text = self._text(trait_node).split("<", 1)[0].strip()
            line, col = self._line_col(trait_node)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=trait_text,
                file=self._rel_path, line=line, col=col,
                kind=RefKind.INHERITANCE,
            ))

        body = node.child_by_field_name("body")
        if body is None:
            return
        # Push the target type as scope so methods get qualified
        # correctly even when target_id is None.
        self._scope_stack.append((target_name or "?", target_id))
        self._impl_depth += 1
        for ch in body.named_children:
            self._dispatch(ch)
        self._impl_depth -= 1
        self._scope_stack.pop()

    # ── function_item — METHOD inside impl/trait, FUNCTION at top ─

    def _visit_function_item(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        kind = SymbolKind.METHOD if self._impl_depth else SymbolKind.FUNCTION
        is_pub = self._has_pub(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=kind, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=is_pub,
            language=RUST_LANGUAGE,
            signature=self._signature_for(node),
        ))
        self.symbol_count += 1
        # Walk the body for refs (calls, etc.).
        body = node.child_by_field_name("body")
        if body is not None:
            self._scope_stack.append((name, sid))
            for ch in body.named_children:
                self._dispatch(ch)
            self._scope_stack.pop()

    def _visit_function_signature_item(self, node: Any) -> None:
        """Trait methods declared without a body — same shape as
        function_item but no ``body`` field."""
        self._visit_function_item(node)

    # ── const / static → ASSIGNMENT ───────────────────────────────

    def _visit_const_item(self, node: Any) -> None:
        self._emit_const_or_static(node)

    def _visit_static_item(self, node: Any) -> None:
        self._emit_const_or_static(node)

    def _emit_const_or_static(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        line, col = self._line_col(node)
        is_pub = self._has_pub(node)
        self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=line, col=col,
            kind=SymbolKind.ASSIGNMENT, name=name,
            qualified_name=self._qualified(name),
            parent_id=self._parent_id(),
            is_public=is_pub,
            language=RUST_LANGUAGE,
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
            elif callee.type == "field_expression":
                # foo.bar() — bar is the called name
                field = callee.child_by_field_name("field")
                if field is not None:
                    line, col = self._line_col(field)
                    self._storage.insert_reference(Reference(
                        symbol_id=None, raw_name=self._text(field),
                        file=self._rel_path, line=line, col=col,
                        kind=RefKind.CALL,
                    ))
                value = callee.child_by_field_name("value")
                if value is not None:
                    self._dispatch(value)
            elif callee.type == "scoped_identifier":
                # Foo::bar() — `bar` is the called name
                # tree-sitter exposes path + name as named children
                ident = callee.child_by_field_name("name")
                if ident is not None:
                    line, col = self._line_col(ident)
                    self._storage.insert_reference(Reference(
                        symbol_id=None, raw_name=self._text(ident),
                        file=self._rel_path, line=line, col=col,
                        kind=RefKind.CALL,
                    ))
            else:
                self._dispatch(callee)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for ch in args.named_children:
                self._dispatch(ch)

    def _visit_field_expression(self, node: Any) -> None:
        field = node.child_by_field_name("field")
        if field is not None:
            line, col = self._line_col(field)
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=self._text(field),
                file=self._rel_path, line=line, col=col,
                kind=RefKind.ATTRIBUTE_ACCESS,
            ))
        value = node.child_by_field_name("value")
        if value is not None:
            self._dispatch(value)

    def _visit_identifier(self, node: Any) -> None:
        line, col = self._line_col(node)
        self._storage.insert_reference(Reference(
            symbol_id=None, raw_name=self._text(node),
            file=self._rel_path, line=line, col=col,
            kind=RefKind.NAME_LOAD,
        ))
