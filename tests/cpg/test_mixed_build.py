"""Tests for mixed Python+TypeScript indexing in one ``build_index()``
call. v0.5-slim ships dispatch by file extension; this test confirms
both languages co-exist in a single index without interference."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.cpg._base import RefKind, SymbolKind


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


def test_mixed_repo_indexes_both_languages(tmp_path: Path) -> None:
    """A repo with both .py and .ts files: build_index walks once,
    dispatches per extension, and the resulting CPGIndex contains
    Symbols from both languages."""
    _populate(tmp_path, {
        "src/handler.py": (
            "def process_request(req):\n"
            "    return req.upper()\n"
        ),
        "frontend/api.ts": (
            "export function callApi(url: string): Promise<string> {\n"
            "  return fetch(url).then(r => r.text());\n"
            "}\n"
        ),
    })
    idx = build_index(tmp_path)
    # Both languages produced symbols
    py_syms = [s for s in idx.get_symbol("process_request")
               if s.kind is SymbolKind.FUNCTION]
    ts_syms = [s for s in idx.get_symbol("callApi")
               if s.kind is SymbolKind.FUNCTION]
    assert len(py_syms) == 1
    assert len(ts_syms) == 1
    assert py_syms[0].language == "python"
    assert ts_syms[0].language == "typescript"


def test_mixed_repo_file_count_includes_both(tmp_path: Path) -> None:
    _populate(tmp_path, {
        "a.py": "def foo(): pass\n",
        "b.ts": "export const x = 1;\n",
        "c.tsx": "export function C(): void {}\n",
    })
    idx = build_index(tmp_path)
    assert idx.file_count() == 3


def test_mixed_repo_resolver_independent_per_language(tmp_path: Path) -> None:
    """Python's resolver and TS's path-based resolver should not
    interfere — same-name symbols in different languages stay
    independently resolvable."""
    _populate(tmp_path, {
        "a.py": "def helper(): pass\n",
        "b.py": (
            "from a import helper\n"
            "def caller(): helper()\n"
        ),
        "x.ts": "export function helper(): void {}\n",
        "y.ts": (
            "import { helper } from './x';\n"
            "function caller() { helper(); }\n"
        ),
    })
    idx = build_index(tmp_path)
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    # 2 helpers — one per language
    by_lang = {s.language: s for s in helpers}
    assert "python" in by_lang
    assert "typescript" in by_lang
    # Each helper has its own callers (cross-contamination would
    # surface as caller from the wrong language file).
    py_callers = idx.callers_of(by_lang["python"].id)
    ts_callers = idx.callers_of(by_lang["typescript"].id)
    py_caller_files = {r.file for r in py_callers}
    ts_caller_files = {r.file for r in ts_callers}
    assert "b.py" in py_caller_files
    assert "x.ts" not in py_caller_files
    assert "y.ts" in ts_caller_files
    assert "a.py" not in ts_caller_files


def test_mixed_repo_blast_radius_works_per_language(tmp_path: Path) -> None:
    """The renderer should produce sections for both .py and .ts
    targets when called with mixed file_hints."""
    from gitoma.cpg.blast_radius import render_blast_radius_block
    _populate(tmp_path, {
        "lib.py": "def py_helper(): pass\n",
        "main.py": (
            "from lib import py_helper\n"
            "def caller_py(): py_helper()\n"
        ),
        "lib.ts": "export function tsHelper(): void {}\n",
        "main.ts": (
            "import { tsHelper } from './lib';\n"
            "function callerTs() { tsHelper(); }\n"
        ),
    })
    idx = build_index(tmp_path)
    block = render_blast_radius_block(["lib.py", "lib.ts"], idx)
    assert "py_helper" in block
    assert "tsHelper" in block
    assert "main.py" in block
    assert "main.ts" in block


def test_mixed_repo_skipdirs_apply_to_both_languages(tmp_path: Path) -> None:
    """``node_modules/`` should be skipped even when it contains TS,
    just like ``.venv/`` is skipped for Python."""
    _populate(tmp_path, {
        "src/main.py": "def real(): pass\n",
        "src/component.ts": "export function realTs(): void {}\n",
        "node_modules/ignored.ts": "export function fake(): void {}\n",
        ".venv/lib/x.py": "def fake_py(): pass\n",
    })
    idx = build_index(tmp_path)
    assert idx.get_symbol("real") != []
    assert idx.get_symbol("realTs") != []
    assert idx.get_symbol("fake") == []
    assert idx.get_symbol("fake_py") == []


# ── v0.5-expansion: 4-language coexistence ─────────────────────────


def test_four_language_repo_indexes_all(tmp_path: Path) -> None:
    """Single build_index call walks .py + .ts + .js + .rs files
    in one pass, dispatches per extension, all show up in the
    resulting index with correct language tags."""
    _populate(tmp_path, {
        "src/handler.py": "def py_handler(): pass\n",
        "frontend/api.ts": "export function tsApi(): void {}\n",
        "frontend/util.js": "export function jsUtil() {}\n",
        "backend/main.rs": "pub fn rust_main() {}\n",
    })
    idx = build_index(tmp_path)
    assert idx.file_count() == 4
    py = next(s for s in idx.get_symbol("py_handler")
              if s.kind is SymbolKind.FUNCTION)
    ts = next(s for s in idx.get_symbol("tsApi")
              if s.kind is SymbolKind.FUNCTION)
    js = next(s for s in idx.get_symbol("jsUtil")
              if s.kind is SymbolKind.FUNCTION)
    rs = next(s for s in idx.get_symbol("rust_main")
              if s.kind is SymbolKind.FUNCTION)
    assert py.language == "python"
    assert ts.language == "typescript"
    assert js.language == "javascript"
    assert rs.language == "rust"


def test_four_language_skipdirs_cover_all(tmp_path: Path) -> None:
    """Every language's tooling-skip dir (target/ for Rust,
    node_modules/ for JS+TS, .venv/ for Python) is honored in
    one walk."""
    _populate(tmp_path, {
        "src/keep.py": "def keep_py(): pass\n",
        "src/keep.ts": "export function keepTs(): void {}\n",
        "src/keep.js": "export function keepJs() {}\n",
        "src/keep.rs": "pub fn keep_rs() {}\n",
        "node_modules/fake.js": "export function nm_fake() {}\n",
        "target/release/fake.rs": "pub fn target_fake() {}\n",
        ".venv/lib/fake.py": "def venv_fake(): pass\n",
        "build/fake.ts": "export function build_fake(): void {}\n",
    })
    idx = build_index(tmp_path)
    for kept in ("keep_py", "keepTs", "keepJs", "keep_rs"):
        assert idx.get_symbol(kept) != [], f"{kept} should be indexed"
    for skipped in ("nm_fake", "target_fake", "venv_fake", "build_fake"):
        assert idx.get_symbol(skipped) == [], (
            f"{skipped} should NOT be indexed (skip-dir)"
        )


def test_blast_radius_works_across_four_languages(tmp_path: Path) -> None:
    """A single render_blast_radius_block call across .py + .ts +
    .js + .rs files produces sections for all four."""
    from gitoma.cpg.blast_radius import render_blast_radius_block
    _populate(tmp_path, {
        "lib.py": "def py_helper(): pass\n",
        "lib.ts": "export function tsHelper(): void {}\n",
        "lib.js": "export function jsHelper() {}\n",
        "lib.rs": "pub fn rust_helper() {}\n",
    })
    idx = build_index(tmp_path)
    block = render_blast_radius_block(
        ["lib.py", "lib.ts", "lib.js", "lib.rs"], idx,
    )
    for fn in ("py_helper", "tsHelper", "jsHelper", "rust_helper"):
        assert fn in block


def test_skeleton_works_across_four_languages(tmp_path: Path) -> None:
    """The skeletal renderer is kind-agnostic; with v0.5-expansion
    in place it produces a section per file across all 4 languages."""
    from gitoma.cpg.skeletal import render_skeleton
    _populate(tmp_path, {
        "lib.py": "def py_helper(x: int) -> str: return ''\n",
        "lib.ts": "export function tsHelper(): void {}\n",
        "lib.js": "export function jsHelper() {}\n",
        "lib.rs": "pub fn rust_helper(n: u32) -> u32 { n }\n",
    })
    idx = build_index(tmp_path)
    out = render_skeleton(idx)
    # Each file appears
    for f in ("lib.py", "lib.ts", "lib.js", "lib.rs"):
        assert f"## {f}" in out, f"missing section for {f}"
    # Signature text from each language survived
    assert "py_helper(x: int) -> str" in out
    assert "tsHelper(): void" in out
    assert "jsHelper()" in out
    assert "rust_helper(n: u32) -> u32" in out


# ── v0.5-expansion-go: 5-language coexistence ─────────────────────


def test_five_language_repo_indexes_all(tmp_path: Path) -> None:
    """Single build_index walks .py + .ts + .js + .rs + .go in one
    pass, dispatches per extension, all 5 show up tagged correctly."""
    _populate(tmp_path, {
        "src/handler.py": "def py_handler(): pass\n",
        "frontend/api.ts": "export function tsApi(): void {}\n",
        "frontend/util.js": "export function jsUtil() {}\n",
        "backend/main.rs": "pub fn rust_main() {}\n",
        "service/main.go": "package main\nfunc GoMain() {}\n",
    })
    idx = build_index(tmp_path)
    assert idx.file_count() == 5
    by_lang = {}
    for name in ("py_handler", "tsApi", "jsUtil", "rust_main", "GoMain"):
        sym = next(s for s in idx.get_symbol(name)
                   if s.kind is SymbolKind.FUNCTION)
        by_lang[sym.language] = sym
    assert set(by_lang.keys()) == {
        "python", "typescript", "javascript", "rust", "go",
    }


def test_five_language_blast_radius(tmp_path: Path) -> None:
    """BLAST RADIUS renders sections for files of all 5 languages."""
    from gitoma.cpg.blast_radius import render_blast_radius_block
    _populate(tmp_path, {
        "lib.py": "def py_helper(): pass\n",
        "lib.ts": "export function tsHelper(): void {}\n",
        "lib.js": "export function jsHelper() {}\n",
        "lib.rs": "pub fn rust_helper() {}\n",
        "lib.go": "package lib\nfunc GoHelper() {}\n",
    })
    idx = build_index(tmp_path)
    block = render_blast_radius_block(
        ["lib.py", "lib.ts", "lib.js", "lib.rs", "lib.go"], idx,
    )
    for fn in ("py_helper", "tsHelper", "jsHelper", "rust_helper", "GoHelper"):
        assert fn in block


def test_five_language_skeleton_with_signatures(tmp_path: Path) -> None:
    """Skeletal renderer with Go signatures alongside the others."""
    from gitoma.cpg.skeletal import render_skeleton
    _populate(tmp_path, {
        "go_lib.go": (
            "package x\n"
            "func GoFn(n int, s string) (bool, error) { return false, nil }\n"
            "type Repo struct{}\n"
            "func (r *Repo) Find(id int) error { return nil }\n"
        ),
    })
    idx = build_index(tmp_path)
    out = render_skeleton(idx)
    assert "## go_lib.go" in out
    assert "GoFn(n int, s string) (bool, error)" in out
    assert "class Repo" in out
    assert "Find(id int) error" in out
