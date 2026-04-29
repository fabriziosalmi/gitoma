"""PHASE 7 (opt-in) — auto-write a diary entry to a remote log repo.

When `GITOMA_DIARY_REPO` and `GITOMA_DIARY_TOKEN` are both set,
the end of every successful `gitoma run` writes a markdown entry
to that repo summarising what just happened. Filename includes a
timestamp + slug so two parallel runs never collide on commit.

Failure modes are intentionally swallowed: any error in this
hook (bad token, network down, push conflict, missing dirs)
gets traced via `current_trace().emit("diary.write_failed", ...)`
and the run completes normally. The diary is best-effort by
design — we never want a flaky log to fail an otherwise-good
gitoma run.

Schema of an entry's frontmatter (machine-readable):

    ---
    date:           ISO 8601 timestamp at PHASE 7 trigger
    repo:           owner/name being improved
    branch:         feature branch gitoma created
    pr:             PR number (or null if no PR)
    pr_url:         full PR URL (or null)
    model:          LM Studio model id used
    endpoint:       LM Studio base URL
    plan_source:    "llm" or "plan-from-file:<name>"
    plan_tasks:     count
    plan_subtasks:  count
    subtasks_done:  "X/Y"
    guards_fired:   list of guard event names from trace
    self_review:    one-line status
    verdict:        auto-derived ("clean" | "partial" | "failed")
    ---

Body is a short markdown summary suitable for grepping later.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gitoma.core.trace import current as current_trace


__all__ = [
    "DiaryConfig",
    "DiaryWriteResult",
    "write_diary_entry",
    "_slugify",
    "_compose_entry",
]


# ── Config ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiaryConfig:
    """Diary settings sourced from env. Both fields required."""

    repo: str          # owner/name (e.g. "fabgpt-coder/log")
    token: str         # GitHub PAT with push rights to ``repo``
    # Optional allowlist of source-repo patterns that are allowed to
    # have diary entries written. Set via ``GITOMA_DIARY_REPO_ALLOWLIST``
    # (comma-separated, supports ``*`` wildcards). Empty list = all
    # repos allowed (backward-compatible default). When non-empty, any
    # source repo whose ``owner/name`` does not match at least one
    # pattern is silently skipped — protects against pushing client
    # repo names to a public diary log when running gitoma on
    # confidential codebases. Matching is case-insensitive.
    allowlist: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "DiaryConfig | None":
        """Return a config if both env vars are set, else None.

        Single-source-of-truth for the opt-in: the rest of the module
        bails out cleanly when this returns None."""
        repo = (os.environ.get("GITOMA_DIARY_REPO") or "").strip()
        token = (os.environ.get("GITOMA_DIARY_TOKEN") or "").strip()
        if not repo or not token:
            return None
        if "/" not in repo:
            return None
        allowlist_raw = (
            os.environ.get("GITOMA_DIARY_REPO_ALLOWLIST") or ""
        ).strip()
        allowlist: tuple[str, ...] = ()
        if allowlist_raw:
            allowlist = tuple(
                p.strip() for p in allowlist_raw.split(",") if p.strip()
            )
        return cls(repo=repo, token=token, allowlist=allowlist)


def _matches_allowlist(repo_url: str, allowlist: tuple[str, ...]) -> bool:
    """Return True iff ``repo_url`` (parsed to ``owner/name``) matches
    at least one pattern in ``allowlist``. Empty allowlist = always
    True (backward-compat default-allow)."""
    if not allowlist:
        return True
    # Reduce the URL to ``owner/name`` for pattern matching.
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        owner_name = f"{parts[-2]}/{parts[-1]}".lower()
    else:
        owner_name = repo_url.lower()
    # Strip .git suffix if present
    if owner_name.endswith(".git"):
        owner_name = owner_name[:-4]
    import fnmatch
    for pattern in allowlist:
        if fnmatch.fnmatchcase(owner_name, pattern.lower()):
            return True
    return False


# ── Result ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiaryWriteResult:
    """What happened. Useful for testing + a future cockpit panel."""

    ok: bool
    entry_path: str = ""              # path within the diary repo
    commit_sha: str = ""              # local commit sha (post-push)
    error: str = ""                   # populated when ok is False


# ── Helpers ───────────────────────────────────────────────────────


_SLUG_BAD = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(s: str, max_len: int = 40) -> str:
    """ASCII-only slug for filenames. Collapses runs of non-alnum
    into a single hyphen, trims, lowercases, caps at ``max_len``."""
    out = _SLUG_BAD.sub("-", s).strip("-").lower()
    if len(out) > max_len:
        out = out[:max_len].rstrip("-")
    return out or "untitled"


def _safe_get(d: dict | None, key: str, default: Any = None) -> Any:
    """``d.get(key, default)`` that tolerates ``d is None``."""
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def _extract_guard_firings(trace_path: Path | None) -> list[str]:
    """Walk the trace JSONL (if available) and pull unique guard-FIRING
    event names. A "firing" is specifically an event whose name ends
    in ``.fail`` AND starts with ``critic_`` — informational events
    (``critic_panel.review.start``, ``critic_panel.finding``, …) are
    excluded so the diary's ``guards_fired`` field represents what
    actually went wrong, not the trace volume.

    Capped at 12 to keep frontmatter sane."""
    if trace_path is None or not trace_path.exists():
        return []
    seen: list[str] = []
    try:
        with trace_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                name = ev.get("event") or ""
                if (
                    name.startswith("critic_")
                    and name.endswith(".fail")
                    and name not in seen
                ):
                    seen.append(name)
                    if len(seen) >= 12:
                        break
    except OSError:
        return []
    return seen


def _derive_verdict(plan_total: int, subtasks_done: int, pr_url: str) -> str:
    """One-word verdict derivation from raw counts.

    * ``clean``    — every subtask landed AND a PR was opened
    * ``partial``  — some subtasks landed, others failed
    * ``failed``   — zero subtasks landed
    * ``no-plan``  — fallback when counts are unknowable
    """
    if plan_total <= 0:
        return "no-plan"
    if subtasks_done == 0:
        return "failed"
    if subtasks_done == plan_total and pr_url:
        return "clean"
    return "partial"


# ── Entry composition (pure: testable without IO) ─────────────────


def _compose_entry(
    *,
    repo_url: str,
    state: Any,
    plan: Any,
    config: Any,
    guard_firings: list[str],
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(filename, content)``. Pure function — no IO.

    ``filename`` follows
    ``entries/YYYY-MM-DD-HHMM-{repo-slug}-{branch-slug}.md``.
    ``content`` is the full markdown (frontmatter + body).
    """
    when = now or datetime.now(timezone.utc).astimezone()

    # Pull whatever fields are reachable; defensively None-tolerant.
    pr_number = getattr(state, "pr_number", None)
    pr_url = getattr(state, "pr_url", "") or ""
    branch = getattr(state, "branch", "") or ""
    repo_slug = repo_url.rstrip("/").rsplit("/", 2)
    repo_owner_name = (
        f"{repo_slug[-2]}/{repo_slug[-1]}"
        if len(repo_slug) >= 2 else repo_url
    )

    # Plan stats. ``plan`` may be a TaskPlan or None.
    plan_tasks = getattr(plan, "total_tasks", 0) if plan else 0
    plan_subtasks = getattr(plan, "total_subtasks", 0) if plan else 0
    plan_source_raw = getattr(plan, "llm_model", "") if plan else ""
    plan_source = plan_source_raw if plan_source_raw.startswith("plan-from-file:") else "llm"

    # Subtasks completed: count via state.task_plan (already a dict).
    subtasks_done = 0
    tp = getattr(state, "task_plan", None)
    for task in (_safe_get(tp, "tasks", []) or []):
        for sub in (_safe_get(task, "subtasks", []) or []):
            if (_safe_get(sub, "status") == "completed"):
                subtasks_done += 1

    # Self-review summary. Best-effort — depends on what the run
    # left in state. Fall back to a neutral string.
    sr_findings = getattr(state, "self_review_findings", None)
    if isinstance(sr_findings, (list, tuple)):
        self_review = f"{len(sr_findings)} finding(s)"
    elif isinstance(sr_findings, str) and sr_findings:
        self_review = sr_findings
    else:
        self_review = "n/a"

    # Endpoint + model.
    model = getattr(getattr(config, "lmstudio", None), "model", "") or "unknown"
    endpoint = getattr(getattr(config, "lmstudio", None), "base_url", "") or "unknown"

    verdict = _derive_verdict(plan_subtasks, subtasks_done, pr_url)

    # Filename pieces.
    ts_part = when.strftime("%Y-%m-%d-%H%M")
    repo_part = _slugify(
        repo_owner_name.replace("/", "-"), max_len=40
    )
    branch_part = _slugify(branch or "no-branch", max_len=40)
    filename = f"entries/{ts_part}-{repo_part}-{branch_part}.md"

    # ── Frontmatter ────────────────────────────────────────────────
    fm_lines = [
        "---",
        f"date: {when.isoformat()}",
        f"repo: {repo_owner_name}",
        f"branch: {branch}",
        f"pr: {pr_number if pr_number else 'null'}",
        f"pr_url: {pr_url if pr_url else 'null'}",
        f"model: {model}",
        f"endpoint: {endpoint}",
        f"plan_source: {plan_source}",
        f"plan_tasks: {plan_tasks}",
        f"plan_subtasks: {plan_subtasks}",
        f"subtasks_done: {subtasks_done}/{plan_subtasks}",
    ]
    if guard_firings:
        fm_lines.append("guards_fired:")
        for g in guard_firings:
            fm_lines.append(f"  - {g}")
    else:
        fm_lines.append("guards_fired: []")
    fm_lines.append(f"self_review: {self_review}")
    fm_lines.append(f"verdict: {verdict}")
    fm_lines.append("---")

    # ── Body ───────────────────────────────────────────────────────
    body_lines = [
        "",
        f"# {repo_owner_name} — {branch}",
        "",
    ]
    if pr_url:
        body_lines.append(f"PR: <{pr_url}>")
        body_lines.append("")
    body_lines.append(f"**Verdict**: {verdict} — {subtasks_done}/{plan_subtasks} subtasks completed.")
    body_lines.append("")
    if plan_source.startswith("plan-from-file:"):
        body_lines.append(f"Plan source: operator-curated (`{plan_source}`).")
    else:
        body_lines.append("Plan source: LLM-generated (PHASE 2 planner).")
    body_lines.append("")
    if guard_firings:
        body_lines.append("Guards that fired during execution:")
        body_lines.append("")
        for g in guard_firings:
            body_lines.append(f"- `{g}`")
        body_lines.append("")
    body_lines.append(
        "_Auto-generated by gitoma PHASE 7. To turn off, unset "
        "`GITOMA_DIARY_REPO` or `GITOMA_DIARY_TOKEN`._"
    )

    return filename, "\n".join(fm_lines) + "\n" + "\n".join(body_lines) + "\n"


# ── Public entry point: write + commit + push ─────────────────────


def write_diary_entry(
    *,
    diary_config: DiaryConfig,
    repo_url: str,
    state: Any,
    plan: Any,
    config: Any,
    trace_path: Path | None = None,
) -> DiaryWriteResult:
    """Compose a diary entry from the run state and push it to the
    diary repo. All errors caught + reported via DiaryWriteResult.

    When ``diary_config.allowlist`` is non-empty AND ``repo_url`` does
    not match any pattern, the write is skipped silently (with a
    trace event) so client/private repos don't leak their identity
    onto a public diary log."""
    if not _matches_allowlist(repo_url, diary_config.allowlist):
        try:
            current_trace().emit(
                "diary.skipped_by_allowlist",
                repo_url=repo_url,
                allowlist=list(diary_config.allowlist),
            )
        except Exception:  # noqa: BLE001
            pass
        return DiaryWriteResult(
            ok=False,
            error="repo not in GITOMA_DIARY_REPO_ALLOWLIST — skipped",
        )
    try:
        guard_firings = _extract_guard_firings(trace_path)
        filename, content = _compose_entry(
            repo_url=repo_url,
            state=state,
            plan=plan,
            config=config,
            guard_firings=guard_firings,
        )
        sha = _commit_and_push(
            diary_config, filename, content,
            commit_msg=f"log: {filename.removeprefix('entries/').removesuffix('.md')}",
        )
        return DiaryWriteResult(ok=True, entry_path=filename, commit_sha=sha)
    except Exception as exc:  # noqa: BLE001
        # Trace + return — never raise out of this function.
        try:
            current_trace().emit(
                "diary.write_failed",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        except Exception:  # noqa: BLE001
            pass
        return DiaryWriteResult(ok=False, error=str(exc)[:300])


def _commit_and_push(
    cfg: DiaryConfig, filename: str, content: str, commit_msg: str,
) -> str:
    """Clone the diary repo, write the entry, commit, push.

    A single retry on push conflict (someone else pushed concurrently
    — possible during parallel gitoma runs) by rebasing on the new
    main and re-pushing. Beyond that, raise."""
    tmpdir = Path(tempfile.mkdtemp(prefix="gitoma-diary-"))
    try:
        clone_url = f"https://x-access-token:{cfg.token}@github.com/{cfg.repo}.git"
        _run(["git", "clone", "--quiet", "--depth", "5", clone_url, str(tmpdir)])
        target = tmpdir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _run(["git", "-C", str(tmpdir), "add", filename])
        _run([
            "git", "-C", str(tmpdir),
            "-c", "user.name=fabgpt-coder",
            "-c", "user.email=fabgpt.inbox@gmail.com",
            "commit", "-q", "-m", commit_msg,
        ])
        sha = _run(
            ["git", "-C", str(tmpdir), "rev-parse", "HEAD"],
            capture=True,
        ).strip()
        try:
            _run(["git", "-C", str(tmpdir), "push", "--quiet"])
        except subprocess.CalledProcessError:
            # Concurrent-write conflict path: pull --rebase + re-push once.
            _run(["git", "-C", str(tmpdir), "pull", "--quiet", "--rebase"])
            _run(["git", "-C", str(tmpdir), "push", "--quiet"])
        return sha
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run(cmd: list[str], capture: bool = False) -> str:
    """Subprocess helper. Captures stderr by default for diagnostics."""
    result = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=30,
    )
    return result.stdout if capture else ""
