"""Meta-eval — does the refinement actually improve the PR? (iter 4)

After the Refiner adds its v1 commit, we have two diffs:
  v0 = base..HEAD-before-refine (the original work)
  v1 = base..HEAD-after-refine (original + refinement commit)

The meta-eval compares them and decides whether to keep v1 or revert.
Default conservative: on tie or uncertainty, KEEP v0 (revert the refine
commit). This protects against the "critic too zealous" failure mode
where the actor obediently rewrites things that were fine.

Decision model: same actor LLM (or the devil model when we want stronger
judgement). Output is deliberately constrained to {"winner": "v0"|"v1"|"tie",
"rationale": str}. Tie → keep v0.

If the meta-eval crashes or returns malformed output, we KEEP v0 (revert
v1) — the safer default. The trace records the failure.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from gitoma.core.trace import current as current_trace
from gitoma.critic.types import LLMMetaVerdict, ValidationError

if TYPE_CHECKING:
    from gitoma.core.config import Config, CriticPanelConfig
    from gitoma.critic.types import Finding
    from gitoma.planner.llm_client import LLMClient


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


META_PROMPT = """\
You are an impartial senior reviewer comparing two versions of the same
Pull Request:

  * v0 — the original branch the agent produced
  * v1 — the same branch with one additional "refinement" commit on top,
    intended to address devil's-advocate findings

Decide whether v1 is GENUINELY better than v0, considering:
  * Did the refinement actually fix the flagged blocker/major issues?
  * Did the refinement introduce NEW problems (slop, dead code, scope
    creep, regressions in untouched areas)?
  * Is the refinement commit cohesive with the original work, or does
    it feel like a separate concern bolted on?

Bias toward keeping v0 unless v1 is clearly better. Half-finished
refinements that fix one issue and break another should LOSE — better
to ship v0 with known issues than v1 with unknown new issues.

Output strictly a JSON object on a single block, nothing before or
after:

{
  "winner": "v0" | "v1" | "tie",
  "rationale": "one sentence on what tipped the decision"
}

"tie" is treated as a v0 win by the runner — explicit conservative
default. So choose "v1" only when you're confident.
"""


class MetaEval:
    """Compares v0 vs v1 and emits a winner verdict."""

    def __init__(
        self,
        critic_config: "CriticPanelConfig",
        primary_llm: "LLMClient",
        full_config: "Config",
    ) -> None:
        self._cfg = critic_config
        self._llm = primary_llm
        self._full_config = full_config
        self._judge_llm: "LLMClient | None" = None

    def _llm_for_judge(self) -> "LLMClient":
        """Client for the meta-eval call.

        When ``devil_base_url`` is set the judge uses the devil's endpoint
        (same model family that produced the findings → coherent worldview,
        and the primary LM Studio may not even have ``devil_model`` loaded,
        producing a 400 at call time). Otherwise falls through to the
        primary client with a ``model=devil_model`` kwarg at chat() time.
        """
        if not self._cfg.devil_base_url:
            return self._llm
        if self._judge_llm is None:
            from copy import deepcopy
            from gitoma.planner.llm_client import LLMClient

            sub_cfg = deepcopy(self._full_config)
            sub_cfg.lmstudio.base_url = self._cfg.devil_base_url
            self._judge_llm = LLMClient(sub_cfg)
        return self._judge_llm

    def judge(
        self,
        *,
        v0_diff: str,
        v1_diff: str,
        devil_findings: list["Finding"],
    ) -> tuple[Literal["v0", "v1", "tie"], str]:
        """Return (winner, rationale).

        Conservative defaults:
          * empty diffs → "v0" (nothing to compare)
          * LLM crash → "v0" (rationale="meta_eval_failed")
          * malformed JSON → "v0"
          * tie → returned as "tie" (caller treats as v0)
        """
        if not v1_diff.strip() or v0_diff == v1_diff:
            return "v0", "no refinement diff"

        # Use devil_model if configured (the deeper reviewer also makes a
        # better judge); otherwise fall back to the worker model.
        model_override = self._cfg.devil_model or None
        # Use a slightly higher temperature than the panel — judging is
        # a comparative task that benefits from a touch of latitude.
        temperature = self._cfg.devil_temperature

        findings_summary = "\n".join(
            f"- ({f.severity}) {f.category}: {f.summary[:120]}"
            for f in devil_findings
            if f.severity in ("blocker", "major")
        )

        user_msg = (
            "Devil's-advocate findings on v0 (what v1 is meant to fix):\n"
            + (findings_summary or "(none)") + "\n\n"
            + "v0 diff (base..HEAD-before-refine):\n"
            + "```diff\n" + v0_diff.rstrip()[:8000] + "\n```\n\n"
            + "v1 diff (base..HEAD-after-refine, same as v0 + one more commit):\n"
            + "```diff\n" + v1_diff.rstrip()[:8000] + "\n```"
        )

        messages = [
            {"role": "system", "content": META_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        llm = self._llm_for_judge()
        try:
            try:
                raw = llm.chat(
                    messages, temperature=temperature, model=model_override,
                )
            except TypeError:
                raw = llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception("critic_meta_eval.call_failed", exc)
            return "v0", f"meta_eval_failed: {type(exc).__name__}"

        winner, rationale = _parse_verdict(raw)
        return winner, rationale


def _parse_verdict(raw: str) -> tuple[Literal["v0", "v1", "tie"], str]:
    """Parse the meta-eval's raw output via STRICT Pydantic validation.

    Conservative on malformed: returns ('v0', '<reason>') for ANY
    failure mode so we never accidentally keep a refinement we
    couldn't validate. Possible reasons:
      * meta_eval_empty_response — empty raw
      * meta_eval_no_json_block — no balanced ``{...}`` found
      * meta_eval_invalid_schema — Pydantic validation failed
        (e.g. winner is not in the Literal set, extra fields present)

    ¬I axiom: a clean v0 fallback on EVERY failure is the conservative
    default the meta-eval contract promises. Schema drift cannot
    silently accept a refinement.
    """
    if not raw:
        return "v0", "meta_eval_empty_response"
    match = _JSON_BLOCK.search(raw)
    if not match:
        return "v0", "meta_eval_no_json_block"
    try:
        verdict = LLMMetaVerdict.model_validate_json(match.group(0))
    except (ValidationError, ValueError):
        return "v0", "meta_eval_invalid_schema"
    return verdict.winner, verdict.rationale  # type: ignore[return-value]
