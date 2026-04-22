"""Q&A phase tests.

We cover the deterministic pieces that don't require a live LLM:
  * Pydantic schema enforcement (3 slots, 3 answers, both sides
    required)
  * _validate_evidence — programmatic flip of "handled" verdicts
    whose evidence_loc doesn't hold up against the diff / current files
  * QAResult.summary_line
  * Prompt contents — anti-sycophancy rules present

The full two-model pipeline is exercised by the bench re-run on rung-3,
not by these unit tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gitoma.critic.qa import _DEFENDER_SYSTEM, _QUESTIONER_SYSTEM, _validate_evidence
from gitoma.critic.types import (
    LLMQAAnswer,
    LLMQADefenderOutput,
    LLMQAQuestion,
    LLMQAQuestionerOutput,
    QAResult,
)


# ── Schema enforcement ──────────────────────────────────────────────────────


def test_questioner_must_emit_exactly_three_slots() -> None:
    """The schema locks the canonical Pareto slots — not 10 questions,
    not 1, not 5. Three, each with a fixed id."""
    with pytest.raises(ValidationError):
        LLMQAQuestionerOutput.model_validate({"questions": []})

    with pytest.raises(ValidationError):
        LLMQAQuestionerOutput.model_validate({
            "questions": [
                {"id": "Q1_evidence", "question": "cite file:line for the fix"},
            ],
        })

    # Two questions with same slot — schema rejects
    with pytest.raises(ValidationError):
        LLMQAQuestionerOutput.model_validate({
            "questions": [
                {"id": "Q1_evidence", "question": "cite file:line for the fix"},
                {"id": "Q1_evidence", "question": "name a covered edge"},
                {"id": "Q3_scope", "question": "over-scope line?"},
            ],
        })


def test_questioner_accepts_exactly_three_slots() -> None:
    LLMQAQuestionerOutput.model_validate({
        "questions": [
            {"id": "Q1_evidence", "question": "cite file:line where the fix lives"},
            {"id": "Q2_edge",     "question": "name an input that breaks it"},
            {"id": "Q3_scope",    "question": "which line was not required"},
        ],
    })


def test_defender_must_answer_all_three_slots() -> None:
    with pytest.raises(ValidationError):
        LLMQADefenderOutput.model_validate({"answers": []})
    with pytest.raises(ValidationError):
        LLMQADefenderOutput.model_validate({
            "answers": [
                {"id": "Q1_evidence", "verdict": "handled",
                 "evidence_loc": "src/x.py:10", "rationale": "see the line"},
                {"id": "Q1_evidence", "verdict": "handled",
                 "evidence_loc": "src/y.py:5", "rationale": "duplicate slot"},
                {"id": "Q3_scope", "verdict": "handled",
                 "evidence_loc": None, "rationale": "minimal diff"},
            ],
        })


# ── _validate_evidence — auto-flip handled→gap on bad citations ─────────────


def test_handled_without_evidence_loc_is_flipped_to_gap() -> None:
    ans = {"id": "Q1_evidence", "verdict": "handled",
           "evidence_loc": None, "rationale": "it's there trust me"}
    out = _validate_evidence(ans, branch_diff="", current_files={})
    assert out["verdict"] == "gap"
    assert "handled-without-evidence" in out["rationale"]


def test_handled_with_nonexistent_file_is_flipped() -> None:
    ans = {"id": "Q1_evidence", "verdict": "handled",
           "evidence_loc": "src/nope.py:10", "rationale": "see there"}
    out = _validate_evidence(ans, branch_diff="", current_files={"src/yes.py": "a\nb\n"})
    assert out["verdict"] == "gap"
    assert "file-not-in-diff" in out["rationale"]


def test_handled_with_line_past_eof_is_flipped() -> None:
    ans = {"id": "Q1_evidence", "verdict": "handled",
           "evidence_loc": "src/x.py:500", "rationale": "see far line"}
    out = _validate_evidence(ans, branch_diff="", current_files={"src/x.py": "x = 1\n"})
    assert out["verdict"] == "gap"
    assert "line-out-of-range" in out["rationale"]


def test_handled_with_valid_citation_is_kept() -> None:
    ans = {"id": "Q1_evidence", "verdict": "handled",
           "evidence_loc": "src/x.py:2", "rationale": "second line"}
    out = _validate_evidence(
        ans, branch_diff="",
        current_files={"src/x.py": "line1\nline2\nline3\n"},
    )
    assert out["verdict"] == "handled"


def test_diff_style_path_prefix_is_normalised() -> None:
    """gemma-4 style citations often use 'a/' diff prefix — the
    validator strips it before looking up the file."""
    ans = {"id": "Q1_evidence", "verdict": "handled",
           "evidence_loc": "a/src/x.py:1", "rationale": "line 1"}
    out = _validate_evidence(
        ans, branch_diff="",
        current_files={"src/x.py": "foo\n"},
    )
    assert out["verdict"] == "handled"


def test_gap_verdict_is_not_touched() -> None:
    """The validator only polices ``handled`` claims. Gaps and
    uncertain are left alone."""
    for v in ("gap", "uncertain"):
        ans = {"id": "Q2_edge", "verdict": v,
               "evidence_loc": None, "rationale": "not handled"}
        out = _validate_evidence(ans, branch_diff="", current_files={})
        assert out["verdict"] == v


# ── Summary line + prompt sanity ────────────────────────────────────────────


def test_qaresult_summary_line_when_not_ran() -> None:
    r = QAResult(ran=False)
    assert "skipped" in r.summary_line()


def test_qaresult_summary_line_counts_verdicts() -> None:
    r = QAResult(
        ran=True,
        answers=[
            {"id": "Q1_evidence", "verdict": "handled", "rationale": "...", "evidence_loc": "f:1"},
            {"id": "Q2_edge",     "verdict": "gap",     "rationale": "...", "evidence_loc": None},
            {"id": "Q3_scope",    "verdict": "handled", "rationale": "...", "evidence_loc": None},
        ],
    )
    out = r.summary_line()
    assert "2 handled" in out
    assert "1 gap" in out


def test_questioner_prompt_has_brutal_markers() -> None:
    """User 2026-04-22pm: brutal, honest, rational, expert, without pity."""
    lower = _QUESTIONER_SYSTEM.lower()
    assert "brutal" in lower
    assert "senior engineer" in lower
    # No praise, no soft language
    assert "short" in lower
    # Three canonical slots
    assert "q1_evidence" in lower
    assert "q2_edge" in lower
    assert "q3_scope" in lower


def test_defender_prompt_has_anti_sycophancy_markers() -> None:
    """User 2026-04-22pm: 'il coder deve essere gated'. The Defender
    prompt must explicitly tell the LLM NOT to concede to every question."""
    lower = _DEFENDER_SYSTEM.lower()
    assert "do not concede" in lower
    # "you do not have to act on\nall of them" — tolerate the newline
    assert "do not have to act on" in lower
    # Schema-strict output
    assert "json object" in lower
    # Patches must close a real gap
    assert "minimal" in lower