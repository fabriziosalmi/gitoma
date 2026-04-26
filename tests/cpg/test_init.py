"""Tests for ``gitoma.cpg`` public surface — `build_index`."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import (
    CPGIndex,
    DEFAULT_MAX_FILES,
    DEFAULT_SKIP_DIRS,
    Symbol,
    SymbolKind,
    build_index,
)


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


def test_build_index_returns_cpgindex(tmp_path: Path) -> None:
    _populate(tmp_path, {"a.py": "def foo(): pass\n"})
    idx = build_index(tmp_path)
    assert isinstance(idx, CPGIndex)
    assert idx.file_count() == 1


def test_build_index_skips_default_dirs(tmp_path: Path) -> None:
    """``.venv`` / ``__pycache__`` / etc. must not be indexed even
    when they contain valid Python."""
    _populate(tmp_path, {
        "src/main.py": "def real(): pass\n",
        ".venv/lib/x.py": "def fake(): pass\n",
        "__pycache__/cached.py": "def cached(): pass\n",
        "node_modules/x.py": "def nm(): pass\n",
    })
    idx = build_index(tmp_path)
    files = {s.file for s in idx.get_symbol("real")}
    assert "src/main.py" in files
    # The fake / cached / nm symbols must not be indexed
    assert idx.get_symbol("fake") == []
    assert idx.get_symbol("cached") == []
    assert idx.get_symbol("nm") == []


def test_build_index_skips_dotfiles_and_dotdirs(tmp_path: Path) -> None:
    """Hidden directories (start with ``.``) are pruned to avoid
    indexing ``.git/``, ``.idea/``, ``.cache/``, etc."""
    _populate(tmp_path, {
        "real.py": "def keep(): pass\n",
        ".github/workflows/ci.py": "def hidden(): pass\n",
    })
    idx = build_index(tmp_path)
    assert idx.get_symbol("keep") != []
    assert idx.get_symbol("hidden") == []


def test_build_index_respects_max_files(tmp_path: Path) -> None:
    """File cap honored; partial index returned without raising."""
    files = {f"m{i}.py": f"def f{i}(): pass\n" for i in range(20)}
    _populate(tmp_path, files)
    idx = build_index(tmp_path, max_files=5)
    assert idx.file_count() == 5


def test_default_max_files_constant() -> None:
    assert DEFAULT_MAX_FILES == 200


def test_default_skip_dirs_includes_common_layouts() -> None:
    for d in (".git", ".venv", "__pycache__", "node_modules", "dist"):
        assert d in DEFAULT_SKIP_DIRS


def test_build_index_resolver_runs_once_on_construction(tmp_path: Path) -> None:
    """``build_index`` must return refs already resolved — caller
    shouldn't have to invoke a resolver step manually."""
    _populate(tmp_path, {
        "a.py": "def helper(): pass\n",
        "b.py": (
            "from a import helper\n"
            "def caller():\n"
            "    helper()\n"
        ),
    })
    idx = build_index(tmp_path)
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    callers = idx.callers_of(helpers[0].id)
    assert len(callers) >= 1
