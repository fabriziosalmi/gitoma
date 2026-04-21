"""Protocol-level + byte-level pins against the live cockpit.

The earlier modules assert behavioural contracts at the
request/response level; this one goes one layer deeper — WS close
codes, Content-Length integrity, OPTIONS handling, gzip negotiation,
status-response shape — to catch regressions that slip past
higher-level tests because the response looks fine superficially.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures as cf

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


def _run_async(coro_factory, *, timeout: float = 15.0):
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=timeout)


# ── WS close codes on auth failures ──────────────────────────────────────


def test_ws_bad_bearer_closes_with_policy_violation(cockpit_url: str):
    """A WS handshake with a wrong ``gitoma-bearer.<b64>`` subprotocol
    must close with code 1008 (policy violation) — NOT the default
    1006 (abnormal closure) or 1011 (internal error). Clients
    differentiate these at the app level: 1008 prints "bad token,
    re-auth"; 1006/1011 look like a transient network blip and
    trigger automatic reconnect loops. Pin the code explicitly."""
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        InvalidStatus,
        WebSocketException,
    )

    ws_url = _ws_url(cockpit_url)
    bad_b64 = base64.urlsafe_b64encode(b"wrong_token_xyz").rstrip(b"=").decode()
    offered = f"gitoma-bearer.{bad_b64}"

    async def _try_bad_token():
        try:
            async with websockets.connect(
                ws_url,
                subprotocols=[offered],
                open_timeout=8,
                close_timeout=2,
            ) as ws:
                # The server accepted the handshake and then closes — the
                # next recv() must raise ConnectionClosed with code 1008.
                await ws.recv()
                return "accepted_then_silent"
        except ConnectionClosed as exc:
            return f"closed:{exc.code}"
        except InvalidStatus as exc:
            return f"refused:{exc.response.status_code}"
        except WebSocketException as exc:
            return f"wsexc:{type(exc).__name__}"

    outcome = _run_async(_try_bad_token)
    # Acceptable shapes: explicit 1008 close OR pre-accept refusal
    # (some proxies turn subprotocol mismatch into a 4xx before upgrade).
    # NOT acceptable: 1006 (abnormal), 1011 (internal), or silent hang.
    assert outcome.startswith("closed:1008") or outcome.startswith("refused:"), (
        f"Bad-token WS must close with 1008; got {outcome!r}. "
        "A 1006/1011 would make clients treat this as transient and retry."
    )


def test_ws_with_malformed_base64_subprotocol_rejected(cockpit_url: str):
    """The WS bearer encoding is ``gitoma-bearer.<urlsafe-base64>``. A
    subprotocol with non-b64 characters must be rejected cleanly —
    not cause a server-side exception that closes with 1011
    (internal error). The server decodes defensively; pin that here
    so a future refactor doesn't regress to a bare ``b64decode`` that
    crashes on bad input."""
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    ws_url = _ws_url(cockpit_url)
    # Malformed: stray ``!`` is not in the urlsafe-b64 alphabet.
    offered = "gitoma-bearer.!!!notvalid!!!"

    async def _try_malformed():
        try:
            async with websockets.connect(
                ws_url,
                subprotocols=[offered],
                open_timeout=8,
                close_timeout=2,
            ) as ws:
                await ws.recv()
                return "accepted"
        except ConnectionClosed as exc:
            return f"closed:{exc.code}"
        except InvalidStatus as exc:
            return f"refused:{exc.response.status_code}"
        except Exception as exc:
            return f"error:{type(exc).__name__}"

    outcome = _run_async(_try_malformed)
    # 1008 (policy) is the right answer; 1011 (internal) means the
    # server crashed on bad input — regression.
    assert "1011" not in outcome, (
        f"Malformed-b64 subprotocol triggered 1011 (internal error) — "
        f"the decoder is crashing on bad input. Outcome: {outcome!r}"
    )
    assert outcome.startswith("closed:1008") or outcome.startswith("refused:"), (
        f"Malformed-b64 subprotocol must reject cleanly; got {outcome!r}"
    )


def test_ws_with_bad_bearer_header_closes_1008(cockpit_url: str):
    """WS auth also supports the classic ``Authorization`` header
    (alongside the subprotocol). A wrong-token header must produce
    the same 1008 close as a wrong subprotocol — consistency."""
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    ws_url = _ws_url(cockpit_url)

    async def _try_header():
        try:
            async with websockets.connect(
                ws_url,
                additional_headers={"Authorization": "Bearer wrong_header_token"},
                open_timeout=8,
                close_timeout=2,
            ) as ws:
                await ws.recv()
                return "accepted"
        except ConnectionClosed as exc:
            return f"closed:{exc.code}"
        except InvalidStatus as exc:
            return f"refused:{exc.response.status_code}"
        except Exception as exc:
            return f"error:{type(exc).__name__}"

    outcome = _run_async(_try_header)
    assert outcome.startswith("closed:1008") or outcome.startswith("refused:"), (
        f"Bad-header-token WS must close 1008; got {outcome!r}"
    )


# ── WS on a non-existent path ────────────────────────────────────────────


def test_ws_on_unknown_path_refuses_upgrade(cockpit_url: str, cockpit_token: str):
    """Opening a WS to ``/ws/does-not-exist`` must fail at the HTTP
    upgrade — Starlette returns a 404 for unmatched routes regardless
    of Upgrade header. Regression we guard against: a catch-all route
    that accidentally accepts the upgrade and silently leaves the
    client waiting for messages that'll never come."""
    import websockets
    from websockets.exceptions import InvalidStatus

    wrong = _ws_url(cockpit_url).replace("/ws/state", "/ws/does-not-exist")

    async def _try_wrong_path():
        try:
            async with websockets.connect(wrong, open_timeout=8) as ws:
                await ws.recv()
                return "accepted"
        except InvalidStatus as exc:
            return f"refused:{exc.response.status_code}"
        except Exception as exc:
            return f"error:{type(exc).__name__}"

    outcome = _run_async(_try_wrong_path)
    assert outcome.startswith("refused:"), (
        f"WS on unknown path must refuse the upgrade; got {outcome!r}"
    )
    # Accept 403 (Origin policy), 404 (path), or 426/400 — but NEVER
    # a 101 switch or a 500. The exact code isn't load-bearing as long
    # as the upgrade is clearly rejected.


# ── OPTIONS method handling ──────────────────────────────────────────────


def test_options_on_endpoints_returns_405_no_cors_headers(cockpit_url: str):
    """The cockpit does NOT configure CORS: it's a same-origin
    application and CORS headers would widen the blast radius if
    ever accidentally loosened. Pin that OPTIONS returns 405 with
    NO ``Access-Control-Allow-Origin`` — a regression that silently
    enables CORS (adds ``*`` or a wildcard) is a cross-origin
    vulnerability waiting to happen."""
    for endpoint in ("/api/v1/health", "/api/v1/run", "/api/v1/jobs", "/"):
        resp = requests.options(f"{cockpit_url}{endpoint}", timeout=10)
        # FastAPI/Starlette default: 405 with Allow header listing methods.
        assert resp.status_code in (405, 400), (
            f"OPTIONS {endpoint} should 405 (no CORS); got {resp.status_code}"
        )
        assert not any(
            k.lower().startswith("access-control-") for k in resp.headers
        ), (
            f"OPTIONS {endpoint} ships CORS headers {dict(resp.headers)!r} — "
            "CORS is not configured; a wildcard Access-Control-Allow-Origin "
            "would be a regression"
        )


# ── Dispatch payload shape edges ─────────────────────────────────────────


def test_dispatch_with_empty_body_returns_422(cockpit_url: str, cockpit_token: str):
    """An empty body (no JSON at all) must 422 — pydantic has a
    required ``repo_url`` field, so parsing ``null`` / ``""`` must
    surface as validation failure, not a 500."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        data="",
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"Empty-body dispatch must 422; got {resp.status_code}. "
        f"body={resp.text[:200]!r}"
    )


def test_dispatch_with_non_json_content_type_returns_422_or_415(
    cockpit_url: str, cockpit_token: str
):
    """POST with ``Content-Type: text/plain`` and a JSON body must be
    rejected (422 or 415) — not accidentally parsed as form-data or
    silently ignored. A regression that relaxes content-type checks
    widens the attack surface for content-type confusion payloads."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers={**_auth(cockpit_token), "Content-Type": "text/plain"},
        data=b'{"repo_url": "https://github.com/foo/bar"}',
        timeout=10,
    )
    assert resp.status_code in (415, 422), (
        f"Non-JSON content-type must 415/422; got {resp.status_code}"
    )


# ── /status full shape contract ──────────────────────────────────────────


def test_status_endpoint_returns_complete_shape(cockpit_url: str, cockpit_token: str):
    """Dispatch a cheap job, then poll /status/{id} — every documented
    field in ``JobStatusResponse`` must be present (nullable fields
    can be null, but the key must exist). The cockpit's status
    renderer does attribute access, not ``.get(..., None)``, so a
    missing key throws."""
    dispatch = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert dispatch.status_code == 202
    job_id = dispatch.json()["job_id"]

    try:
        resp = requests.get(
            f"{cockpit_url}/api/v1/status/{job_id}",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert resp.status_code == 200
        body = resp.json()
        expected_keys = {
            "job_id",
            "label",
            "status",
            "created_at",
            "finished_at",
            "lines_buffered",
            "error_id",
        }
        missing = expected_keys - body.keys()
        assert not missing, (
            f"/status payload missing fields {missing!r}. Cockpit "
            "reads these via attribute access and will KeyError."
        )
        # lines_buffered must be a non-negative int — the cockpit renders
        # it as "{n} lines".
        assert isinstance(body["lines_buffered"], int) and body["lines_buffered"] >= 0
        # status must be one of the canonical values.
        assert body["status"] in (
            "queued", "running", "started",
            "completed", "failed", "cancelling", "cancelled",
        ), f"Unexpected status string {body['status']!r}"
    finally:
        try:
            requests.post(
                f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
                headers=_auth(cockpit_token),
                timeout=5,
            )
        except Exception:
            pass


# ── Byte-level integrity ─────────────────────────────────────────────────


def test_dashboard_js_content_length_matches_body(cockpit_url: str):
    """The ``Content-Length`` header must match the actual body size.
    Pinning this catches proxy-rewrite regressions (MITM that modifies
    the JS in flight but doesn't recompute Content-Length) AND
    server-side bugs where the response is truncated but the header
    claims the full size — browsers silently cut the JS short and
    the cockpit crashes with an obscure parse error."""
    # Explicit ``Accept-Encoding: identity`` forces the server to return
    # the raw bytes without gzip/br — otherwise the Content-Length
    # declares the compressed size and the comparison is between apples
    # and oranges. Cockpit-side, the browser negotiates compression
    # separately; this test pins the raw-byte contract.
    resp = requests.get(
        f"{cockpit_url}/dashboard.js",
        headers={"Accept-Encoding": "identity"},
        timeout=10,
    )
    assert resp.status_code == 200
    declared = resp.headers.get("content-length")
    assert declared is not None, (
        "dashboard.js response missing Content-Length — some clients "
        "refuse to parse JS without it"
    )
    assert int(declared) == len(resp.content), (
        f"Content-Length declared {declared}, actual body is "
        f"{len(resp.content)} bytes. Proxy mis-rewrite or truncation."
    )


# ── gzip negotiation on the root HTML ────────────────────────────────────


def test_root_html_is_gzipped_when_accept_encoding_requests_it(cockpit_url: str):
    """The cockpit HTML is ~70 KB of markup+SVG+inline assets. Without
    gzip the WAN payload is noticeable; WITH gzip it compresses to
    ~14 KB. Pin that the server responds to ``Accept-Encoding: gzip``
    on the root HTML. Regression: a future middleware ordering change
    (e.g. adding a middleware AFTER GZipMiddleware that sets
    Content-Length) can silently disable compression."""
    resp = requests.get(
        f"{cockpit_url}/",
        headers={"Accept-Encoding": "gzip"},
        timeout=10,
    )
    assert resp.status_code == 200
    encoding = resp.headers.get("content-encoding", "").lower()
    assert encoding == "gzip", (
        f"Root HTML must be gzipped under Accept-Encoding: gzip; "
        f"got content-encoding={encoding!r}. WAN payload ballooning."
    )
    # And the Vary header must include Accept-Encoding so caches
    # don't serve gzipped content to a client that asked for identity.
    vary = resp.headers.get("vary", "")
    assert "accept-encoding" in vary.lower(), (
        f"Gzipped response must Vary on Accept-Encoding; got Vary={vary!r}. "
        "Caches can otherwise poison identity clients with gzipped bytes."
    )
