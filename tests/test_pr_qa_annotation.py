"""PR-body Q&A annotation block — visibility surface for unfixed gaps.

Caught live on rung-3 v6 (2026-04-22pm): Q&A pipeline reported a gap
honestly, but the PR shipped without surfacing the signal — operator
had no easy way to spot the unfixed flaw before merging. ``build_qa_section``
closes that gap.
"""

from __future__ import annotations

from gitoma.critic.types import QAResult
from gitoma.pr.templates import build_qa_section


def _result(*, ran=True, answers=None, revised_applied=False, revert_reason=None):
    return QAResult(
        ran=ran,
        answers=answers or [],
        revised_applied=revised_applied,
        revert_reason=revert_reason,
    )


def test_no_qa_result_returns_empty() -> None:
    """When the run never reached the Q&A phase, the body must NOT
    grow a Q&A section — keeps the body focused for ordinary runs."""
    assert build_qa_section(None) == ""


def test_qa_disabled_returns_empty() -> None:
    """``ran=False`` (env-disabled) → no annotation."""
    assert build_qa_section(_result(ran=False)) == ""


def test_all_handled_returns_empty() -> None:
    """When every slot was answered ``handled``, there's no actionable
    signal for the operator. Don't pollute the body."""
    out = build_qa_section(_result(answers=[
        {"id": "Q1_evidence", "verdict": "handled",
         "evidence_loc": "src/x.py:10", "rationale": "see line"},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "tests/x.py:5", "rationale": "test covers"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": None, "rationale": "minimal diff"},
    ]))
    assert out == ""


def test_uncertain_only_returns_empty() -> None:
    """``uncertain`` is honest non-information. Without a real gap the
    operator has nothing to act on."""
    out = build_qa_section(_result(answers=[
        {"id": "Q1_evidence", "verdict": "uncertain",
         "evidence_loc": None, "rationale": "can't tell"},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "src/x.py:1", "rationale": "covered"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": None, "rationale": "minimal"},
    ]))
    assert out == ""


def test_unfixed_gap_emits_warning_block() -> None:
    """The most important case: gap admitted, no patch landed → block
    in the PR body that the operator can't miss."""
    out = build_qa_section(_result(answers=[
        {"id": "Q1_evidence", "verdict": "gap",
         "evidence_loc": None,
         "rationale": "no line in db.py shows the parameterised query"},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "src/db.py:12", "rationale": "init covers it"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": None, "rationale": "minimal"},
    ]))
    assert "⚠️ Q&A gap (unfixed)" in out
    assert "Review carefully before merging" in out
    assert "Q1_evidence" in out
    assert "no line in db.py shows" in out


def test_revised_applied_emits_positive_block() -> None:
    """Gap was real AND closed by gated revision — block exists for
    transparency but reads as confirmation, not warning."""
    out = build_qa_section(_result(
        answers=[
            {"id": "Q1_evidence", "verdict": "gap",
             "evidence_loc": None, "rationale": "missing parameterised query"},
            {"id": "Q2_edge", "verdict": "handled",
             "evidence_loc": "src/db.py:12", "rationale": "covered"},
            {"id": "Q3_scope", "verdict": "handled",
             "evidence_loc": None, "rationale": "minimal"},
        ],
        revised_applied=True,
    ))
    assert "✅ Q&A revised patch landed" in out
    # Original gap context still preserved for the reader
    assert "Q1_evidence" in out
    # Must NOT carry the warning headline
    assert "⚠️ Q&A gap (unfixed)" not in out


def test_revert_reason_surfaces_when_present() -> None:
    """When the apply gate rejected a Defender revision (build/test
    failed), the operator deserves to know WHY, not just that there's
    a gap. Revert reason is part of the actionable signal."""
    out = build_qa_section(_result(
        answers=[
            {"id": "Q1_evidence", "verdict": "gap",
             "evidence_loc": None, "rationale": "no fix line"},
            {"id": "Q2_edge", "verdict": "handled",
             "evidence_loc": "src/x.py:1", "rationale": "covered"},
            {"id": "Q3_scope", "verdict": "handled",
             "evidence_loc": None, "rationale": "minimal"},
        ],
        revert_reason="Q&A revised tests failed: 2 tests fail with TypeError",
    ))
    assert "⚠️ Q&A gap (unfixed)" in out
    assert "revised patch was attempted but reverted" in out
    assert "tests failed" in out


def test_block_is_appended_at_end_with_separator() -> None:
    """The block opens with a markdown separator + heading so it never
    blurs into the previous Tasks-Completed section visually."""
    out = build_qa_section(_result(answers=[
        {"id": "Q1_evidence", "verdict": "gap",
         "evidence_loc": None, "rationale": "missing"},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "x:1", "rationale": "ok"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": None, "rationale": "ok"},
    ]))
    assert out.startswith("\n---\n\n## ")


def test_long_rationale_is_truncated() -> None:
    """Defender rationales are capped at 400 chars by Pydantic, but
    the section caps each bullet at 300 to keep the block scannable."""
    huge = "x" * 1000
    out = build_qa_section(_result(answers=[
        {"id": "Q1_evidence", "verdict": "gap",
         "evidence_loc": None, "rationale": huge},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "x:1", "rationale": "ok"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": None, "rationale": "ok"},
    ]))
    # Truncated to <= 300 x's in the block (one bullet)
    assert "x" * 301 not in out
    assert "x" * 300 in out
