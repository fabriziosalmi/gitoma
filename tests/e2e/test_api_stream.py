"""Tests for the SSE live-log streaming endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gitoma.api.server import app
from gitoma.api.routers import JobRecord, _JOBS, _publish

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


def _parse_sse_data(body: str) -> list[str]:
    """Extract each `data: {...}` line from an SSE body."""
    out = []
    for ln in body.splitlines():
        if ln.startswith("data:"):
            out.append(ln[len("data:"):].strip())
    return out


def test_stream_replays_buffered_history_when_job_done():
    """A finished job's history is replayed on connect, then the stream ends."""
    job = JobRecord(id="j1", label="run", argv=["x"])
    _JOBS["j1"] = job
    _publish(job, "hello")
    _publish(job, "world")
    _publish(job, "__END__:completed")

    resp = client.get("/api/v1/stream/j1", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse_data(resp.text)
    # At least the three buffered lines should be replayed
    assert any("hello" in f for f in frames)
    assert any("world" in f for f in frames)
    assert any("__END__" in f for f in frames)


def test_stream_404_for_unknown_job():
    resp = client.get("/api/v1/stream/nope", headers=HEADERS)
    assert resp.status_code == 404


def test_stream_requires_auth():
    # No headers → unauthenticated → 401 from the shared Bearer dep
    resp = client.get("/api/v1/stream/any")
    assert resp.status_code == 401
