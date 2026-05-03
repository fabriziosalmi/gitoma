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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    # Ensemble metadata (2026-05-02). Empty when solo reviewer path
    # was taken. ``per_member_findings`` is parallel to ``ensemble_models``.
    ensemble_models: list[str] = field(default_factory=list)
    ensemble_min_agree: int = 0
    per_member_findings: list[list[Finding]] = field(default_factory=list)


_SEVERITY_EMOJI = {"blocker": "🛑", "major": "⚠️", "minor": "💭", "nit": "✨"}


class SelfCriticAgent:
    """Run an adversarial-critic LLM pass over a PR diff and post findings."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # Reviewer-route resolution — three modes (precedence):
        #   1. ENSEMBLE (2026-05-02): when both ``review_base_urls`` and
        #      ``review_models`` are set with matching length ≥ 2,
        #      build N reviewer clients and fan out PHASE 5 in parallel.
        #      ``self.llms`` is the list; ``self.llm`` aliases the first
        #      member for legacy callers.
        #   2. SOLO 3-way (2026-05-01): when ``review_base_url`` or
        #      ``review_model`` is set, build one reviewer-routed
        #      client (out-of-family second opinion).
        #   3. PLANNER fallback: same client as the planner.
        # Calls ``LLMClient(config, role=…)`` / overrides directly
        # (not the ``for_*`` factories) so test fixtures patching
        # ``self_critic.LLMClient`` catch every branch.
        _lm = config.lmstudio
        is_ensemble = bool(
            getattr(_lm, "is_review_ensemble", lambda: False)()
        )
        if is_ensemble:
            urls = _lm.parsed_review_base_urls()
            models = _lm.parsed_review_models()
            self.llms: list[LLMClient] = [
                LLMClient(
                    config,
                    role="reviewer",
                    base_url_override=u,
                    model_override=m,
                )
                for u, m in zip(urls, models)
            ]
            self.min_agree = max(2, int(getattr(_lm, "review_ensemble_min_agree", 2)))
            # Cap min_agree at len(self.llms) so a misconfig like
            # MIN_AGREE=5 with N=3 reviewers degrades to "all must
            # agree" instead of "nothing ever passes".
            if self.min_agree > len(self.llms):
                self.min_agree = len(self.llms)
            self.llm = self.llms[0]  # legacy alias
        elif (getattr(_lm, "review_base_url", "") or "") or (
            getattr(_lm, "review_model", "") or ""
        ):
            self.llm = LLMClient(config, role="reviewer")
            self.llms = [self.llm]
            self.min_agree = 1
        else:
            self.llm = LLMClient(config)
            self.llms = [self.llm]
            self.min_agree = 1
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

            is_ensemble = len(self.llms) >= 2
            ensemble_models: list[str] = []
            per_member: list[list[Finding]] = []
            raw_first = ""

            if is_ensemble:
                # Parallel fan-out across reviewers. Each member is a
                # distinct (endpoint, model) pair so ThreadPoolExecutor
                # is the right shape — no shared state, network-bound.
                tr.emit(
                    "self_critic.ensemble.start",
                    members=len(self.llms),
                    min_agree=self.min_agree,
                    models=[c.model for c in self.llms],
                )
                ensemble_models = [c.model for c in self.llms]
                results: dict[int, tuple[str, list[Finding], str | None]] = {}
                with ThreadPoolExecutor(max_workers=len(self.llms)) as ex:
                    futs = {
                        ex.submit(self._ask_llm_with, c, pr.title, pr.body or "", diff): i
                        for i, c in enumerate(self.llms)
                    }
                    for fut in as_completed(futs):
                        i = futs[fut]
                        c = self.llms[i]
                        try:
                            raw_i = fut.result()
                            f_i = parse_findings(raw_i)
                            results[i] = (raw_i, f_i, None)
                            tr.emit(
                                "self_critic.ensemble.member",
                                index=i,
                                model=c.model,
                                findings=len(f_i),
                            )
                        except LLMError as exc:
                            tr.exception("self_critic.ensemble.member.error", exc)
                            results[i] = ("", [], f"member {c.model}: {exc}")

                # Stable ordering by member index — important for diary
                # + tests + per_member_findings parity with ensemble_models.
                per_member = [results.get(i, ("", [], None))[1] for i in range(len(self.llms))]
                # If EVERY member errored, treat as fatal skip so
                # operators see the failure instead of an empty pass.
                err_count = sum(1 for i in range(len(self.llms)) if results.get(i, ("", [], None))[2])
                if err_count == len(self.llms):
                    first_err = next(
                        (results[i][2] for i in range(len(self.llms)) if results.get(i, ("", [], None))[2]),
                        "all members failed",
                    )
                    return SelfReviewResult(
                        findings=[], comment_posted=False,
                        skipped_reason=f"llm: {first_err}",
                        ensemble_models=ensemble_models,
                        ensemble_min_agree=self.min_agree,
                        per_member_findings=per_member,
                    )
                # Operator-tunable Jaccard floor for title similarity.
                # Default 0.4 — see ``_FUZZY_DEFAULT`` for the rationale.
                # Validated on b2v PR #34 (2026-05-02 live-fire).
                import os as _fz_os
                try:
                    _fz_threshold = float(
                        _fz_os.environ.get("GITOMA_REVIEW_FUZZY_THRESHOLD")
                        or _FUZZY_DEFAULT
                    )
                except ValueError:
                    _fz_threshold = _FUZZY_DEFAULT
                _fz_threshold = max(0.0, min(1.0, _fz_threshold))
                findings = merge_ensemble_findings(
                    per_member, self.min_agree,
                    fuzzy_threshold=_fz_threshold,
                )
                raw_first = results.get(0, ("", [], None))[0]
                _unique = {
                    _fingerprint(f)
                    for member in per_member
                    for f in member
                }
                tr.emit(
                    "self_critic.ensemble.merged",
                    kept=len(findings),
                    unique=len(_unique),
                    raw_total=sum(len(p) for p in per_member),
                    min_agree=self.min_agree,
                )
            else:
                # Solo path — preserves original single-call shape exactly.
                try:
                    raw_first = self._ask_llm(pr.title, pr.body or "", diff)
                except LLMError as exc:
                    tr.exception("self_critic.llm.error", exc)
                    return SelfReviewResult(findings=[], comment_posted=False,
                                            skipped_reason=f"llm: {exc}")
                findings = parse_findings(raw_first)

            span["findings"] = len(findings)

            # Post summary comment — but only if the critic actually found
            # something. Posting "LGTM!" on every PR would be noise.
            posted = False
            if findings:
                body = render_comment_body(
                    findings,
                    ensemble_models=ensemble_models,
                    min_agree=self.min_agree if is_ensemble else 0,
                )
                try:
                    issue = self.gh.get_repo(owner, repo).get_issue(pr_number)
                    issue.create_comment(body)
                    posted = True
                except Exception as exc:
                    tr.exception("self_critic.post_comment.error", exc)

            return SelfReviewResult(
                findings=findings,
                comment_posted=posted,
                raw_response=raw_first if not findings else "",
                ensemble_models=ensemble_models,
                ensemble_min_agree=self.min_agree if is_ensemble else 0,
                per_member_findings=per_member,
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
        """Single-shot adversarial-critic prompt against ``self.llm``.

        Solo-path entry point. Ensemble path uses ``_ask_llm_with``
        directly with each member client. Both share the prompt build
        + max_tokens resolution + trace emit shape.

        Uses ``LM_STUDIO_SELFREVIEW_MAX_TOKENS`` (default 8192) instead
        of the global ``LM_STUDIO_MAX_TOKENS`` (which defaults to 4096
        for the worker patches). Caught live 2026-04-30 EVE on PR #12:
        the review prompt is full-PR-diff sized and 4096 tokens
        truncated the response, leaving "Self-review skipped". The
        env knob lets ops bump just this phase without inflating the
        worker budget. Clamped to a sane range to surface stuck calls.
        """
        return self._ask_llm_with(self.llm, title, body, diff)

    def _ask_llm_with(self, client: LLMClient, title: str, body: str, diff: str) -> str:
        """Run the critic prompt through a specific reviewer client.

        Pulled out so ensemble members can share the prompt build +
        max_tokens resolution + trace emit shape with the solo path.
        Each call emits its own ``self_critic.llm.request`` /
        ``self_critic.llm.response`` events tagged with the member's
        model — so ``gitoma logs <url>`` shows per-member latency.
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
            model=getattr(client, "model", self.config.lmstudio.model),
            prompt_chars=len(prompt),
            max_tokens=_sr_max,
        )
        response = client.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=_sr_max,
        )
        current().emit(
            "self_critic.llm.response",
            model=getattr(client, "model", self.config.lmstudio.model),
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


def _fingerprint(f: Finding) -> tuple[str | None, int, str]:
    """Coarse bucket key for ensemble metrics (2026-05-02).

    Tuple of ``(file, line // 5, normalised_title_prefix)``. Used by
    trace events as a "how many distinct shapes did the ensemble
    report" metric and by tests, but NO LONGER load-bearing in the
    merge logic — that uses similarity clustering (see
    ``_findings_match``) since hash-equality on title prefixes was
    too stringent for LLM-natural paraphrasing.

    Caught live 2026-05-02 on b2v PR #34: three reviewers each
    flagged the same wrong cargo command at ``performance.md:28``
    but with titles "Incorrect cargo command in documentation" vs
    "Incorrect cargo command in performance guide" — the trailing
    words diverged and the prefix-hash dropped them into separate
    buckets, yielding 0 consensus on a clearly-shared defect.

    Severity is intentionally NOT in the fingerprint — same defect
    flagged at ``major`` vs ``minor`` IS agreement, and the merger
    keeps the most-severe vote.
    """
    norm = re.sub(r"\s+", " ", (f.title or "").lower()).strip()[:60]
    bucket = (f.line // 5) if isinstance(f.line, int) else -1
    return (f.file, bucket, norm)


# Title tokens shorter than this are treated as noise (English
# function words like ``in``/``of``/``to`` are all 2 chars; ``the``
# slips in at 3 but contributes harmlessly to both numerator and
# denominator). Tuned empirically — 3 keeps the signal-to-noise
# ratio in Jaccard high enough that paraphrases of the same defect
# clear 0.4 while genuinely-different defects on the same line stay
# below it.
_TITLE_TOKEN_MIN_LEN = 3
# Default Jaccard threshold for "two titles describe the same
# defect". Operator-tunable via ``GITOMA_REVIEW_FUZZY_THRESHOLD``;
# this is the SHIPPED default. Validated on b2v PR #34: 0.4 clusters
# "incorrect cargo command in documentation" with "incorrect cargo
# command in performance guide" (Jaccard ≈ 0.5) while keeping
# "Prettier conflict" and "no-console rule" (Jaccard ≈ 0) apart on
# the same line.
_FUZZY_DEFAULT = 0.4


def _title_tokens(s: str) -> set[str]:
    """Lowercased alphanumeric tokens, dropping function-word noise."""
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (s or "").lower())
        if len(t) >= _TITLE_TOKEN_MIN_LEN
    }


def _title_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity between two titles (0.0 to 1.0)."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _line_bucket(line: int | None) -> int:
    """Coarse bucket-of-5 for trace metrics + ``_fingerprint``.

    No longer used by the merger — see ``_lines_close`` for the new
    absolute-distance gate that handles bucket-boundary edge cases.
    """
    return (line // 5) if isinstance(line, int) else -1


# Maximum line distance for two findings to count as locally "close".
# Bucket equality (line // 5) had a boundary bug — finding @14 and
# @15 fall into bucket 2 and 3 respectively, so adjacent-line
# observations were rejected. Caught live 2026-05-02 on b2v PR #34
# round 2 (eslint.config.js:14 vs :15). Absolute distance is
# bucket-boundary free.
_LINE_NEAR_DELTA = 5


def _lines_close(a: int | None, b: int | None) -> bool:
    """True if both lines are absent OR within ``_LINE_NEAR_DELTA``.

    Two file-less findings (line=None) count as close — file equality
    is the locality gate in that case. Mixed (one None, one int)
    means one reviewer was specific and the other wasn't; treat that
    as NOT close — the pinning reviewer saw something the other
    didn't.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= _LINE_NEAR_DELTA


def _findings_match(a: Finding, b: Finding, *, threshold: float = _FUZZY_DEFAULT) -> bool:
    """Two findings describe the same defect iff:

    * same ``file`` (None counts as a distinct value, so two file-less
      findings with similar titles can still match)
    * lines within ``_LINE_NEAR_DELTA`` (or both None — see
      ``_lines_close``); tolerates small offset drift between
      reviewers reading the same defect
    * Jaccard(title tokens) ≥ ``threshold``

    The triple gate keeps the precision/recall balance: file + line
    locality blocks spurious cross-file matches, and the Jaccard
    floor permits LLM-natural paraphrasing without merging
    genuinely-different defects on the same line.
    """
    if a.file != b.file:
        return False
    if not _lines_close(a.line, b.line):
        return False
    return _title_jaccard(a.title, b.title) >= threshold


def merge_ensemble_findings(
    per_member: list[list[Finding]],
    min_agree: int,
    *,
    fuzzy_threshold: float | None = None,
) -> list[Finding]:
    """Fold N reviewers' findings into one list by ≥M-of-N agreement.

    Algorithm: greedy single-link clustering by ``_findings_match``.
    Each finding is attached to the first existing cluster where ANY
    representative matches; otherwise a new cluster is opened. A
    cluster is kept iff the set of distinct member indices that
    contributed to it has size ≥ ``min_agree``.

    Within-member dedupe is implicit: ``cluster["members"]`` is a
    set, so a chatty reviewer reporting the same defect 3× still
    contributes a single vote. The "1 finding clusters with 0 other
    members" case correctly yields a 1-vote cluster that fails the
    floor (≥2).

    For each kept cluster the merged finding inherits:

    * highest severity across votes (lowest rank wins — blocker over
      major over minor over nit)
    * longest detail (more information beats less, ties broken by
      first occurrence)
    * file + line + title from the highest-severity representative

    Returns findings sorted by ``(severity rank, file, line)`` so the
    PR comment reads top-down by importance, matching the solo path.

    Cost is O(N²) on the total finding count summed across members.
    With typical N ≈ 5-20 findings × 3 reviewers, this is trivial.

    The ``fuzzy_threshold`` kwarg overrides the default Jaccard floor
    for tests; production callers normally read it from the env via
    ``GITOMA_REVIEW_FUZZY_THRESHOLD`` (handled in ``review_pr``).
    """
    if min_agree <= 0:
        min_agree = 1
    threshold = _FUZZY_DEFAULT if fuzzy_threshold is None else fuzzy_threshold

    # Each cluster: list of votes + set of contributing member indices.
    clusters: list[dict[str, Any]] = []

    for member_idx, member in enumerate(per_member):
        for f in member:
            attached_to: int | None = None
            for ci, cluster in enumerate(clusters):
                if any(
                    _findings_match(f, rep, threshold=threshold)
                    for rep in cluster["votes"]
                ):
                    attached_to = ci
                    break
            if attached_to is None:
                clusters.append({"votes": [f], "members": {member_idx}})
            else:
                clusters[attached_to]["votes"].append(f)
                clusters[attached_to]["members"].add(member_idx)

    kept: list[Finding] = []
    for cluster in clusters:
        if len(cluster["members"]) < min_agree:
            continue
        votes = cluster["votes"]
        votes_sorted = sorted(votes, key=lambda f: f.rank())
        best = votes_sorted[0]
        detail = max((v.detail or "" for v in votes), key=len, default="")
        kept.append(Finding(
            severity=best.severity,
            file=best.file,
            line=best.line,
            title=best.title,
            detail=detail,
        ))
    kept.sort(key=lambda f: (f.rank(), f.file or "", f.line if f.line is not None else 0))
    return kept


def render_comment_body(
    findings: list[Finding],
    *,
    ensemble_models: list[str] | None = None,
    min_agree: int = 0,
) -> str:
    """Format a list of findings into a single PR comment body.

    When ``ensemble_models`` is non-empty, the header announces the
    ensemble shape so readers know the findings already passed the
    ≥``min_agree``-of-N agreement floor — they're consensus, not
    one model's opinion.
    """
    is_ensemble = bool(ensemble_models) and min_agree >= 2
    if not findings:
        if is_ensemble:
            return (
                "🤖 **gitoma self-review** "
                f"(ensemble {min_agree}/{len(ensemble_models or [])})\n\n"
                "_No issues survived the agreement floor._"
            )
        return "🤖 **gitoma self-review**\n\n_No issues found._"

    by_sev: dict[str, list[Finding]] = {"blocker": [], "major": [], "minor": [], "nit": []}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    counts = ", ".join(
        f"{len(by_sev[s])} {s}" for s in ("blocker", "major", "minor", "nit") if by_sev[s]
    )
    if is_ensemble:
        members = ", ".join(f"`{m}`" for m in ensemble_models or [])
        header_line = (
            f"🤖 **gitoma self-review** — ensemble "
            f"{min_agree}/{len(ensemble_models or [])} consensus across {members}"
        )
        intro = (
            f"Automated critic ensemble found **{len(findings)} finding(s)** "
            f"agreed on by ≥{min_agree} reviewers: {counts}."
        )
    else:
        header_line = "🤖 **gitoma self-review**"
        intro = (
            f"Automated critic pass found **{len(findings)} finding(s)**: {counts}."
        )
    lines: list[str] = [
        header_line,
        "",
        intro,
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
