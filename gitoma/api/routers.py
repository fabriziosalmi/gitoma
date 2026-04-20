"""Routers for Gitoma REST API.

The agent commands (`run`, `analyze`, `review`, `fix-ci`) all dispatch the
corresponding `gitoma` CLI subcommand as an **async** subprocess. Each spawn
is tracked by a :class:`JobRecord` that holds a ring buffer of recent output
plus a set of asyncio queue subscribers. The `/api/v1/stream/{job_id}` SSE
endpoint lets any HTTP client tail the job's output line-by-line in real
time (used by the cockpit live-log panel).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import logging
import shutil
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from gitoma.core.config import load_config
from gitoma.core.state import delete_state as _delete_state
from gitoma.core.state import load_state as _load_state
from gitoma.planner.llm_client import check_lmstudio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ── Request / response schemas ───────────────────────────────────────────────


class RunRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None
    dry_run: bool = False


class ReviewRequest(BaseModel):
    repo_url: str
    integrate: bool = False


class AnalyzeRequest(BaseModel):
    repo_url: str


class JobResponse(BaseModel):
    job_id: str
    status: str
    message: str


# ── Job tracking ─────────────────────────────────────────────────────────────

_LOG_BUFFER_LINES = 500
_SUBSCRIBER_QUEUE_DEPTH = 1000
_END_SENTINEL = "__END__"

# Long-lived server hygiene: we never want _JOBS to grow unboundedly, so
# finished records are evicted after this TTL or when the cap is exceeded
# (oldest finished first; running jobs are never evicted).
MAX_JOBS = 50
JOB_TTL_SECONDS = 900  # 15 min


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

    @property
    def is_terminal(self) -> bool:
        return self.status not in ("queued", "running")


# Keyed by job_id. In production this would be a durable store.
_JOBS: dict[str, JobRecord] = {}


def _evict_stale() -> None:
    """Drop finished jobs past their TTL, then enforce the hard cap.

    Running jobs are never evicted — they are still producing output and may
    have active SSE subscribers. Only finished/failed/cancelled records are
    eligible. Called lazily from `_dispatch` so every new job pays for a
    single small sweep rather than needing a background timer.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(seconds=JOB_TTL_SECONDS)
    # 1. TTL eviction
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


# ── Pub/sub plumbing for live log streaming ─────────────────────────────────


def _publish(job: JobRecord, line: str) -> None:
    """Append to the ring buffer and fan out to every active subscriber.

    A slow/stuck consumer (full queue) is dropped rather than blocking the
    producer, so one stale client can't stall the job.
    """
    job.lines.append(line)
    dead: list["asyncio.Queue[str]"] = []
    for q in job.subscribers:
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        job.subscribers.discard(q)


async def _spawn_cli_job(job: JobRecord) -> None:
    """Run the CLI subprocess for this job and stream its output.

    stdout+stderr are merged into a single stream so timing between the two
    is preserved. Each decoded line is broadcast via `_publish`. A terminal
    sentinel is always emitted so SSE consumers know to close the stream.
    """
    job.status = "running"
    _publish(job, f"$ {' '.join(job.argv)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *job.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        logger.exception("Job %s (%s) could not start", job.id, job.label)
        job.status = f"failed: {exc}"
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"[error] {exc}")
        _publish(job, f"{_END_SENTINEL}:{job.status}")
        return

    assert proc.stdout is not None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            _publish(job, line)
        rc = await proc.wait()
    except asyncio.CancelledError:
        # Cancel requested (via /cancel or server shutdown): SIGTERM the
        # subprocess, give it 5 s to exit, then SIGKILL. Always set a
        # terminal status + end sentinel so subscribers don't hang.
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            try:
                await proc.wait()
            except asyncio.CancelledError:
                pass
        job.status = "cancelled"
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"{_END_SENTINEL}:cancelled")
        raise
    except Exception as exc:
        logger.exception("Job %s (%s) crashed", job.id, job.label)
        job.status = f"failed: {exc}"
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _publish(job, f"[error] {exc}")
        _publish(job, f"{_END_SENTINEL}:{job.status}")
        return

    job.status = "completed" if rc == 0 else f"failed (rc={rc})"
    job.finished_at = _dt.datetime.now(_dt.timezone.utc)
    _publish(job, f"{_END_SENTINEL}:{job.status}")


def _dispatch(label: str, argv: list[str]) -> JobRecord:
    """Register a new job and schedule it on the event loop."""
    _evict_stale()
    job_id = str(uuid.uuid4())
    job = JobRecord(id=job_id, label=label, argv=argv)
    _JOBS[job_id] = job
    job.task = asyncio.create_task(_spawn_cli_job(job))
    return job


async def cancel_all_jobs() -> None:
    """Cancel every running task and wait briefly for cleanup.

    Called from the app lifespan shutdown handler so the server exits
    without leaving orphan gitoma subprocesses behind.
    """
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
            logger.debug("Exception while draining cancelled job %s", job.id, exc_info=True)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/health")
def health_check() -> dict[str, object]:
    """Verify system health and configuration."""
    config = load_config()
    lm_state = check_lmstudio(config)
    return {
        "status": "ok",
        "lm_studio": dataclasses.asdict(lm_state),
        "github_token_set": bool(config.github.token),
    }


@router.post("/run", response_model=JobResponse)
async def trigger_agent_run(req: RunRequest) -> JobResponse:
    """Trigger a full autonomous run pipeline (Analyzer → Planner → Worker → PR)."""
    argv = [*_gitoma_cli_argv(), "run", req.repo_url, "--yes"]
    if req.branch:
        argv += ["--branch", req.branch]
    if req.dry_run:
        argv.append("--dry-run")
    job = _dispatch("run", argv)
    return JobResponse(job_id=job.id, status="started", message="Autonomous run dispatched in background.")


@router.post("/fix-ci", response_model=JobResponse)
async def trigger_fix_ci(req: RunRequest) -> JobResponse:
    """Trigger the CIDiagnostic Agent (Reflexion Dual-Agent) to fix a CI breakage."""
    if not req.branch:
        raise HTTPException(status_code=400, detail="Branch must be provided for CI fixing.")
    argv = [*_gitoma_cli_argv(), "fix-ci", req.repo_url, "--branch", req.branch]
    job = _dispatch("fix-ci", argv)
    return JobResponse(job_id=job.id, status="started", message="CI Reflexion Agent dispatched in background.")


@router.post("/analyze", response_model=JobResponse)
async def trigger_analyze(req: AnalyzeRequest) -> JobResponse:
    """Trigger a read-only analysis pass (no commits, no PR)."""
    argv = [*_gitoma_cli_argv(), "analyze", req.repo_url]
    job = _dispatch("analyze", argv)
    return JobResponse(job_id=job.id, status="started", message="Analysis dispatched in background.")


@router.post("/review", response_model=JobResponse)
async def trigger_review(req: ReviewRequest) -> JobResponse:
    """Fetch Copilot review comments; optionally auto-integrate them."""
    argv = [*_gitoma_cli_argv(), "review", req.repo_url]
    if req.integrate:
        argv.append("--integrate")
    job = _dispatch("review", argv)
    msg = "Review integration dispatched." if req.integrate else "Review fetch dispatched."
    return JobResponse(job_id=job.id, status="started", message=msg)


@router.get("/status/{job_id}")
def get_job_status(job_id: str) -> dict[str, str]:
    """Poll the status of an asynchronous job."""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job.id, "status": job.status, "label": job.label}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel a running job — SIGTERM the subprocess, set status=cancelled.

    Idempotent-ish: calling on an already-terminal job returns 409 so the
    client can distinguish "nothing to do" from "just cancelled".
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.is_terminal or job.task is None or job.task.done():
        raise HTTPException(status_code=409, detail=f"Job is already {job.status}")
    job.task.cancel()
    return {"job_id": job.id, "status": "cancelling"}


@router.get("/jobs")
def list_jobs() -> dict[str, dict[str, object]]:
    """List every in-memory job with its current status and a buffered-lines count."""
    return {
        jid: {
            "status": job.status,
            "label": job.label,
            "lines": len(job.lines),
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }
        for jid, job in _JOBS.items()
    }


@router.get("/stream/{job_id}")
async def stream_job_output(job_id: str) -> StreamingResponse:
    """SSE endpoint: stream the job's merged stdout/stderr line by line.

    Buffered lines are replayed on connect so late joiners see the full
    history up to the ring-buffer limit. The stream terminates when the
    job emits the end sentinel.
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
                line = await q.get()
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
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering if fronted by nginx
        },
    )


def _format_event(event: str, line: str) -> str:
    """Format a single SSE frame."""
    payload = json.dumps({"line": line})
    return f"event: {event}\ndata: {payload}\n\n"


@router.delete("/state/{owner}/{name}")
def reset_repo_state(owner: str, name: str) -> dict[str, str]:
    """Delete the persisted agent state for a repo (idempotent)."""
    existed = _load_state(owner, name) is not None
    _delete_state(owner, name)
    return {
        "owner": owner,
        "name": name,
        "result": "deleted" if existed else "not_found",
    }
