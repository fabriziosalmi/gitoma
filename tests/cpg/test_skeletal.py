"""Tests for Skeletal Representation v1 renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.cpg.skeletal import DEFAULT_MAX_CHARS, render_skeleton


def _build(tmp_path: Path, files: dict[str, str]):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return build_index(tmp_path)


# ── Default values ────────────────────────────────────────────────


def test_default_max_chars_constant() -> None:
    """20000 chars ≈ 5000 tokens. If retuning, update both this
    test and the docstring."""
    assert DEFAULT_MAX_CHARS == 20000


# ── Empty / degraded paths ────────────────────────────────────────


def test_render_empty_index_returns_empty_string() -> None:
    """None or empty index → caller knows no skeleton block to
    inject; returns empty string for back-compat with the existing
    "if skeleton: inject" pattern."""
    assert render_skeleton(None) == ""


def test_render_zero_budget_returns_empty(tmp_path: Path) -> None:
    idx = _build(tmp_path, {"a.py": "def foo(): pass\n"})
    assert render_skeleton(idx, max_chars=0) == ""


def test_render_index_without_relevant_symbols(tmp_path: Path) -> None:
    """A repo with only a file containing private (underscore)
    symbols emits empty skeleton (skipping bare headers with no
    body)."""
    idx = _build(tmp_path, {"a.py": "def _private(): pass\n"})
    assert render_skeleton(idx) == ""


# ── Single-file rendering ─────────────────────────────────────────


def test_render_single_function(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "a.py": "def helper(x: int) -> str:\n    return str(x)\n",
    })
    out = render_skeleton(idx)
    assert "## a.py" in out
    assert "def helper(x: int) -> str" in out


def test_render_class_with_method_indented(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "w.py": (
            "class Worker:\n"
            "    def run(self, x: int) -> bool:\n"
            "        return True\n"
        ),
    })
    out = render_skeleton(idx)
    assert "class Worker" in out
    # Method is indented 2 spaces under the class
    assert "  run(self, x: int) -> bool" in out


def test_render_multiple_top_level_symbols(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "lib.py": (
            "MAX = 5\n"
            "def helper(): pass\n"
            "class Foo:\n"
            "    def bar(self): pass\n"
            "def other(): pass\n"
        ),
    })
    out = render_skeleton(idx)
    assert "MAX = ..." in out
    assert "def helper()" in out
    assert "class Foo" in out
    assert "  bar(self)" in out
    assert "def other()" in out


# ── TS rendering ──────────────────────────────────────────────────


def test_render_ts_function(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "a.ts": "export function tick(x: number): void {}\n",
    })
    out = render_skeleton(idx)
    assert "## a.ts" in out
    assert "def tick(x: number): void" in out


def test_render_ts_interface_and_type_alias(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "t.ts": (
            "export interface User { id: number }\n"
            "export type Maybe<T> = T | null;\n"
        ),
    })
    out = render_skeleton(idx)
    assert "interface User" in out
    assert "type Maybe" in out


def test_render_ts_class_with_methods(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "r.ts": (
            "export class Repo {\n"
            "  find(id: number): User | null { return null; }\n"
            "  static create(): Repo { return new Repo(); }\n"
            "}\n"
        ),
    })
    out = render_skeleton(idx)
    assert "class Repo" in out
    # Both methods indented under the class
    assert "  find(id: number): User | null" in out
    assert "  create(): Repo" in out


# ── Multi-file ordering ───────────────────────────────────────────


def test_render_files_in_alphabetical_order(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "z.py": "def z_fn(): pass\n",
        "a.py": "def a_fn(): pass\n",
        "m.py": "def m_fn(): pass\n",
    })
    out = render_skeleton(idx)
    a_idx = out.index("## a.py")
    m_idx = out.index("## m.py")
    z_idx = out.index("## z.py")
    assert a_idx < m_idx < z_idx


# ── Privacy filter ────────────────────────────────────────────────


def test_render_excludes_private_by_default(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "a.py": (
            "def public_fn(): pass\n"
            "def _private_fn(): pass\n"
        ),
    })
    out = render_skeleton(idx)
    assert "public_fn" in out
    assert "_private_fn" not in out


def test_render_includes_private_when_opted_in(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "a.py": (
            "def public_fn(): pass\n"
            "def _private_fn(): pass\n"
        ),
    })
    out = render_skeleton(idx, include_private=True)
    assert "public_fn" in out
    assert "_private_fn" in out


# ── Truncation ────────────────────────────────────────────────────


def test_render_truncates_at_budget_and_emits_marker(tmp_path: Path) -> None:
    """50 files × 1 function each, with a tiny budget. The renderer
    should emit the first few files then stop with a marker showing
    the omitted count."""
    files = {
        f"file_{i:03d}.py": f"def fn_{i}(): pass\n"
        for i in range(50)
    }
    idx = _build(tmp_path, files)
    out = render_skeleton(idx, max_chars=200)
    # Some files are present
    assert "## file_000.py" in out
    # Marker present with omitted count
    assert "omitted" in out
    # The earlier files (alphabetical) make it; later ones don't
    assert "## file_049.py" not in out


def test_render_no_marker_when_budget_fits(tmp_path: Path) -> None:
    idx = _build(tmp_path, {"a.py": "def foo(): pass\n"})
    out = render_skeleton(idx, max_chars=DEFAULT_MAX_CHARS)
    assert "omitted" not in out


# ── Defensive: signature falls back to () when empty ─────────────


def test_render_function_without_signature_uses_empty_parens(tmp_path: Path) -> None:
    """If for any reason the signature is empty (legacy data, indexer
    bug), the renderer must still produce readable output — defaults
    to ``()`` parens so the line is grammatically valid."""
    from gitoma.cpg._base import Symbol, SymbolKind
    from gitoma.cpg.queries import CPGIndex
    from gitoma.cpg.storage import Storage
    storage = Storage()
    storage.insert_symbol(Symbol(
        id=0, file="x.py", line=1, col=0,
        kind=SymbolKind.MODULE, name="x",
        qualified_name="x", parent_id=None, is_public=True,
    ))
    storage.insert_symbol(Symbol(
        id=0, file="x.py", line=2, col=0,
        kind=SymbolKind.FUNCTION, name="legacy_fn",
        qualified_name="x.legacy_fn", parent_id=1,
        is_public=True, signature="",  # empty by intent
    ))
    idx = CPGIndex(storage)
    out = render_skeleton(idx)
    assert "def legacy_fn()" in out
