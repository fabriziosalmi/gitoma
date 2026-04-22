"""Refinement turn — one-shot actor response to devil's-advocate findings (iter 4).

When the devil flags blocker/major findings on the full branch diff, the
refiner gives the actor model EXACTLY ONE chance to address them with a
follow-up patch. No recursion, no second pass — design constraint that
keeps the loop bounded by construction.

The refiner's prompt frames the call as "your previous PR was reviewed
and these are the blockers — emit a patch that fixes them, no more, no
less". The output shape mirrors the worker's so the existing patcher +
committer pipeline can consume it without forks.

If the refinement adds a commit, the meta-eval (next module) decides
whether to keep it or revert. Default conservative: keep v0 on tie.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from gitoma.core.trace import current as current_trace
from gitoma.critic.types import Finding

if TYPE_CHECKING:
    from gitoma.core.config import Config, CriticPanelConfig
    from gitoma.planner.llm_client import LLMClient


# Severities that justify a refinement turn — anything below is
# cosmetic and not worth the LLM round-trip + extra commit. Keep
# this list narrow; expanding it pays N× tokens for marginal gain.
_REFINE_TRIGGER = {"blocker", "major"}


REFINER_PROMPT = """\
You are the same agent that just produced this Pull Request, but now
playing the role of "fix-up engineer". A devil's-advocate critic
reviewed your full branch diff and flagged these blocker/major issues
that the per-subtask panel missed.

Your task: generate ONE follow-up patch (set of file edits) that
addresses ALL the listed blocker/major issues — no more, no less.

Constraints:
  * Do not revert your previous work — only fix what's broken.
  * Do not introduce new features or new files unless directly needed
    to address a finding.
  * If a finding is "remove dependency X" or "restore section Y", do
    that exactly. Don't propose alternatives.
  * If you genuinely cannot address a finding without major rework,
    skip it (the meta-eval will catch the gap) — better to fix 3 of 4
    cleanly than 4 of 4 messily.

Output strictly a JSON object on a single block, nothing else:

{
  "patches": [
    {
      "action": "create" | "modify" | "delete",
      "path": "relative/path/to/file",
      "content": "full file content as a single string (omit for delete)"
    }
  ],
  "commit_message": "refine: <one short sentence on what was fixed>"
}

If you cannot or should not refine (no actionable blockers in the
findings), output ``{"patches": [], "commit_message": ""}``.
"""


class Refiner:
    """One-shot refinement caller. Stateless — keep one instance per run.

    Iteration 4 design: cap=1 turn, triggered only by blocker or major
    devil findings, output consumed by the existing patcher+committer.
    """

    def __init__(
        self,
        critic_config: "CriticPanelConfig",
        primary_llm: "LLMClient",
        full_config: "Config",
    ) -> None:
        self._cfg = critic_config
        self._llm = primary_llm
        self._full_config = full_config

    def should_refine(self, devil_findings: list[Finding]) -> bool:
        """True iff the devil's findings list contains at least one
        blocker or major. nit/minor doesn't justify a refinement turn."""
        return any(f.severity in _REFINE_TRIGGER for f in devil_findings)

    def propose(
        self,
        *,
        branch_diff: str,
        devil_findings: list[Finding],
        repo_files_summary: str = "",
    ) -> dict[str, Any]:
        """Run one refinement turn. Returns a dict with ``patches`` (list)
        and ``commit_message`` (str). Empty patches list = "no actionable
        refinement, keep v0".

        Crash-safe: any LLM/parse error returns ``{"patches": [], ...}``
        with the failure recorded in the trace, NOT raised.
        """
        triggers = [f for f in devil_findings if f.severity in _REFINE_TRIGGER]
        if not triggers:
            return {"patches": [], "commit_message": ""}

        # Use worker-class model by default — same as the agent that
        # produced v0. The refinement is "the same engineer fixing their
        # own PR", not a new contributor.
        # Future enhancement: dedicated CRITIC_PANEL_REFINER_MODEL config
        # if we want a different model for the fix-up step.
        findings_block = _format_findings_for_actor(triggers)

        user_msg = (
            (f"Repository file context:\n{repo_files_summary}\n\n" if repo_files_summary else "")
            + f"Devil's-advocate findings to address ({len(triggers)}):\n{findings_block}\n\n"
            + "Current full branch diff (your previous work):\n"
            + "```diff\n" + branch_diff.rstrip() + "\n```"
        )

        messages = [
            {"role": "system", "content": REFINER_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            parsed = self._llm.chat_json(messages)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception("critic_refiner.call_failed", exc)
            return {"patches": [], "commit_message": ""}

        # Tolerate small schema drift — empty patches is the safe default
        patches = parsed.get("patches") if isinstance(parsed.get("patches"), list) else []
        commit_message = parsed.get("commit_message") or "refine: address devil findings"
        return {"patches": patches, "commit_message": str(commit_message)[:200]}


def _format_findings_for_actor(findings: list[Finding]) -> str:
    """Render findings as a numbered block suitable for prompt injection.

    Format kept terse on purpose — small models follow short lists better
    than verbose markdown."""
    lines: list[str] = []
    for i, f in enumerate(findings, start=1):
        loc = ""
        if f.file:
            loc = f" [{f.file}"
            if f.line_range:
                loc += f":{f.line_range[0]}-{f.line_range[1]}"
            loc += "]"
        lines.append(f"{i}. ({f.severity}) {f.category}{loc}: {f.summary}")
    return "\n".join(lines)
