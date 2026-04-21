"""Tests for the command-dispatch endpoints that back the cockpit UI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gitoma.api.server import app

client = TestClient(app)


HEADERS = {"Authorization": "Bearer TOKEN"}


def _mock_token(mocker):
    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"


@pytest.fixture(autouse=True)
def _neutralize_spawn(mocker):
    """Prevent tests from actually spawning `gitoma` subprocesses.

    Every dispatch endpoint schedules `_spawn_cli_job` on the event loop, so
    we replace it with a no-op coroutine for the whole module.
    """
    async def _noop(job):
        return None

    mocker.patch("gitoma.api.routers._spawn_cli_job", side_effect=_noop)


# ── analyze ──────────────────────────────────────────────────────────────


def test_analyze_dispatches_job(mocker):
    _mock_token(mocker)

    resp = client.post(
        "/api/v1/analyze",
        json={"repo_url": "https://github.com/mock/repo"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "started"
    assert "job_id" in data


def test_analyze_rejects_unauthenticated(mocker):
    _mock_token(mocker)
    resp = client.post("/api/v1/analyze", json={"repo_url": "https://github.com/mock/repo"})
    assert resp.status_code == 401


# ── review ───────────────────────────────────────────────────────────────


def test_review_dispatches_job_without_integrate(mocker):
    _mock_token(mocker)

    resp = client.post(
        "/api/v1/review",
        json={"repo_url": "https://github.com/mock/repo"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    assert "Review fetch" in resp.json()["message"]


def test_review_with_integrate_flag(mocker):
    _mock_token(mocker)

    resp = client.post(
        "/api/v1/review",
        json={"repo_url": "https://github.com/mock/repo", "integrate": True},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    assert "integration" in resp.json()["message"]


# ── jobs ─────────────────────────────────────────────────────────────────


def test_jobs_lists_tracked_background_jobs(mocker):
    _mock_token(mocker)
    from gitoma.api.routers import JobRecord

    fake = {
        "abc": JobRecord(id="abc", label="run", argv=["a"], status="running"),
        "def": JobRecord(id="def", label="analyze", argv=["a"], status="completed"),
    }
    mocker.patch.dict("gitoma.api.routers._JOBS", fake, clear=True)

    resp = client.get("/api/v1/jobs", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["abc"]["status"] == "running"
    assert data["abc"]["label"] == "run"
    assert data["def"]["status"] == "completed"


# ── state reset ──────────────────────────────────────────────────────────


def test_reset_state_deletes_existing(mocker):
    _mock_token(mocker)
    mocker.patch("gitoma.api.routers._load_state", return_value=object())
    delete = mocker.patch("gitoma.api.routers._delete_state")

    resp = client.delete("/api/v1/state/mock/repo", headers=HEADERS)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["result"] == "deleted"
    assert payload["owner"] == "mock"
    delete.assert_called_once_with("mock", "repo")


def test_reset_state_is_idempotent_when_missing(mocker):
    _mock_token(mocker)
    mocker.patch("gitoma.api.routers._load_state", return_value=None)
    mocker.patch("gitoma.api.routers._delete_state")

    resp = client.delete("/api/v1/state/mock/repo", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["result"] == "not_found"


def test_reset_state_requires_auth(mocker):
    _mock_token(mocker)
    resp = client.delete("/api/v1/state/mock/repo")
    assert resp.status_code == 401


# ── dashboard regression ─────────────────────────────────────────────────


def test_dashboard_wires_command_buttons():
    """Every command tile should be present and addressable by JS (data-cmd=*)."""
    body = client.get("/").text
    for cmd in ("run", "analyze", "review", "fix-ci"):
        assert f'data-cmd="{cmd}"' in body


def test_dashboard_ships_dialogs_for_each_command():
    body = client.get("/").text
    for dialog_id in ("run-dialog", "analyze-dialog", "review-dialog", "fixci-dialog",
                      "token-dialog", "confirm-dialog"):
        assert f'id="{dialog_id}"' in body


def test_dashboard_has_mobile_viewport():
    body = client.get("/").text
    assert 'viewport' in body
    assert 'width=device-width' in body


def test_dashboard_ships_current_op_and_task_plan_widgets():
    """Current-op row + task-plan card make run progress visible between
    coarse phase transitions. Regression guard for the `cosa sta facendo?` UX fix."""
    body = client.get("/").text
    # Current operation row (shows what the agent is doing right now)
    assert 'id="current-op-row"' in body
    assert 'id="current-op-text"' in body
    assert 'id="current-op-age"' in body
    # Task plan card (shows the full plan with per-task status)
    assert 'id="task-plan-card"' in body
    assert 'id="task-list"' in body
    # JS hooks
    assert "renderCurrentOp" in body
    assert "renderTaskPlan" in body


def test_dashboard_ships_agents_and_errors_widgets():
    """Agents card + Errors banner. Regression guard for the
    `cosa non torna nel giro?` visibility fix."""
    body = client.get("/").text
    # Agents card (Analyzer / Planner / Worker / PR Agent / Reviewer)
    assert 'id="agents-card"' in body
    assert 'id="agents-row"' in body
    assert "renderAgents" in body
    # Errors banner (shows state.errors when persisted by _abort/_phase)
    assert 'id="errors-banner"' in body
    assert 'id="errors-list"' in body
    assert "renderErrors" in body
    # Orphan banner (shows when CLI process is gone mid-run)
    assert 'id="orphan-banner"' in body
    assert "renderOrphan" in body
