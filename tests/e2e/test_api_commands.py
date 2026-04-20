"""Tests for the command-dispatch endpoints that back the cockpit UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gitoma.api.server import app

client = TestClient(app)


HEADERS = {"Authorization": "Bearer TOKEN"}


def _mock_token(mocker):
    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"


def _stub_subprocess(mocker, returncode=0, stdout="ok", stderr=""):
    stub = mocker.MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
    mocker.patch("gitoma.api.routers.subprocess.run", return_value=stub)


# ── analyze ──────────────────────────────────────────────────────────────


def test_analyze_dispatches_job(mocker):
    _mock_token(mocker)
    _stub_subprocess(mocker)

    resp = client.post(
        "/api/v1/analyze",
        json={"repo_url": "https://github.com/mock/repo"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
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
    _stub_subprocess(mocker)

    resp = client.post(
        "/api/v1/review",
        json={"repo_url": "https://github.com/mock/repo"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert "Review fetch" in resp.json()["message"]


def test_review_with_integrate_flag(mocker):
    _mock_token(mocker)
    _stub_subprocess(mocker)

    resp = client.post(
        "/api/v1/review",
        json={"repo_url": "https://github.com/mock/repo", "integrate": True},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert "integration" in resp.json()["message"]


# ── jobs ─────────────────────────────────────────────────────────────────


def test_jobs_lists_tracked_background_jobs(mocker):
    _mock_token(mocker)
    mocker.patch.dict("gitoma.api.routers._JOBS", {"abc": "running", "def": "completed"})

    resp = client.get("/api/v1/jobs", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["abc"]["status"] == "running"
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
