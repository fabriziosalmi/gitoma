"""Tests for CPG-lite v0 public query API + reference resolver.

Builds tiny synthetic Python repos in tmp_path, indexes them, then
exercises the resolver heuristic + the public surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.python_indexer import index_python_file
from gitoma.cpg.queries import CPGIndex
from gitoma.cpg.storage import Storage


def _build_index(tmp_path: Path, files: dict[str, str]) -> CPGIndex:
    """Write each ``(rel_path, src)`` pair to ``tmp_path``, index them
    all, and return a populated CPGIndex."""
    storage = Storage()
    for rel, src in files.items():
        abs_path = tmp_path / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(src)
        index_python_file(abs_path, rel, storage)
    return CPGIndex(storage)


# ── get_symbol ─────────────────────────────────────────────────────


def test_get_symbol_returns_all_definitions(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, {
        "a.py": "def foo(): pass\n",
        "b.py": "def foo(): pass\n",
    })
    foos = idx.get_symbol("foo")
    funcs = [s for s in foos if s.kind is SymbolKind.FUNCTION]
    assert len(funcs) == 2
    assert {s.file for s in funcs} == {"a.py", "b.py"}


def test_get_symbol_empty_for_missing(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, {"a.py": "x = 1\n"})
    assert idx.get_symbol("nonexistent") == []


# ── Same-file resolution ──────────────────────────────────────────


def test_resolver_same_file_call(tmp_path: Path) -> None:
    """``foo`` defined in same file as the call → call resolves to it."""
    idx = _build_index(tmp_path, {
        "a.py": (
            "def foo(): pass\n"
            "def caller():\n"
            "    foo()\n"
        ),
    })
    foos = [s for s in idx.get_symbol("foo") if s.kind is SymbolKind.FUNCTION]
    assert len(foos) == 1
    callers = idx.callers_of(foos[0].id)
    # Should have at least one CALL ref from the same file
    assert any(r.kind is RefKind.CALL and r.file == "a.py" for r in callers)


# ── Cross-file via from-import ─────────────────────────────────────


def test_resolver_from_import_chain(tmp_path: Path) -> None:
    """b.py: from a import helper; helper() → call resolves to a.helper."""
    idx = _build_index(tmp_path, {
        "a.py": "def helper():\n    return 1\n",
        "b.py": (
            "from a import helper\n"
            "def caller():\n"
            "    helper()\n"
        ),
    })
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    callers = idx.callers_of(helpers[0].id)
    # Should include both: the IMPORT_FROM in b.py AND the CALL in b.py
    kinds = {(r.kind, r.file) for r in callers}
    assert (RefKind.IMPORT_FROM, "b.py") in kinds
    assert (RefKind.CALL, "b.py") in kinds


# ── Global fallback ───────────────────────────────────────────────


def test_resolver_global_fallback_unique_match(tmp_path: Path) -> None:
    """When neither same-file nor explicit import covers a name BUT
    there's exactly one definition globally, fallback picks it."""
    idx = _build_index(tmp_path, {
        "a.py": "def unique_symbol(): pass\n",
        "b.py": (
            "def caller():\n"
            "    unique_symbol()\n"  # no import; used to exercise fallback
        ),
    })
    syms = [s for s in idx.get_symbol("unique_symbol")
            if s.kind is SymbolKind.FUNCTION]
    callers = idx.callers_of(syms[0].id)
    assert any(r.file == "b.py" and r.kind is RefKind.CALL for r in callers)


def test_resolver_does_not_pick_when_ambiguous(tmp_path: Path) -> None:
    """When multiple definitions exist and no import disambiguates,
    the resolver must leave the ref unresolved (better unknown than
    wrong)."""
    idx = _build_index(tmp_path, {
        "a.py": "def process(): pass\n",
        "b.py": "def process(): pass\n",
        "c.py": (
            "def caller():\n"
            "    process()\n"  # ambiguous — no import in c.py
        ),
    })
    procs = [s for s in idx.get_symbol("process")
             if s.kind is SymbolKind.FUNCTION]
    assert len(procs) == 2
    # Neither process should have been picked by the c.py call
    for p in procs:
        callers_in_c = [r for r in idx.callers_of(p.id) if r.file == "c.py"]
        assert callers_in_c == []


def test_resolver_picks_correctly_with_import_disambiguation(tmp_path: Path) -> None:
    """Two functions named ``process`` in different files; c.py
    explicitly imports from b → resolver must pick b's process."""
    idx = _build_index(tmp_path, {
        "a.py": "def process(): pass\n",
        "b.py": "def process(): pass\n",
        "c.py": (
            "from b import process\n"
            "def caller():\n"
            "    process()\n"
        ),
    })
    a_proc = next(s for s in idx.get_symbol("process") if s.file == "a.py")
    b_proc = next(s for s in idx.get_symbol("process") if s.file == "b.py")
    a_callers = [r for r in idx.callers_of(a_proc.id) if r.file == "c.py"]
    b_callers = [r for r in idx.callers_of(b_proc.id) if r.file == "c.py"]
    assert a_callers == []  # a's process must NOT be linked from c.py
    assert any(r.kind is RefKind.CALL for r in b_callers)


# ── Star imports ──────────────────────────────────────────────────


def test_resolver_skips_star_import_opacity(tmp_path: Path) -> None:
    """``from foo import *`` is opaque; resolver must not lie by
    inventing a binding."""
    idx = _build_index(tmp_path, {
        "foo.py": "def helper(): pass\n",
        "bar.py": (
            "from foo import *\n"
            "def caller():\n"
            "    helper()\n"
        ),
    })
    # `helper` is unique globally → fallback CAN resolve it.
    # But this test focuses on the star not lying about specific
    # bindings; the fallback rule is allowed.
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    # Resolution succeeds via global fallback; that's correct.
    callers = idx.callers_of(helpers[0].id)
    assert any(r.file == "bar.py" for r in callers)


# ── who_imports ───────────────────────────────────────────────────


def test_who_imports_finds_module_consumers(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, {
        "lib/util.py": "def helper(): pass\n",
        "app.py": "from lib.util import helper\n",
        "other.py": "import lib.util\n",
        "unrelated.py": "import os\n",
    })
    importers = idx.who_imports("lib/util.py")
    paths = {f for f, _ in importers}
    assert paths == {"app.py", "other.py"}


def test_who_imports_handles_init_dropdown(tmp_path: Path) -> None:
    """A package ``foo/__init__.py`` should appear as ``foo``;
    importers of ``foo`` must show up."""
    idx = _build_index(tmp_path, {
        "pkg/__init__.py": "x = 1\n",
        "consumer.py": "import pkg\n",
    })
    importers = idx.who_imports("pkg/__init__.py")
    paths = {f for f, _ in importers}
    assert paths == {"consumer.py"}


# ── call_graph_for ────────────────────────────────────────────────


def test_call_graph_for_depth_1(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, {
        "a.py": (
            "def helper(): pass\n"
            "def b():\n"
            "    helper()\n"
            "def c():\n"
            "    helper()\n"
        ),
    })
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    graph = idx.call_graph_for(helpers[0].id, depth=1)
    assert helpers[0].id in graph
    assert len(graph[helpers[0].id]) == 2  # b and c


def test_call_graph_for_handles_cycle(tmp_path: Path) -> None:
    """Mutual recursion shouldn't loop forever."""
    idx = _build_index(tmp_path, {
        "a.py": (
            "def x():\n    y()\n"
            "def y():\n    x()\n"
        ),
    })
    x_sym = next(s for s in idx.get_symbol("x")
                 if s.kind is SymbolKind.FUNCTION)
    graph = idx.call_graph_for(x_sym.id, depth=5)
    assert x_sym.id in graph


# ── Stats passthrough ─────────────────────────────────────────────


def test_index_stats(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, {
        "a.py": "def foo(): pass\ndef bar(): pass\n",
        "b.py": "def baz(): pass\n",
    })
    assert idx.file_count() == 2
    # 2 funcs in a + 1 in b + 2 module symbols = 5 minimum
    assert idx.symbol_count() >= 5
