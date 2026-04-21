"""Routers for Gitoma REST API.

The agent commands (`run`, `analyze`, `review`, `fix-ci`) all dispatch the
corresponding `gitoma` CLI subcommand as an **async** subprocess. Each spawn
is tracked by a :class:`JobRecord` that holds a ring buffer of recent output
plus a set of asyncio queue subscribers. The `/api/v1/stream/{job_id}` SSE
endpoint lets any HTTP client tail the job's output line-by-line in real
time (used by the cockpit live-log panel).

Hardening notes (industrial-grade pass):

* ``_JOBS`` is guarded by an asyncio.Lock — no silent race if uvicorn ever
  spins up a second worker thread or a cancel/dispatch fight over eviction.
* Subprocesses run in their own process group (``os.setsid``) so
  ``os.killpg`` terminates the whole subtree on cancel/shutdown.
* The spawned CLI inherits a **scrubbed env** — the server's
  ``GITOMA_API_TOKEN`` never crosses the process boundary.
* Stdout is published line-by-line with URL credential redaction + a hard
  per-line size cap; SSE frames include periodic heartbeats so proxies
  don't prematurely close idle streams.
* Error surface uses opaque ``error_id`` strings; the real exception is
  logged server-side and never emitted to the client.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import time as _time
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from gitoma.core.config import load_config
from gitoma.core.state import delete_state as _delete_state
from gitoma.core.state import load_state as _load_state
from gitoma.planner.llm_client import check_lmstudio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ── Request / response schemas ───────────────────────────────────────────────

# GitHub's own rules: owner/repo are 1..39 / 1..100 alnum + `._-`. We accept
# the same plus an optional trailing ``.git`` and trailing slash. Rejecting
# anything else early means a malformed value can never reach `typer` as an
# argv token it might misinterpret as a flag.
_REPO_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]{0,38}/"
    r"[A-Za-z0-9._-]{1,100}(?:\.git)?/?$"
)
# Git ref names: see git-check-ref-format(1) — we apply a conservative subset.
_BRANCH_RE = re.compile(r"^(?!-)[A-Za-z0-9._/-]{1,255}$")

# State filenames are interpolated into ``STATE_DIR / f"{owner}__{name}.json"``
# (see :func:`gitoma.core.state._state_path`). Any character that lets the
# value escape that directory — slash, backslash, NUL, leading dot — must be
# rejected at the HTTP boundary so a percent-decoded ``..%2F..%2Fpasswd``
# can never reach ``Path.unlink`` on something outside ``~/.gitoma/state``.
# The allow-set matches GitHub's own owner/repo charset.
_STATE_SLUG_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,100}$")


class RunRequest(BaseModel):
    repo_url: str = Field(
        ...,
        max_length=255,
        description="https://github.com/<owner>/<repo>",
        examples=["https://github.com/octocat/hello-world"],
    )
    branch: Optional[str] = Field(
        None,
        max_length=255,
        description="Feature branch name; validated against git ref-format rules.",
    )
    dry_run: bool = False
    # Resume picks up where a previous run left off (state file must
    # exist on disk). The CLI's --resume re-uses the saved branch and
    # skips analyze / plan / already-completed subtasks. No-op if no
    # state is present, so safe to pass defensively from the cockpit.
    resume: bool = False
    # Reset deletes the persisted state file before starting, giving a
    # clean slate. Useful when an orphaned run is unrecoverable or the
    # user wants to re-plan from scratch on a repo that already has a
    # state snapshot. Mutually exclusive with ``resume``.
    reset: bool = False

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        if not _REPO_URL_RE.match(v):
            raise ValueError(
                "repo_url must be https://github.com/<owner>/<repo> "
                "(no credentials, no trailing spaces, no query string)"
            )
        return v

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _BRANCH_RE.match(v):
            raise ValueError("branch does not look like a valid git ref")
        return v

    @field_validator("reset")
    @classmethod
    def _mutually_exclusive(cls, v: bool, info) -> bool:  # type: ignore[no-untyped-def]
        # Pydantic v2 passes ``info`` with already-validated fields in
        # ``info.data``. ``resume`` is declared above, so it's populated
        # here regardless of field order in the JSON body.
        if v and info.data.get("resume"):
            raise ValueError("resume and reset are mutually exclusive")
        return v


class ReviewRequest(BaseModel):
    repo_url: str = Field(..., max_length=255)
    integrate: bool = False

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        if not _REPO_URL_RE.match(v):
            raise ValueError("repo_url must be https://github.com/<owner>/<repo>")
        return v


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(..., max_length=255)

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        if not _REPO_URL_RE.match(v):
            raise ValueError("repo_url must be https://github.com/<owner>/<repo>")
        return v


class JobResponse(BaseModel):
    job_id: str = Field(..., description="UUID assigned to this background job")
    status: Literal["queued", "started"] = "started"
    message: str = ""


class JobStatusResponse(BaseModel):
    job_id: str
    label: str
    status: str = Field(
        ...,
        description=(
            "One of: queued, running, completed, cancelled, timed_out, or "
            "failed. Use /stream/{job_id} for live output."
        ),
    )
    created_at: str
    finished_at: Optional[str] = None
    lines_buffered: int = 0
    error_id: Optional[str] = Field(
        None,
        description=(
            "Opaque correlation id set when status is `failed`/`timed_out`. "
            "The real exception is written to server logs keyed by this id."
        ),
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    lm_studio: dict[str, object]
    github_token_set: bool


class StateDeleteResponse(BaseModel):
    owner: str
    name: str
    result: Literal["deleted", "not_found"]


class CancelResponse(BaseModel):
    job_id: str
    status: Literal["cancelling"]


# ── Job tracking ─────────────────────────────────────────────────────────────

_LOG_BUFFER_LINES = 500
_SUBSCRIBER_QUEUE_DEPTH = 1000
# Hard cap on concurrent SSE consumers per job. Without this an
# authenticated client can open hundreds of /stream/{job_id} subscriptions
# and balloon RAM (each subscriber owns a ``_SUBSCRIBER_QUEUE_DEPTH``-deep
# queue × ``_MAX_LINE_BYTES``). 16 covers every realistic case (one cockpit
# tab + a handful of curl tails); past that we 429 the new connection so
# the existing subscribers stay live.
_MAX_SUBSCRIBERS_PER_JOB = 16
_END_SENTINEL = "__END__"
# A runaway CLI that prints one 50 MB line would blow up both the ring
# buffer and every subscriber queue. Truncate first, ship a short marker.
_MAX_LINE_BYTES = 4096

# Long-lived server hygiene: we never want _JOBS to grow unboundedly, so
# finished records are evicted after this TTL or when the cap is exceeded
# (oldest finished first; running jobs are never evicted).
MAX_JOBS = 50
JOB_TTL_SECONDS = 900  # 15 min
# Hard ceiling on a single job's runtime. If a CLI hangs (e.g. LLM stuck
# in an infinite retry loop), the job is SIGTERM'd at the boundary and
# its status becomes ``timed_out``. 1 h is generous for real runs but
# bounds the worst case.
JOB_MAX_RUNTIME_SECONDS = 3600

# SSE heartbeat: most reverse proxies (nginx, Cloudflare) kill idle
# streams at 30–60 s. A comment frame every 15 s keeps the connection
# alive without confusing clients — comments are ignored by EventSource.
_SSE_HEARTBEAT_SECONDS = 15.0

# Environment variables we actively strip before spawning the CLI
# subprocess. ``GITOMA_API_TOKEN`` is the server's own auth token — the
# CLI has no business seeing it, and we don't want it in the child's
# /proc/<pid>/environ where another user could read it.
_SECRET_ENV_VARS: frozenset[str] = frozenset({"GITOMA_API_TOKEN"})

# Pattern-based scrub: anything whose UPPERCASED name ends with one of
# these suffixes is presumed to be a secret and stripped, EVEN IF we
# don't know what it is. Belt-and-braces for the long tail of vendor
# credentials (AWS_*, GCP_*, DOCKER_*, NPM_TOKEN, CARGO_REGISTRY_TOKEN,
# DATABASE_URL with embedded creds, …) that the operator may have in
# their shell env. Without this the CLI subprocess inherits everything
# and another local user can read /proc/<pid>/environ.
_SECRET_NAME_SUFFIXES: tuple[str, ...] = (
    "_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PASS",
    "_CREDENTIAL", "_CREDENTIALS", "_PRIVATE_KEY", "_API_KEY",
    "_ACCESS_KEY", "_AUTH",
)
# Names that *match* the suffix rule above but are legitimately needed by
# the CLI — kept on the explicit allow-list so they survive the scrub.
# Anything not in this set, with a secret-shaped name, is dropped.
_SECRET_NAME_ALLOWLIST: frozenset[str] = frozenset({
    "GITHUB_TOKEN",       # core/config.py: GitHub API auth for the worker
    "GH_TOKEN",           # alias gh CLI uses; harmless if unset
    "LM_STUDIO_API_KEY",  # core/config.py: LMStudio (placeholder, but read)
    "OPENAI_API_KEY",     # planner uses OpenAI-compat clients pointed at LM Studio
    "ANTHROPIC_API_KEY",  # self-critic / future LLM backends
    "SSH_AUTH_SOCK",      # not a "secret" but matches no suffix; here for clarity
})


def _looks_like_secret(name: str) -> bool:
    """True if ``name`` matches a known secret-name suffix and is not
    on the explicit allow-list of credentials the CLI legitimately uses."""
    if name in _SECRET_NAME_ALLOWLIST:
        return False
    upper = name.upper()
    return any(upper.endswith(suffix) for suffix in _SECRET_NAME_SUFFIXES)

# Strip embedded credentials in git/https URLs before publishing to the
# ring buffer. Matches `https://user:password@host/…` and `ssh://…`.
_URL_CREDS_RE = re.compile(r"(https?|ssh|git)://[^/\s:]+:[^@\s]+@")


@dataclass
class JobRecord:
    """In-memory record for a single background CLI job."""

    id: str
    label: str
    argv: list[str]
    status: str = "queued"
    lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=_LOG_BUFFER_LINES)
    )
    subscribers: set["asyncio.Queue[str]"] = field(default_factory=set)
    created_at: _dt.datetime = field(
        default_factory=lambda: _dt.datetime.now(_dt.timezone.utc)
    )
    finished_at: _dt.datetime | None = None
    # Populated by `_dispatch` — lets `/jobs/{id}/cancel` and the shutdown
    # hook abort the running subprocess cleanly.
    task: "asyncio.Task[None] | None" = None
    # Correlation id surfaced in the API when status becomes non-OK. The
    # full stack trace is in server logs; the client only ever sees this.
    error_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status not in ("queued", "running")


# Keyed by job_id. In production this would be a durable store.
_JOBS: dict[str, JobRecord] = {}
# Guards the set of active jobs against interleaved dispatch / eviction /
# cancel races. Held only for short dictionary mutations — no I/O happens
# under the lock.
_JOBS_LOCK = asyncio.Lock()


# ── Per-token dispatch rate limiter ─────────────────────────────────────────
#
# ``MAX_JOBS`` already caps total in-memory jobs at 50, but a compromised or
# runaway client can saturate the pool in <1 s, denying legitimate users
# for 15 min and burning the LM-Studio / GitHub quota. A sliding-window
# counter per bearer token is the smallest sufficient defence:
#
#   * ``DISPATCH_RATE_LIMIT_BURST`` requests
#   * within ``DISPATCH_RATE_LIMIT_WINDOW_S`` seconds
#   * per (sha256-truncated) bearer token
#
# We hash the token before using it as a key so an accidental log dump
# (a future ``logger.debug({...this dict...})``) doesn't leak the secret.
# Anonymous (no Authorization) requests share a single bucket — they
# can't reach the dispatch endpoints anyway (verify_token rejects them).
DISPATCH_RATE_LIMIT_BURST = 20
DISPATCH_RATE_LIMIT_WINDOW_S = 60.0

_dispatch_recent: dict[str, deque[float]] = defaultdict(deque)
_dispatch_recent_lock = asyncio.Lock()


def _token_bucket_key(request: Request) -> str:
    """Return a stable, non-reversible bucket id for the rate limiter.

    Hash protects against the secret leaking via a log line that prints
    the limiter's internal state. Truncated to 16 hex chars — collisions
    here would just merge two clients' buckets, never confuse auth.
    """
    auth = request.headers.get("authorization", "")
    return "sha256:" + hashlib.sha256(auth.encode("utf-8")).hexdigest()[:16]


async def _enforce_dispatch_rate_limit(request: Request) -> None:
    """Raise 429 when the caller exceeds the dispatch burst quota."""
    key = _token_bucket_key(request)
    now = _time.monotonic()
    cutoff = now - DISPATCH_RATE_LIMIT_WINDOW_S
    async with _dispatch_recent_lock:
        bucket = _dispatch_recent[key]
        # Drop entries older than the window. Cheap: deques are O(1)
        # popleft and we only walk until we hit something fresh.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= DISPATCH_RATE_LIMIT_BURST:
            # Compute Retry-After from the oldest in-window timestamp so
            # the client can wait minimum sufficient time, not a fixed value.
            retry_after = max(1, int(bucket[0] + DISPATCH_RATE_LIMIT_WINDOW_S - now) + 1)
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Dispatch rate limit exceeded "
                    f"({DISPATCH_RATE_LIMIT_BURST} per {int(DISPATCH_RATE_LIMIT_WINDOW_S)}s). "
                    f"Retry after {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def _reset_dispatch_rate_limiter() -> None:
    """Clear the rate-limit buckets (used by tests)."""
    _dispatch_recent.clear()


async def _evict_stale_locked() -> None:
    """Drop finished jobs past their TTL, then enforce the hard cap.

    Must be called with ``_JOBS_LOCK`` already held. Running jobs are
    never evicted — only finished/failed/cancelled/timed_out records.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(seconds=JOB_TTL_SECONDS)
    # 1. TTL eviction — snapshot items first so we never mutate while
    # iterating (paranoia: the lock should be enough, but the snapshot
    # is free and deterministic).
    for jid, job in list(_JOBS.items()):
        if job.is_terminal and job.finished_at and job.finished_at < cutoff:
            _JOBS.pop(jid, None)

    # 2. Hard cap — drop oldest terminal records first
    if len(_JOBS) <= MAX_JOBS:
        return
    terminal_by_age = sorted(
        (job for job in _JOBS.values() if job.is_terminal),
        key=lambda j: j.finished_at or j.created_at,
    )
    for job in terminal_by_age:
        if len(_JOBS) <= MAX_JOBS:
            return
        _JOBS.pop(job.id, None)


# ── CLI resolution ───────────────────────────────────────────────────────────


def _gitoma_cli_argv() -> list[str]:
    """Resolve how to invoke the gitoma CLI in a subprocess.

    Prefers the `gitoma` entrypoint if on PATH (installed via pip), otherwise
    falls back to `python -m gitoma.cli` using the current interpreter.
    """
    exe = shutil.which("gitoma")
    if exe:
        return [exe]
    return [sys.executable, "-m", "gitoma.cli"]


def _scrubbed_env() -> dict[str, str]:
    """Return ``os.environ`` minus secrets the CLI subprocess shouldn't see.

    Two layers, both deny-by-default:

    1. **Explicit drops** (``_SECRET_ENV_VARS``): the API's own Bearer
       token. The CLI has no business seeing it, and we don't want it in
       the child's ``/proc/<pid>/environ`` where another user could read it.
    2. **Pattern drops** (``_looks_like_secret``): anything whose name
       matches a known credential suffix (``_TOKEN``, ``_KEY``,
       ``_SECRET``, ``_PASSWORD``, …) UNLESS it's on the explicit
       allow-list of secrets the CLI legitimately needs (``GITHUB_TOKEN``,
       ``LM_STUDIO_API_KEY``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``).

    The pattern layer catches the long tail of vendor credentials the
    operator may have in their shell env (AWS_*, GCP_*, DOCKER_*,
    NPM_TOKEN, CARGO_REGISTRY_TOKEN, DATABASE_URL-style creds, etc.)
    that would otherwise silently leak to the child process and to any
    trace/debug log it might produce.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _SECRET_ENV_VARS and not _looks_like_secret(k)
    }


# ── Pub/sub plumbing for live log streaming ─────────────────────────────────


def _sanitize_line(line: str) -> str:
    """Strip embedded credentials and truncate overlong lines.

    Defence-in-depth: Pydantic validators already reject repo URLs with
    ``user:pass@`` credentials, but the CLI can still print authenticated
    URLs in its own stack traces (e.g. GitPython error strings). We redact
    proactively so they never land in the ring buffer.
    """
    redacted = _URL_CREDS_RE.sub(lambda m: f"{m.group(1)}://REDACTED@", line)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_LINE_BYTES:
        truncated = encoded[:_MAX_LINE_BYTES].decode("utf-8", errors="replace")
        return truncated + "…(truncated)"
    return redacted


def _publish(job: JobRecord, line: str) -> None:
    """Append to the ring buffer and fan out to every active subscriber.

    Back-pressure policy: a full subscriber queue means the consumer fell
    behind. Rather than blocking the producer or dropping *this* line
    (which is usually the most recent, most valuable), we drop the
    *oldest* queued line to make room. The client sees a small gap but
    keeps following the tail in real time — classic log-tailing semantics.
    """
    safe = _sanitize_line(line)
    job.lines.append(safe)
    for q in list(job.subscribers):
        try:
            q.put_nowait(safe)
        except asyncio.QueueFull:
            # Drop oldest, then append. If pop-then-put still races we drop
            # the line entirely — correctness over completeness.
            with suppress(asyncio.QueueEmpty):
                q.get_nowait()
            with suppress(asyncio.QueueFull):
                q.put_nowait(safe)


async def _spawn_cli_job(job: JobRecord) -> None:
    """Run the CLI subprocess for this job and stream its output.

    stdout+stderr are merged into a single stream so timing between the two
    is preserved. Each decoded line is broadcast via `_publish`. A terminal
    sentinel is always emitted so SSE consumers know to close the stream.
    """
    job.status = "running"
    _publish(job, f"$ {' '.join(job.argv)}")

    # POSIX: ``start_new_session`` makes the child the leader of a new
    # session/process group so ``os.killpg`` reaps the whole tree on cancel.
    # We used to pass ``preexec_fn=os.setsid``, but that is deprecated in
    # 3.12 and deadlock-prone in multi-threaded parents (uvicorn's default
    # threadpool counts). ``start_new_session=True`` does the same thing
    # without forking a Python callback. Ignored on Windows.
    try:
        proc = await asyncio.create_subprocess_exec(
            *job.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=_scrubbed_env(),
            start_new_session=(sys.platform != "win32"),
        )
    except (OSError, ValueError):
        eid = uuid.uuid4().hex[:12]
        logger.exception("job_spawn_failed", extra={"job_id": job.id, "error_id": eid})
        job.status = "failed"
        job.error_id = eid
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"[error] could not spawn CLI (error_id={eid})")
        _publish(job, f"{_END_SENTINEL}:{job.status}")
        return

    # ``stdout=PIPE`` was passed above, so this should never trigger — but
    # ``assert`` is stripped under ``python -O`` and the next line would
    # crash with a less actionable AttributeError. Fail loudly instead.
    if proc.stdout is None:
        eid = uuid.uuid4().hex[:12]
        logger.error(
            "job_subprocess_no_stdout",
            extra={"job_id": job.id, "error_id": eid, "pid": proc.pid},
        )
        _kill_process_group(proc, signal.SIGKILL)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        job.status = "failed"
        job.error_id = eid
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"[error] subprocess started without stdout (error_id={eid})")
        _publish(job, f"{_END_SENTINEL}:{job.status}")
        return
    try:
        # Readline coroutine wrapped in wait_for enforces the global job
        # timeout. If no line arrives within the remaining budget, the
        # subprocess is killed and the job transitions to timed_out.
        deadline = asyncio.get_running_loop().time() + JOB_MAX_RUNTIME_SECONDS
        while True:
            remaining = max(1.0, deadline - asyncio.get_running_loop().time())
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                # Job exceeded its deadline while still producing (or
                # stalled with pipe open). Kill the group and surface it.
                _kill_process_group(proc)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                job.status = "timed_out"
                job.finished_at = _dt.datetime.now(_dt.timezone.utc)
                _publish(job, f"[error] job exceeded {JOB_MAX_RUNTIME_SECONDS}s — killed")
                _publish(job, f"{_END_SENTINEL}:timed_out")
                return
            if not raw:
                break
            line = raw.decode("utf-8", errors="backslashreplace").rstrip("\r\n")
            _publish(job, line)
        rc = await proc.wait()
    except asyncio.CancelledError:
        # Cancel requested (via /cancel or server shutdown): SIGTERM the
        # whole process group so git/ssh children get reaped, give them
        # 5 s, then SIGKILL. If even SIGKILL doesn't reap inside its
        # grace window, the process is wedged (kernel-level hang, broken
        # signal mask, …) — record an error_id so the client can tell
        # that "cancelled" is suspect and the operator can investigate
        # the leftover PID.
        term_outcome = _kill_process_group(proc, signal.SIGTERM)
        kill_failed = term_outcome == "failed"
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            kill_outcome = _kill_process_group(proc, signal.SIGKILL)
            if kill_outcome == "failed":
                kill_failed = True
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Process is wedged past SIGKILL — surface it.
                kill_failed = True
        if kill_failed:
            eid = uuid.uuid4().hex[:12]
            logger.error(
                "cancel_did_not_reap",
                extra={"job_id": job.id, "error_id": eid, "pid": proc.pid},
            )
            job.error_id = eid
            _publish(
                job,
                f"[warn] cancel may have left pid={proc.pid} alive "
                f"(error_id={eid}); inspect server log",
            )
        job.status = "cancelled"
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"{_END_SENTINEL}:cancelled")
        raise
    except Exception:
        eid = uuid.uuid4().hex[:12]
        logger.exception("job_crashed", extra={"job_id": job.id, "error_id": eid})
        job.status = "failed"
        job.error_id = eid
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"[error] job crashed (error_id={eid})")
        _publish(job, f"{_END_SENTINEL}:{job.status}")
        return

    if rc == 0:
        job.status = "completed"
    else:
        job.status = f"failed (rc={rc})"
    job.finished_at = _dt.datetime.now(_dt.timezone.utc)
    _publish(job, f"{_END_SENTINEL}:{job.status}")


def _kill_process_group(
    proc: asyncio.subprocess.Process, sig: int = signal.SIGTERM
) -> str:
    """Signal the process group; return a short outcome code.

    Outcomes:
      * ``"signaled"`` — the signal was delivered (process may not yet
        be reaped; the caller still has to ``proc.wait()``).
      * ``"already_gone"`` — the kernel says the target is no longer
        there (``ProcessLookupError`` / ``ESRCH``). Effectively success.
      * ``"failed"`` — the syscall failed for another reason (permission,
        invalid signal, …). Logged at warning level so an operator can
        diagnose; the caller should treat the cancel as suspect and not
        report unconditional success.

    We spawned with ``start_new_session=True`` on POSIX; the child's pgid
    equals its pid. On Windows we fall back to ``proc.kill``/``terminate``
    semantics which only target the immediate child.
    """
    if sys.platform == "win32" or proc.pid is None:
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            return "already_gone"
        except OSError as e:
            logger.warning(
                "kill_process_group_failed",
                extra={"pid": proc.pid, "sig": int(sig), "error": str(e)[:200]},
            )
            return "failed"
        return "signaled"
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        return "already_gone"
    except OSError as e:
        logger.warning(
            "kill_process_group_failed",
            extra={"pid": proc.pid, "sig": int(sig), "error": str(e)[:200]},
        )
        return "failed"
    return "signaled"


async def _dispatch(label: str, argv: list[str]) -> JobRecord:
    """Register a new job and schedule it on the event loop."""
    async with _JOBS_LOCK:
        await _evict_stale_locked()
        job_id = str(uuid.uuid4())
        job = JobRecord(id=job_id, label=label, argv=argv)
        _JOBS[job_id] = job
    # Task created *outside* the lock — asyncio.create_task is safe, and
    # holding a lock across ``create_task`` would serialise startup.
    job.task = asyncio.create_task(
        _spawn_cli_job(job), name=f"gitoma-job-{job_id[:8]}"
    )
    return job


async def cancel_all_jobs() -> None:
    """Cancel every running task and wait briefly for cleanup.

    Called from the app lifespan shutdown handler so the server exits
    without leaving orphan gitoma subprocesses behind.
    """
    async with _JOBS_LOCK:
        # Filter out the None tasks here (instead of `assert`-ing later)
        # so the loops below never have to second-guess the invariant.
        # ``assert`` would be stripped under ``python -O``.
        victims = [job for job in _JOBS.values() if job.task and not job.task.done()]
    for job in victims:
        if job.task is None:
            continue
        job.task.cancel()
    for job in victims:
        task = job.task
        if task is None:
            continue
        try:
            await asyncio.wait_for(task, timeout=6.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("drain_cancelled_job_exception", extra={"job_id": job.id}, exc_info=True)


# ── Endpoints ────────────────────────────────────────────────────────────────


# /health is hit by load balancers, k8s liveness probes, the cockpit
# banner, and humans curling for sanity. None of those callers can wait
# 10 s for ``check_lmstudio`` (which is sync and does its own network
# round-trip). We call it on a worker thread with a hard wall-clock
# budget; a stalled LM Studio surfaces as ``status="timeout"`` instead
# of a hung probe that flaps the upstream service as unhealthy.
_HEALTH_LM_TIMEOUT_S = 2.5


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check() -> HealthResponse:
    """Verify system health and configuration.

    The handler is **async** so a slow ``check_lmstudio`` cannot tie up
    a sync threadpool slot indefinitely — we run it in ``to_thread`` and
    cap it at ``_HEALTH_LM_TIMEOUT_S``. Past the budget we return a
    structured "timeout" result; the rest of the payload (config presence,
    GitHub token state) is still useful even when LM Studio is down.
    """
    config = load_config()
    try:
        lm_state = await asyncio.wait_for(
            asyncio.to_thread(check_lmstudio, config, _HEALTH_LM_TIMEOUT_S),
            timeout=_HEALTH_LM_TIMEOUT_S + 0.5,
        )
        lm_payload: dict[str, object] = dataclasses.asdict(lm_state)
    except asyncio.TimeoutError:
        # Hard timeout: ``check_lmstudio``'s own httpx timeout should fire
        # first and return a clean ERROR result, but a hung DNS or kernel
        # half-open socket can still wedge the call. The outer
        # ``wait_for`` is the belt-and-braces guarantee.
        lm_payload = {
            "level": "error",
            "message": "LM Studio health check timed out",
            "detail": (
                f"check_lmstudio did not return within "
                f"{_HEALTH_LM_TIMEOUT_S + 0.5:.1f}s; the LLM endpoint is "
                f"unresponsive or the network is wedged."
            ),
            "available_models": [],
            "target_model_loaded": False,
        }
    return HealthResponse(
        status="ok",
        lm_studio=lm_payload,
        github_token_set=bool(config.github.token),
    )


@router.post(
    "/run",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
    responses={422: {"description": "Invalid repo_url or branch"}},
)
async def trigger_agent_run(req: RunRequest, request: Request) -> JobResponse:
    """Trigger a full autonomous run pipeline (Analyzer → Planner → Worker → PR).

    Supports ``resume`` and ``reset`` to let the cockpit recover from an
    orphaned state file without the user having to drop to the CLI. The
    two flags are mutually exclusive (validated in :class:`RunRequest`):

    * ``resume``: pass ``--resume`` to the CLI — picks up from the saved
      phase (analysis/plan are re-used, worker skips completed subtasks,
      PR reuses ``state.pr_number`` if already open).
    * ``reset``: pass ``--reset`` — deletes ``~/.gitoma/state/<slug>.json``
      before starting. Fresh plan on a repo that already had one.
    """
    await _enforce_dispatch_rate_limit(request)
    argv = [*_gitoma_cli_argv(), "run", req.repo_url, "--yes"]
    if req.branch:
        argv += ["--branch", req.branch]
    if req.dry_run:
        argv.append("--dry-run")
    if req.resume:
        argv.append("--resume")
    if req.reset:
        argv.append("--reset")
    label = "run-resume" if req.resume else "run-reset" if req.reset else "run"
    job = await _dispatch(label, argv)
    msg = (
        "Resuming autonomous run from last checkpoint." if req.resume else
        "Fresh autonomous run — existing state will be deleted." if req.reset else
        "Autonomous run dispatched in background."
    )
    return JobResponse(job_id=job.id, status="started", message=msg)


@router.post(
    "/fix-ci",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
    responses={400: {"description": "Missing branch"}},
)
async def trigger_fix_ci(req: RunRequest, request: Request) -> JobResponse:
    """Trigger the CIDiagnostic Agent (Reflexion Dual-Agent) to fix a CI breakage."""
    await _enforce_dispatch_rate_limit(request)
    if not req.branch:
        raise HTTPException(status_code=400, detail="branch is required for fix-ci")
    argv = [*_gitoma_cli_argv(), "fix-ci", req.repo_url, "--branch", req.branch]
    job = await _dispatch("fix-ci", argv)
    return JobResponse(job_id=job.id, status="started", message="CI Reflexion Agent dispatched in background.")


@router.post(
    "/analyze",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
)
async def trigger_analyze(req: AnalyzeRequest, request: Request) -> JobResponse:
    """Trigger a read-only analysis pass (no commits, no PR)."""
    await _enforce_dispatch_rate_limit(request)
    argv = [*_gitoma_cli_argv(), "analyze", req.repo_url]
    job = await _dispatch("analyze", argv)
    return JobResponse(job_id=job.id, status="started", message="Analysis dispatched in background.")


@router.post(
    "/review",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
)
async def trigger_review(req: ReviewRequest, request: Request) -> JobResponse:
    """Fetch Copilot review comments; optionally auto-integrate them."""
    await _enforce_dispatch_rate_limit(request)
    argv = [*_gitoma_cli_argv(), "review", req.repo_url]
    if req.integrate:
        argv.append("--integrate")
    job = await _dispatch("review", argv)
    msg = "Review integration dispatched." if req.integrate else "Review fetch dispatched."
    return JobResponse(job_id=job.id, status="started", message=msg)


@router.get(
    "/status/{job_id}",
    response_model=JobStatusResponse,
    tags=["jobs"],
    responses={404: {"description": "Job not found"}},
)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Poll the status of an asynchronous job.

    Clients may poll once a second or so; prefer ``/stream/{job_id}`` for
    push-based updates. ``error_id`` is populated when the job ended with
    a non-OK status — correlate it with server logs to diagnose.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job.id,
        label=job.label,
        status=job.status,
        created_at=job.created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        lines_buffered=len(job.lines),
        error_id=job.error_id,
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    tags=["jobs"],
    responses={
        404: {"description": "Job not found"},
        409: {"description": "Job already terminal"},
    },
)
async def cancel_job(job_id: str) -> CancelResponse:
    """Cancel a running job — SIGTERM the whole process group, then SIGKILL
    after a 5 s grace period. The response is immediate; the transition to
    ``cancelled`` is visible via ``/status/{job_id}`` or the SSE stream.

    Idempotent-ish: calling on an already-terminal job returns 409 so the
    client can distinguish "nothing to do" from "just cancelled".
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.is_terminal or job.task is None or job.task.done():
        raise HTTPException(status_code=409, detail=f"Job is already {job.status}")
    job.task.cancel()
    return CancelResponse(job_id=job.id, status="cancelling")


@router.get("/jobs", tags=["jobs"])
async def list_jobs() -> dict[str, dict[str, object]]:
    """List every in-memory job with its current status and a buffered-lines count.

    Holds ``_JOBS_LOCK`` while *snapshotting* the dict — never while doing
    I/O — so a concurrent ``_dispatch`` eviction can't mutate the dict
    mid-iteration. Today the comprehension is GIL-safe because nothing
    awaits inside it; this guard locks in that invariant against any
    future refactor that adds an ``await`` (e.g. enriching a job record
    with a live state lookup).
    """
    async with _JOBS_LOCK:
        snapshot = list(_JOBS.items())
    return {
        jid: {
            "status": job.status,
            "label": job.label,
            "lines": len(job.lines),
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error_id": job.error_id,
        }
        for jid, job in snapshot
    }


@router.get("/stream/{job_id}", tags=["jobs"])
async def stream_job_output(job_id: str) -> StreamingResponse:
    """SSE endpoint: stream the job's merged stdout/stderr line by line.

    Buffered lines are replayed on connect so late joiners see the full
    history up to the ring-buffer limit. A comment heartbeat frame is
    emitted every ``_SSE_HEARTBEAT_SECONDS`` so reverse-proxies don't
    drop the connection during quiet periods. The stream terminates when
    the job emits the end sentinel.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Subscriber cap: refuse the connection BEFORE building the response so
    # the client gets a clean 429 instead of an SSE that times out. The
    # set lookup is racy with concurrent unsubscribes — that's acceptable;
    # at worst we accept one extra subscriber under contention. We never
    # accept arbitrarily many, which is the actual DoS we care about.
    if len(job.subscribers) >= _MAX_SUBSCRIBERS_PER_JOB:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many concurrent subscribers for this job "
                f"(limit {_MAX_SUBSCRIBERS_PER_JOB})."
            ),
            headers={"Retry-After": "5"},
        )

    async def event_stream() -> AsyncIterator[str]:
        # Replay history first so the client can render whatever happened
        # before subscription.
        replayed_end = False
        for line in list(job.lines):
            yield _format_event("line", line)
            if line.startswith(_END_SENTINEL):
                replayed_end = True
                break
        if replayed_end:
            return

        q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_DEPTH)
        # Re-check the cap inside the generator — between the route-level
        # check above and here, other subscribers may have arrived. If we
        # blew the cap, drop the connection silently (the client gets an
        # empty stream + close, equivalent to a server-side hang-up).
        if len(job.subscribers) >= _MAX_SUBSCRIBERS_PER_JOB:
            return
        job.subscribers.add(q)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # No new lines — keep the connection alive so the proxy
                    # doesn't half-close on us. Comment lines are ignored
                    # by EventSource clients but count as traffic.
                    yield ": heartbeat\n\n"
                    continue
                yield _format_event("line", line)
                if line.startswith(_END_SENTINEL):
                    break
        except asyncio.CancelledError:
            # Client disconnected
            pass
        finally:
            job.subscribers.discard(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Disable proxy buffering if fronted by nginx.
            "X-Accel-Buffering": "no",
        },
    )


def _format_event(event: str, line: str) -> str:
    """Format a single SSE frame."""
    payload = json.dumps({"line": line})
    return f"event: {event}\ndata: {payload}\n\n"


@router.delete(
    "/state/{owner}/{name}",
    response_model=StateDeleteResponse,
    tags=["state"],
    responses={422: {"description": "Invalid owner or name"}},
)
def reset_repo_state(owner: str, name: str) -> StateDeleteResponse:
    """Delete the persisted agent state for a repo (idempotent).

    ``owner``/``name`` flow into a filesystem path downstream, so anything
    outside the GitHub-style charset is rejected here with 422 — never
    forwarded to ``_delete_state``. Without this guard a percent-encoded
    traversal (``..%2F..%2Fetc%2Fpasswd``) decoded after route matching
    could let an authenticated client unlink files outside the state dir.
    """
    if not _STATE_SLUG_RE.match(owner) or not _STATE_SLUG_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="owner and name must match [A-Za-z0-9._-]{1,100} and not start with a dot",
        )
    existed = _load_state(owner, name) is not None
    _delete_state(owner, name)
    return StateDeleteResponse(
        owner=owner,
        name=name,
        result="deleted" if existed else "not_found",
    )
