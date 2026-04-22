"""Devil's advocate critic — the broad-scope brutal pass (iter 3).

Distinct from the per-subtask multi-persona panel:
  * runs ONCE per run (not per subtask)
  * sees the FULL branch diff (base..HEAD) not just one patch
  * uses a DIFFERENT model when configured (typically a bigger one,
    e.g. qwen3.5-9b-sushi-coder-rl-mlx) on potentially a different
    endpoint (parallelism across machines)
  * prompt is explicitly brutal: "find at least one blocker or state
    nothing is broken — but argue it"

Outcome: a list of Findings, same shape as the panel's. The runner
then either logs them (advisory mode) or — in iter 4 with refinement
turn — uses them to drive a one-shot rewrite of the highest-severity
files. Iteration 3 only logs.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from gitoma.core.trace import current as current_trace
from gitoma.critic.panel import _DEBUG_RAW, _parse_findings
from gitoma.critic.types import Finding, PanelResult

if TYPE_CHECKING:
    from gitoma.core.config import Config, CriticPanelConfig
    from gitoma.planner.llm_client import LLMClient


# The devil's prompt is intentionally adversarial. Tells the model:
#   * skip the cosmetic stuff (the panel already covered it)
#   * focus on architectural / behavioural / supply-chain damage
#   * be willing to say "nothing real here" — but ARGUE it, don't
#     just emit empty findings
DEVIL_PROMPT = """\
You are a hostile, deeply experienced senior engineer reviewing a Pull
Request that an AI agent generated automatically. Your role is the
DEVIL'S ADVOCATE — the LAST gate before this PR opens against a real
repository.

A 3-persona panel has already reviewed each individual subtask diff
and emitted line-level findings. Your job is the OPPOSITE: zoom out,
look at the entire branch diff as ONE thing, and find what those
narrowly-scoped reviewers MISSED.

What you specifically look for (in priority order):
  1. BUILD-BREAKING changes hiding in the diff: removed entry-points
     (fn main / __init__ / index.* / app.tsx), missing imports,
     dependency added but not declared, dependency declared but not
     used in any new code.
  2. PROJECT-IDENTITY violations: a Rust project gaining React
     packages; a CLI gaining a web framework; a backend gaining
     frontend code with no path to render it. The agent often
     bolts on stack-shaped slop.
  3. SILENT REGRESSIONS: things removed from the diff that the
     surrounding code or docs still reference. README references a
     section that's gone. CONTRIBUTING claims a feature that was
     deleted. A config key disappears but env-loading still expects it.
  4. SCOPE EXPLOSION: a "small docs improvement" PR that also touches
     CI workflows, package.json runtime fields, and src/ entry points.
     Cohesion matters; this is a chance to call it out.
  5. TROJAN DEPENDENCIES: new packages added under devDependencies
     that don't relate to anything in the diff. New CI steps that
     download arbitrary code.

Output strictly a JSON object on a single block, nothing before or after:

{
  "findings": [
    {
      "severity": "blocker" | "major" | "minor" | "nit",
      "category": "short_slug_under_30_chars",
      "summary": "one sentence — what is broken or wrong, file/area if knowable",
      "file": "primary file or null",
      "line_range": [start, end] or null
    }
  ]
}

Empty ``"findings": []`` is acceptable ONLY if you can defend it. If you
emit empty, also include a ``"defense"`` field at the top of the JSON
explaining WHY this PR is safe end-to-end. Most non-trivial PRs have at
least one issue worth flagging.

DO NOT re-flag cosmetic things the panel already covered (verbose
comments, missing newlines, minor naming). Your value is the things
they missed because they only saw a slice.
"""


class DevilsAdvocate:
    """Single-shot broad-scope critic. One LLM call per run.

    Construction is lazy by the caller — only build the instance when
    ``config.critic_panel.devil_advocate`` is True AND the panel is enabled.
    """

    def __init__(
        self,
        critic_config: "CriticPanelConfig",
        primary_llm: "LLMClient",
        full_config: "Config",
    ) -> None:
        self._cfg = critic_config
        self._primary_llm = primary_llm
        self._full_config = full_config
        # Lazily constructed if devil_base_url is set — the secondary
        # client points at a different endpoint (e.g. another machine on
        # the tailnet). Building it now would force eager imports of the
        # OpenAI SDK on every config load; defer.
        self._devil_llm: "LLMClient | None" = None

    def review(self, *, full_branch_diff: str, branch_name: str = "") -> PanelResult:
        """Run the devil against the full branch diff.

        Returns a PanelResult with subtask_id="__devil__" so it slots into
        the same state log as panel results without confusion.
        """
        if not full_branch_diff.strip():
            return PanelResult(subtask_id="__devil__", verdict="no_op")

        llm = self._llm_for_devil()
        model_override = self._cfg.devil_model or None

        messages = [
            {"role": "system", "content": DEVIL_PROMPT},
            {
                "role": "user",
                "content": (
                    (f"Branch: {branch_name}\n\n" if branch_name else "")
                    + "Full branch diff (all subtasks combined, this is what the PR will look like):\n"
                    + "```diff\n" + full_branch_diff.rstrip() + "\n```"
                ),
            },
        ]

        try:
            try:
                raw = llm.chat(
                    messages,
                    temperature=self._cfg.devil_temperature,
                    model=model_override,
                )
            except TypeError:
                raw = llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception(
                "critic_devil.call_failed",
                exc,
            )
            return PanelResult(
                subtask_id="__devil__",
                verdict="advisory_logged",
                personas_called=["devil"],
                findings=[
                    Finding(
                        persona="devil",
                        severity="minor",
                        category="critic_call_failed",
                        summary=f"Devil's advocate call failed: {type(exc).__name__}: {exc}",
                    )
                ],
            )

        usage = getattr(llm, "_last_usage", None)

        if _DEBUG_RAW:
            try:
                current_trace().emit(
                    "critic_devil.raw",
                    raw_head=str(raw or "")[:1500],
                    raw_len=len(raw or ""),
                )
            except Exception:
                pass

        findings = _parse_findings(raw, persona="devil")
        return PanelResult(
            subtask_id="__devil__",
            verdict="advisory_logged",
            personas_called=["devil"],
            findings=findings,
            tokens_extra=usage,
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _llm_for_devil(self) -> "LLMClient":
        """Return the LLMClient to use for the devil's call.

        If ``devil_base_url`` is set, build a second client targeting that
        endpoint (independent of the worker's). Otherwise reuse the
        primary client and only swap the model via the ``model`` kwarg.
        """
        if not self._cfg.devil_base_url:
            return self._primary_llm
        if self._devil_llm is None:
            from copy import deepcopy
            from gitoma.planner.llm_client import LLMClient

            # Clone the full config and swap the LM Studio base_url so the
            # secondary client thinks that's its primary endpoint. The
            # devil_model override at chat()-call-time picks the right model.
            sub_cfg = deepcopy(self._full_config)
            sub_cfg.lmstudio.base_url = self._cfg.devil_base_url
            self._devil_llm = LLMClient(sub_cfg)
        return self._devil_llm
