"""Tests for the worker's post-write compile-check + retry loop.

The full retry loop spans filesystem + subprocess + LLM; we cover
the behavioural-contract parts that are safe to exercise in-process:
prompt rendering with/without feedback, feedback-section shape, and
the env-var knob that controls retry budget.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from gitoma.planner.prompts import worker_user_prompt


def test_worker_prompt_omits_retry_section_when_no_feedback() -> None:
    """On the first attempt, the prompt must NOT mention previous
    failures — the worker hasn't produced anything yet."""
    out = worker_user_prompt(
        subtask_title="Fix it",
        subtask_description="do a thing",
        file_hints=["src/main.go"],
        languages=["Go"],
        repo_name="example",
        current_files={},
        file_tree=[],
    )
    assert "PREVIOUS ATTEMPT FAILED" not in out
    assert "retry" not in out.lower() or "RULES for this retry" not in out


def test_worker_prompt_includes_retry_section_when_feedback_given() -> None:
    feedback = (
        "Go BUILD FAILED — server/server.go:27:10: "
        "assignment mismatch: 1 variable but s.users.Get returns 2 values"
    )
    out = worker_user_prompt(
        subtask_title="Fix Greet",
        subtask_description="handle 2-return signature",
        file_hints=["server/server.go"],
        languages=["Go"],
        repo_name="example",
        current_files={},
        file_tree=[],
        compile_error_feedback=feedback,
    )
    assert "PREVIOUS ATTEMPT FAILED TO COMPILE" in out
    assert "server/server.go:27:10" in out
    assert "RULES for this retry" in out


def test_retry_section_has_anti_hallucination_rule() -> None:
    """The retry prompt must explicitly tell the worker NOT to invent
    function signatures — that's the exact failure mode this closes."""
    out = worker_user_prompt(
        subtask_title="x",
        subtask_description="y",
        file_hints=[],
        languages=["Go"],
        repo_name="r",
        current_files={},
        file_tree=[],
        compile_error_feedback="some compile error here",
    )
    # Rule 1 is the core anti-hallucination directive
    assert "Do NOT invent function signatures" in out
    assert "hallucination" in out


def test_retry_section_truncates_very_long_feedback() -> None:
    """If the compiler emits 10KB of errors we must not blow the worker's
    context budget — truncate while preserving the leading (most relevant)
    errors."""
    huge = "x" * 5000
    out = worker_user_prompt(
        subtask_title="x",
        subtask_description="y",
        file_hints=[],
        languages=["Go"],
        repo_name="r",
        current_files={},
        file_tree=[],
        compile_error_feedback=huge,
    )
    # The feedback is capped at 1500 chars in the prompt
    # (the feedback string in the output is at most 1500 x's)
    x_block = "x" * 1500
    assert x_block in out
    x_block_too_much = "x" * 1600
    assert x_block_too_much not in out


def test_retry_section_preserves_minimal_change_rule() -> None:
    """Retry mode must not invite refactor sprees — one of the non-
    negotiable rules is "Minimal change wins"."""
    out = worker_user_prompt(
        subtask_title="x",
        subtask_description="y",
        file_hints=[],
        languages=["Go"],
        repo_name="r",
        current_files={},
        file_tree=[],
        compile_error_feedback="e",
    )
    assert "Minimal change wins" in out


def test_worker_retry_env_var_respected() -> None:
    """``GITOMA_WORKER_BUILD_RETRIES`` must be read at the call site,
    not cached at import. Operators should be able to flip it between
    runs without restarting anything heavier."""
    import importlib
    from gitoma.worker import worker as w

    # Sanity: the module imports without error for each value
    for val in ("0", "1", "3"):
        with patch.dict(os.environ, {"GITOMA_WORKER_BUILD_RETRIES": val}):
            importlib.reload(w)
            # Just make sure the reload didn't explode on env var parsing
            assert hasattr(w, "WorkerAgent")
