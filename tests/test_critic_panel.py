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


# ── Iteration 3: multi-model routing + devil's advocate ──────────────────────


def test_panel_passes_panel_model_to_chat_when_set():
    """When ``CriticPanelConfig.panel_model`` is non-empty, the panel
    must thread it through to ``LLMClient.chat(model=...)``. Empty value
    falls back to the client's default model."""
    llm = _make_llm(content=_F001_LIKE)
    cfg = _cfg(mode="advisory", personas="dev", panel_model="qwen3.5-9b-sushi-coder")
    CriticPanel(cfg, llm).review(subtask_id="x", diff_text="diff")
    # Inspect the kwargs of the actual chat() call.
    _, kwargs = llm.chat.call_args
    assert kwargs.get("model") == "qwen3.5-9b-sushi-coder"
    assert kwargs.get("temperature") == 0.3


def test_panel_passes_no_model_override_when_empty():
    """Empty ``panel_model`` (the default) must result in ``model=None``
    so the LLMClient uses its configured default — the worker's model."""
    llm = _make_llm(content=_F001_LIKE)
    cfg = _cfg(mode="advisory", personas="dev", panel_model="")
    CriticPanel(cfg, llm).review(subtask_id="x", diff_text="diff")
    _, kwargs = llm.chat.call_args
    assert kwargs.get("model") is None


def test_devil_advocate_runs_against_full_branch_diff():
    """Smoke: DevilsAdvocate.review() processes a full-branch diff,
    parses findings, and returns a PanelResult with subtask_id="__devil__"
    so the state log can distinguish devil entries from per-subtask ones."""
    from gitoma.critic.devil import DevilsAdvocate

    devil_response = json.dumps({"findings": [{
        "severity": "blocker",
        "category": "compile_breakage",
        "summary": "removed fn main() — Rust binary won't link",
        "file": "src/main.rs",
        "line_range": [56, 103],
    }]})
    llm = MagicMock()
    llm.chat = MagicMock(return_value=devil_response)
    llm._last_usage = (5000, 200)

    full_cfg = MagicMock()
    full_cfg.lmstudio.base_url = "http://localhost:1234/v1"
    cp_cfg = _cfg(devil_advocate=True, devil_model="qwen3.5-9b")

    devil = DevilsAdvocate(cp_cfg, llm, full_cfg)
    result = devil.review(full_branch_diff="diff --git a/main.rs ...", branch_name="feat/x")

    assert result.subtask_id == "__devil__"
    assert result.verdict == "advisory_logged"
    assert result.personas_called == ["devil"]
    assert len(result.findings) == 1
    assert result.findings[0].severity == "blocker"
    assert result.findings[0].persona == "devil"
    assert result.tokens_extra == (5000, 200)
    # Used the devil_model override
    _, kwargs = llm.chat.call_args
    assert kwargs.get("model") == "qwen3.5-9b"
    # Used devil_temperature (default 0.4), NOT panel temperature
    assert kwargs.get("temperature") == cp_cfg.devil_temperature


def test_devil_advocate_empty_diff_returns_no_op():
    """If somehow the devil is invoked with an empty branch diff
    (very-empty PR, or diff command failed silently), short-circuit to
    no_op without spending an LLM call."""
    from gitoma.critic.devil import DevilsAdvocate

    llm = MagicMock()
    llm.chat = MagicMock()
    devil = DevilsAdvocate(_cfg(devil_advocate=True), llm, MagicMock())
    result = devil.review(full_branch_diff="")
    assert result.subtask_id == "__devil__"
    assert result.verdict == "no_op"
    llm.chat.assert_not_called()


def test_devil_advocate_call_failure_becomes_synthetic_finding():
    """If the LLM call crashes (network error, malformed response that
    triggers TypeError), the devil must NOT propagate — it returns a
    synthetic ``critic_call_failed`` finding so the trace still records
    the failure."""
    from gitoma.critic.devil import DevilsAdvocate

    llm = MagicMock()
    llm.chat = MagicMock(side_effect=ConnectionError("endpoint unreachable"))
    devil = DevilsAdvocate(_cfg(devil_advocate=True), llm, MagicMock())
    result = devil.review(full_branch_diff="diff content")
    assert result.verdict == "advisory_logged"
    assert len(result.findings) == 1
    assert result.findings[0].category == "critic_call_failed"
    assert "ConnectionError" in result.findings[0].summary


# ── Iteration 4: refinement turn (cap 1) + meta-eval keep-if-better ─────────


def test_refiner_should_refine_only_on_blocker_or_major():
    """The refinement turn is expensive (extra LLM call + extra commit
    that may need revert). Only worth it for blocker/major. nit/minor
    is cosmetic and the panel already captured it."""
    from gitoma.critic.refiner import Refiner

    r = Refiner.__new__(Refiner)  # bypass __init__, only testing pure method
    assert r.should_refine([Finding("devil", "blocker", "x", "y")]) is True
    assert r.should_refine([Finding("devil", "major", "x", "y")]) is True
    assert r.should_refine([Finding("devil", "minor", "x", "y")]) is False
    assert r.should_refine([Finding("devil", "nit", "x", "y")]) is False
    assert r.should_refine([]) is False
    # Mixed list: at least one trigger ⇒ refine
    assert r.should_refine([
        Finding("devil", "nit", "x", "y"),
        Finding("devil", "blocker", "x", "y"),
    ]) is True


def test_refiner_propose_returns_empty_when_no_triggers():
    """Defensive: if propose() is called with only nit/minor findings
    (caller forgot to gate on should_refine), it returns empty patches
    instead of burning an LLM call on a non-trigger."""
    from gitoma.critic.refiner import Refiner

    llm = MagicMock()
    llm.chat_json = MagicMock()
    r = Refiner(_cfg(), llm, MagicMock())
    out = r.propose(
        branch_diff="diff",
        devil_findings=[Finding("devil", "nit", "x", "y")],
    )
    assert out == {"patches": [], "commit_message": ""}
    llm.chat_json.assert_not_called()


def test_refiner_propose_calls_llm_with_findings_in_prompt():
    """Happy path — devil flagged a blocker, refiner builds the prompt
    and emits a patch. The prompt MUST include the finding text so the
    actor knows what to fix."""
    from gitoma.critic.refiner import Refiner

    llm = MagicMock()
    llm.chat_json = MagicMock(return_value={
        "patches": [{"action": "modify", "path": "src/main.rs", "content": "fn main() {}"}],
        "commit_message": "refine: restore fn main()",
    })
    r = Refiner(_cfg(), llm, MagicMock())
    findings = [
        Finding("devil", "blocker", "compile_breakage",
                "removed fn main()", file="src/main.rs", line_range=(56, 103)),
    ]
    out = r.propose(branch_diff="-fn main() {...}", devil_findings=findings)

    assert len(out["patches"]) == 1
    assert out["commit_message"].startswith("refine:")
    # Verify the finding text reached the user message
    _, kwargs = llm.chat_json.call_args
    messages = llm.chat_json.call_args.args[0]
    user_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    assert "removed fn main()" in user_text
    assert "src/main.rs" in user_text


def test_refiner_crash_returns_empty_not_propagates():
    """LLM crashes during refinement (network error, OOM, etc.) → return
    empty patches, do NOT propagate. The worker is far past commit; a
    crash here would orphan the run for no reason."""
    from gitoma.critic.refiner import Refiner

    llm = MagicMock()
    llm.chat_json = MagicMock(side_effect=ConnectionError("LM Studio dead"))
    r = Refiner(_cfg(), llm, MagicMock())
    out = r.propose(
        branch_diff="diff",
        devil_findings=[Finding("devil", "blocker", "x", "y")],
    )
    assert out == {"patches": [], "commit_message": ""}


# ── MetaEval keep-if-better ──────────────────────────────────────────────────


def test_meta_eval_v0_kept_when_no_diff_change():
    """If v1 is identical to v0 (refinement produced no actual changes)
    or v1 is empty, keep v0 — short-circuit no LLM call needed."""
    from gitoma.critic.meta import MetaEval

    llm = MagicMock()
    llm.chat = MagicMock()
    m = MetaEval(_cfg(), llm, MagicMock())
    winner, _ = m.judge(v0_diff="diff a", v1_diff="diff a", devil_findings=[])
    assert winner == "v0"
    winner, _ = m.judge(v0_diff="diff a", v1_diff="", devil_findings=[])
    assert winner == "v0"
    llm.chat.assert_not_called()


def test_meta_eval_returns_v1_when_llm_says_so():
    """LLM emits valid JSON with winner=v1 → meta returns v1 with rationale."""
    from gitoma.critic.meta import MetaEval

    llm = MagicMock()
    llm.chat = MagicMock(return_value=json.dumps({
        "winner": "v1",
        "rationale": "fixed the blocker without introducing new issues",
    }))
    llm._last_usage = (200, 50)

    m = MetaEval(_cfg(), llm, MagicMock())
    winner, rationale = m.judge(
        v0_diff="diff a", v1_diff="diff b",
        devil_findings=[Finding("devil", "blocker", "x", "y")],
    )
    assert winner == "v1"
    assert "fixed the blocker" in rationale


def test_meta_eval_falls_back_to_v0_on_garbage_response():
    """Conservative default: any malformed LLM output → v0.
    Misclassifying a refinement as 'better' than it is is the dangerous
    failure mode; misclassifying a real improvement as not-better just
    means we ship v0 (which we'd ship anyway without iter-4)."""
    from gitoma.critic.meta import MetaEval

    llm = MagicMock()
    llm.chat = MagicMock(return_value="not json at all")
    m = MetaEval(_cfg(), llm, MagicMock())
    winner, rationale = m.judge(
        v0_diff="a", v1_diff="b", devil_findings=[],
    )
    assert winner == "v0"
    assert "no_json_block" in rationale


def test_meta_eval_falls_back_to_v0_on_llm_crash():
    """Same conservative default for LLM call failures (network, etc.)."""
    from gitoma.critic.meta import MetaEval

    llm = MagicMock()
    llm.chat = MagicMock(side_effect=ConnectionError("nope"))
    m = MetaEval(_cfg(), llm, MagicMock())
    winner, rationale = m.judge(
        v0_diff="a", v1_diff="b", devil_findings=[],
    )
    assert winner == "v0"
    assert "meta_eval_failed" in rationale


def test_meta_eval_unknown_winner_string_falls_back_to_v0():
    """LLM returns valid JSON with winner='maybe' or some other invented
    string → v0 (safer than guessing intent)."""
    from gitoma.critic.meta import MetaEval

    llm = MagicMock()
    llm.chat = MagicMock(return_value=json.dumps({
        "winner": "maybe", "rationale": "i can't decide",
    }))
    m = MetaEval(_cfg(), llm, MagicMock())
    winner, rationale = m.judge(
        v0_diff="a", v1_diff="b", devil_findings=[],
    )
    assert winner == "v0"
    assert "unknown_winner" in rationale


def test_devil_advocate_separate_endpoint_builds_secondary_client():
    """When devil_base_url differs from the worker's, the devil builds a
    second LLMClient pointing at that URL. The primary client is
    untouched (worker keeps using it for patches)."""
    from gitoma.critic.devil import DevilsAdvocate
    from unittest.mock import patch as patchmock

    primary_llm = MagicMock()
    primary_llm.chat = MagicMock()
    full_cfg = MagicMock()
    full_cfg.lmstudio.base_url = "http://localhost:1234/v1"
    cp_cfg = _cfg(
        devil_advocate=True,
        devil_base_url="http://100.108.97.78:8000/v1",
        devil_model="some-model",
    )
    devil = DevilsAdvocate(cp_cfg, primary_llm, full_cfg)

    # Patch the LLMClient constructor used inside _llm_for_devil.
    with patchmock("gitoma.planner.llm_client.LLMClient") as MockClient:
        secondary_instance = MagicMock()
        secondary_instance.chat = MagicMock(return_value='{"findings": []}')
        secondary_instance._last_usage = None
        MockClient.return_value = secondary_instance

        devil.review(full_branch_diff="diff x")

        # The constructor was called once with a config whose lmstudio
        # base_url matches devil_base_url.
        assert MockClient.call_count == 1
        ctor_arg = MockClient.call_args.args[0]
        assert ctor_arg.lmstudio.base_url == "http://100.108.97.78:8000/v1"
        # Primary client was NOT touched
        primary_llm.chat.assert_not_called()
