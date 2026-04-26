"""CPG-lite v0 — base data model.

A *Code Property Graph (CPG)* maps source code into a queryable graph
of symbols (definitions) and references (usages). v0 is the smallest
useful cut: Python only, AST-only (no control flow / data flow yet),
in-memory SQLite as the back end, no caching across runs. Future
versions add tree-sitter (v0.5 multi-language), CFG (v1), PDG (v1),
and cross-language lineage (v2).

This module is pure data + light helpers. No I/O, no parsing, no
SQLite. The walker (:mod:`gitoma.cpg.python_indexer`) emits these
records into a storage layer (:mod:`gitoma.cpg.storage`); the public
query API (:mod:`gitoma.cpg.queries`) returns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["SymbolKind", "RefKind", "Symbol", "Reference", "compose_qualified_name"]


class SymbolKind(str, Enum):
    """The kind of a Symbol record. String-valued so SQLite stores
    the human-readable form (better debuggability than enum integers).

    v0.5-slim added INTERFACE + TYPE_ALIAS for TypeScript declarations
    that have no Python equivalent. Both are additive — Python code
    paths are unaffected."""

    FUNCTION = "function"      # top-level def / function_declaration
    METHOD = "method"          # def inside a class / method_definition
    CLASS = "class"
    MODULE = "module"          # the file itself, root of the qualified name
    ASSIGNMENT = "assignment"  # module-level Name = ... / lexical const|let
    IMPORT = "import"          # bound name from `import` / `from ... import`
    INTERFACE = "interface"    # TS `interface X { ... }` (no Python analogue)
    TYPE_ALIAS = "type_alias"  # TS `type X = ...` (no Python analogue)


class RefKind(str, Enum):
    """The kind of a Reference record. ``raw_name`` is always the
    textual reference; ``symbol_id`` is set when reference-resolution
    finds a defining Symbol, ``None`` when unresolved (built-in,
    star-import, dynamic, etc.)."""

    CALL = "call"                          # foo()
    ATTRIBUTE_ACCESS = "attribute_access"  # foo.bar (load only — store is a different shape)
    NAME_LOAD = "name_load"                # bare Name appearing in expression context
    IMPORT_FROM = "import_from"            # `from X import Y` — Y is the reference
    INHERITANCE = "inheritance"            # class C(Base): — Base is the reference


@dataclass(frozen=True)
class Symbol:
    """A defined symbol in the indexed source.

    ``id`` is assigned by storage on insert (``0`` until persisted).
    ``parent_id`` chains methods → classes → modules. ``is_public``
    is the leading-underscore heuristic; consumers should treat it
    as advisory, not authoritative.

    ``language`` defaults to ``"python"`` for back-compat with v0
    code that didn't pass it. v0.5-slim TS records pass
    ``language="typescript"``.
    """

    id: int
    file: str             # repo-relative, forward-slash normalised
    line: int             # 1-based, matches ast.lineno
    col: int              # 0-based, matches ast.col_offset
    kind: SymbolKind
    name: str             # leaf name (no qualifiers)
    qualified_name: str   # "package.module.Class.method"
    parent_id: int | None
    is_public: bool
    language: str = "python"
    # Skeletal v1: compressed signature text used by the planner-
    # prompt skeleton renderer. Empty for kinds where the kind itself
    # IS the signature (CLASS, MODULE, ASSIGNMENT, IMPORT, INTERFACE,
    # TYPE_ALIAS) and for indexers that haven't been extended yet
    # (back-compat default). Populated as
    # ``"(req: dict) -> str"`` for Python functions / methods,
    # ``"(url: string): Promise<string>"`` for TS, etc.
    signature: str = ""


@dataclass(frozen=True)
class Reference:
    """A textual usage of a name. ``symbol_id`` is filled by the
    reference-resolution pass (:mod:`gitoma.cpg.queries`) when a
    defining Symbol matches the ``raw_name`` under the resolution
    rules; ``None`` when unresolved (built-in, dynamic, etc.).

    Resolution rules (v0, in priority order):
      1. Local symbol in the same file (innermost scope first)
      2. Imported name (``imports`` rows for that file)
      3. Otherwise: leave ``symbol_id=None``
    """

    symbol_id: int | None
    raw_name: str
    file: str
    line: int
    col: int
    kind: RefKind


def compose_qualified_name(parts: tuple[str, ...]) -> str:
    """Join non-empty name parts with ``.``. Empty inputs produce
    ``""`` (which is valid for module-root symbols whose parent is
    the synthetic module record itself)."""
    return ".".join(p for p in parts if p)
