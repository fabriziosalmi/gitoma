"""Tests for the BLAST RADIUS prompt block renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.cpg.blast_radius import (
    MAX_CALLERS_PER_SYMBOL,
    render_blast_radius_block,
)


def _build(tmp_path: Path, files: dict[str, str]):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return build_index(tmp_path)


def test_render_returns_empty_when_no_python_files(tmp_path: Path) -> None:
    idx = _build(tmp_path, {"a.py": "def foo(): pass\n"})
    assert render_blast_radius_block(["README.md", ".prettierrc"], idx) == ""


def test_render_returns_empty_when_no_relevant_symbols(tmp_path: Path) -> None:
    """A file containing only private symbols (leading underscore)
    contributes no public surface — block should be empty rather than
    emit a header with nothing under it."""
    idx = _build(tmp_path, {"only_private.py": "def _helper(): pass\n"})
    assert render_blast_radius_block(["only_private.py"], idx) == ""


def test_render_includes_callers_when_present(tmp_path: Path) -> None:
    idx = _build(tmp_path, {
        "a.py": "def helper(): pass\n",
        "b.py": (
            "from a import helper\n"
            "def caller():\n"
            "    helper()\n"
        ),
    })
    block = render_blast_radius_block(["a.py"], idx)
    assert "BLAST RADIUS" in block
    assert "helper" in block
    assert "b.py" in block


def test_render_marks_no_callers_explicitly(tmp_path: Path) -> None:
    """Symbols with zero cross-file callers must be labeled — silent
    omission would let the LLM assume "no info" instead of "verified
    no callers"."""
    idx = _build(tmp_path, {"a.py": "def lonely(): pass\n"})
    block = render_blast_radius_block(["a.py"], idx)
    assert "lonely" in block
    assert "no cross-file callers" in block


def test_render_caps_caller_count_with_more_marker(tmp_path: Path) -> None:
    """For symbols with too many callers, cap the list and add a
    ``+N more`` marker so prompt size stays bounded."""
    callers_src = "from a import widely_used\n"
    callers_src += "\n".join(
        f"def caller_{i}():\n    widely_used()\n"
        for i in range(MAX_CALLERS_PER_SYMBOL + 3)
    )
    idx = _build(tmp_path, {
        "a.py": "def widely_used(): pass\n",
        "b.py": callers_src,
    })
    block = render_blast_radius_block(["a.py"], idx)
    # The exact "+N more" count varies (CALL refs + the IMPORT_FROM
    # ref both count as callers); just assert the marker shape is
    # present and N > 0.
    import re
    match = re.search(r"\(\+(\d+) more\)", block)
    assert match is not None, f"Expected '+N more' marker in block: {block}"
    assert int(match.group(1)) >= 1


def test_render_skips_non_python_paths_silently(tmp_path: Path) -> None:
    """Mixed file_hints: Python entries get a section, non-Python are
    silently dropped (not an error)."""
    idx = _build(tmp_path, {
        "a.py": "def foo(): pass\n",
        "b.py": (
            "from a import foo\n"
            "def caller():\n    foo()\n"
        ),
    })
    block = render_blast_radius_block(["a.py", "config.toml"], idx)
    assert "a.py" in block
    assert "config.toml" not in block


def test_render_handles_empty_input_list(tmp_path: Path) -> None:
    idx = _build(tmp_path, {"a.py": "def foo(): pass\n"})
    assert render_blast_radius_block([], idx) == ""


# ── v0.5-slim: TypeScript coverage ─────────────────────────────────


def test_render_recognises_ts_files(tmp_path: Path) -> None:
    """A .ts file with public symbols should produce a section just
    like a .py file would. Verifies blast_radius doesn't filter
    out TypeScript paths."""
    from gitoma.cpg.storage import Storage
    from gitoma.cpg.typescript_indexer import index_typescript_file
    from gitoma.cpg.queries import CPGIndex
    a = tmp_path / "a.ts"
    a.write_text(
        "export function helper(): void {}\n"
        "export interface User { id: number; }\n"
    )
    s = Storage()
    index_typescript_file(a, "a.ts", s)
    idx = CPGIndex(s)
    block = render_blast_radius_block(["a.ts"], idx)
    assert "BLAST RADIUS" in block
    assert "helper" in block
    assert "User" in block  # interface should also surface


def test_render_recognises_tsx_files(tmp_path: Path) -> None:
    from gitoma.cpg.storage import Storage
    from gitoma.cpg.typescript_indexer import index_typescript_file
    from gitoma.cpg.queries import CPGIndex
    f = tmp_path / "Comp.tsx"
    f.write_text("export function Button(): void {}\n")
    s = Storage()
    index_typescript_file(f, "Comp.tsx", s)
    idx = CPGIndex(s)
    block = render_blast_radius_block(["Comp.tsx"], idx)
    assert "Button" in block
