"""CPG-lite v0 — Python AST → Symbol/Reference rows.

Walks ``ast.parse(src)`` of a single Python file and emits
:class:`Symbol` records (one per def / class / module-level name)
and :class:`Reference` records (one per call / attribute access /
name load / inheritance / import-from). Imports also produce
``imports`` table rows so the resolver can chain a ``raw_name`` in
file F to its origin module.

Scope rules for v0 (kept honest in the docstring so callers don't
get surprised):

* Module-level functions → kind=function.
* Methods (def inside a class) → kind=method, parent_id = class.
* Nested functions (def inside a function) → kept as kind=function
  with parent_id = outer function. Resolution prefers innermost.
* Module-level Name = ... assignments (one target only) →
  kind=assignment. Tuple/star/AugAssign targets are skipped.
* Decorators are recorded as references to the decorator name but
  do NOT replace the decorated symbol's identity (we lie a bit
  here — see risk register #1 in the sprint plan).
* ``from X import *`` rows are recorded with ``bound_name="*"``
  so the resolver can mark unresolvable refs as opaque.
* ``__all__`` is NOT consulted for is_public (we use the leading-
  underscore heuristic only — v0 simplification).
* Async defs are treated identically to sync defs.
"""

from __future__ import annotations

import ast
from pathlib import Path

from gitoma.cpg._base import (
    Reference,
    RefKind,
    Symbol,
    SymbolKind,
    compose_qualified_name,
)
from gitoma.cpg.storage import Storage

__all__ = ["index_python_file", "module_qualified_name_for"]


_MAX_SIGNATURE_CHARS = 200
"""Cap on captured signature text — protects the planner prompt
from blowup on functions with very long type annotations
(union types, generics nested 5 levels deep, etc.)."""


def _format_python_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Return a compact one-line signature text for a Python function.

    Uses ``ast.unparse`` on args + return annotation. Falls back to
    ``"(...)"`` on parser errors. Capped at ``_MAX_SIGNATURE_CHARS``
    with a trailing ellipsis when truncated.
    """
    try:
        args_text = ast.unparse(node.args)
    except Exception:
        return "(...)"
    parts = [f"({args_text})"]
    if node.returns is not None:
        try:
            parts.append(f" -> {ast.unparse(node.returns)}")
        except Exception:
            pass
    sig = "".join(parts)
    # Collapse internal newlines (annotations like Union[A, B] can
    # span lines after unparsing some grammar shapes).
    sig = " ".join(sig.split())
    if len(sig) > _MAX_SIGNATURE_CHARS:
        sig = sig[: _MAX_SIGNATURE_CHARS - 3] + "..."
    return sig


def module_qualified_name_for(rel_path: str) -> str:
    """Convert a repo-relative path like ``gitoma/cpg/queries.py`` to
    the qualified module name ``gitoma.cpg.queries``. Strips the
    ``.py`` suffix and drops ``__init__`` (so ``foo/__init__.py`` →
    ``foo``)."""
    p = rel_path.replace("\\", "/")
    if p.endswith(".py"):
        p = p[:-3]
    parts = [seg for seg in p.split("/") if seg]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def index_python_file(
    path: Path, rel_path: str, storage: Storage,
) -> int:
    """Parse ``path`` as Python source and emit Symbol + Reference +
    Import rows into ``storage``. Returns the number of Symbol rows
    inserted (useful for trace + tests).

    Parse failures are silent: we return 0 and emit nothing rather
    than crash the whole index build on a single corrupt file.
    """
    try:
        src = path.read_text(errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return 0

    visitor = _Visitor(rel_path=rel_path, storage=storage)
    visitor.visit_module(tree)
    return visitor.symbol_count


class _Visitor:
    """AST walker that emits Symbol + Reference + Import rows.

    Maintains two stacks during traversal:

    * ``_scope_stack`` — list of (qualified_name_part, parent_id)
      tuples representing the chain from module root to current
      enclosing definition. The root entry is the module itself.
    * ``_class_stack`` — subset of the scope stack containing only
      class definitions. Used to decide function vs method on def.
    """

    def __init__(self, rel_path: str, storage: Storage) -> None:
        self._rel_path = rel_path
        self._storage = storage
        self._scope_stack: list[tuple[str, int | None]] = []
        self._class_stack: list[int] = []
        self.symbol_count = 0

    # ── Module entry point ─────────────────────────────────────────

    def visit_module(self, node: ast.Module) -> None:
        module_qname = module_qualified_name_for(self._rel_path)
        module_name = module_qname.rsplit(".", 1)[-1] if module_qname else ""
        module_id = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=1, col=0,
            kind=SymbolKind.MODULE,
            name=module_name, qualified_name=module_qname,
            parent_id=None, is_public=not module_name.startswith("_"),
        ))
        self.symbol_count += 1
        self._scope_stack.append((module_qname, module_id))
        for child in node.body:
            self._visit(child)
        self._scope_stack.pop()

    # ── Dispatch ───────────────────────────────────────────────────

    def _visit(self, node: ast.AST) -> None:
        method = getattr(self, f"_visit_{type(node).__name__}", None)
        if method is not None:
            method(node)
        else:
            # Walk into bodies / fields / iterables of unknown nodes
            # so nested defs and refs aren't missed.
            for child in ast.iter_child_nodes(node):
                self._visit(child)

    def _visit_children(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self._visit(child)

    # ── Definitions ────────────────────────────────────────────────

    def _visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def _visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = SymbolKind.METHOD if self._class_stack else SymbolKind.FUNCTION
        parent_id = self._scope_stack[-1][1] if self._scope_stack else None
        qname = compose_qualified_name(
            tuple(part for part, _ in self._scope_stack) + (node.name,),
        )
        signature = _format_python_signature(node)
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=node.lineno, col=node.col_offset,
            kind=kind, name=node.name, qualified_name=qname,
            parent_id=parent_id, is_public=not node.name.startswith("_"),
            signature=signature,
        ))
        self.symbol_count += 1
        # Decorators are refs in the enclosing scope, not nested.
        for deco in node.decorator_list:
            self._record_expression_refs(deco)
        # Default values evaluated in enclosing scope.
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self._record_expression_refs(default)
        # Recurse into the body under this function's scope.
        self._scope_stack.append((node.name, sid))
        for child in node.body:
            self._visit(child)
        self._scope_stack.pop()

    def _visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent_id = self._scope_stack[-1][1] if self._scope_stack else None
        qname = compose_qualified_name(
            tuple(part for part, _ in self._scope_stack) + (node.name,),
        )
        sid = self._storage.insert_symbol(Symbol(
            id=0, file=self._rel_path, line=node.lineno, col=node.col_offset,
            kind=SymbolKind.CLASS, name=node.name, qualified_name=qname,
            parent_id=parent_id, is_public=not node.name.startswith("_"),
        ))
        self.symbol_count += 1
        # Bases are inheritance references in the enclosing scope.
        for base in node.bases:
            self._record_inheritance_ref(base)
        for deco in node.decorator_list:
            self._record_expression_refs(deco)
        self._scope_stack.append((node.name, sid))
        self._class_stack.append(sid)
        for child in node.body:
            self._visit(child)
        self._class_stack.pop()
        self._scope_stack.pop()

    def _visit_Assign(self, node: ast.Assign) -> None:
        # Module-level single-target Name = ... only.
        if len(self._scope_stack) == 1 and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name):
                parent_id = self._scope_stack[-1][1]
                qname = compose_qualified_name(
                    tuple(part for part, _ in self._scope_stack) + (tgt.id,),
                )
                self._storage.insert_symbol(Symbol(
                    id=0, file=self._rel_path,
                    line=tgt.lineno, col=tgt.col_offset,
                    kind=SymbolKind.ASSIGNMENT,
                    name=tgt.id, qualified_name=qname,
                    parent_id=parent_id,
                    is_public=not tgt.id.startswith("_"),
                ))
                self.symbol_count += 1
        # Always record refs in the value (even when target is skipped).
        self._record_expression_refs(node.value)

    # ── Imports ────────────────────────────────────────────────────

    def _visit_Import(self, node: ast.Import) -> None:
        parent_id = self._scope_stack[-1][1] if self._scope_stack else None
        for alias in node.names:
            module = alias.name
            bound = alias.asname or alias.name.split(".", 1)[0]
            self._storage.insert_import(
                self._rel_path, module, bound, node.lineno,
            )
            qname = compose_qualified_name(
                tuple(part for part, _ in self._scope_stack) + (bound,),
            )
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=node.lineno, col=node.col_offset,
                kind=SymbolKind.IMPORT, name=bound,
                qualified_name=qname, parent_id=parent_id,
                is_public=not bound.startswith("_"),
            ))
            self.symbol_count += 1

    def _visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        parent_id = self._scope_stack[-1][1] if self._scope_stack else None
        # ``from X import Y`` — module is X (with relative dots
        # collapsed into the dotted name as raw text). v0 doesn't
        # resolve relative imports; that's a v0.5 nicety.
        module = ("." * (node.level or 0)) + (node.module or "")
        for alias in node.names:
            imported = alias.name
            bound = alias.asname or alias.name
            self._storage.insert_import(
                self._rel_path, module, bound, node.lineno,
            )
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=imported,
                file=self._rel_path, line=node.lineno,
                col=node.col_offset, kind=RefKind.IMPORT_FROM,
            ))
            qname = compose_qualified_name(
                tuple(part for part, _ in self._scope_stack) + (bound,),
            )
            self._storage.insert_symbol(Symbol(
                id=0, file=self._rel_path, line=node.lineno, col=node.col_offset,
                kind=SymbolKind.IMPORT, name=bound,
                qualified_name=qname, parent_id=parent_id,
                is_public=not bound.startswith("_"),
            ))
            self.symbol_count += 1

    # ── References ────────────────────────────────────────────────

    def _visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        if isinstance(callee, ast.Name):
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=callee.id,
                file=self._rel_path, line=callee.lineno,
                col=callee.col_offset, kind=RefKind.CALL,
            ))
        elif isinstance(callee, ast.Attribute):
            # foo.bar() — emit a CALL ref on bar AND the attribute
            # chain as ATTRIBUTE_ACCESS.
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=callee.attr,
                file=self._rel_path, line=callee.lineno,
                col=callee.col_offset, kind=RefKind.CALL,
            ))
            self._record_expression_refs(callee.value)
        else:
            self._record_expression_refs(callee)
        for arg in node.args:
            self._record_expression_refs(arg)
        for kw in node.keywords:
            if kw.value is not None:
                self._record_expression_refs(kw.value)

    def _visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=node.id,
                file=self._rel_path, line=node.lineno,
                col=node.col_offset, kind=RefKind.NAME_LOAD,
            ))

    def _visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=node.attr,
                file=self._rel_path, line=node.lineno,
                col=node.col_offset, kind=RefKind.ATTRIBUTE_ACCESS,
            ))
        self._record_expression_refs(node.value)

    # ── Helpers ────────────────────────────────────────────────────

    def _record_expression_refs(self, node: ast.AST) -> None:
        """Walk an expression sub-tree recording refs without
        re-entering definition handlers (which would double-count).
        We dispatch through ``_visit`` so nested Calls / Names /
        Attributes still emit refs."""
        self._visit(node)

    def _record_inheritance_ref(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=node.id,
                file=self._rel_path, line=node.lineno,
                col=node.col_offset, kind=RefKind.INHERITANCE,
            ))
        elif isinstance(node, ast.Attribute):
            # base like `module.Base` — record the leaf as inheritance
            self._storage.insert_reference(Reference(
                symbol_id=None, raw_name=node.attr,
                file=self._rel_path, line=node.lineno,
                col=node.col_offset, kind=RefKind.INHERITANCE,
            ))
            self._record_expression_refs(node.value)
        else:
            self._record_expression_refs(node)
