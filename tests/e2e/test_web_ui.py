"""Tests for the public web cockpit (/, /ws/state)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gitoma.api.server import app

client = TestClient(app)


def test_dashboard_is_public_and_returns_html():
    """GET / is reachable without a Bearer token and returns the cockpit HTML."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Gitoma" in body
    assert "/ws/state" in body  # frontend wires up the WS endpoint
    assert "<svg" in body  # icon sprite present — dashboard uses inline SVGs, no emoji


def test_ws_state_pushes_snapshot_on_connect(monkeypatch):
    """The WS immediately sends a full state snapshot read from disk."""
    fake_state = {
        "repo_url": "https://github.com/mock/repo",
        "owner": "mock",
        "name": "repo",
        "branch": "gitoma/demo",
        "phase": "WORKING",
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
    }
    monkeypatch.setattr(
        "gitoma.api.web._snapshot_states",
        lambda: [fake_state],
    )

    with client.websocket_connect("/ws/state") as ws:
        frame = ws.receive_text()
    payload = json.loads(frame)
    assert isinstance(payload, list)
    assert payload[0]["phase"] == "WORKING"
    assert payload[0]["owner"] == "mock"


def test_ws_state_handles_empty_state_dir(monkeypatch):
    """No state files on disk → WS emits an empty list, never errors out."""
    monkeypatch.setattr("gitoma.api.web._snapshot_states", lambda: [])
    with client.websocket_connect("/ws/state") as ws:
        frame = ws.receive_text()
    assert json.loads(frame) == []


def test_snapshot_states_skips_unreadable_files(tmp_path, monkeypatch):
    """Malformed JSON files should be skipped without raising."""
    monkeypatch.setattr("gitoma.api.web.STATE_DIR", tmp_path)
    (tmp_path / "good.json").write_text(json.dumps({"phase": "IDLE"}))
    (tmp_path / "bad.json").write_text("{not-json}")

    from gitoma.api.web import _snapshot_states

    states = _snapshot_states()
    assert len(states) == 1
    assert states[0]["phase"] == "IDLE"
