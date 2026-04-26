"""Tests for ``gitoma.cpg.diff`` — public symbol diff helpers used by
Test Gen + Ψ-full's ΔI."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import SymbolKind
from gitoma.cpg.diff import (
    DEFINING_KINDS,
    INDEXABLE_EXTS,
    diff_symbols,
    index_text_to_storage,
)


# ── Constants ──────────────────────────────────────────────────────


def test_indexable_exts_covers_all_5_languages() -> None:
    for ext in (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".rs", ".go"):
        assert ext in INDEXABLE_EXTS


def test_defining_kinds_excludes_module_and_import() -> None:
    """MODULE + IMPORT are not "defining" in the test-generation
    sense — we don't generate tests for module headers."""
    assert SymbolKind.MODULE not in DEFINING_KINDS
    assert SymbolKind.IMPORT not in DEFINING_KINDS
    assert SymbolKind.FUNCTION in DEFINING_KINDS
    assert SymbolKind.METHOD in DEFINING_KINDS
    assert SymbolKind.CLASS in DEFINING_KINDS


# ── index_text_to_storage ──────────────────────────────────────────


def test_index_text_python_round_trip() -> None:
    s = index_text_to_storage("a.py", "def helper(x: int) -> str:\n    return ''\n")
    syms = s.get_symbols_in_file("a.py")
    funcs = [sym for sym in syms if sym.kind is SymbolKind.FUNCTION]
    assert len(funcs) == 1
    assert funcs[0].name == "helper"
    assert funcs[0].signature == "(x: int) -> str"


def test_index_text_typescript_round_trip() -> None:
    s = index_text_to_storage(
        "a.ts",
        "export function helper(x: number): boolean { return true; }\n",
    )
    funcs = [sym for sym in s.get_symbols_in_file("a.ts")
             if sym.kind is SymbolKind.FUNCTION]
    assert funcs[0].signature == "(x: number): boolean"


def test_index_text_unknown_extension_returns_empty_storage() -> None:
    s = index_text_to_storage("config.toml", "[x]\nval = 1\n")
    assert s.symbol_count() == 0


# ── diff_symbols: empty / no-op ────────────────────────────────────


def test_diff_unchanged_returns_empty(tmp_path: Path) -> None:
    src = "def helper(): pass\n"
    new, changed = diff_symbols("a.py", src, src)
    assert new == []
    assert changed == []


def test_diff_non_indexable_returns_empty() -> None:
    new, changed = diff_symbols("config.toml", "[x]\n", "[y]\n")
    assert new == []
    assert changed == []


# ── diff_symbols: new symbols ──────────────────────────────────────


def test_diff_new_function_added() -> None:
    before = "def existing(): pass\n"
    after = (
        "def existing(): pass\n"
        "def added(x: int) -> str: return ''\n"
    )
    new, changed = diff_symbols("a.py", before, after)
    assert len(new) == 1
    assert new[0].name == "added"
    assert new[0].signature == "(x: int) -> str"
    assert changed == []


def test_diff_new_class_with_methods_only_class_returned() -> None:
    """A new class brings new methods too — both surface as ``new``."""
    before = ""
    after = (
        "class Worker:\n"
        "    def run(self): pass\n"
    )
    new, changed = diff_symbols("a.py", before, after)
    names = {s.name for s in new}
    assert "Worker" in names
    assert "run" in names


def test_diff_completely_new_file() -> None:
    """When BEFORE is empty (file=create), every public symbol is new."""
    after = (
        "def a(): pass\n"
        "def b(): pass\n"
        "class C: pass\n"
    )
    new, changed = diff_symbols("a.py", "", after)
    assert {s.name for s in new} == {"a", "b", "C"}
    assert changed == []


# ── diff_symbols: signature changes ────────────────────────────────


def test_diff_signature_change_returns_changed() -> None:
    before = "def helper(x): pass\n"
    after = "def helper(x: int, y: str = 'a') -> bool: pass\n"
    new, changed = diff_symbols("a.py", before, after)
    assert new == []
    assert len(changed) == 1
    assert changed[0].name == "helper"
    assert "y: str" in changed[0].signature


def test_diff_body_only_change_not_reported() -> None:
    """Same signature, different body → NOT a "changed" symbol
    for test-gen purposes (the test would still cover it)."""
    before = "def helper(x): return x + 1\n"
    after = "def helper(x): return x * 2\n"
    new, changed = diff_symbols("a.py", before, after)
    assert new == []
    assert changed == []


# ── diff_symbols: removed (not returned) ───────────────────────────


def test_diff_removed_symbols_not_returned() -> None:
    """Removed symbols don't produce test-gen targets — there's
    nothing to test."""
    before = (
        "def kept(): pass\n"
        "def removed(): pass\n"
    )
    after = "def kept(): pass\n"
    new, changed = diff_symbols("a.py", before, after)
    assert new == []
    assert changed == []


# ── diff_symbols: privacy filter ───────────────────────────────────


def test_diff_filters_private_by_default() -> None:
    before = ""
    after = (
        "def public_fn(): pass\n"
        "def _private_fn(): pass\n"
    )
    new, _ = diff_symbols("a.py", before, after)
    assert {s.name for s in new} == {"public_fn"}


def test_diff_includes_private_when_opted_in() -> None:
    before = ""
    after = (
        "def public_fn(): pass\n"
        "def _private_fn(): pass\n"
    )
    new, _ = diff_symbols("a.py", before, after, public_only=False)
    assert {s.name for s in new} == {"public_fn", "_private_fn"}


# ── Cross-language coverage ────────────────────────────────────────


@pytest.mark.parametrize("rel_path,after_src,expected_new_name", [
    ("a.py",  "def py_new(): pass\n",                                     "py_new"),
    ("a.ts",  "export function tsNew(): void {}\n",                       "tsNew"),
    ("a.js",  "export function jsNew() {}\n",                             "jsNew"),
    ("a.rs",  "pub fn rust_new() {}\n",                                   "rust_new"),
    ("a.go",  "package x\nfunc GoNew() {}\n",                             "GoNew"),
])
def test_diff_works_across_5_languages(
    rel_path: str, after_src: str, expected_new_name: str,
) -> None:
    new, _ = diff_symbols(rel_path, "", after_src)
    assert any(s.name == expected_new_name for s in new), (
        f"{expected_new_name} not in new symbols for {rel_path}"
    )