"""CPG-lite diff helpers — index file content from a string and
compute per-file symbol diffs (new / signature-changed / removed).

Two pure functions:

* :func:`index_text_to_storage` — write content to a tempfile and
  run the per-language indexer. Returns a populated :class:`Storage`.
  Used by Ψ-full ΔI (was a private helper in psi_delta_i; promoted
  here so Test Gen can share it without crossing layer boundaries).

* :func:`diff_symbols` — given (before content, after content,
  rel_path), return ``(new_symbols, changed_symbols)`` lists where:
    - ``new_symbols``     = public defining symbols present in
      AFTER but not in BEFORE (matched by name+kind).
    - ``changed_symbols`` = public defining symbols present in
      both, but with different ``signature``.
  Removed symbols are NOT returned (no test to generate for code
  that no longer exists).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from gitoma.cpg._base import Symbol, SymbolKind
from gitoma.cpg.go_indexer import index_go_file
from gitoma.cpg.javascript_indexer import index_javascript_file
from gitoma.cpg.python_indexer import index_python_file
from gitoma.cpg.rust_indexer import index_rust_file
from gitoma.cpg.storage import Storage
from gitoma.cpg.typescript_indexer import index_typescript_file

__all__ = [
    "index_text_to_storage",
    "diff_symbols",
    "DEFINING_KINDS",
    "INDEXABLE_EXTS",
]


# Mirrors blast_radius / psi_phi / psi_delta_i conventions.
DEFINING_KINDS: frozenset[SymbolKind] = frozenset({
    SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS,
    SymbolKind.INTERFACE, SymbolKind.TYPE_ALIAS, SymbolKind.ASSIGNMENT,
})

INDEXABLE_EXTS: tuple[str, ...] = (
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".rs", ".go",
)


def index_text_to_storage(rel_path: str, content: str) -> Storage:
    """Build a throwaway in-memory ``Storage`` from a single file's
    content. Writes to a tempfile because the indexers all take a
    ``Path`` (they read+parse it themselves). Suffix preserved so
    the right indexer is invoked. Returns the populated Storage —
    the caller must ``close()`` it when done."""
    storage = Storage()
    suffix = "".join(Path(rel_path).suffixes) or ".py"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        if rel_path.endswith(".py"):
            index_python_file(tmp_path, rel_path, storage)
        elif rel_path.endswith((".ts", ".tsx")):
            index_typescript_file(tmp_path, rel_path, storage)
        elif rel_path.endswith((".js", ".mjs", ".cjs")):
            index_javascript_file(tmp_path, rel_path, storage)
        elif rel_path.endswith(".rs"):
            index_rust_file(tmp_path, rel_path, storage)
        elif rel_path.endswith(".go"):
            index_go_file(tmp_path, rel_path, storage)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return storage


def diff_symbols(
    rel_path: str,
    before_content: str,
    after_content: str,
    *,
    public_only: bool = True,
) -> tuple[list[Symbol], list[Symbol]]:
    """Return ``(new, changed)`` symbol lists for ``rel_path``.

    Compares public defining symbols in BEFORE vs AFTER content.
    A symbol is considered:
      * **new** — present in AFTER but no symbol with same
        ``(name, kind)`` exists in BEFORE.
      * **changed** — same ``(name, kind)`` in both, but
        ``signature`` differs.
    Removed symbols are NOT returned (no test to generate).

    When the file extension isn't indexable, returns ``([], [])``.
    """
    if not any(rel_path.endswith(ext) for ext in INDEXABLE_EXTS):
        return [], []

    before_store = index_text_to_storage(rel_path, before_content)
    after_store = index_text_to_storage(rel_path, after_content)
    try:
        before_syms = before_store.get_symbols_in_file(rel_path)
        after_syms = after_store.get_symbols_in_file(rel_path)
    finally:
        before_store.close()
        # Keep after_store open until we return — caller may want
        # to introspect further; but actually we return Symbol
        # objects which are frozen dataclasses, so closing is fine.
        after_store.close()

    if public_only:
        before_syms = [s for s in before_syms if s.is_public]
        after_syms = [s for s in after_syms if s.is_public]
    before_syms = [s for s in before_syms if s.kind in DEFINING_KINDS]
    after_syms = [s for s in after_syms if s.kind in DEFINING_KINDS]

    before_index: dict[tuple[str, SymbolKind], Symbol] = {
        (s.name, s.kind): s for s in before_syms
    }
    new: list[Symbol] = []
    changed: list[Symbol] = []
    for sym in after_syms:
        key = (sym.name, sym.kind)
        prev = before_index.get(key)
        if prev is None:
            new.append(sym)
        elif prev.signature != sym.signature:
            changed.append(sym)
    return new, changed
