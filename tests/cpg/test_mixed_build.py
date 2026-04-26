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
