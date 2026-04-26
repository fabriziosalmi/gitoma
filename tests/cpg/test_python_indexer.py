"""Tests for CPG-lite v0 Python AST indexer.

Fixture strategy: synthetic Python source written into ``tmp_path``
per-test rather than file-system fixtures under tests/cpg/fixtures/.
Keeps the tests local + readable + easy to vary."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.python_indexer import (
    index_python_file,
    module_qualified_name_for,
)
from gitoma.cpg.storage import Storage


def _write(tmp_path: Path, rel: str, src: str) -> tuple[Path, str]:
    """Write ``src`` to ``tmp_path/rel`` and return (abs_path, rel)."""
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src)
    return abs_path, rel


# ── module_qualified_name_for ──────────────────────────────────────


def test_module_qname_basic() -> None:
    assert module_qualified_name_for("gitoma/cpg/queries.py") == "gitoma.cpg.queries"


def test_module_qname_init_drops_to_package() -> None:
    assert module_qualified_name_for("gitoma/cpg/__init__.py") == "gitoma.cpg"


def test_module_qname_root_module() -> None:
    assert module_qualified_name_for("setup.py") == "setup"


def test_module_qname_windows_path_normalised() -> None:
    assert module_qualified_name_for("a\\b\\c.py") == "a.b.c"


# ── Module symbol ──────────────────────────────────────────────────


def test_indexer_emits_module_symbol(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "x.py", "x = 1\n")
    s = Storage()
    n = index_python_file(abs_path, rel, s)
    assert n >= 1
    syms = s.get_symbols_in_file(rel)
    module_syms = [sym for sym in syms if sym.kind is SymbolKind.MODULE]
    assert len(module_syms) == 1
    assert module_syms[0].qualified_name == "x"


# ── Functions ──────────────────────────────────────────────────────


def test_indexer_emits_function(tmp_path: Path) -> None:
    src = "def foo():\n    return 1\n"
    abs_path, rel = _write(tmp_path, "m.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert len(funcs) == 1
    assert funcs[0].name == "foo"
    assert funcs[0].qualified_name == "m.foo"
    assert funcs[0].is_public is True


def test_indexer_marks_underscore_function_private(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "m.py", "def _foo(): pass\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert funcs[0].is_public is False


def test_indexer_handles_async_function(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "m.py", "async def fetch(): pass\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert funcs[0].name == "fetch"


# ── Classes + methods ──────────────────────────────────────────────


def test_indexer_emits_class_with_method_having_class_parent(tmp_path: Path) -> None:
    src = (
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
    )
    abs_path, rel = _write(tmp_path, "w.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    syms = s.get_symbols_in_file(rel)
    classes = [sym for sym in syms if sym.kind is SymbolKind.CLASS]
    methods = [sym for sym in syms if sym.kind is SymbolKind.METHOD]
    assert len(classes) == 1
    assert classes[0].name == "Worker"
    assert len(methods) == 1
    assert methods[0].name == "run"
    assert methods[0].parent_id == classes[0].id
    assert methods[0].qualified_name == "w.Worker.run"


def test_indexer_records_inheritance_reference(tmp_path: Path) -> None:
    src = "class Worker(BaseAgent):\n    pass\n"
    abs_path, rel = _write(tmp_path, "w.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    inheritance = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.raw_name == "BaseAgent" for r in inheritance)


# ── Module-level assignments ──────────────────────────────────────


def test_indexer_emits_module_level_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.py", "MAX_RETRIES = 3\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert len(assigns) == 1
    assert assigns[0].name == "MAX_RETRIES"


def test_indexer_skips_assignment_inside_function(tmp_path: Path) -> None:
    src = "def f():\n    x = 1\n"
    abs_path, rel = _write(tmp_path, "f.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert assigns == []


def test_indexer_skips_tuple_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.py", "a, b = 1, 2\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert assigns == []


# ── Imports ────────────────────────────────────────────────────────


def test_indexer_records_plain_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.py", "import json\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert ("json", "json", 1) in imports


def test_indexer_records_import_with_alias(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.py", "import numpy as np\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert ("numpy", "np", 1) in imports


def test_indexer_records_from_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.py", "from os.path import join, dirname\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    bound_names = {b for _, b, _ in imports}
    assert "join" in bound_names
    assert "dirname" in bound_names
    # Each from-import also emits an IMPORT_FROM reference for the
    # original symbol name (so resolver can chain).
    refs = s.get_refs_in_file(rel)
    raw_names = {r.raw_name for r in refs if r.kind is RefKind.IMPORT_FROM}
    assert "join" in raw_names


def test_indexer_records_from_import_star_with_marker(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.py", "from foo import *\n")
    s = Storage()
    index_python_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert any(b == "*" for _, b, _ in imports)


# ── Calls + name loads ────────────────────────────────────────────


def test_indexer_records_call_reference(tmp_path: Path) -> None:
    src = "def caller():\n    foo()\n"
    abs_path, rel = _write(tmp_path, "c.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    assert any(c.raw_name == "foo" for c in calls)


def test_indexer_records_attribute_call(tmp_path: Path) -> None:
    src = "def caller():\n    obj.bar()\n"
    abs_path, rel = _write(tmp_path, "c.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    # `bar` should be CALL (the attr being called).
    calls = [r for r in refs if r.kind is RefKind.CALL]
    assert any(c.raw_name == "bar" for c in calls)
    # `obj` should be NAME_LOAD (the receiver).
    loads = [r for r in refs if r.kind is RefKind.NAME_LOAD]
    assert any(l.raw_name == "obj" for l in loads)


def test_indexer_records_attribute_access(tmp_path: Path) -> None:
    src = "def f():\n    return obj.attr\n"
    abs_path, rel = _write(tmp_path, "c.py", src)
    s = Storage()
    index_python_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    attrs = [r for r in refs if r.kind is RefKind.ATTRIBUTE_ACCESS]
    assert any(a.raw_name == "attr" for a in attrs)


# ── Failure modes ──────────────────────────────────────────────────


def test_indexer_returns_zero_on_syntax_error(tmp_path: Path) -> None:
    """Corrupt source should not crash the build — just yield 0."""
    abs_path, rel = _write(tmp_path, "bad.py", "def (((:\n")
    s = Storage()
    n = index_python_file(abs_path, rel, s)
    assert n == 0
    assert s.symbol_count() == 0


def test_indexer_handles_empty_file(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "empty.py", "")
    s = Storage()
    n = index_python_file(abs_path, rel, s)
    # Even an empty file emits the module symbol.
    assert n == 1


# ── Integration: 2-file scenario for downstream resolver tests ──


def test_indexer_two_module_setup_emits_consistent_records(tmp_path: Path) -> None:
    """Used by the resolver in queries.py — pre-flight that the
    indexer produces the rows the resolver needs to work."""
    abs_a, rel_a = _write(tmp_path, "a.py", "def helper():\n    return 1\n")
    abs_b, rel_b = _write(tmp_path, "b.py",
                           "from a import helper\n"
                           "def caller():\n    helper()\n")
    s = Storage()
    index_python_file(abs_a, rel_a, s)
    index_python_file(abs_b, rel_b, s)

    # a.py defines helper as a function
    helpers = [sym for sym in s.get_symbols_by_name("helper")
               if sym.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    assert helpers[0].file == "a.py"

    # b.py imports helper from a → import row + ref + import-symbol
    imports = s.get_imports_for_file("b.py")
    assert any(b == "helper" for _, b, _ in imports)

    # b.py calls helper → CALL ref with raw_name "helper", unresolved
    refs_b = s.get_refs_in_file("b.py")
    calls = [r for r in refs_b if r.kind is RefKind.CALL]
    assert any(c.raw_name == "helper" and c.symbol_id is None for c in calls)
