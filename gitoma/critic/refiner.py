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

CRITICAL: ``content`` must be the COMPLETE FINAL file as it will be
written to disk — NOT a diff, NOT a hunk, NOT a delta. Include every
line that should exist in the file after the edit, exactly as it
should appear. The patcher does NOT understand ``@@ -X,Y +A,B @@``,
``+`` / ``-`` line prefixes, or any other unified-diff syntax. If you
emit a diff string in ``content``, the patch will be REJECTED.

WRONG (will be rejected):
{
  "action": "modify",
  "path": ".eslintrc.json",
  "content": "@@ -6,7 +6,8 @@\n   \"extends\": [\n-    \"x\"\n+    \"y\"\n   ]"
}

RIGHT (entire post-edit file content):
{
  "action": "modify",
  "path": ".eslintrc.json",
  "content": "{\n  \"extends\": [\n    \"y\",\n    \"prettier\"\n  ]\n}\n"
}

For "modify" actions you MUST receive the current file content first
to know what to keep — if you don't have it, prefer to skip that
finding rather than guess.

Output strictly a JSON object on a single block, nothing else:

{
  "patches": [
    {
      "action": "create" | "modify" | "delete",
      "path": "relative/path/to/file",
      "content": "<entire final file content; omit for delete>"
    }
  ],
  "commit_message": "refine: <one short sentence on what was fixed>"
}

If you cannot or should not refine (no actionable blockers in the
findings, or you'd need file contents you don't have), output
``{"patches": [], "commit_message": ""}``.
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
        flagged_files_content: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run one refinement turn. Returns a dict with ``patches`` (list)
        and ``commit_message`` (str). Empty patches list = "no actionable
        refinement, keep v0".

        ``flagged_files_content`` maps relative path → current content
        for the files referenced in the devil's findings. WITHOUT these,
        the model is asked to ``modify`` files it has never seen — and
        small models hallucinate content (or, observed live, emit unified
        diffs into the ``content`` field). Even an empty dict is better
        than None: it tells the prompt template "context omitted on
        purpose, prefer to skip rather than guess".

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
        files_block = _format_flagged_files(flagged_files_content or {})

        user_msg = (
            (f"Repository file context:\n{repo_files_summary}\n\n" if repo_files_summary else "")
            + f"Devil's-advocate findings to address ({len(triggers)}):\n{findings_block}\n\n"
            + (files_block + "\n\n" if files_block else "")
            + "Current full branch diff (your previous work, for orientation only — DO NOT echo back as patch content):\n"
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


# Per-file content payload cap — keeps the prompt manageable on small
# models (4-9B with practical 8-32K usable context). Files larger than
# this are truncated with a clear marker; the model is told the file
# was truncated so it can decide to skip rather than fabricate.
_MAX_FILE_PAYLOAD_CHARS = 12_000


def _format_flagged_files(content_by_path: dict[str, str]) -> str:
    """Render the current content of files mentioned in the findings.

    Each file gets a labelled fence with the language inferred from the
    extension. Large files are truncated explicitly so the model never
    silently sees a partial file."""
    if not content_by_path:
        return ""
    parts: list[str] = ["Current content of files referenced in findings (for ``modify`` patches, this is what you must transform):"]
    for path, content in sorted(content_by_path.items()):
        lang = _lang_for_path(path)
        body = content
        if len(body) > _MAX_FILE_PAYLOAD_CHARS:
            half = _MAX_FILE_PAYLOAD_CHARS // 2
            body = (
                body[:half]
                + f"\n\n... [TRUNCATED {len(content) - _MAX_FILE_PAYLOAD_CHARS} chars; do NOT modify this file unless absolutely necessary] ...\n\n"
                + body[-half:]
            )
        parts.append(f"### `{path}`\n```{lang}\n{body}\n```")
    return "\n\n".join(parts)


def _lang_for_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python", "js": "javascript", "ts": "typescript",
        "rs": "rust", "go": "go", "rb": "ruby", "java": "java",
        "kt": "kotlin", "swift": "swift", "c": "c", "cpp": "cpp",
        "h": "c", "hpp": "cpp", "cs": "csharp", "sh": "bash",
        "yaml": "yaml", "yml": "yaml", "json": "json", "toml": "toml",
        "md": "markdown", "html": "html", "css": "css",
    }.get(ext, "")
