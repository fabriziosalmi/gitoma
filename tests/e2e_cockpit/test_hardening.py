"""Hardening + contract tests against the live cockpit.

Fills gaps the earlier files missed, all without mutating state:

  * Concurrency: 3 parallel dispatches → distinct job_ids, all cancel
  * State delete: path-traversal rejected (422), unknown repo is
    idempotent (``not_found``)
  * dashboard.js caching: 304 on conditional GET with matching ETag,
    security headers (``X-Content-Type-Options``, ``Referrer-Policy``)
    preserved on both 200 and 304 paths
  * HTTP method constraints: GET on dispatch endpoints → 405
  * repo_url validation edges: max-length + credentials-in-URL
  * WS subprotocol echo: the server must echo the presented
    ``gitoma-bearer.<b64>`` subprotocol on accept, otherwise
    conforming browsers reject the upgrade
  * Keyboard shortcut ``r`` opens the run dialog (operator UX pin)
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
import requests
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e_cockpit


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_TINY_REPO = "https://github.com/fabriziosalmi/b2v"


# ── Concurrency: parallel dispatches ──────────────────────────────────────


def test_three_parallel_analyze_dispatches_get_distinct_job_ids(
    cockpit_url: str, cockpit_token: str
):
    """Three concurrent dispatches must each get their own job_id.
    A shared/duplicate job_id would mean the ``_JOBS_LOCK`` isn't
    doing its job and the server is racing on the dict key. We cancel
    all three immediately so nothing expensive actually runs."""
    import concurrent.futures as cf

    def _dispatch() -> str:
        resp = requests.post(
            f"{cockpit_url}/api/v1/analyze",
            headers=_auth(cockpit_token),
            json={"repo_url": _TINY_REPO},
            timeout=10,
        )
        assert resp.status_code == 202, resp.text[:200]
        return resp.json()["job_id"]

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(_dispatch) for _ in range(3)]
        job_ids = [f.result() for f in futures]

    try:
        assert len(set(job_ids)) == 3, (
            f"Concurrent dispatches produced a duplicate job_id: {job_ids}. "
            "The _JOBS_LOCK is not serialising dict mutations."
        )
        # Every one must appear in the jobs list.
        all_jobs = requests.get(
            f"{cockpit_url}/api/v1/jobs", headers=_auth(cockpit_token), timeout=10
        ).json()
        for jid in job_ids:
            assert jid in all_jobs, (
                f"Job {jid} dispatched 202 but missing from /jobs list — "
                "race between dispatch ack and dict insert"
            )
    finally:
        # Cleanup: cancel all three.
        for jid in job_ids:
            try:
                requests.post(
                    f"{cockpit_url}/api/v1/jobs/{jid}/cancel",
                    headers=_auth(cockpit_token),
                    timeout=5,
                )
            except Exception:
                pass


# ── State deletion safety ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "malicious_owner,malicious_name",
    [
        ("..", "repo"),
        ("owner", ".."),
        (".hidden", "repo"),  # starts with dot — rejected by regex
        ("owner", "name with space"),
        ("owner", "na$me"),
        ("owner", "na/me"),  # extra slash — must break route matching
    ],
)
def test_state_delete_rejects_path_traversal(
    cockpit_url: str, cockpit_token: str, malicious_owner: str, malicious_name: str
):
    """``DELETE /api/v1/state/{owner}/{name}`` flows into a filesystem
    path. A slug that escapes the state directory must 422 at the
    router level — if it ever reaches the filesystem helper we could
    unlink files outside ``~/.gitoma/state``. This is a security
    regression worth pinning 6× over."""
    resp = requests.delete(
        f"{cockpit_url}/api/v1/state/{malicious_owner}/{malicious_name}",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    # 422 is the intended response; 404 from FastAPI's routing is also
    # acceptable because some malicious shapes (``..``, slashes) break
    # the route match itself and never reach the validator.
    assert resp.status_code in (404, 422), (
        f"Malicious slug owner={malicious_owner!r} name={malicious_name!r} "
        f"must be rejected (404 route-mismatch or 422 validated); "
        f"got {resp.status_code}"
    )


def test_state_delete_unknown_repo_is_idempotent(cockpit_url: str, cockpit_token: str):
    """Deleting state for a repo that never had one must 200 with
    ``result=not_found``, NOT 404. That makes the endpoint idempotent
    so the cockpit's Reset button can fire it blindly."""
    resp = requests.delete(
        f"{cockpit_url}/api/v1/state/definitely-not-an-owner/never-existed-123xyz",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 200, (
        f"Unknown state must be idempotent (200 not_found); got {resp.status_code}"
    )
    body = resp.json()
    assert body.get("result") == "not_found", (
        f"Unknown state must carry result='not_found'; got {body!r}"
    )


# ── dashboard.js caching + security headers ──────────────────────────────


def test_dashboard_js_conditional_get_returns_304(cockpit_url: str):
    """The dashboard.js route advertises an ETag so a cockpit reload
    costs 304 bytes, not 30 KB. Pin the conditional-GET contract:
    ``If-None-Match`` with the current ETag must 304."""
    # First request to discover the ETag.
    r1 = requests.get(f"{cockpit_url}/dashboard.js", timeout=10)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag, "dashboard.js must ship an ETag"

    # Conditional GET with the ETag.
    r2 = requests.get(
        f"{cockpit_url}/dashboard.js",
        headers={"If-None-Match": etag},
        timeout=10,
    )
    assert r2.status_code == 304, (
        f"If-None-Match with matching ETag must 304; got {r2.status_code}. "
        "A cockpit reload is now paying full JS bytes on every tab open."
    )
    # The 304 must echo the ETag back so a strict cache knows it matched.
    assert r2.headers.get("etag") == etag, (
        "304 response must carry the same ETag so the cache can refresh "
        f"its validator. Got: {r2.headers.get('etag')!r}"
    )


def test_dashboard_js_ships_nosniff_and_no_referrer(cockpit_url: str):
    """Two belt-and-braces headers we explicitly set on the JS route:
      * ``X-Content-Type-Options: nosniff`` — defeats a misconfigured
        upstream proxy rewriting the content-type
      * ``Referrer-Policy: no-referrer`` — a JS asset doesn't need to
        leak the referer to wherever it might redirect
    Either missing is a silent regression; a future refactor of the
    route that copy-pastes from a vanilla FileResponse would lose them."""
    resp = requests.get(f"{cockpit_url}/dashboard.js", timeout=10)
    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options", "").lower() == "nosniff", (
        f"dashboard.js must ship X-Content-Type-Options: nosniff; "
        f"got {resp.headers.get('x-content-type-options')!r}"
    )
    rp = resp.headers.get("referrer-policy", "").lower()
    assert rp == "no-referrer", (
        f"dashboard.js must ship Referrer-Policy: no-referrer; got {rp!r}"
    )


# ── HTTP method constraints ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    ["/api/v1/run", "/api/v1/analyze", "/api/v1/review", "/api/v1/fix-ci"],
)
def test_get_on_post_endpoints_is_405(cockpit_url: str, cockpit_token: str, endpoint: str):
    """Dispatch endpoints are POST-only. A GET must 405, not 200 or
    401. This catches the regression where a future refactor adds a
    ``@router.get`` on the same path (e.g. a "status" helper) without
    removing the POST — the GET would shadow unexpectedly."""
    resp = requests.get(
        f"{cockpit_url}{endpoint}",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 405, (
        f"GET {endpoint} must be 405 (Method Not Allowed); got {resp.status_code}"
    )


# ── repo_url validation edges ────────────────────────────────────────────


def test_repo_url_max_length_is_enforced(cockpit_url: str, cockpit_token: str):
    """``max_length=255`` on repo_url. A 300-char URL is an obvious
    attack vector (resource exhaustion via cloning a maliciously-named
    repo), and pydantic enforces it pre-dispatch. 422 before anything
    touches git."""
    long_url = "https://github.com/" + ("x" * 290) + "/r"  # ~315 chars
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": long_url},
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"Overlong repo_url must 422; got {resp.status_code}"
    )


def test_repo_url_with_embedded_credentials_is_rejected(
    cockpit_url: str, cockpit_token: str
):
    """``https://user:pass@github.com/...`` is a canonical credential-
    leak vector: an operator might paste a URL copied from a private
    mirror and ship their PAT into server logs. The regex pinned in
    ``RunRequest._check_repo_url`` explicitly rejects it — pin that
    here so the regex can't loosen without visible failure."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": "https://user:secret@github.com/owner/repo"},
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"repo_url with embedded credentials must 422; got {resp.status_code}. "
        f"body={resp.text[:200]!r}"
    )


# ── WS subprotocol echo ──────────────────────────────────────────────────


def test_ws_server_echoes_subprotocol_on_accept(cockpit_url: str, cockpit_token: str):
    """The cockpit's WS auth relies on the ``gitoma-bearer.<b64>``
    subprotocol being **echoed** back on accept. Conforming browsers
    (chromium, firefox, safari) refuse the upgrade if the server
    doesn't echo at least one of the offered protocols. A server-side
    regression that forgets to call ``await ws.accept(subprotocol=...)``
    silently breaks every cockpit without a log line to anchor on.

    We build the handshake manually here rather than through chromium
    so the assertion is on the wire bytes, not the UI consequence."""
    import base64
    import websockets

    ws_url = cockpit_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/state"
    b64_token = base64.urlsafe_b64encode(cockpit_token.encode()).rstrip(b"=").decode()
    offered = f"gitoma-bearer.{b64_token}"

    async def _handshake():
        # websockets ≥13 accepts ``subprotocols`` arg; we send ours and
        # read back the one the server chose via the ``.subprotocol``
        # attribute after connect.
        async with websockets.connect(
            ws_url,
            subprotocols=[offered],
            open_timeout=10,
            close_timeout=2,
        ) as ws:
            return ws.subprotocol

    # Run inside a fresh thread so ``asyncio.run`` never races with any
    # event loop that pytest-asyncio or pytest-playwright left on the
    # main thread. Isolation > cleverness here.
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        chosen = ex.submit(lambda: asyncio.run(_handshake())).result(timeout=15)
    assert chosen == offered, (
        f"WS server must echo the offered subprotocol {offered!r}; "
        f"got {chosen!r}. Browsers will refuse the upgrade."
    )


# ── Keyboard shortcut: 'r' opens run dialog ──────────────────────────────


def test_keyboard_shortcut_r_opens_run_dialog(authed_page: Page, cockpit_url: str):
    """Pressing ``r`` (no modifier, nothing focused, no dialog open)
    must open the Run dialog. Pin the UX contract from dashboard.js
    (the map `{r: "run", a: "analyze", v: "review", f: "fix-ci"}`)
    so a refactor that drops the dispatch accidentally breaks this
    test instead of quietly disabling keyboard-only dispatch."""
    authed_page.goto(cockpit_url)
    # Ensure WS is live so the UI is fully booted.
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=12_000,
    )
    # Focus the body so any inherited focus (e.g. a button) doesn't
    # suppress the handler. The handler bails on INPUT/TEXTAREA focus.
    authed_page.evaluate("document.body.focus();")
    authed_page.keyboard.press("r")
    expect(authed_page.locator("#run-dialog")).to_have_attribute(
        "open", "", timeout=3000
    )
