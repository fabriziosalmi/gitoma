"""Tests for job eviction, cancellation and shutdown hygiene."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from gitoma.api.server import app
from gitoma.api.routers import (
    JOB_TTL_SECONDS,
    MAX_JOBS,
    JobRecord,
    _evict_stale,
    _JOBS,
    cancel_all_jobs,
)

client = TestClient(app)
HEADERS = {"Authorization": "Bearer TOKEN"}


@pytest.fixture(autouse=True)
def _mock_auth(mocker):
    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"


@pytest.fixture(autouse=True)
def _clean_jobs():
    _JOBS.clear()
    yield
    _JOBS.clear()


def _make_job(status: str, finished_minutes_ago: float | None = None) -> JobRecord:
    now = dt.datetime.now(dt.timezone.utc)
    finished_at = None
    if finished_minutes_ago is not None:
        finished_at = now - dt.timedelta(minutes=finished_minutes_ago)
    return JobRecord(
        id=f"j-{status}-{finished_minutes_ago}",
        label="run",
        argv=["x"],
        status=status,
        finished_at=finished_at,
    )


# ── TTL eviction ─────────────────────────────────────────────────────────────


def test_evict_stale_drops_old_terminal_jobs():
    fresh = _make_job("completed", finished_minutes_ago=1)
    stale = _make_job("completed", finished_minutes_ago=JOB_TTL_SECONDS / 60 + 5)
    _JOBS[fresh.id] = fresh
    _JOBS[stale.id] = stale

    _evict_stale()

    assert fresh.id in _JOBS
    assert stale.id not in _JOBS


def test_evict_stale_never_touches_running_jobs():
    running = JobRecord(id="r", label="run", argv=["x"], status="running")
    _JOBS[running.id] = running

    _evict_stale()

    assert running.id in _JOBS


def test_evict_stale_enforces_hard_cap_when_saturated():
    # Fill with MAX_JOBS + 5 terminal jobs, all within TTL.
    for i in range(MAX_JOBS + 5):
        job = JobRecord(
            id=f"t{i}",
            label="run",
            argv=["x"],
            status="completed",
            finished_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=i),
        )
        _JOBS[job.id] = job

    _evict_stale()

    assert len(_JOBS) == MAX_JOBS
    # The oldest (largest index since we went now-i, so bigger i = further in the past)
    # should have been dropped.
    assert f"t{MAX_JOBS + 4}" not in _JOBS


# ── Cancel endpoint ──────────────────────────────────────────────────────────


def test_cancel_404_on_unknown_job():
    resp = client.post("/api/v1/jobs/missing/cancel", headers=HEADERS)
    assert resp.status_code == 404


def test_cancel_409_on_terminal_job():
    done = _make_job("completed", finished_minutes_ago=1)
    _JOBS[done.id] = done

    resp = client.post(f"/api/v1/jobs/{done.id}/cancel", headers=HEADERS)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_endpoint_signals_running_task():
    """Tests the cancel handler against a real asyncio task in the same loop.

    TestClient isn't usable here: each HTTP call runs on a fresh event loop
    and discards `asyncio.create_task` work from the previous request, so
    the task would always be `.done()` by the time cancel hits it.
    """
    from gitoma.api.routers import cancel_job

    async def _long() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_long())
    job = JobRecord(id="run-abc", label="run", argv=["x"], status="running", task=task)
    _JOBS[job.id] = job

    result = await cancel_job(job.id)
    assert result["status"] == "cancelling"

    # Give the loop one tick to propagate the cancellation.
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


# ── Shutdown hook ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_jobs_cancels_pending_tasks():
    async def _long():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_long())
    job = JobRecord(id="lifespan-one", label="run", argv=["x"], status="running", task=task)
    _JOBS[job.id] = job

    await cancel_all_jobs()

    assert task.done()
    assert task.cancelled() or task.result() is None
