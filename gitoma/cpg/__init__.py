"""CPG-lite v0 — public package surface.

The single function callers need is :func:`build_index`. Walks a
repo root, indexes every Python file under it (subject to skip
rules + a file cap), runs the resolver pass, and hands back a
ready-to-query :class:`CPGIndex`.

Usage::

    from gitoma.cpg import build_index
    idx = build_index(Path("/path/to/repo"))
    for sym in idx.get_symbol("Worker"):
        callers = idx.callers_of(sym.id)
        ...

The build is deterministic, in-memory only, and fits the v0 cap of
200 files (configurable). For larger repos the file cap kicks in
and we log a warning; v0.1 will add caching across runs.
"""

from __future__ import annotations

from pathlib import Path

from gitoma.cpg._base import Reference, RefKind, Symbol, SymbolKind
from gitoma.cpg.javascript_indexer import index_javascript_file
from gitoma.cpg.python_indexer import index_python_file
from gitoma.cpg.queries import CPGIndex
from gitoma.cpg.rust_indexer import index_rust_file
from gitoma.cpg.storage import Storage
from gitoma.cpg.typescript_indexer import index_typescript_file

__all__ = [
    "Reference",
    "RefKind",
    "Symbol",
    "SymbolKind",
    "CPGIndex",
    "build_index",
    "DEFAULT_MAX_FILES",
    "DEFAULT_SKIP_DIRS",
    "INDEXED_SUFFIXES",
]


DEFAULT_MAX_FILES = 200

# File extensions ↔ indexer dispatch table. Keep in sync with
# ``gitoma.cpg.blast_radius.INDEXED_EXTENSIONS`` so a file we index
# can also produce a BLAST RADIUS section.
#   v0           .py only (stdlib `ast`)
#   v0.5-slim    .ts + .tsx via tree-sitter
#   v0.5-expansion .js + .mjs + .cjs (tree-sitter-javascript) + .rs
#                  (tree-sitter-rust)
INDEXED_SUFFIXES: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
}

DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    "target",
    "vendor",
})


def build_index(
    root: Path,
    max_files: int = DEFAULT_MAX_FILES,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
) -> CPGIndex:
    """Index every ``*.py`` under ``root`` and return a populated
    :class:`CPGIndex`.

    Args:
        root: repo root (absolute path). All emitted Symbol / Reference
            ``file`` fields are POSIX-normalised relative paths.
        max_files: hard cap on Python files indexed. v0 default = 200,
            chosen so a typical mid-size repo (gitoma itself ~50,
            FastAPI ~300) fits. When the cap is hit the function
            returns the partial index without raising.
        skip_dirs: directory basenames to prune. Default covers the
            common virtualenv / build / cache layouts.

    Returns:
        A :class:`CPGIndex` whose internal :class:`Storage` is owned
        by the caller — call ``.close()`` when done if memory matters.
    """
    storage = Storage()
    indexed = 0
    for abs_path, rel, suffix in _iter_indexable_files(root, skip_dirs):
        if indexed >= max_files:
            break
        if suffix == ".py":
            index_python_file(abs_path, rel, storage)
        elif suffix in (".ts", ".tsx"):
            index_typescript_file(abs_path, rel, storage)
        elif suffix in (".js", ".mjs", ".cjs"):
            index_javascript_file(abs_path, rel, storage)
        elif suffix == ".rs":
            index_rust_file(abs_path, rel, storage)
        indexed += 1
    storage.commit()
    return CPGIndex(storage)


def _iter_indexable_files(
    root: Path, skip_dirs: frozenset[str],
):
    """Yield ``(abs_path, rel_path, suffix)`` for every file under
    ``root`` whose suffix is in :data:`INDEXED_SUFFIXES`, pruning
    any directory whose basename is in ``skip_dirs``.

    Manual recursion (rather than ``rglob``) so we can prune entire
    subtrees without descending — important when ``.venv/`` or
    ``node_modules/`` lives under ``root``.
    """
    root = root.resolve()

    def _walk(current: Path):
        try:
            entries = sorted(current.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.is_dir():
                if entry.name in skip_dirs or entry.name.startswith("."):
                    continue
                yield from _walk(entry)
            elif entry.is_file() and entry.suffix in INDEXED_SUFFIXES:
                rel = entry.relative_to(root).as_posix()
                yield entry, rel, entry.suffix

    yield from _walk(root)
