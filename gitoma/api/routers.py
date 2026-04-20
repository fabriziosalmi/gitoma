"""Routers for Gitoma REST API."""

import logging
import shutil
import subprocess
import sys
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from gitoma.core.config import load_config
from gitoma.core.state import delete_state as _delete_state
from gitoma.core.state import load_state as _load_state
from gitoma.planner.llm_client import check_lmstudio
from gitoma.review.reflexion import CIDiagnosticAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


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


# Basic in-memory job tracker for the Background Tasks
# In a real enterprise system this would be Redis/Celery.
_JOBS: dict[str, str] = {}


def _gitoma_cli_argv() -> list[str]:
    """Resolve how to invoke the gitoma CLI in a subprocess.

    Prefers the `gitoma` entrypoint if on PATH (installed via pip), otherwise
    falls back to `python -m gitoma.cli` using the current interpreter.
    """
    exe = shutil.which("gitoma")
    if exe:
        return [exe]
    return [sys.executable, "-m", "gitoma.cli"]


def _run_cli_job(argv: list[str], job_id: str, label: str) -> None:
    """Shared subprocess runner for CLI-backed background jobs.

    Captures full stdout/stderr and stores the outcome in the in-memory job
    tracker. On non-zero exit, embeds a short tail of the output so the
    client can surface a meaningful failure message.
    """
    _JOBS[job_id] = "running"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception("%s job %s could not start", label, job_id)
        _JOBS[job_id] = f"failed: {exc}"
        return

    if proc.returncode == 0:
        _JOBS[job_id] = "completed"
    else:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        _JOBS[job_id] = f"failed (rc={proc.returncode}): {' | '.join(tail)[:400]}"


def _run_autonomous_agent(repo_url: str, branch: Optional[str], dry_run: bool, job_id: str) -> None:
    """Spawn the `gitoma run` CLI in a subprocess and track its status.

    Subprocess isolation sidesteps typer.Exit() propagating into the background
    worker thread and keeps the API process alive across pipeline failures.
    """
    argv = [*_gitoma_cli_argv(), "run", repo_url, "--yes"]
    if branch:
        argv += ["--branch", branch]
    if dry_run:
        argv.append("--dry-run")
    _run_cli_job(argv, job_id, "run")


def _run_analyze(repo_url: str, job_id: str) -> None:
    """Spawn `gitoma analyze <repo_url>` and track outcome."""
    argv = [*_gitoma_cli_argv(), "analyze", repo_url]
    _run_cli_job(argv, job_id, "analyze")


def _run_review(repo_url: str, integrate: bool, job_id: str) -> None:
    """Spawn `gitoma review <repo_url> [--integrate]` and track outcome."""
    argv = [*_gitoma_cli_argv(), "review", repo_url]
    if integrate:
        argv.append("--integrate")
    _run_cli_job(argv, job_id, "review")


def _run_fix_ci(repo_url: str, branch: str, job_id: str) -> None:
    """Run CIDiagnosticAgent in the background and report status."""
    _JOBS[job_id] = "running"
    try:
        config = load_config()
        agent = CIDiagnosticAgent(config)
        agent.analyze_and_fix(repo_url, branch)
        _JOBS[job_id] = "completed"
    except Exception as exc:
        logger.exception("Fix-CI job %s failed", job_id)
        _JOBS[job_id] = f"failed: {exc}"


@router.get("/health")
def health_check() -> dict[str, object]:
    """Verify system health and configuration."""
    config = load_config()
    lm_state = check_lmstudio(config)
    import dataclasses
    return {
        "status": "ok",
        "lm_studio": dataclasses.asdict(lm_state),
        "github_token_set": bool(config.github.token),
    }


@router.post("/run", response_model=JobResponse)
def trigger_agent_run(req: RunRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """Trigger a full autonomous run pipeline (Analyzer -> Planner -> Worker -> PR)."""
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_autonomous_agent, req.repo_url, req.branch, req.dry_run, job_id)
    return JobResponse(job_id=job_id, status="started", message="Autonomous run dispatched in background.")


@router.post("/fix-ci", response_model=JobResponse)
def trigger_fix_ci(req: RunRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """Trigger the CIDiagnostic Agent (Reflexion Dual-Agent) to fix a CI breakage."""
    if not req.branch:
        raise HTTPException(status_code=400, detail="Branch must be provided for CI fixing.")

    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_fix_ci, req.repo_url, req.branch, job_id)
    return JobResponse(job_id=job_id, status="started", message="CI Reflexion Agent dispatched in background.")


@router.post("/analyze", response_model=JobResponse)
def trigger_analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """Trigger a read-only analysis pass (no commits, no PR)."""
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_analyze, req.repo_url, job_id)
    return JobResponse(job_id=job_id, status="started", message="Analysis dispatched in background.")


@router.post("/review", response_model=JobResponse)
def trigger_review(req: ReviewRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """Fetch Copilot review comments; optionally auto-integrate them."""
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_review, req.repo_url, req.integrate, job_id)
    msg = "Review integration dispatched." if req.integrate else "Review fetch dispatched."
    return JobResponse(job_id=job_id, status="started", message=msg)


@router.get("/status/{job_id}")
def get_job_status(job_id: str) -> dict[str, str]:
    """Poll the status of an asynchronous job."""
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": _JOBS[job_id]}


@router.get("/jobs")
def list_jobs() -> dict[str, dict[str, str]]:
    """List every in-memory job and its current status."""
    return {jid: {"status": status} for jid, status in _JOBS.items()}


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
