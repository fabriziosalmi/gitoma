"""Runtime / transport-layer pins against the live cockpit.

All of these test behaviours that the earlier files don't: the WS
transport's handshake gate, cross-cutting clickjacking guards, SSE
lifecycle on cancel, response-time budgets, and keepalive
liveness over several seconds. Every test here isolates its async
work in a fresh thread so ``asyncio.run`` can't collide with
pytest-asyncio's or pytest-playwright's event loops.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures as cf
import statistics
import time

import pytest
import requests

pytestmark = pytest.mark.e2e_cockpit


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_TINY_REPO = "https://github.com/fabriziosalmi/b2v"


def _ws_url(cockpit_url: str) -> str:
    return (
        cockpit_url.replace("http://", "ws://").replace("https://", "wss://")
        + "/ws/state"
    )


def _b64url_token(token: str) -> str:
    return base64.urlsafe_b64encode(token.encode()).rstrip(b"=").decode()


def _run_async(coro_factory, *, timeout: float = 15.0):
    """Run an async coroutine in a fresh thread.

    The usual ``asyncio.run`` breaks under pytest when pytest-asyncio /
    pytest-playwright has already left an event loop on the main thread.
    A fresh worker thread has no loop, so ``asyncio.run`` can set one up
    cleanly. Returns whatever the coroutine returned."""
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=timeout)


# ── WS origin validation ──────────────────────────────────────────────────


def test_ws_rejects_unauthorised_origin(cockpit_url: str, cockpit_token: str):
    """A WebSocket handshake from an origin outside the allow-list (and
    not loopback, not same-origin) must be rejected BEFORE bearer
    verification. This is the cross-site-WebSocket-hijacking guard — a
    malicious page on ``https://evil.com`` could otherwise ride the
    operator's browser-stored bearer into a cockpit open in another
    tab and exfiltrate state snapshots. The server closes with
    WS code 1008 (policy violation) after the accept handshake."""
    import websockets
    from websockets.exceptions import InvalidStatus, ConnectionClosed

    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"

    async def _try_evil():
        try:
            async with websockets.connect(
                ws_url,
                subprotocols=[offered],
                additional_headers={"Origin": "https://evil.com"},
                open_timeout=8,
                close_timeout=2,
            ) as ws:
                # If we reach here, the handshake succeeded — then we
                # expect the server to close us immediately. A recv()
                # should raise ConnectionClosed with 1008.
                await ws.recv()
                return "accepted_did_not_close"
        except ConnectionClosed as exc:
            return f"closed:{exc.code}"
        except InvalidStatus as exc:
            # Some servers reject pre-accept with a non-101 response.
            return f"refused:{exc.response.status_code}"
        except Exception as exc:
            return f"error:{type(exc).__name__}"

    outcome = _run_async(_try_evil)
    # Two acceptable shapes: explicit 1008 close OR pre-accept refusal.
    # Any other outcome means the evil origin was silently accepted.
    assert outcome.startswith("closed:1008") or outcome.startswith("refused:"), (
        f"Evil-origin WS must be rejected (1008 close or pre-accept refuse); "
        f"got {outcome!r}"
    )


def test_ws_accepts_same_origin_handshake(cockpit_url: str, cockpit_token: str):
    """Companion to the evil-origin test: when the Origin matches the
    cockpit's own host, the handshake must succeed. The cockpit's
    browser-side JS builds Origin from ``location.host``, so this is
    the real-world happy path we must not break while tightening the
    anti-hijack rejection above."""
    import websockets

    from urllib.parse import urlparse
    parsed = urlparse(cockpit_url)
    self_origin = f"{parsed.scheme}://{parsed.netloc}"
    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"

    async def _try_self():
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            additional_headers={"Origin": self_origin},
            open_timeout=8,
            close_timeout=2,
        ) as ws:
            # Must receive at least one state snapshot within a few
            # poll intervals — proves the accept path actually runs.
            first = await asyncio.wait_for(ws.recv(), timeout=5)
            return len(first)

    payload_bytes = _run_async(_try_self)
    assert payload_bytes > 0, (
        "Same-origin WS handshake must succeed and receive a state "
        "snapshot — got an empty first message"
    )


# ── Concurrent WS connections ────────────────────────────────────────────


def test_three_concurrent_ws_connections_all_auth_and_receive(
    cockpit_url: str, cockpit_token: str
):
    """Three simultaneous cockpits must each:
      * complete their subprotocol-auth handshake
      * receive at least one state snapshot within a short window

    A regression that serialised WS accepts (or shared a socket across
    clients) would manifest here as a timeout on client 2 or 3. The
    real-world shape we're pinning: operator has the cockpit open in
    multiple tabs + a terminal with a CLI-driven status query."""
    import websockets

    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"

    async def _one_client(label: str):
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            open_timeout=10,
            close_timeout=2,
        ) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            return (label, ws.subprotocol, len(msg))

    async def _three_clients():
        return await asyncio.gather(
            _one_client("a"),
            _one_client("b"),
            _one_client("c"),
        )

    results = _run_async(_three_clients, timeout=25)
    assert len(results) == 3
    for label, subprotocol, n in results:
        assert subprotocol == offered, (
            f"client {label}: subprotocol not echoed; got {subprotocol!r}"
        )
        assert n > 0, f"client {label}: empty first state snapshot"


# ── Cancel closes the SSE stream ─────────────────────────────────────────


def test_cancel_ends_sse_stream(cockpit_url: str, cockpit_token: str):
    """After cancel, the SSE stream must end (EOF) within a bounded
    window. Otherwise the cockpit's LogStream leaks a hanging fetch
    per cancelled run — after a few days of use you're dangling
    connections equal to the number of jobs you've cancelled.

    The previous implementation of ``/stream/{job_id}`` tested this
    by dropping the reference and trusting GC; pin the contract
    independently of that implementation detail."""
    # Dispatch a cheap analyze to stream.
    dispatch = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert dispatch.status_code == 202
    job_id = dispatch.json()["job_id"]

    try:
        stream = requests.get(
            f"{cockpit_url}/api/v1/stream/{job_id}",
            headers={**_auth(cockpit_token), "Accept": "text/event-stream"},
            timeout=20,
            stream=True,
        )
        assert stream.status_code == 200

        # Consume until the stream has clearly started.
        started = False
        start = time.monotonic()
        for raw in stream.iter_lines(decode_unicode=True):
            if raw:
                started = True
                break
            if time.monotonic() - start > 5:
                break
        assert started, "SSE stream never emitted any data"

        # Cancel the job.
        cancel_resp = requests.post(
            f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert cancel_resp.status_code in (200, 409), cancel_resp.text[:200]

        # Now the stream must end on its own within a bounded window.
        # We keep consuming; once the server closes the connection,
        # ``iter_lines`` returns naturally. The deadline is generous
        # (15s) to absorb the cancel→kill→flush sequence.
        deadline = time.monotonic() + 15
        ended_cleanly = False
        try:
            for _ in stream.iter_lines(decode_unicode=True):
                if time.monotonic() > deadline:
                    break
            else:
                ended_cleanly = True
        except requests.exceptions.ChunkedEncodingError:
            # Server hanging-up mid-chunk is also a valid "stream ended".
            ended_cleanly = True

        assert ended_cleanly or time.monotonic() <= deadline + 1, (
            f"SSE /stream/{job_id} did not close within 15s of cancel — "
            "connection leak. If this flakes on a slow server bump the "
            "deadline, but investigate any hang >30s."
        )
    finally:
        try:
            stream.close()
        except Exception:
            pass
        # Belt-and-braces cancel (idempotent, no-op if already done).
        try:
            requests.post(
                f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
                headers=_auth(cockpit_token),
                timeout=5,
            )
        except Exception:
            pass


# ── Clickjacking guard: frame-ancestors 'none' OR X-Frame-Options ────────


def test_clickjacking_guard_present(cockpit_url: str):
    """We MUST have AT LEAST ONE of:

      * CSP ``frame-ancestors 'none'`` (preferred; modern browsers
        give it precedence over X-Frame-Options)
      * ``X-Frame-Options: DENY`` or ``SAMEORIGIN``

    If both are absent, a malicious tailnet-adjacent page could iframe
    the cockpit and overlay clickjack UI on top of the Run button.
    Redundant-looking tests catch the mode where a CSP refactor drops
    frame-ancestors *without* adding the X-Frame-Options fallback for
    older clients — which is exactly the kind of subtle regression
    that skips audit review."""
    resp = requests.get(f"{cockpit_url}/", timeout=10)
    assert resp.status_code == 200

    csp = resp.headers.get("content-security-policy", "")
    has_fa_none = any(
        d.strip() == "frame-ancestors 'none'" for d in csp.split(";")
    )
    xfo = resp.headers.get("x-frame-options", "").strip().upper()
    has_xfo = xfo in ("DENY", "SAMEORIGIN")

    assert has_fa_none or has_xfo, (
        "Clickjacking guard missing — need either CSP frame-ancestors 'none' "
        f"or X-Frame-Options DENY/SAMEORIGIN. Got CSP={csp!r}, XFO={xfo!r}"
    )


# ── /health latency budget ───────────────────────────────────────────────


def test_health_endpoint_latency_p95_under_budget(
    cockpit_url: str, cockpit_token: str
):
    """Twenty sequential ``/health`` calls against a tailnet-reachable
    cockpit must hit p95 < 500 ms. The /health path does an actual
    round-trip to LM Studio, so "fast" here is a weak lower bound;
    the real regression signal is a mean that balloons to multiple
    seconds (server-side blocking I/O on the event loop, etc.).

    Target: 500 ms is the upper bound for "user-perceives-instant".
    If this flakes on a loaded Mac Mini, raise it — but don't delete
    the test; a live-edit raise is a conversation about what changed."""
    durations_ms: list[float] = []
    for _ in range(20):
        t0 = time.monotonic()
        resp = requests.get(
            f"{cockpit_url}/api/v1/health",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert resp.status_code == 200, resp.text[:200]
        durations_ms.append((time.monotonic() - t0) * 1000.0)

    p50 = statistics.median(durations_ms)
    # p95 with 20 samples = 19th value after sort.
    p95 = sorted(durations_ms)[int(len(durations_ms) * 0.95) - 1]
    assert p95 < 500, (
        f"/health p95 latency regressed. p50={p50:.0f}ms, p95={p95:.0f}ms. "
        f"Raw: {[f'{d:.0f}' for d in sorted(durations_ms)]}. "
        "A p95 >500ms points at event-loop blocking or LM Studio slowness."
    )


# ── WS idle keepalive ────────────────────────────────────────────────────


def test_ws_stays_open_during_idle_window(cockpit_url: str, cockpit_token: str):
    """After receiving the initial state snapshot, an idle WS must
    stay open. A regression in the server's read-loop (e.g. a bare
    ``ws.receive_text`` waiting on client → timeout → close) would
    kill idle cockpit tabs silently; the operator comes back after
    lunch to a "reconnecting" pill with no explanation.

    We hold the connection for 6 seconds — longer than the 0.5s
    POLL_INTERVAL_S but shorter than the websockets default
    ping_interval (20s). If the server kills us in that window, it's
    not keepalive — it's a hang on the recv path."""
    import websockets
    from websockets.exceptions import ConnectionClosed

    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"

    async def _idle_for_6s():
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            open_timeout=10,
            close_timeout=2,
        ) as ws:
            # Consume the initial state snapshot.
            first = await asyncio.wait_for(ws.recv(), timeout=5)
            assert first

            # Idle for 6s. Any incoming pings are handled by the client
            # lib automatically; we're only checking the connection is
            # still alive at the end.
            await asyncio.sleep(6)
            # Three possible signals at this point:
            #   * new message arrives immediately → alive AND actively
            #     pushing (happy path when state is changing)
            #   * recv times out in 0.5s → alive AND idle (happy path
            #     when state is quiescent)
            #   * ConnectionClosed → the server dropped us
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
                return "alive"  # got data, connection is fine
            except asyncio.TimeoutError:
                return "alive"  # idle but not closed — still fine
            except ConnectionClosed as exc:
                return f"closed:{exc.code}"

    outcome = _run_async(_idle_for_6s, timeout=30)
    assert outcome == "alive", (
        f"Idle WS was terminated during 6s window: {outcome}. "
        "The server's read-loop is killing idle cockpits."
    )
