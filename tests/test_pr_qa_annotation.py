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


def _crashed(reason: str) -> QAResult:
    """Synthetic crash result — what run.py constructs in its Q&A
    except-handler. Mirrors the real call site so the tests would
    catch a contract drift if QAResult fields move around."""
    return QAResult(ran=False, crashed=True, crash_reason=reason)


# ── Crash branch (silent absence ≠ all clear) ───────────────────────────


def test_crashed_qa_emits_warning_block() -> None:
    """When the Q&A phase raised mid-flight, the PR body MUST flag
    it. Without this signal, a reviewer sees a clean-looking PR
    body and merges thinking the gate succeeded — when in reality
    the gate never produced answers."""
    out = build_qa_section(_crashed("ValueError: bad JSON in Defender output"))
    assert "Q&A self-consistency phase CRASHED" in out
    assert "Treat this PR as ungated" in out
    assert "ValueError: bad JSON in Defender output" in out


def test_crash_block_uses_warning_emoji_and_separator() -> None:
    """The block follows the same shape as the unfixed-gap block —
    leading separator + ⚠ heading — so it visually composes with the
    rest of the PR body without surprising layout shifts."""
    out = build_qa_section(_crashed("KeyError: 'verdict'"))
    assert out.startswith("\n---\n\n")
    assert "⚠️" in out


def test_crash_reason_first_line_only() -> None:
    """Multi-line tracebacks would explode the PR body. Cap to one
    line — the full traceback lives in the run's jsonl trace."""
    multi = "ValueError: explode\n  File 'x', line 1\n    raise ValueError"
    out = build_qa_section(_crashed(multi))
    # The Python-style ``File 'x'`` traceback frame must NOT leak in.
    assert "File 'x'" not in out
    # First line preserved.
    assert "ValueError: explode" in out


def test_crash_reason_is_truncated_to_300_chars() -> None:
    """Long single-line crash messages (LLM JSON parse-error dumps,
    repr of huge structures) should be capped to keep the block
    scannable — same 300-char policy as gap bullets."""
    huge = "RuntimeError: " + ("x" * 5000)
    out = build_qa_section(_crashed(huge))
    # Total `x` run inside the block must be at or under 300.
    assert "x" * 301 not in out
    assert "x" * 286 in out  # "RuntimeError: " + 286 x's = 300


def test_crashed_overrides_ran_check() -> None:
    """Even if ``ran=False`` (true for a crash that happened before
    Defender produced any answer), the crash branch must fire FIRST.
    Without this ordering, the crash signal would be eaten by the
    ``not qa_result.ran → empty`` early return."""
    out = build_qa_section(_crashed("any reason"))
    assert "CRASHED" in out


def test_crashed_with_no_reason_still_flags() -> None:
    """``crash_reason`` may legitimately be None (e.g. exception's
    str() returned empty). The block still fires with a placeholder
    so the operator-visible signal isn't suppressed."""
    out = build_qa_section(QAResult(ran=False, crashed=True, crash_reason=None))
    assert "CRASHED" in out
    assert "no reason captured" in out


def test_summary_line_for_crashed_result() -> None:
    """The console / state summary line should reflect the crash too,
    not silently say "Q&A: skipped (disabled)" (which would be a
    second invisible failure)."""
    line = _crashed("ValueError: x").summary_line()
    assert "crashed" in line.lower()
    assert "ValueError" in line


# ── Original gap/handled branches still take precedence over crashed ────


def test_handled_path_unaffected_by_new_crashed_field() -> None:
    """A normal handled result with crashed=False (default) must
    still return empty — ensure the new crash branch doesn't
    swallow the all-handled path."""
    out = build_qa_section(QAResult(ran=True, answers=[
        {"id": "Q1_evidence", "verdict": "handled",
         "evidence_loc": "x:1", "rationale": "ok"},
        {"id": "Q2_edge", "verdict": "handled",
         "evidence_loc": "x:2", "rationale": "ok"},
        {"id": "Q3_scope", "verdict": "handled",
         "evidence_loc": "x:3", "rationale": "ok"},
    ]))
    assert out == ""


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
