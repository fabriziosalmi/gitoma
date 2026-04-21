"""Final surface-area pass — last remaining regression pockets.

Six tests covering coverage we deliberately saved for the end:
  * WS receives a new state snapshot *after* a dispatch (end-to-end
    change propagation, not just handshake)
  * SSE /stream requires a bearer (pins the auth gate on the one
    endpoint where "just open it and watch" is the common op)
  * dashboard.js ETag is actually SHA-256(body)[:16] — verifiable
    at the client, not trusted blindly
  * Two browser contexts hit the cockpit concurrently and both go
    live (multi-tab real-world scenario)
  * Unicode repo_url edge cases (emoji / RTL / zero-width / NFC)
    rejected by the validator
  * 60 rapid WS connect/disconnect cycles — server doesn't degrade,
    every handshake still under 2 s at p95
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures as cf
import hashlib
import re
import statistics
import time

import pytest
import requests
from playwright.sync_api import Browser, expect

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


def _run_async(coro_factory, *, timeout: float = 30.0):
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=timeout)


# ── WS observes live state change after a dispatch ───────────────────────


def test_ws_receives_state_snapshot_after_dispatch(
    cockpit_url: str, cockpit_token: str
):
    """End-to-end change propagation: open a WS, count the state
    snapshots that arrive before and after a dispatch. A successful
    dispatch mutates on-disk state (new job, current_operation, etc.),
    which must surface as a fresh snapshot on the WS within a bounded
    window.

    The previous tests pin:
      * WS handshake succeeds (smoke.test_ws_conn_pill)
      * Dispatch works (live_flows.test_sse_dispatch)
    This one pins the *link between them*: that the WS change-detection
    loop actually re-serialises when state changes on disk."""
    import websockets

    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"

    async def _observe():
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            open_timeout=10,
            close_timeout=2,
        ) as ws:
            # Read the first snapshot (always arrives on connect).
            initial = await asyncio.wait_for(ws.recv(), timeout=5)
            assert initial

            # Drain anything else already queued so we can reliably
            # attribute the next snapshot to our dispatch.
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

            # Fire the dispatch from a sync requests call off-loop.
            # asyncio.to_thread keeps the event loop responsive.
            dispatch = await asyncio.to_thread(
                requests.post,
                f"{cockpit_url}/api/v1/analyze",
                headers=_auth(cockpit_token),
                json={"repo_url": _TINY_REPO},
                timeout=10,
            )
            assert dispatch.status_code == 202
            job_id = dispatch.json()["job_id"]

            try:
                # Expect at least one new snapshot within 5 s. The
                # server polls POLL_INTERVAL_S=0.5s so the window is
                # generous but bounded.
                new_snapshot = await asyncio.wait_for(ws.recv(), timeout=5)
                return (job_id, len(new_snapshot), new_snapshot[:200])
            finally:
                # Cleanup: cancel immediately regardless.
                await asyncio.to_thread(
                    requests.post,
                    f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
                    headers=_auth(cockpit_token),
                    timeout=5,
                )

    job_id, n, preview = _run_async(_observe, timeout=30)
    assert n > 0, (
        f"WS did not receive a state snapshot within 5s of dispatching "
        f"{job_id} — the change-detection loop is not reacting to disk."
    )


# ── SSE /stream requires a bearer ────────────────────────────────────────


def test_sse_stream_without_bearer_is_401(cockpit_url: str):
    """A silent regression class: a refactor that splits router
    registration can accidentally move a handler outside the
    ``Depends(verify_token)`` gate. Pin that ``/api/v1/stream/{id}``
    — the highest-value leak if left open, since it exposes live
    stdout of server subprocesses — rejects an unauthenticated
    reader."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/stream/any-job-id-whatever",
        headers={"Accept": "text/event-stream"},
        timeout=10,
    )
    assert resp.status_code == 401, (
        f"SSE stream without bearer must 401; got {resp.status_code}. "
        "A 200 empty stream or 404 would mean the auth gate was silently lost."
    )


# ── ETag is self-verifiable SHA-256 of the body ──────────────────────────


def test_dashboard_js_etag_matches_sha256_of_body(cockpit_url: str):
    """The server computes ``ETag = '"' + sha256(body)[:16] + '"'``.
    Verifying this independently makes the ETag *integrity-proof*
    for the cockpit: if an intercepting proxy rewrites the JS, the
    ETag can't keep matching unless the proxy also recomputes it
    (most don't).

    We ask for raw bytes (identity encoding) so we can hash what
    the server actually served, not a decompressed approximation."""
    resp = requests.get(
        f"{cockpit_url}/dashboard.js",
        headers={"Accept-Encoding": "identity"},
        timeout=10,
    )
    assert resp.status_code == 200
    declared = (resp.headers.get("etag") or "").strip('"')
    assert declared, "dashboard.js response missing ETag"
    # 16 hex chars, lowercase — that's our contract.
    assert re.fullmatch(r"[0-9a-f]{16}", declared), (
        f"ETag has unexpected shape {declared!r} — server contract is "
        "16 lowercase hex chars (SHA-256 prefix)"
    )
    computed = hashlib.sha256(resp.content).hexdigest()[:16]
    assert computed == declared, (
        f"ETag {declared!r} does not match sha256(body)[:16]={computed!r}. "
        "Either the server changed the derivation (update this test) or "
        "a proxy tampered with the bytes in flight."
    )


# ── Two browser contexts concurrently hit the cockpit ────────────────────


def test_two_concurrent_browser_sessions_both_go_live(
    browser: Browser, cockpit_url: str, cockpit_token: str
):
    """The real cockpit is often open in multiple tabs or browsers
    (laptop + phone, main + incognito). Pin that two independent
    browser contexts can both reach "live" status in parallel — a
    regression that serialised session init (e.g. via a file lock)
    would show up here as one context hanging on "connecting".

    Uses two separate contexts so sessionStorage + cookies are
    isolated per session — the token must flow through the conftest
    init_script into each independently."""
    import json

    token_literal = json.dumps(cockpit_token)
    init_script = (
        "try { sessionStorage.setItem('gitoma.api_token.v2', "
        + token_literal + "); } catch (e) {}"
    )

    contexts = [browser.new_context() for _ in range(2)]
    try:
        pages = []
        for ctx in contexts:
            ctx.add_init_script(init_script)
            p = ctx.new_page()
            p.goto(cockpit_url)
            pages.append(p)

        # Both pills must reach "live" within the shared window. We
        # run them in parallel-ish by starting the navigation first
        # (above) and then asserting on each — Playwright's auto-
        # retry polling does the rest.
        for idx, page in enumerate(pages):
            expect(page.locator("#conn-label")).to_have_text(
                re.compile(r"live", re.I),
                timeout=12_000,
            )
    finally:
        for ctx in contexts:
            try:
                ctx.close()
            except Exception:
                pass


# ── Unicode / confusable repo_url validation ─────────────────────────────


@pytest.mark.parametrize(
    "bad_url",
    [
        # Emoji in slug
        "https://github.com/fab/pizza-🍕-repo",
        # RTL override — visually confusable with forward-slashes
        "https://github.com/fab/name‮repo",
        # Zero-width joiner injection
        "https://github.com/fab/name‍-repo",
        # Mixed-script cyrillic/latin lookalike (а vs a)
        "https://github.com/fаb/repo",  # first 'a' is U+0430 (cyrillic)
        # Non-ASCII whitespace (ideographic space)
        "https://github.com/fab/　repo",
        # Wrong host (github.com → github.com.attacker.xyz)
        "https://github.com.attacker.xyz/fab/repo",
    ],
)
def test_unicode_and_confusable_repo_url_is_rejected(
    cockpit_url: str, cockpit_token: str, bad_url: str
):
    """The ``repo_url`` regex must reject visually-confusable strings
    AND hostnames that *look* like ``github.com`` but aren't. Each
    shape here is a real attack vector:
      * emoji / non-ASCII in owner/name: the LLM sees one thing, git
        clones something else (or crashes, but silently)
      * RTL-override + zero-width: visual spoofing of path segments
      * mixed-script: display and clone target differ
      * suffix-trick hostnames: ``github.com.X.Y`` passes a naive
        ``.endswith('github.com')`` check but clones from X.Y"""
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": bad_url},
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"Confusable/unicode repo_url ({bad_url!r}) must 422; "
        f"got {resp.status_code}. body={resp.text[:200]!r}"
    )


# ── Rapid WS connect/disconnect stress ───────────────────────────────────


def test_rapid_ws_connect_disconnect_stays_fast(
    cockpit_url: str, cockpit_token: str
):
    """60 rapid WS connect → read-one → close cycles. Each handshake
    must complete in under 2 s at p95. A server that leaks task
    references / sockets / auth state under churn would show this
    as latency climbing with cycle count. We also fail if ANY cycle
    times out — the server never got stuck in a half-closed state.

    60 cycles is large enough to detect leaks but small enough to
    stay under a 60 s test budget (at ~500 ms per cycle on tailnet
    that's 30 s worst case)."""
    import websockets

    ws_url = _ws_url(cockpit_url)
    offered = f"gitoma-bearer.{_b64url_token(cockpit_token)}"
    n_cycles = 60
    per_cycle_ms: list[float] = []

    async def _one_cycle():
        t0 = time.monotonic()
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            open_timeout=8,
            close_timeout=2,
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
        return (time.monotonic() - t0) * 1000.0

    async def _loop():
        for _ in range(n_cycles):
            per_cycle_ms.append(await _one_cycle())

    _run_async(_loop, timeout=90)

    assert len(per_cycle_ms) == n_cycles, (
        f"Only {len(per_cycle_ms)} of {n_cycles} cycles completed — "
        "server got stuck"
    )
    p50 = statistics.median(per_cycle_ms)
    p95 = sorted(per_cycle_ms)[int(n_cycles * 0.95) - 1]
    p99 = sorted(per_cycle_ms)[int(n_cycles * 0.99) - 1]
    assert p95 < 2000, (
        f"WS handshake p95 regressed under churn: "
        f"p50={p50:.0f}ms, p95={p95:.0f}ms, p99={p99:.0f}ms. "
        f"Any cycle over 5s points to socket/task leakage on the server."
    )
    # Stability check: the LAST 10 cycles must not be meaningfully
    # slower than the FIRST 10 — otherwise leakage is accreting.
    first_mean = statistics.mean(per_cycle_ms[:10])
    last_mean = statistics.mean(per_cycle_ms[-10:])
    # Allow up to 3× slowdown (generous) — real leakage shows 10×+.
    assert last_mean < max(first_mean * 3.0, first_mean + 500), (
        f"WS handshake slowed under sustained churn: "
        f"first-10 mean={first_mean:.0f}ms, last-10 mean={last_mean:.0f}ms. "
        "Server is accreting state per connection."
    )
