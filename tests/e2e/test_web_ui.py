"""Tests for the public web cockpit (/, /ws/state).

The /ws/state WebSocket requires the same Bearer token as the REST API
when one is configured — see test_api_industrial.py for the auth-path
coverage. The tests here exercise the no-token-configured path, which is
how a fresh install behaves until the operator sets ``GITOMA_API_TOKEN``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from gitoma.api.server import app

client = TestClient(app)


@pytest.fixture
def _no_server_token(mocker):
    """Pin the WS auth check to the 'no token configured' branch.

    Without this, the test inherits whatever token the developer happens
    to have in their real ~/.gitoma/.env, which would either close the
    WS (auth fail) or use a token the test doesn't know — both flake.
    """
    cfg = mocker.patch("gitoma.api.web.load_config")
    cfg.return_value.api_auth_token = ""
    return cfg


def test_dashboard_is_public_and_returns_html():
    """GET / is reachable without a Bearer token and returns the cockpit HTML."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Gitoma" in body
    # JS lives at /dashboard.js (extracted from inline so CSP can drop
    # `'unsafe-inline'` on script-src). The HTML must reference it.
    assert '<script src="/dashboard.js"' in body
    assert "<svg" in body  # icon sprite present — dashboard uses inline SVGs, no emoji


def test_dashboard_js_endpoint_serves_the_cockpit_runtime():
    """The cockpit JS is now an external asset. The endpoint must serve
    the runtime with the right Content-Type and a tight nosniff guard."""
    resp = client.get("/dashboard.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("etag")
    body = resp.text
    # Spot-check a few unique markers — the wired entrypoint (init), the
    # WebSocket URL builder (/ws/state), and the bearer subprotocol.
    assert "/ws/state" in body
    assert "function init" in body
    assert "gitoma-bearer" in body  # WS auth subprotocol added by the auth fix


def test_dashboard_js_endpoint_honours_if_none_match():
    """Conditional GETs must return 304 when the ETag matches, so cockpit
    reloads stay cheap on a server that doesn't change between sessions."""
    first = client.get("/dashboard.js")
    etag = first.headers["etag"]
    second = client.get("/dashboard.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_dashboard_csp_drops_unsafe_inline_on_script_src():
    """Regression guard: the whole point of extracting the JS to a
    separate asset is to tighten the CSP. If a future contributor
    re-inlines the script (and adds 'unsafe-inline' back to make it
    work), this test fails — making the security regression visible."""
    csp = client.get("/").headers["content-security-policy"]
    # The script-src directive is a substring before the next semicolon.
    script_src = next(d for d in csp.split(";") if "script-src" in d)
    assert "'unsafe-inline'" not in script_src, (
        f"script-src should not include 'unsafe-inline' anymore: {script_src!r}"
    )
    # Defence-in-depth: connect-src tightened to same-origin only.
    connect_src = next(d for d in csp.split(";") if "connect-src" in d)
    for risky in ("ws:", "wss:", "http://localhost:*", "https://localhost:*"):
        assert risky not in connect_src, (
            f"connect-src must not allow {risky!r} (token-exfil footgun): {connect_src!r}"
        )


def test_dashboard_csp_drops_unsafe_inline_on_style_src():
    """Regression guard: static inline ``style="..."`` attributes were
    moved to CSS classes and dynamic styles routed through
    ``node.style.cssText`` (DOM property write — not gated by style-src).
    The CSP can therefore drop ``'unsafe-inline'`` from style-src and
    block ``style-src-attr`` entirely. Reverting any of those changes
    and breaking the cockpit will surface here."""
    csp = client.get("/").headers["content-security-policy"]
    style_src_dirs = [d.strip() for d in csp.split(";") if d.strip().startswith("style-src")]
    # We expect at least the base ``style-src`` directive AND a
    # ``style-src-attr 'none'`` directive that explicitly forbids
    # legacy ``style="..."`` attributes.
    base = next((d for d in style_src_dirs if d.startswith("style-src ")), "")
    attr = next((d for d in style_src_dirs if d.startswith("style-src-attr")), "")
    assert "'unsafe-inline'" not in base, (
        f"style-src must not include 'unsafe-inline': {base!r}"
    )
    # The inline <style> block (dashboard.css) is allowed via SHA-256 hash.
    assert "'sha256-" in base, (
        f"style-src should allow the dashboard CSS via a SHA-256 hash: {base!r}"
    )
    assert "'none'" in attr, (
        f"style-src-attr should be 'none' to block style attributes: {attr!r}"
    )


def test_ws_state_pushes_snapshot_on_connect(monkeypatch, _no_server_token):
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


def test_ws_state_handles_empty_state_dir(monkeypatch, _no_server_token):
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
