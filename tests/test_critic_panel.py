"""Regression tests for the multi-persona critic panel (M7, walking skeleton).

Covers the orchestrator + parser + state interaction without hitting a real
LLM. The mock-LLM returns canned JSON shaped after the F001 finding from
``tests/fixtures/slop_audit_b2v_pr10.json`` — that's the regression baseline:
if the orchestrator can't surface a blocker on this canned input, the panel
is broken before we ever blame the model.

Live LLM tests (real model against the real PR#10 diff) live in a future
opt-in suite (probably ``tests/e2e_critic/``); keeping them out of the
default run so ``pytest -q`` stays hermetic.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from gitoma.core.config import CriticPanelConfig
from gitoma.critic.panel import CriticPanel, _parse_findings
from gitoma.critic.types import Finding, PanelResult


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_llm(*, content: str = "{}", usage: tuple[int, int] | None = (50, 30)):
    """Return a minimal LLM stub that satisfies CriticPanel's expectations.

    ``chat`` is a plain attr (not MagicMock) so failure modes surface as
    AttributeError if we mistakenly call something else."""
    llm = MagicMock()
    llm.chat = MagicMock(return_value=content)
    llm._last_usage = usage
    return llm


def _cfg(**overrides) -> CriticPanelConfig:
    """Build a CriticPanelConfig with sensible test defaults + overrides."""
    cfg = CriticPanelConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# F001-shape canned response: a 'dev' persona spotting the broken pre-commit
# config. Matches the golden audit fixture's expected_findings[0] shape so a
# future cross-check is one-line.
_F001_LIKE = json.dumps({
    "findings": [
        {
            "severity": "blocker",
            "category": "broken_configuration",
            "summary": "pre-commit config references invented hook URL and uses files: as a list",
            "file": ".pre-commit-config.yaml",
            "line_range": [1, 23],
        }
    ]
})


# ── Mode handling ──────────────────────────────────────────────────────────


def test_mode_off_returns_no_op_without_calling_llm():
    """Defensive: even if the panel is somehow invoked with mode=off, it
    must return ``no_op`` and NOT touch the LLM. Caller (worker) also gates
    on mode, but defence in depth."""
    llm = _make_llm()
    panel = CriticPanel(_cfg(mode="off"), llm)
    result = panel.review(subtask_id="T001-S01", diff_text="diff --git ... real diff")
    assert result.verdict == "no_op"
    assert result.findings == []
    llm.chat.assert_not_called()


def test_empty_diff_returns_no_op():
    """Whitespace-only or empty diffs short-circuit before any LLM call —
    nothing to critique, the patcher de-duped or only touched whitespace."""
    llm = _make_llm()
    panel = CriticPanel(_cfg(mode="advisory"), llm)
    assert panel.review(subtask_id="x", diff_text="").verdict == "no_op"
    assert panel.review(subtask_id="x", diff_text="   \n   ").verdict == "no_op"
    llm.chat.assert_not_called()


# ── Persona invocation + aggregation ───────────────────────────────────────


def test_advisory_mode_calls_each_persona_once():
    """One LLM call per persona on the configured list; iteration 1 only
    has 'dev' but the wiring should cleanly handle a multi-persona list
    once we enable arch + contributor."""
    llm = _make_llm(content=_F001_LIKE)
    panel = CriticPanel(_cfg(mode="advisory", personas="dev"), llm)
    result = panel.review(subtask_id="T001-S01", diff_text="diff --git a/x b/x\n+broken")
    assert result.verdict == "advisory_logged"
    assert result.personas_called == ["dev"]
    assert llm.chat.call_count == 1


def test_aggregates_findings_across_personas():
    """When multiple personas are configured, all their findings flow into
    a single result. Iteration 2 ships dev + arch + contributor as real
    personas, so we exercise the actual registry instead of monkey-patching."""
    arch_finding = json.dumps({
        "findings": [{
            "severity": "minor",
            "category": "redundant_duplicate",
            "summary": "this index duplicates docs/index.md",
            "file": "docs/guide/index.md",
            "line_range": [1, 26],
        }]
    })
    contributor_finding = json.dumps({
        "findings": [{
            "severity": "major",
            "category": "setup_breakage",
            "summary": "pre-commit config will fail on first run; broken hook URL",
            "file": ".pre-commit-config.yaml",
        }]
    })
    # Three personas, three distinct return values — answers cycle by call order.
    llm = MagicMock()
    llm.chat = MagicMock(side_effect=[_F001_LIKE, arch_finding, contributor_finding])
    llm._last_usage = (40, 20)

    panel = CriticPanel(_cfg(mode="advisory", personas="dev,arch,contributor"), llm)
    result = panel.review(subtask_id="x", diff_text="diff")

    assert result.verdict == "advisory_logged"
    assert result.personas_called == ["dev", "arch", "contributor"]
    assert len(result.findings) == 3
    by_persona = {f.persona for f in result.findings}
    assert by_persona == {"dev", "arch", "contributor"}
    # has_blocker reflects the worst-of across personas (dev returned blocker).
    assert result.has_blocker() is True
    # Token totals stack across calls (same usage stub each time).
    assert result.tokens_extra == (120, 60)


# ── Iteration 2: persona registry + prompt distinctness ──────────────────────


def test_three_personas_are_registered():
    """Iteration 2 contract: dev + arch + contributor are all available
    in the registry. A test pins it so a future refactor that drops
    a persona by accident fails loud at unit-test time, not at the
    next gitoma run."""
    from gitoma.critic.personas import available
    assert set(available()) >= {"dev", "arch", "contributor"}


def test_persona_prompts_are_distinct_in_focus():
    """Each persona's system prompt must mention its angle and explicitly
    delegate the other angles. A drift where two personas converge on
    the same angle = wasted LLM call. The check is a substring sniff —
    not perfect but enough to catch a copy-paste regression that
    accidentally re-uses one prompt for all three."""
    from gitoma.critic.personas import system_prompt_for
    dev_prompt = system_prompt_for("dev")
    arch_prompt = system_prompt_for("arch")
    contrib_prompt = system_prompt_for("contributor")

    # Each prompt must declare its specific role term.
    assert "developer" in dev_prompt.lower()
    assert "architect" in arch_prompt.lower()
    assert "contributor" in contrib_prompt.lower()

    # Each prompt must explicitly delegate the OTHER personas' areas
    # (defence against copy-paste drift where one prompt overlaps).
    assert "'arch'" in dev_prompt and "'contributor'" in dev_prompt
    assert "'dev'" in arch_prompt and "'contributor'" in arch_prompt
    assert "'dev'" in contrib_prompt and "'arch'" in contrib_prompt


def test_persona_failure_becomes_synthetic_finding():
    """A crashing persona must NOT bubble up — it becomes a 'minor'
    critic_call_failed finding so the audit trail captures the failure
    without blowing up the worker."""
    llm = MagicMock()
    llm.chat = MagicMock(side_effect=RuntimeError("boom"))
    llm._last_usage = None

    panel = CriticPanel(_cfg(mode="advisory", personas="dev"), llm)
    result = panel.review(subtask_id="x", diff_text="diff")

    assert result.verdict == "advisory_logged"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity == "minor"
    assert f.category == "critic_call_failed"
    assert "RuntimeError" in f.summary
    assert "boom" in f.summary


# ── Parser robustness ─────────────────────────────────────────────────────


def test_parser_extracts_findings_from_well_formed_json():
    """Happy path — a clean JSON response with the expected shape."""
    findings = _parse_findings(_F001_LIKE, persona="dev")
    assert len(findings) == 1
    assert findings[0].severity == "blocker"
    assert findings[0].category == "broken_configuration"
    assert findings[0].file == ".pre-commit-config.yaml"
    assert findings[0].line_range == (1, 23)


def test_parser_tolerates_prose_padding_around_json():
    """Small models often wrap output in prose / code fences. The
    extractor must grab the first balanced ``{...}`` block and ignore
    the surrounding noise."""
    raw = (
        "Sure, here is my analysis:\n\n"
        "```json\n" + _F001_LIKE + "\n```\n\n"
        "Let me know if you need clarification."
    )
    findings = _parse_findings(raw, persona="dev")
    assert len(findings) == 1
    assert findings[0].severity == "blocker"


def test_parser_handles_unknown_severity_by_downgrading_to_minor():
    """Schema drift: small models occasionally emit invented severities
    (``"crit"``, ``"high"``, ``"WAT"``). Rather than drop the finding
    we downgrade to 'minor' so the audit trail still captures it."""
    raw = json.dumps({"findings": [
        {"severity": "WAT", "category": "x", "summary": "y"}
    ]})
    findings = _parse_findings(raw, persona="dev")
    assert len(findings) == 1
    assert findings[0].severity == "minor"


def test_parser_returns_empty_on_garbage():
    """Hard guarantee: parser NEVER raises — returns an empty list when
    it can't make sense of the input. The orchestrator turns parse
    failures into critic_call_failed findings (not the parser's job)."""
    assert _parse_findings("", persona="dev") == []
    assert _parse_findings("just prose, no json at all", persona="dev") == []
    assert _parse_findings("{not even valid json", persona="dev") == []
    # Non-dict top-level
    assert _parse_findings("[1, 2, 3]", persona="dev") == []
    # Right shape but findings is wrong type
    assert _parse_findings('{"findings": "should be list"}', persona="dev") == []


def test_parser_skips_malformed_findings_keeps_well_formed_ones():
    """Mixed batch: one valid finding + one garbage finding → valid
    survives, garbage skipped silently."""
    raw = json.dumps({"findings": [
        {"severity": "blocker", "category": "x", "summary": "good"},
        "this is not even an object",
        {"severity": "major", "category": "y", "summary": "also good"},
    ]})
    findings = _parse_findings(raw, persona="dev")
    assert len(findings) == 2
    assert [f.severity for f in findings] == ["blocker", "major"]


# ── Token usage / cost telemetry hook ─────────────────────────────────────


def test_panel_records_token_usage_when_llm_reports_it():
    """First step toward M2 cost telemetry: when the LLM client surfaces
    ``_last_usage``, the panel sums it and exposes via ``tokens_extra``."""
    llm = _make_llm(content=_F001_LIKE, usage=(120, 80))
    panel = CriticPanel(_cfg(mode="advisory", personas="dev"), llm)
    result = panel.review(subtask_id="x", diff_text="diff")
    assert result.tokens_extra == (120, 80)


def test_panel_skips_token_usage_when_llm_does_not_report():
    """Some self-hosted backends omit usage. The panel must surface
    ``None`` rather than fake-zero — knowing 'we don't know' is more
    useful than a misleading 0."""
    llm = _make_llm(content=_F001_LIKE, usage=None)
    panel = CriticPanel(_cfg(mode="advisory", personas="dev"), llm)
    result = panel.review(subtask_id="x", diff_text="diff")
    assert result.tokens_extra is None


# ── Cross-check against the golden fixture schema ─────────────────────────


def test_finding_to_dict_matches_fixture_schema():
    """The golden fixture (tests/fixtures/slop_audit_b2v_pr10.json) is the
    contract for what the panel must eventually surface against the real
    PR#10 diff. The Finding.to_dict() shape must match the fixture's
    ``expected_findings[*]`` keys so a future eval test can compare
    directly without translation."""
    f = Finding(
        persona="dev",
        severity="blocker",
        category="broken_configuration",
        summary="pre-commit config is non-functional end-to-end",
        file=".pre-commit-config.yaml",
        line_range=(1, 23),
    )
    d = f.to_dict()
    # Same keys (subset) as the fixture's per-finding schema.
    for required in ("severity", "category", "summary", "file", "line_range"):
        assert required in d, f"Finding.to_dict() missing {required!r}"
    # line_range must be JSON-serialisable (list, not tuple)
    assert d["line_range"] == [1, 23]
    # Round-trip through json.dumps must succeed (state.json safety).
    json.dumps(d)


def test_panel_result_to_dict_serialisable_for_state_log():
    """PanelResult.to_dict() is what gets appended to
    AgentState.critic_panel_findings_log and persisted in state.json. It
    must round-trip through json.dumps without error."""
    result = PanelResult(
        subtask_id="T001-S01",
        verdict="advisory_logged",
        personas_called=["dev"],
        findings=[Finding("dev", "blocker", "x", "y", file="a.py", line_range=(1, 5))],
        tokens_extra=(50, 30),
    )
    json.dumps(result.to_dict())  # must not raise
