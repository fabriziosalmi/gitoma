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
from gitoma.planner.llm_client import check_lmstudio
from gitoma.review.reflexion import CIDiagnosticAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


class RunRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None
    dry_run: bool = False


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


def _run_autonomous_agent(repo_url: str, branch: Optional[str], dry_run: bool, job_id: str) -> None:
    """Spawn the `gitoma run` CLI in a subprocess and track its status.

    Subprocess isolation sidesteps typer.Exit() propagating into the background
    worker thread and keeps the API process alive across pipeline failures.
    """
    _JOBS[job_id] = "running"
    argv = [*_gitoma_cli_argv(), "run", repo_url, "--yes"]
    if branch:
        argv += ["--branch", branch]
    if dry_run:
        argv.append("--dry-run")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception("Autonomous run job %s could not start", job_id)
        _JOBS[job_id] = f"failed: {exc}"
        return

    if proc.returncode == 0:
        _JOBS[job_id] = "completed"
    else:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        _JOBS[job_id] = f"failed (rc={proc.returncode}): {' | '.join(tail)[:400]}"


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


@router.get("/status/{job_id}")
def get_job_status(job_id: str) -> dict[str, str]:
    """Poll the status of an asynchronous job."""
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": _JOBS[job_id]}
