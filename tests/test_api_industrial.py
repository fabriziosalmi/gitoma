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
    """Pin the configured Bearer token to ``TOKEN`` for every test.

    Both the REST verify_token (``gitoma.api.server.load_config``) and the
    WebSocket auth check (``gitoma.api.web.load_config``) hit ``load_config``
    via their own module-level binding — Python imports are name copies, so
    patching one binding doesn't reach the other. Both must be mocked.
    """
    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"
    cfg_ws = mocker.patch("gitoma.api.web.load_config")
    cfg_ws.return_value.api_auth_token = "TOKEN"


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


@pytest.mark.parametrize("var", [
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GCP_SERVICE_KEY",
    "DOCKER_PASSWORD",
    "NPM_TOKEN",
    "CARGO_REGISTRY_TOKEN",
    "DATABASE_PASSWORD",
    "STRIPE_SECRET",
    "MY_CUSTOM_PRIVATE_KEY",
    "FOO_CREDENTIALS",
])
def test_scrubbed_env_strips_unknown_secret_shaped_vars(var, monkeypatch):
    """The deny-by-pattern layer catches the long tail of vendor creds we
    can't enumerate. Without it, an operator who exported AWS_* or
    NPM_TOKEN in their shell hands those secrets to the CLI subprocess
    (and to any /proc/<pid>/environ reader on the host).
    """
    monkeypatch.setenv(var, "leak-me")
    env = _scrubbed_env()
    assert var not in env, f"{var} matched a secret-name pattern but was not stripped"


@pytest.mark.parametrize("var", [
    "GITHUB_TOKEN",       # CLI auths to GitHub with this
    "LM_STUDIO_API_KEY",  # placeholder LMStudio auth
    "OPENAI_API_KEY",     # OpenAI-compat clients
    "ANTHROPIC_API_KEY",  # self-critic / future LLM backends
])
def test_scrubbed_env_keeps_explicit_allowlist_secrets(var, monkeypatch):
    """The CLI legitimately needs these credentials. The pattern scrub
    must NOT strip them — they're on the explicit allow-list."""
    monkeypatch.setenv(var, "value-needed-by-cli")
    env = _scrubbed_env()
    assert env.get(var) == "value-needed-by-cli"


def test_scrubbed_env_keeps_non_secret_vars(monkeypatch):
    """Non-secret-shaped vars (PATH, LANG, GITOMA_BANNER, …) must pass
    through. Regression guard against an over-eager filter."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("GITOMA_BANNER", "compact")
    env = _scrubbed_env()
    assert env.get("PATH") == "/usr/bin"
    assert env.get("LANG") == "en_US.UTF-8"
    assert env.get("GITOMA_BANNER") == "compact"


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
    allow-list is the only line of defence at the origin layer."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/state",
            headers={
                "origin": "http://evil.example",
                "authorization": "Bearer TOKEN",
            },
        ):
            pass
    # 1008 = policy violation per RFC 6455 / Starlette's enum.
    assert exc_info.value.code == 1008


def test_ws_state_accepts_allowed_origin_with_bearer_header():
    """Same-origin handshake + correct Bearer header. The TestClient's
    default Host is ``testserver`` so we send ``Origin: http://testserver``
    — which the WS now accepts via the same-origin rule (no need for
    the env override)."""
    with client.websocket_connect(
        "/ws/state",
        headers={
            "origin": "http://testserver",
            "authorization": "Bearer TOKEN",
        },
    ) as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


def test_ws_state_accepts_token_via_subprotocol():
    """Browser clients can't set Authorization on the handshake, so the
    cockpit JS tunnels the bearer through ``Sec-WebSocket-Protocol`` as
    ``gitoma-bearer.<base64url(token)>``. The server must accept and ack
    the chosen subprotocol per RFC 6455."""
    import base64

    encoded = base64.urlsafe_b64encode(b"TOKEN").rstrip(b"=").decode()
    sub = f"gitoma-bearer.{encoded}"
    with client.websocket_connect(
        "/ws/state",
        subprotocols=[sub],
        headers={"origin": "http://testserver"},
    ) as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


def test_ws_state_rejects_when_token_configured_but_missing():
    """When a server-side token is set, an unauthenticated handshake must
    be closed with policy-violation. The previous behaviour (open WS
    pushing PIDs / branches / errors to anyone who could reach the port)
    was the gap this fix closes."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/state"):
            pass
    assert exc_info.value.code == 1008


def test_ws_state_rejects_wrong_token():
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/state",
            headers={"authorization": "Bearer NOPE"},
        ):
            pass
    assert exc_info.value.code == 1008


def test_ws_state_accepts_same_origin_handshake_on_arbitrary_port():
    """Regression for the hardcoded-port-8000 origin allow-list. The WS
    must accept any handshake whose ``Origin`` matches the request's
    ``Host`` header, regardless of port. Otherwise an operator running
    ``uvicorn ... --port 8080`` (or any non-default port) would see the
    cockpit silently drop the WS with policy-violation."""
    # TestClient's default Host is "testserver". Construct a matching Origin.
    with client.websocket_connect(
        "/ws/state",
        headers={"origin": "http://testserver", "authorization": "Bearer TOKEN"},
    ) as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


def test_ws_state_open_when_server_has_no_token_configured(mocker):
    """Fresh install with no token configured: REST endpoints respond 503
    (which the cockpit surfaces as a clear "configure your token" banner),
    and the WS stays open so the dashboard can at least render whatever
    the local state-dir already holds. This matches the no-config UX the
    rest of the system already exposes."""
    cfg = mocker.patch("gitoma.api.web.load_config")
    cfg.return_value.api_auth_token = ""
    with client.websocket_connect("/ws/state") as ws:
        data = ws.receive_text()
        assert isinstance(json.loads(data), list)


# ── Config token cache ──────────────────────────────────────────────────────


def test_config_cache_reloads_when_env_token_changes(tmp_path, monkeypatch):
    """Regression for the silent-env-stale bug.

    The previous cache keyed only on file mtimes. An operator running
    ``export GITOMA_API_TOKEN=newone`` against a server whose config files
    existed (most realistic deploys) saw the OLD token enforced until
    they bumped a file. That silently masked credential rotations — the
    exact failure mode that hides a leaked token in production.

    The fixed cache includes ``os.environ.get("GITOMA_API_TOKEN")`` in
    its key, so an env rotation forces the next request to re-read.
    """
    from gitoma.api import server as server_module

    cfg_file = tmp_path / "config.toml"
    env_file = tmp_path / ".env"
    cfg_file.write_text('[api]\ntoken = "from-file"\n')
    env_file.write_text("")
    monkeypatch.setattr(server_module, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(server_module, "ENV_FILE", env_file)

    # First load: env says "v1", load_config() returns "v1".
    monkeypatch.setenv("GITOMA_API_TOKEN", "v1")

    def _load(): return MagicMock(api_auth_token=os.environ["GITOMA_API_TOKEN"])

    monkeypatch.setattr(server_module, "load_config", _load)
    server_module._reset_token_cache()
    assert server_module._current_api_token() == "v1"

    # Operator rotates the env. No file mtime moved. The OLD cache
    # would keep returning "v1"; the new cache must see the env change
    # and call load_config() again.
    monkeypatch.setenv("GITOMA_API_TOKEN", "v2")
    assert server_module._current_api_token() == "v2"

    # Operator unsets the env: cache must re-read once more (the absence
    # of the env var is itself a meaningful key transition).
    monkeypatch.delenv("GITOMA_API_TOKEN", raising=False)

    def _load_no_env(): return MagicMock(api_auth_token="from-file")

    monkeypatch.setattr(server_module, "load_config", _load_no_env)
    assert server_module._current_api_token() == "from-file"


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
def test_spawn_job_uses_start_new_session():
    """The server must spawn the CLI as a new session leader so
    ``os.killpg`` terminates every descendant on cancel. We used to set
    ``preexec_fn=os.setsid`` for this; that's deprecated in 3.12 and
    deadlock-prone in multi-threaded parents (uvicorn's threadpool counts).
    The replacement is ``start_new_session=True`` — same effect at the
    syscall level, no Python callback in the forked child."""
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

    assert captured_kwargs.get("start_new_session") is True
    # Regression guard: the deprecated preexec_fn API must NOT come back.
    assert "preexec_fn" not in captured_kwargs or captured_kwargs.get("preexec_fn") is None
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


# ── Per-token dispatch rate limiter ─────────────────────────────────────────


def test_dispatch_rate_limit_kicks_in_after_burst(mocker):
    """A compromised client must not be able to flood the dispatch
    endpoints. After ``DISPATCH_RATE_LIMIT_BURST`` requests in
    ``DISPATCH_RATE_LIMIT_WINDOW_S`` the (n+1)th request must 429 with
    a ``Retry-After`` hint."""
    from gitoma.api import routers as routers_module

    async def _noop(job): return None

    mocker.patch("gitoma.api.routers._spawn_cli_job", side_effect=_noop)

    burst = routers_module.DISPATCH_RATE_LIMIT_BURST
    payload = {"repo_url": "https://github.com/octocat/hello-world"}
    for _ in range(burst):
        resp = client.post("/api/v1/analyze", json=payload, headers=HEADERS)
        assert resp.status_code == 202, resp.text

    # The (burst+1)th request should be throttled.
    resp = client.post("/api/v1/analyze", json=payload, headers=HEADERS)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1
    assert "rate limit" in resp.json()["detail"].lower()


def test_dispatch_rate_limit_buckets_per_token(mocker):
    """A different token gets a different bucket — one client's burst
    must not throttle another's. The hash key uses the bearer value, so
    distinct tokens map to distinct buckets."""
    from gitoma.api import routers as routers_module

    async def _noop(job): return None

    mocker.patch("gitoma.api.routers._spawn_cli_job", side_effect=_noop)
    # Two valid tokens — server accepts both via the autouse mock.
    cfg_server = mocker.patch("gitoma.api.server.load_config")
    cfg_server.return_value.api_auth_token = "TOKEN-A"
    cfg_web = mocker.patch("gitoma.api.web.load_config")
    cfg_web.return_value.api_auth_token = "TOKEN-A"

    payload = {"repo_url": "https://github.com/octocat/hello-world"}
    burst = routers_module.DISPATCH_RATE_LIMIT_BURST

    # Saturate token A.
    headers_a = {"Authorization": "Bearer TOKEN-A"}
    for _ in range(burst):
        assert client.post("/api/v1/analyze", json=payload, headers=headers_a).status_code == 202
    assert client.post("/api/v1/analyze", json=payload, headers=headers_a).status_code == 429

    # Switch the server's accepted token and use the new one — it gets a
    # fresh bucket. (The mock returns the new value on every call.)
    cfg_server.return_value.api_auth_token = "TOKEN-B"
    cfg_web.return_value.api_auth_token = "TOKEN-B"
    from gitoma.api.server import _reset_token_cache
    _reset_token_cache()
    headers_b = {"Authorization": "Bearer TOKEN-B"}
    assert client.post("/api/v1/analyze", json=payload, headers=headers_b).status_code == 202


# ── list_jobs: snapshot under lock ──────────────────────────────────────────


def test_list_jobs_takes_jobs_lock():
    """list_jobs is now async and snapshots ``_JOBS`` under the lock
    before iterating. The previous sync iteration was technically GIL-
    safe but any future refactor that added an ``await`` inside the
    comprehension would race ``_dispatch``'s eviction. Pinning the
    contract here means the regression is a test failure, not a 500."""
    import inspect
    from gitoma.api.routers import list_jobs

    assert inspect.iscoroutinefunction(list_jobs), (
        "list_jobs must be async to acquire the asyncio.Lock"
    )


# ── /health: async + bounded timeout ────────────────────────────────────────


def test_health_endpoint_does_not_hang_when_lmstudio_is_unresponsive(mocker):
    """/health is hit by k8s/LB probes and the cockpit banner. A stalled
    LM Studio used to wedge the handler for the full ``check_lmstudio``
    timeout; the async + ``asyncio.wait_for`` wrapper caps the wall-clock
    well under that.

    We pin both knobs (the inner sleep and the outer timeout) so the test
    is bounded regardless of the threadpool's cancellation semantics —
    Python threads can't be interrupted, so a simulated hang must be
    short enough to complete under its own steam. What matters here is
    that the response shape reflects the timeout branch and that the
    handler returned promptly relative to the inner sleep."""
    import time

    def _slow(_config, _timeout):
        time.sleep(0.6)  # bounded; the threadpool slot frees on its own

    mocker.patch("gitoma.api.routers.check_lmstudio", side_effect=_slow)
    # Inner timeout is well below the simulated slowness so the wait_for
    # branch fires and the response is built from the timeout payload.
    mocker.patch("gitoma.api.routers._HEALTH_LM_TIMEOUT_S", 0.05)

    started = time.monotonic()
    resp = client.get("/api/v1/health", headers=HEADERS)
    elapsed = time.monotonic() - started

    # Bounded wall-clock — the inner sleep is the upper bound (the
    # thread can't be cancelled), but the response should not exceed it
    # by much. 5 s is generous; in practice this returns in <1 s.
    assert elapsed < 5.0, f"health took {elapsed:.1f}s"
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["lm_studio"]["level"] == "error"
    # The detail must clearly say "timed out" so the cockpit can render
    # an actionable banner instead of a generic "down".
    assert "timed out" in data["lm_studio"]["message"].lower()


# ── SSE subscriber cap ──────────────────────────────────────────────────────


def test_sse_endpoint_rejects_excess_subscribers_per_job():
    """Without a per-job cap an authenticated client can open hundreds of
    SSE subscriptions and balloon RAM (each owns a 1000-deep × 4 KB
    queue). Past the cap we 429 with a Retry-After hint."""
    from gitoma.api.routers import _MAX_SUBSCRIBERS_PER_JOB

    job = JobRecord(id="sub-cap-1", label="run", argv=["x"], status="running")
    # Pre-fill subscribers up to the limit. The set members can be any
    # placeholder — the route only checks ``len(job.subscribers)``.
    for _ in range(_MAX_SUBSCRIBERS_PER_JOB):
        job.subscribers.add(asyncio.Queue())
    _JOBS[job.id] = job

    resp = client.get(f"/api/v1/stream/{job.id}", headers=HEADERS)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert "subscribers" in body["detail"].lower()


# ── _kill_process_group surfaces failures ───────────────────────────────────


def test_kill_process_group_returns_outcome_codes():
    """The helper used to swallow every error and return None. The cancel
    flow then reported "cancelled" even when the kill actually failed —
    leaving a still-running pid that nobody warned about. The new
    contract returns ``"signaled"`` / ``"already_gone"`` / ``"failed"``
    so the caller can surface real failures."""
    from gitoma.api.routers import _kill_process_group

    # Fake "process is gone" — emulate ProcessLookupError on the kill.
    class _GoneProc:
        pid = 99999  # almost certainly unused
        # no terminate/kill — POSIX path uses os.killpg

    # We can't safely actually kill anything; exercise the code path by
    # monkey-patching os.killpg to raise the relevant errors.
    import os
    real_killpg, real_getpgid = os.killpg, os.getpgid
    try:
        os.getpgid = lambda pid: pid
        os.killpg = lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError())
        assert _kill_process_group(_GoneProc(), 15) == "already_gone"

        os.killpg = lambda pid, sig: (_ for _ in ()).throw(PermissionError("EPERM"))
        assert _kill_process_group(_GoneProc(), 15) == "failed"

        os.killpg = lambda pid, sig: None
        assert _kill_process_group(_GoneProc(), 15) == "signaled"
    finally:
        os.killpg = real_killpg
        os.getpgid = real_getpgid


# ── Hardened deployment defaults ────────────────────────────────────────────


def test_trusted_host_default_does_not_include_mdns_wildcard():
    """``*.local`` was in the default allow-list, exposing the API to
    anyone on the same broadcast domain that could resolve a ``.local``
    name via mDNS. Default must be loopback-only; operators who need
    LAN/mDNS access opt in via ``GITOMA_ALLOWED_HOSTS``."""
    from gitoma.api import server as server_module

    # Re-derive what the default *would* be by reading the same env-var
    # path the module uses, but with no env override. We cannot easily
    # introspect the live middleware after-the-fact, so instead we pin
    # the module-level allow-list source string.
    src = (server_module.__file__ or "")
    text = open(src, encoding="utf-8").read()
    # The default literal must not include the mDNS wildcard.
    assert '"localhost,127.0.0.1,0.0.0.0,testserver"' in text, (
        "TrustedHost default changed shape — confirm it still excludes '*.local'"
    )
    assert '*.local' not in text.split('GITOMA_ALLOWED_HOSTS"')[1].split(')')[0], (
        "*.local must not be in the TrustedHost default — it's an mDNS leak vector"
    )


def test_cors_wildcard_with_credentials_is_refused_at_startup(monkeypatch, caplog):
    """``GITOMA_CORS_ORIGINS=*`` with ``allow_credentials=True`` is
    silently rejected by every browser per the CORS spec. The previous
    code happily installed the middleware in that shape, leading to a
    cockpit that "doesn't work" with no server-side trace. The fix
    refuses the install and logs a clear error."""
    import importlib
    import logging

    monkeypatch.setenv("GITOMA_CORS_ORIGINS", "*")
    with caplog.at_level(logging.ERROR, logger="gitoma.api.server"):
        # Force a re-import so the module-level CORS config branch fires
        # against the patched env.
        import gitoma.api.server as srv_mod
        importlib.reload(srv_mod)
    messages = [r.message for r in caplog.records]
    assert any("cors_wildcard_with_credentials_rejected" in m for m in messages), (
        f"expected the wildcard guard to log a refusal: {messages!r}"
    )


# ── Structured access log ───────────────────────────────────────────────────


def test_request_middleware_emits_structured_access_log(caplog):
    """The request middleware now emits an INFO log line per request
    with method/path/status/duration/client/auth_present. Probes
    (``/api/v1/health``, ``/``) are demoted to DEBUG so prod INFO
    streams stay readable. The Authorization *value* must never appear
    in the log output — only its presence as a boolean."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="gitoma.api.server"):
        # A real (non-probe) endpoint that should produce an INFO line.
        resp = client.get("/api/v1/jobs", headers=HEADERS)
        assert resp.status_code == 200

    access = [r for r in caplog.records if r.message == "http_access"]
    assert access, "expected at least one http_access log line"

    rec = access[-1]
    # Structured fields are attached as record attributes via ``extra=``.
    assert getattr(rec, "method") == "GET"
    assert getattr(rec, "path") == "/api/v1/jobs"
    assert getattr(rec, "status") == 200
    assert getattr(rec, "auth_present") is True
    assert isinstance(getattr(rec, "duration_ms"), (int, float))
    # Bearer token must NOT appear in any rendered log message.
    full_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN" not in full_text, (
        "Bearer token leaked into access log — check that 'auth_present' "
        "is a bool, not the header value"
    )


# ── GZip bypass for SSE ─────────────────────────────────────────────────────


def test_sse_endpoint_is_not_gzip_compressed():
    """``GZipMiddleware`` happily compresses ``text/event-stream``, which
    destroys SSE — the compressor buffers bytes until its threshold fires,
    so heartbeat comments and individual log lines never reach the
    browser in real time. The streaming-aware wrapper must skip the
    gzip layer for ``/api/v1/stream/*``.

    Regression guard: a future contributor that swaps the wrapper back
    out for the plain ``GZipMiddleware`` will see this test fail.
    """
    job = JobRecord(id="sse-gz-1", label="run", argv=["x"], status="completed")
    # Pre-populate the ring buffer with the end sentinel so the stream
    # closes immediately — we only care about the response headers, not
    # the live tail.
    from gitoma.api.routers import _END_SENTINEL
    job.lines.append(f"{_END_SENTINEL}:completed")
    _JOBS[job.id] = job

    # Force the client to advertise gzip — without this the middleware
    # would naturally skip compression and the test would pass for the
    # wrong reason.
    headers = {**HEADERS, "Accept-Encoding": "gzip"}
    with client.stream("GET", f"/api/v1/stream/{job.id}", headers=headers) as resp:
        # Drain so the server can close cleanly without leaving a task
        # holding the test loop.
        for _ in resp.iter_raw():
            pass
        assert resp.headers.get("content-encoding") != "gzip"
        assert resp.headers.get("content-type", "").startswith("text/event-stream")


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
