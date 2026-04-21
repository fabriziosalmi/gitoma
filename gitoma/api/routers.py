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
import json
import logging
import os
import re
import shutil
import signal
import sys
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, HTTPException
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
    """Return ``os.environ`` minus the server's own secrets.

    The spawned CLI doesn't need (and mustn't see) the API's Bearer token —
    that's a secret of the HTTP surface, not of the agent. If the CLI ever
    dumped ``os.environ`` into a trace file, we'd be leaking the server's
    auth credential by proxy.
    """
    return {k: v for k, v in os.environ.items() if k not in _SECRET_ENV_VARS}


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

    # POSIX: create a new session so the whole process tree gets SIGTERM
    # on cancel (``os.killpg``). On Windows we skip — no setsid, and our
    # cancel path doesn't support killing process trees there anyway.
    preexec_fn = os.setsid if sys.platform != "win32" else None

    try:
        proc = await asyncio.create_subprocess_exec(
            *job.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=_scrubbed_env(),
            preexec_fn=preexec_fn,
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

    assert proc.stdout is not None
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
        # 5 s, then SIGKILL. Always set a terminal status + end sentinel.
        _kill_process_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _kill_process_group(proc, signal.SIGKILL)
            with suppress(asyncio.CancelledError):
                await proc.wait()
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


def _kill_process_group(proc: asyncio.subprocess.Process, sig: int = signal.SIGTERM) -> None:
    """Best-effort kill of the whole process group.

    We spawned with ``setsid`` on POSIX; its pgid == pid. On Windows the
    ``preexec_fn`` was not applied, so we fall back to ``proc.kill`` /
    ``proc.terminate`` semantics which only target the immediate child.
    Either way we swallow ProcessLookupError — the child may already be
    gone, which is fine.
    """
    if sys.platform == "win32" or proc.pid is None:
        with suppress(ProcessLookupError):
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        return
    with suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), sig)


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
        victims = [job for job in _JOBS.values() if job.task and not job.task.done()]
    for job in victims:
        assert job.task is not None
        job.task.cancel()
    for job in victims:
        assert job.task is not None
        try:
            await asyncio.wait_for(job.task, timeout=6.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("drain_cancelled_job_exception", extra={"job_id": job.id}, exc_info=True)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health_check() -> HealthResponse:
    """Verify system health and configuration."""
    config = load_config()
    lm_state = check_lmstudio(config)
    return HealthResponse(
        status="ok",
        lm_studio=dataclasses.asdict(lm_state),
        github_token_set=bool(config.github.token),
    )


@router.post(
    "/run",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
    responses={422: {"description": "Invalid repo_url or branch"}},
)
async def trigger_agent_run(req: RunRequest) -> JobResponse:
    """Trigger a full autonomous run pipeline (Analyzer → Planner → Worker → PR)."""
    argv = [*_gitoma_cli_argv(), "run", req.repo_url, "--yes"]
    if req.branch:
        argv += ["--branch", req.branch]
    if req.dry_run:
        argv.append("--dry-run")
    job = await _dispatch("run", argv)
    return JobResponse(job_id=job.id, status="started", message="Autonomous run dispatched in background.")


@router.post(
    "/fix-ci",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
    responses={400: {"description": "Missing branch"}},
)
async def trigger_fix_ci(req: RunRequest) -> JobResponse:
    """Trigger the CIDiagnostic Agent (Reflexion Dual-Agent) to fix a CI breakage."""
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
async def trigger_analyze(req: AnalyzeRequest) -> JobResponse:
    """Trigger a read-only analysis pass (no commits, no PR)."""
    argv = [*_gitoma_cli_argv(), "analyze", req.repo_url]
    job = await _dispatch("analyze", argv)
    return JobResponse(job_id=job.id, status="started", message="Analysis dispatched in background.")


@router.post(
    "/review",
    response_model=JobResponse,
    status_code=202,
    tags=["jobs"],
)
async def trigger_review(req: ReviewRequest) -> JobResponse:
    """Fetch Copilot review comments; optionally auto-integrate them."""
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
def list_jobs() -> dict[str, dict[str, object]]:
    """List every in-memory job with its current status and a buffered-lines count."""
    return {
        jid: {
            "status": job.status,
            "label": job.label,
            "lines": len(job.lines),
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error_id": job.error_id,
        }
        for jid, job in _JOBS.items()
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
)
def reset_repo_state(owner: str, name: str) -> StateDeleteResponse:
    """Delete the persisted agent state for a repo (idempotent)."""
    existed = _load_state(owner, name) is not None
    _delete_state(owner, name)
    return StateDeleteResponse(
        owner=owner,
        name=name,
        result="deleted" if existed else "not_found",
    )
