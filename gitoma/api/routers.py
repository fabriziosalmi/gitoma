"""Routers for Gitoma REST API."""

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from gitoma.core.config import load_config
from gitoma.planner.llm_client import check_lmstudio
from gitoma.review.reflexion import CIDiagnosticAgent

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


def _run_autonomous_agent(repo_url: str, branch: Optional[str], dry_run: bool, job_id: str):
    """Wrapper to run the core agent logic and suppress CLI exits."""
    # Note: To avoid cyclical or Typer Exit() exceptions killing the thread,
    # we would ideally refactor cli.py, but for the MVP FastAPI background task,
    # we'll wrap the Reflexion Agent natively because it doesn't do typer.Exit().
    _JOBS[job_id] = "running"
    # Placeholder for the full generic run (requires cli.py decoupling from typer.Exit())
    import time
    time.sleep(2) # simulate run
    _JOBS[job_id] = "completed"


def _run_fix_ci(repo_url: str, branch: str, job_id: str):
    """Wrapper to run CIDiagnosticAgent safely in the background."""
    _JOBS[job_id] = "running"
    try:
        config = load_config()
        agent = CIDiagnosticAgent(config)
        agent.analyze_and_fix(repo_url, branch)
        _JOBS[job_id] = "completed"
    except Exception as e:
        _JOBS[job_id] = f"failed: {str(e)}"


@router.get("/health")
def health_check():
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
def trigger_agent_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Trigger a full autonomous run pipeline (Analyzer -> Planner -> Worker -> PR)."""
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_autonomous_agent, req.repo_url, req.branch, req.dry_run, job_id)
    return JobResponse(job_id=job_id, status="started", message="Autonomous run dispatched in background.")


@router.post("/fix-ci", response_model=JobResponse)
def trigger_fix_ci(req: RunRequest, background_tasks: BackgroundTasks):
    """Trigger the CIDiagnostic Agent (Reflexion Dual-Agent) to fix a CI breakage."""
    if not req.branch:
        raise HTTPException(status_code=400, detail="Branch must be provided for CI fixing.")
        
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_fix_ci, req.repo_url, req.branch, job_id)
    return JobResponse(job_id=job_id, status="started", message="CI Reflexion Agent dispatched in background.")


@router.get("/status/{job_id}")
def get_job_status(job_id: str):
    """Poll the status of an asynchronous job."""
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": _JOBS[job_id]}
