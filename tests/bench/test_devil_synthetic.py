"""Synthetic devil's-advocate detection bench against the PR#12 golden fixture.

Why this exists
---------------
The live e2e runs (iter4/5/6) have shown a recurring pattern: the
devil emits 0-2 minor findings on a freshly-reset b2v repo, never
flagging blocker/major. This means the refiner+meta-eval downstream
chain never fires — the loop is structurally complete but functionally
inert.

But the b2v PR#12 diff is KNOWN to contain 4 real high-severity
regressions (per slop_audit_b2v_pr12.json):
  * F005 [blocker] src/main.rs: fn main() entirely removed (compile breaks)
  * F006 [blocker] package.json: React deps added to Rust+Jest project
  * F007 [major]   package.json: removed engines pin without justification
  * F008 [major]   README.md: removed License + Legal sections

If the devil — given THIS diff — still emits 0 blockers/majors, the
problem is the devil's calibration (model size, prompt severity, or
both), NOT the rest of the pipeline.

This bench bypasses the live worker and feeds the PR#12 diff straight
to DevilsAdvocate.review(). Reports per-finding severity + axiom +
which expected findings (F005-F008) the devil hit by file overlap.
Live-only (opt-in via -m antislop_live).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitoma.critic.devil import DevilsAdvocate


_PR12_DIFF_PATH = Path("/tmp/pr12_diff.txt")
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "slop_audit_b2v_pr12.json"

# Files the 4 expected blockers/majors live in. The devil is
# "successful" on a finding if it produces a finding whose ``file``
# matches one of these AND severity is blocker or major.
_EXPECTED_FILES = {
    "src/main.rs",
    "package.json",
    "README.md",
}


@pytest.mark.antislop_live
def test_devil_severity_calibration_on_pr12_diff():
    """Live: feed the PR#12 diff to the devil and report its
    blocker/major catch rate against the 4 known regressions.

    NOT a hard pass/fail on the result — that would be flaky on a
    stochastic model. Asserts only:
      * the devil ran, returned a valid PanelResult
      * the result is logged with per-finding severity + axiom
        + which expected files were hit
    The OPERATOR reads the table and decides whether to ship the
    devil with this model or to size up.

    The bench acts as the "no vibes" gate for any future devil
    upgrade (model swap, prompt change, axiom-organised vs
    free-form): the same PR#12 diff fed twice, compare the table.
    """
    base_url = os.environ.get("LM_STUDIO_BASE_URL")
    model = os.environ.get("LM_STUDIO_MODEL")
    if not base_url or not model:
        pytest.skip("LM_STUDIO_BASE_URL + LM_STUDIO_MODEL not set")
    if not _PR12_DIFF_PATH.is_file():
        pytest.skip(
            "PR#12 diff fixture missing at /tmp/pr12_diff.txt — fetch it via: "
            "curl -H 'Accept: application/vnd.github.v3.diff' -H 'Authorization: Bearer <token>' "
            "https://api.github.com/repos/fabriziosalmi/b2v/pulls/12 > /tmp/pr12_diff.txt"
        )

    diff_text = _PR12_DIFF_PATH.read_text(encoding="utf-8")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    # Build a thin LLMClient that hits the live endpoint at T=0.
    # Same shim as test_refiner_synthetic.py — minimal interface.
    from openai import OpenAI

    client = OpenAI(
        base_url=base_url,
        api_key=os.environ.get("LM_STUDIO_API_KEY", "lm-studio"),
    )

    class _LiveLLM:
        """Minimal LLMClient interface for DevilsAdvocate."""

        def __init__(self) -> None:
            self._last_usage: tuple[int, int] | None = None

        def chat(self, messages, retries: int = 3, *, temperature=None, model=None):
            # Disable thinking mode when DEVIL_NO_THINK=1 — Qwen3/Qwen3.5
            # have thinking ON by default which burns completion budget on
            # internal reasoning that doesn't end up in the JSON. Pass
            # ``chat_template_kwargs.enable_thinking: false`` to short-circuit.
            extra_body: dict = {}
            if os.environ.get("DEVIL_NO_THINK") == "1":
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
            resp = client.chat.completions.create(
                model=(model or os.environ["LM_STUDIO_MODEL"]),
                messages=messages,  # type: ignore[arg-type]
                temperature=(temperature if temperature is not None else 0),
                max_tokens=2048,
                extra_body=extra_body or None,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                pt = getattr(usage, "prompt_tokens", None)
                ct = getattr(usage, "completion_tokens", None)
                if pt is not None and ct is not None:
                    self._last_usage = (int(pt), int(ct))
            return resp.choices[0].message.content or ""

    cp_cfg = MagicMock()
    cp_cfg.devil_temperature = 0.4
    cp_cfg.devil_model = ""
    cp_cfg.devil_base_url = ""
    full_cfg = MagicMock()

    devil = DevilsAdvocate(cp_cfg, _LiveLLM(), full_cfg)

    # ── Run ────────────────────────────────────────────────────────────
    import time
    t0 = time.monotonic()
    result = devil.review(
        full_branch_diff=diff_text,
        branch_name="gitoma/improve-pr12-replay",
    )
    elapsed = time.monotonic() - t0

    # ── Report ─────────────────────────────────────────────────────────
    print(f"\n=== DEVIL SEVERITY BENCH on PR#12 diff (model: {model}) ===")
    print(f"  elapsed: {elapsed:.1f}s")
    if result.tokens_extra:
        print(f"  tokens:  prompt={result.tokens_extra[0]} completion={result.tokens_extra[1]}")
    print(f"  verdict: {result.verdict}  findings_count: {len(result.findings)}  "
          f"has_blocker: {result.has_blocker()}")

    # Helper: normalise the path the model reports. Some models echo
    # back the git-diff "a/path" / "b/path" prefix; some prepend ./;
    # operators care about whether the path POINTS at the right file,
    # not the formatting accident. Without this normalisation the
    # bench reported false misses on findings that did hit the right
    # file (caught live on the gemma-4-heretic run that prefixed
    # everything with "a/").
    def _norm(p: str | None) -> str | None:
        if not p:
            return p
        s = p.lstrip("./").strip()
        if s.startswith(("a/", "b/")):
            s = s[2:]
        return s

    # Per-finding table
    print(f"\n  {'severity':<8} {'axiom':<5} {'file':<40} summary")
    for f in result.findings:
        norm_file = _norm(f.file)
        marker = "★" if f.severity in ("blocker", "major") and norm_file in _EXPECTED_FILES else " "
        sev = (f.severity or "?")[:8]
        ax = (f.axiom or "?")[:5]
        fn = (norm_file or "(none)")[:40]
        print(f"  {marker} {sev:<8} {ax:<5} {fn:<40} {f.summary[:80]}")

    # Coverage of the 4 expected high-severity findings
    expected_findings = expected["expected_findings_iter3_must_surface"]
    print(f"\n  expected high-sev findings on this diff: {len(expected_findings)}")
    hits_by_file: dict[str, list[str]] = {}
    for f in result.findings:
        norm_file = _norm(f.file)
        if f.severity in ("blocker", "major") and norm_file in _EXPECTED_FILES:
            hits_by_file.setdefault(norm_file, []).append(f"{f.severity}: {f.category}")
    for ef in expected_findings:
        f_path = ef["file"]
        hit = hits_by_file.get(f_path, [])
        marker = "✓" if hit else "✗"
        print(f"  {marker} {ef['id']} [{ef['severity']}] {f_path}: {hit or '— MISSED'}")

    catch_rate = sum(1 for ef in expected_findings if ef["file"] in hits_by_file) / len(expected_findings)
    print(f"\n  CATCH RATE (file-overlap, severity ≥ major): {catch_rate*100:.0f}%  "
          f"({len(hits_by_file)}/{len(expected_findings)} files hit)")

    # Axiom profile
    profile = result.axiom_profile()
    print(f"  axiom profile: {profile}")

    # Soft assertion: the devil must AT LEAST run and return SOMETHING.
    # The catch rate is the operator-facing signal, not a hard gate
    # (we'll add a hard gate once we have baseline + improvement data).
    assert result.verdict != "no_op", "devil short-circuited — diff was empty?"
    assert isinstance(result.findings, list)
