"""LLM self-critic: the agent reviews its own PR before anyone else does.

This closes the autonomy gap where gitoma opened a PR and then waited
indefinitely for a human (or Copilot, if configured) to review. A
"Critic" variant of the same LLM reads the diff in adversarial mode,
classifies findings by severity, and posts a single summary comment on
the PR so the maintainer has something concrete to read on arrival.

Design notes
------------
* The critic runs in-process right after Phase 4, NOT as a separate
  async job — the run command is the only writer for the state file
  during its lifetime, and we want findings persisted alongside the
  state (see `state.current_operation`) so the cockpit shows progress.
* Output is a SINGLE issue comment, not inline review comments — the
  latter requires per-line diff coordinates that PyGithub makes
  annoying to plumb and LLMs routinely get wrong. A summary comment
  is also easier for the maintainer to scan.
* If the LLM response isn't parseable JSON, we treat that as "critic
  produced nothing useful" rather than crashing — the PR stays open,
  the caller gets an empty list.
* Every external call (LLM, GitHub API, JSON parse) is traced via
  `gitoma.core.trace.current()` so `gitoma logs <url>` tells you
  exactly where the pass went sideways.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from gitoma.core.config import Config
from gitoma.core.github_client import GitHubClient
from gitoma.core.trace import current
from gitoma.planner.llm_client import LLMClient, LLMError

_DIFF_CHAR_BUDGET = 40_000           # keep prompt within ~10-15k tokens
_MAX_FINDINGS = 30                   # sanity cap on model output


@dataclass
class Finding:
    """A single observation posted to the PR."""

    severity: str   # "blocker" | "major" | "minor" | "nit"
    file: str | None
    line: int | None
    title: str
    detail: str

    def rank(self) -> int:
        return {"blocker": 0, "major": 1, "minor": 2, "nit": 3}.get(self.severity, 4)


@dataclass
class SelfReviewResult:
    """What the critic pass produced. All findings + a reason if skipped."""

    findings: list[Finding]
    comment_posted: bool
    skipped_reason: str | None = None
    raw_response: str = ""


_SEVERITY_EMOJI = {"blocker": "🛑", "major": "⚠️", "minor": "💭", "nit": "✨"}


class SelfCriticAgent:
    """Run an adversarial-critic LLM pass over a PR diff and post findings."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.llm = LLMClient(config)
        self.gh = GitHubClient(config)

    # ── Public API ────────────────────────────────────────────────────────

    def review_pr(self, owner: str, repo: str, pr_number: int) -> SelfReviewResult:
        tr = current()
        with tr.span("self_critic.review", owner=owner, repo=repo, pr=pr_number) as span:
            try:
                pr = self.gh.get_repo(owner, repo).get_pull(pr_number)
            except Exception as exc:
                tr.exception("self_critic.fetch_pr.error", exc)
                return SelfReviewResult(findings=[], comment_posted=False,
                                        skipped_reason=f"fetch pr: {exc}")

            diff = self._collect_diff(pr)
            if not diff.strip():
                return SelfReviewResult(findings=[], comment_posted=False,
                                        skipped_reason="empty diff")

            # Ask the LLM to critique.
            try:
                raw = self._ask_llm(pr.title, pr.body or "", diff)
            except LLMError as exc:
                tr.exception("self_critic.llm.error", exc)
                return SelfReviewResult(findings=[], comment_posted=False,
                                        skipped_reason=f"llm: {exc}")

            findings = parse_findings(raw)
            span["findings"] = len(findings)

            # Post summary comment — but only if the critic actually found
            # something. Posting "LGTM!" on every PR would be noise.
            posted = False
            if findings:
                body = render_comment_body(findings)
                try:
                    issue = self.gh.get_repo(owner, repo).get_issue(pr_number)
                    issue.create_comment(body)
                    posted = True
                except Exception as exc:
                    tr.exception("self_critic.post_comment.error", exc)

            return SelfReviewResult(
                findings=findings,
                comment_posted=posted,
                raw_response=raw if not findings else "",  # only retain if nothing parsed
            )

    # ── Internals ─────────────────────────────────────────────────────────

    def _collect_diff(self, pr: Any) -> str:
        """Concatenate per-file unified patches up to the char budget."""
        pieces: list[str] = []
        total = 0
        for f in pr.get_files():
            patch = getattr(f, "patch", None) or ""
            if not patch:
                continue
            header = f"### {f.filename}\n```diff\n"
            footer = "\n```"
            block = header + patch + footer
            if total + len(block) > _DIFF_CHAR_BUDGET:
                pieces.append(
                    f"\n\n_(diff truncated — budget {_DIFF_CHAR_BUDGET} chars)_"
                )
                break
            pieces.append(block)
            total += len(block)
        return "\n\n".join(pieces)

    def _ask_llm(self, title: str, body: str, diff: str) -> str:
        """Single-shot adversarial-critic prompt.

        Uses ``LM_STUDIO_SELFREVIEW_MAX_TOKENS`` (default 8192) instead
        of the global ``LM_STUDIO_MAX_TOKENS`` (which defaults to 4096
        for the worker patches). Caught live 2026-04-30 EVE on PR #12:
        the review prompt is full-PR-diff sized and 4096 tokens
        truncated the response, leaving "Self-review skipped". The
        env knob lets ops bump just this phase without inflating the
        worker budget. Clamped to a sane range to surface stuck calls.
        """
        import os as _sr_os
        try:
            _sr_max = int(
                _sr_os.environ.get("LM_STUDIO_SELFREVIEW_MAX_TOKENS") or "8192"
            )
        except ValueError:
            _sr_max = 8192
        _sr_max = max(1024, min(32768, _sr_max))
        prompt = _CRITIC_PROMPT.format(
            title=title or "(no title)",
            body=body or "(none)",
            diff=diff,
        )
        current().emit(
            "self_critic.llm.request",
            model=self.config.lmstudio.model,
            prompt_chars=len(prompt),
            max_tokens=_sr_max,
        )
        response = self.llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=_sr_max,
        )
        current().emit(
            "self_critic.llm.response",
            response_chars=len(response),
        )
        return response


_CRITIC_PROMPT = """\
You are an adversarial code reviewer. Your job is to find *real* problems in
the pull request below — not to congratulate it. Be honest, specific, and
constructive. If the PR is actually clean, return an empty array — noise is
worse than silence.

## PR Title
{title}

## PR Description
{body}

## Diff
{diff}

## What to look for

- **blocker**: will break things, introduces bugs, contradicts its stated
  scope, uses APIs incorrectly, missing required cleanup
- **major**: significant concerns (missing edge cases, wrong abstraction,
  security risks, test-coverage gaps on new behaviour)
- **minor**: real issues but non-blocking (inefficient code, style drift,
  unclear naming, dead code)
- **nit**: subjective suggestions — use sparingly

## Output format

Respond with ONLY a JSON array of findings. No prose before or after.

[
  {{
    "severity": "blocker" | "major" | "minor" | "nit",
    "file": "path/to/file.ext",      // or null if not file-specific
    "line": 42,                       // or null
    "title": "Short summary under 80 chars",
    "detail": "Concrete explanation, including what would go wrong and how to fix"
  }}
]

If you find nothing worth saying, return exactly:
[]
"""


# ── Pure parsers (unit-testable without a network or an LLM) ──────────────


def parse_findings(raw: str) -> list[Finding]:
    """Extract findings from whatever the LLM returned.

    Three fallbacks:
      1. Strip markdown fences, parse as JSON array.
      2. Regex out the first ``[ … ]`` block and parse.
      3. Give up — return empty. The critic didn't produce usable JSON,
         treat it as "no findings" rather than crashing the pipeline.
    """
    if not raw or not raw.strip():
        return []

    candidates = [
        raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip(),
    ]
    match = re.search(r"\[\s*[\s\S]*\]", raw)
    if match:
        candidates.append(match.group())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        return [_coerce_finding(item) for item in data[:_MAX_FINDINGS] if isinstance(item, dict)]

    return []


def _coerce_finding(item: dict[str, Any]) -> Finding:
    severity = str(item.get("severity", "minor")).lower().strip()
    if severity not in ("blocker", "major", "minor", "nit"):
        severity = "minor"
    line = item.get("line")
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
    return Finding(
        severity=severity,
        file=(str(item["file"]) if item.get("file") else None),
        line=line,
        title=str(item.get("title", "unnamed finding"))[:200],
        detail=str(item.get("detail", ""))[:2000],
    )


def render_comment_body(findings: list[Finding]) -> str:
    """Format a list of findings into a single PR comment body."""
    if not findings:
        return "🤖 **gitoma self-review**\n\n_No issues found._"

    by_sev: dict[str, list[Finding]] = {"blocker": [], "major": [], "minor": [], "nit": []}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    counts = ", ".join(
        f"{len(by_sev[s])} {s}" for s in ("blocker", "major", "minor", "nit") if by_sev[s]
    )
    lines: list[str] = [
        "🤖 **gitoma self-review**",
        "",
        f"Automated critic pass found **{len(findings)} finding(s)**: {counts}.",
        "",
        "_This is a best-effort signal — review, don't rubber-stamp._",
        "",
    ]

    for severity in ("blocker", "major", "minor", "nit"):
        bucket = by_sev[severity]
        if not bucket:
            continue
        emoji = _SEVERITY_EMOJI.get(severity, "")
        lines.append(f"### {emoji} {severity.title()} ({len(bucket)})")
        lines.append("")
        for f in bucket:
            loc = ""
            if f.file:
                loc = f" — `{f.file}`"
                if f.line is not None:
                    loc += f":{f.line}"
            lines.append(f"- **{f.title}**{loc}")
            if f.detail:
                for dline in f.detail.splitlines():
                    lines.append(f"  > {dline}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
