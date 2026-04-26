"""Integration tests for CPG-lite ↔ worker prompt assembly.

These tests bypass the LLM call but exercise the full prompt-build
path: worker constructed with a CPGIndex, subtask with a Python
file_hint, expected BLAST RADIUS block surfaces in the user prompt.

Closes the gap between the unit tests (each layer in isolation) and
the real LM-Studio run (too expensive for every commit)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.cpg.blast_radius import render_blast_radius_block
from gitoma.planner.prompts import worker_user_prompt


def _build(tmp_path: Path, files: dict[str, str]):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return build_index(tmp_path)


def test_worker_prompt_contains_blast_radius_when_cpg_provided(
    tmp_path: Path,
) -> None:
    """End-to-end at the prompt level: when the indexer + renderer +
    worker_user_prompt are wired together, the final user prompt
    contains a BLAST RADIUS block citing the right callers."""
    idx = _build(tmp_path, {
        "lib.py": "def helper(): pass\n",
        "app.py": (
            "from lib import helper\n"
            "def caller():\n"
            "    helper()\n"
        ),
    })
    block = render_blast_radius_block(["lib.py"], idx)
    prompt = worker_user_prompt(
        subtask_title="Refactor helper()",
        subtask_description="Tighten the helper signature.",
        file_hints=["lib.py"],
        languages=["Python"],
        repo_name="demo",
        current_files={"lib.py": "def helper(): pass\n"},
        file_tree=["lib.py", "app.py"],
        extra_context_block=block,
    )
    assert "== BLAST RADIUS (CPG-lite) ==" in prompt
    assert "helper" in prompt
    assert "app.py" in prompt
    # Crucial positioning check: the block must appear AFTER the
    # files section (so the LLM sees the symbol contents first, then
    # the impact map) and BEFORE the FILE TREE.
    files_idx = prompt.index("== CURRENT FILE CONTENTS ==")
    blast_idx = prompt.index("== BLAST RADIUS (CPG-lite) ==")
    tree_idx = prompt.index("== FILE TREE ==")
    assert files_idx < blast_idx < tree_idx


def test_worker_prompt_no_blast_radius_when_cpg_off(tmp_path: Path) -> None:
    """Default path: when ``extra_context_block=None`` the prompt
    must be exactly what it was pre-CPG (no header, no empty
    placeholder)."""
    prompt = worker_user_prompt(
        subtask_title="t",
        subtask_description="d",
        file_hints=["x.py"],
        languages=["Python"],
        repo_name="r",
        current_files={"x.py": "x = 1\n"},
        file_tree=["x.py"],
        extra_context_block=None,
    )
    assert "BLAST RADIUS" not in prompt


def test_worker_prompt_no_blast_radius_for_non_python_subtask(
    tmp_path: Path,
) -> None:
    """A subtask whose file_hints are all non-Python (e.g. config
    files in the quality vertical) should produce an EMPTY block,
    which the worker-side gate must filter to None — verified by
    rendering directly."""
    idx = _build(tmp_path, {"x.py": "def foo(): pass\n"})
    block = render_blast_radius_block(["config.toml", ".prettierrc"], idx)
    assert block == ""
    # When the worker passes "" through, the prompt should still
    # contain no header (current implementation accepts truthy only).
    prompt = worker_user_prompt(
        subtask_title="t", subtask_description="d",
        file_hints=["config.toml"], languages=["Python"],
        repo_name="r", current_files={}, file_tree=[],
        extra_context_block=block or None,
    )
    assert "BLAST RADIUS" not in prompt


def test_blast_radius_against_real_gitoma_module() -> None:
    """The signal-density check: render the BLAST RADIUS for
    ``gitoma/cpg/storage.py`` (a known-load-bearing module) using
    the live gitoma index. Verifies the integration produces
    actionable output, not just a header."""
    repo_root = Path(__file__).resolve().parents[2]
    idx = build_index(repo_root, max_files=500)
    block = render_blast_radius_block(["gitoma/cpg/storage.py"], idx)
    assert block != ""
    # Must mention the Storage class explicitly (it's the module's
    # primary export and we verified callers exist in self-bench).
    assert "Storage" in block
    # Must cite at least one consumer file.
    assert any(
        line.startswith("    called from")
        for line in block.splitlines()
    )
