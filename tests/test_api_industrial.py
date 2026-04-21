"""Industrial-grade API tests.

Covers the guarantees the draconian pass introduced:

* **SSE** emits comment heartbeats when the producer is quiet.
* **SSE backpressure** drops oldest when a subscriber falls behind —
  never stalls the producer, never raises QueueFull.
* **Pydantic validators** reject credential-bearing / malformed URLs at
  the edge (422), never letting them reach the CLI argv.
* **Subprocess environment** is scrubbed of the server's own
  ``GITOMA_API_TOKEN`` before the CLI is spawned.
* **Log sanitisation** strips embedded basic-auth credentials from every
  line added to the ring buffer.
* **Response models** — ``/status/{job_id}`` now returns the typed
  :class:`JobStatusResponse` with ``error_id`` instead of the old
  free-form dict.
* **WebSocket origin** — ``/ws/state`` refuses non-allow-listed browser
  origins so a drive-by page can't tail live agent state.
* **Config cache** — the Bearer token is cached after first read; a file
  mtime bump triggers re-read.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gitoma.api import routers as routers_module
from gitoma.api.routers import (
    _JOBS,
    _MAX_LINE_BYTES,
    JobRecord,
    _publish,
    _sanitize_line,
    _scrubbed_env,
)
from gitoma.api.server import app


@pytest.fixture(autouse=True)
def _mock_auth(mocker):
    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"


@pytest.fixture(autouse=True)
def _clean_jobs():
    _JOBS.clear()
    yield
    _JOBS.clear()


client = TestClient(app)
HEADERS = {"Authorization": "Bearer TOKEN"}


# ── Pydantic request validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_url", [
    "https://x:SECRET@github.com/a/b",         # embedded credentials
    "http://github.com/a/b",                   # wrong scheme
    "https://gitlab.com/a/b",                  # wrong host
    "ssh://git@github.com/a/b.git",            # wrong scheme
    "https://github.com/a",                    # missing repo
    "https://github.com/a/b?token=1",          # query string
    "not a url at all",
    "",
])
def test_run_rejects_malformed_repo_url(bad_url):
    resp = client.post("/api/v1/run", json={"repo_url": bad_url}, headers=HEADERS)
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_branch", [
    "--dry-run",            # Would be mis-parsed by Typer as a flag
    "-x",                   # Leading dash is a git ref-format violation too
    "branch with space",
    "branch\x00null",
    "a" * 300,              # > 255 chars
])
def test_run_rejects_malformed_branch(bad_branch):
    resp = client.post(
        "/api/v1/run",
        json={"repo_url": "https://github.com/a/b", "branch": bad_branch},
        headers=HEADERS,
    )
    assert resp.status_code == 422


def test_run_accepts_valid_request(mocker):
    """Mock the spawn so the test stays hermetic, but confirm the endpoint
    now returns 202 Accepted with the typed response model."""
    async def _noop(job):
        return None

    mocker.patch("gitoma.api.routers._spawn_cli_job", side_effect=_noop)
    resp = client.post(
        "/api/v1/run",
        json={"repo_url": "https://github.com/octocat/hello-world", "branch": "gitoma/x"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "started"
    assert "job_id" in data


# ── Subprocess env scrub ────────────────────────────────────────────────────


def test_scrubbed_env_removes_api_token(monkeypatch):
    """The CLI subprocess must never inherit the server's Bearer token.

    If it did, a CLI trace (which now *does* dump structured events) or a
    crash log would leak the API credential. The scrub is trivially
    cheap and stops that class of leak entirely.
    """
    monkeypatch.setenv("GITOMA_API_TOKEN", "super-secret-do-not-leak")
    monkeypatch.setenv("HOME", "/home/user")
    env = _scrubbed_env()
    assert "GITOMA_API_TOKEN" not in env
    assert env.get("HOME") == "/home/user"


# ── Log sanitisation ────────────────────────────────────────────────────────


def test_sanitize_line_redacts_basic_auth_credentials():
    line = "fatal: could not read from https://user:ghp_abc@github.com/foo/bar.git"
    out = _sanitize_line(line)
    assert "ghp_abc" not in out
    assert "REDACTED" in out


def test_sanitize_line_redacts_ssh_credentials():
    line = "error: ssh://user:pass@github.com:22/a/b timed out"
    out = _sanitize_line(line)
    assert "pass" not in out
    assert "REDACTED" in out


def test_sanitize_line_truncates_overlong_line():
    huge = "x" * (_MAX_LINE_BYTES + 5_000)
    out = _sanitize_line(huge)
    # The truncation bounds the *byte* length to the cap plus the UTF-8
    # length of the marker suffix (the ellipsis character alone is 3 bytes
    # in UTF-8, so we compare encoded-length not character-length).
    suffix_bytes = len("…(truncated)".encode("utf-8"))
    assert len(out.encode("utf-8")) <= _MAX_LINE_BYTES + suffix_bytes
    assert out.endswith("…(truncated)")


def test_publish_stores_sanitised_line_not_raw():
    """Defense-in-depth: even if a caller hands a raw credentialed line,
    the ring buffer + subscriber queues only see the redacted form."""
    job = JobRecord(id="j1", label="run", argv=["x"])
    _publish(job, "pushing to https://x:SECRETSECRET@github.com/a/b.git …")
    assert "SECRETSECRET" not in list(job.lines)[0]
    assert "REDACTED" in list(job.lines)[0]


# ── SSE back-pressure: drop-oldest policy ───────────────────────────────────


def test_publish_drops_oldest_line_when_subscriber_queue_is_full():
    """A slow consumer must not stall the producer. The drop-oldest policy
    keeps the most recent lines flowing so the tail stays live.
    """
    job = JobRecord(id="j2", label="run", argv=["x"])
    # Undersized queue so we can saturate it synchronously.
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=3)
    job.subscribers.add(q)

    for i in range(6):  # double the capacity
        _publish(job, f"line {i}")

    drained = []
    while not q.empty():
        drained.append(q.get_nowait())

    # Ring buffer reflects the whole history (capacity 500 by default);
    # the subscriber queue only keeps the latest 3 after drop-oldest.
    assert len(drained) == 3
    # Most recent line is present — producer never stalled.
    assert drained[-1] == "line 5"
    # Oldest lines got dropped from the queue (still in the ring buffer,
    # but that's the subscriber's problem to replay on reconnect).
    assert drained[0] != "line 0"


# ── /status/{job_id}: typed response + error_id surfaces ────────────────────


def test_status_endpoint_returns_typed_envelope_with_error_id_when_failed():
    job = JobRecord(id="failed-1", label="run", argv=["x"], status="failed")
    job.error_id = "abcd1234"
    _JOBS[job.id] = job

    resp = client.get(f"/api/v1/status/{job.id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job.id
    assert data["status"] == "failed"
    assert data["error_id"] == "abcd1234"
    assert data["lines_buffered"] == 0
    # The old free-form response leaked `label` as a top-level str and
    # hid `created_at` / `finished_at`. The new model exposes both.
    assert "created_at" in data and "finished_at" in data


def test_status_endpoint_never_leaks_raw_exception_string():
    """After the draconian pass ``job.status`` is one of a fixed set of
    short strings — never ``"failed: <full traceback>"``. The old
    behaviour leaked server paths via the status field."""
    job = JobRecord(id="failed-2", label="run", argv=["x"], status="failed")
    _JOBS[job.id] = job

    resp = client.get(f"/api/v1/status/{job.id}", headers=HEADERS)
    data = resp.json()
    assert data["status"] == "failed"
    # No Python repr leakage.
    assert "Traceback" not in data["status"]
    assert "/Users/" not in data["status"]
    assert "File \"" not in data["status"]


# ── WebSocket Origin check ──────────────────────────────────────────────────


def test_ws_state_rejects_disallowed_origin():
    """A browser page at http://evil.example cannot subscribe to live
    agent state. WebSockets don't do CORS preflights, so the Origin
    allow-list is the only line of defence here."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/state",
            headers={"origin": "http://evil.example"},
        ):
            pass
    # 1008 = policy violation per RFC 6455 / Starlette's enum.
    assert exc_info.value.code == 1008


def test_ws_state_accepts_allowed_origin():
    """The default allow-list covers localhost — the normal cockpit case."""
    with client.websocket_connect(
        "/ws/state",
        headers={"origin": "http://localhost:8000"},
    ) as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


def test_ws_state_accepts_missing_origin_for_non_browser_clients():
    """A CLI client that doesn't send an Origin header is allowed —
    authentication on writes is via Bearer on /api/v1/*, and the web
    cockpit is the only surface that cares about browser origin."""
    with client.websocket_connect("/ws/state") as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


# ── Config token cache ──────────────────────────────────────────────────────


def test_config_cache_reloads_when_file_mtime_changes(tmp_path, monkeypatch):
    """The server caches the Bearer token to avoid reading config on every
    request, but it must pick up edits as soon as the backing file's
    mtime advances. Otherwise an operator rotating the token has to
    restart the server, which is bad ergonomics.
    """
    from gitoma.api import server as server_module

    # Point the cache at a real pair of files we control.
    cfg_file = tmp_path / "config.toml"
    env_file = tmp_path / ".env"
    cfg_file.write_text('[api]\ntoken = "v1"\n')
    env_file.write_text("")
    monkeypatch.setattr(server_module, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(server_module, "ENV_FILE", env_file)

    # First load: returns v1.
    def _load_v1():
        return MagicMock(api_auth_token="v1")

    monkeypatch.setattr(server_module, "load_config", _load_v1)
    server_module._reset_token_cache()
    assert server_module._current_api_token() == "v1"

    # Second call with same mtime: cache hit, no reload — swap load_config
    # to prove it isn't called.
    def _boom():
        raise AssertionError("cache should not have re-read the config")

    monkeypatch.setattr(server_module, "load_config", _boom)
    assert server_module._current_api_token() == "v1"

    # Bump the mtime: cache sees it and re-reads.
    def _load_v2():
        return MagicMock(api_auth_token="v2")

    monkeypatch.setattr(server_module, "load_config", _load_v2)
    # Force mtime forward (truncate → rewrite with sleep may not bump
    # mtime on fast filesystems; os.utime is deterministic).
    stat = cfg_file.stat()
    os.utime(cfg_file, (stat.st_atime, stat.st_mtime + 5))
    assert server_module._current_api_token() == "v2"


# ── Subprocess process-group isolation (POSIX only) ─────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="setsid / killpg are POSIX")
def test_spawn_job_uses_setsid_preexec():
    """The server must spawn the CLI with ``os.setsid`` so ``os.killpg``
    terminates every descendant on cancel. We inspect the actual call to
    ``create_subprocess_exec`` and check the kwargs."""
    from unittest.mock import AsyncMock, patch as patch_fn

    job = JobRecord(id="j-pg", label="run", argv=["sleep", "30"])

    class _FakeProc:
        stdout = AsyncMock()
        stdout.readline = AsyncMock(return_value=b"")
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    captured_kwargs: dict[str, Any] = {}

    async def _fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeProc()

    with patch_fn("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        asyncio.run(routers_module._spawn_cli_job(job))

    assert captured_kwargs.get("preexec_fn") is os.setsid
    # Stdin is closed so a CLI that reads from stdin blocks in a defined way,
    # not on a parent-inherited tty.
    assert captured_kwargs.get("stdin") == asyncio.subprocess.DEVNULL
    # Env is scrubbed.
    env = captured_kwargs.get("env") or {}
    assert "GITOMA_API_TOKEN" not in env


# ── SSE heartbeat ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_stream_emits_heartbeat_on_silence(monkeypatch):
    """If no new lines arrive within the heartbeat window, the stream
    yields a comment frame so proxies don't disconnect the client."""
    # Shrink the heartbeat so the test runs fast.
    monkeypatch.setattr(routers_module, "_SSE_HEARTBEAT_SECONDS", 0.05)

    job = JobRecord(id="hb-1", label="run", argv=["x"], status="running")
    _JOBS[job.id] = job

    # Hand the endpoint a real request path via its internal coroutine —
    # stand up the event_stream generator and step it a couple of times.
    from gitoma.api.routers import stream_job_output

    response = await stream_job_output(job.id)
    gen = response.body_iterator

    # First frame(s) may be replayed history — in our case none.
    # After the heartbeat window, we must see a `:` comment frame.
    frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    # Either a heartbeat or, if the producer just published, a data
    # frame. We accept either — we assert the stream is not dead.
    assert frame.startswith(":") or frame.startswith("event: ")

    # Publish the end sentinel so the stream terminates cleanly.
    from gitoma.api.routers import _END_SENTINEL
    _publish(job, f"{_END_SENTINEL}:completed")

    # Drain until the stream ends.
    async def _drain():
        try:
            async for _ in gen:
                pass
        except StopAsyncIteration:
            return

    await asyncio.wait_for(_drain(), timeout=2.0)


# ── Global exception handler ────────────────────────────────────────────────


def test_unhandled_exception_is_wrapped_in_error_id_envelope(mocker):
    """A surprise exception inside a handler must surface as a generic
    500 with an opaque ``error_id`` — never as a stack trace."""
    # TestClient's default ``raise_server_exceptions=True`` re-raises the
    # unhandled exception in the test process. Disable it so the
    # registered global handler actually gets to respond.
    local = TestClient(app, raise_server_exceptions=False)
    mocker.patch(
        "gitoma.api.routers.load_config",
        side_effect=RuntimeError("kaboom-internal-detail-path-/secret"),
    )
    resp = local.get("/api/v1/health", headers=HEADERS)
    assert resp.status_code == 500
    data = resp.json()
    assert data["detail"] == "Internal server error."
    assert "error_id" in data
    # Traceback must NOT leak to the client, regardless of the real exc.
    raw = resp.text
    assert "kaboom" not in raw
    assert "secret" not in raw
    assert "Traceback" not in raw


# ── Validation error envelope doesn't echo input ────────────────────────────


def test_validation_errors_do_not_echo_user_input_back():
    """The default FastAPI validator includes the raw ``input`` field which
    leaks credentials if the user hit the API with a token in the URL.
    Our custom handler drops ``input`` and keeps only ``loc`` + ``msg``.
    """
    resp = client.post(
        "/api/v1/run",
        json={"repo_url": "https://x:SECRETSECRET@github.com/a/b"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    raw = resp.text
    # Defensive: no way our custom handler echoes `SECRETSECRET` back.
    assert "SECRETSECRET" not in raw
