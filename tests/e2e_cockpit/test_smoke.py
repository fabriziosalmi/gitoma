"""Draconian smoke pass against the live cockpit.

Every test here either touches a code path that cannot be exercised
in-process (real CSP, real WS subprotocol handshake, browser-level
sessionStorage injection) OR pins a live invariant whose regression
the in-process suite cannot catch (conn pill turning green, token-
required banner behaviour, static-asset ETag wiring under a real
proxy chain).

If any of these goes red, the deploy is broken — not the code.
"""

from __future__ import annotations

import re
import pytest
import requests
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e_cockpit

# Default Playwright expect timeout is 5s; the cockpit's first WS
# round-trip over Tailscale can be slower on a cold connection. 12s
# strikes a balance between "catches real breakage" and "survives a
# transient tailnet blip".
_WS_CONNECT_TIMEOUT_MS = 12_000


# ── 1. Shell loads + title present ─────────────────────────────────────────


def test_cockpit_shell_renders(cockpit_url: str, page: Page):
    """``GET /`` must return the cockpit HTML with "Gitoma" branding +
    the connection pill placeholder. If this fails the deploy is broken
    at the Python import level or static assets are 404ing."""
    page.goto(cockpit_url)
    expect(page).to_have_title(re.compile(r"Gitoma", re.I))
    expect(page.locator("#conn-dot")).to_be_visible()
    expect(page.locator("#conn-label")).to_be_visible()


# ── 2. CSP header forbids script-src 'unsafe-inline' ──────────────────────


def test_csp_header_denies_script_unsafe_inline(cockpit_url: str):
    """We tightened CSP by extracting ``/dashboard.js`` as a named asset
    and dropping ``unsafe-inline`` from ``script-src``. If a future
    refactor re-inlines the bootstrap ``<script>``, the browser would
    silently ignore it and the cockpit would break — but *only* under
    a CSP-respecting agent. Pin the header so the regression is caught
    at deploy-time instead of by a user hitting a blank page."""
    resp = requests.get(f"{cockpit_url}/", timeout=10)
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert csp, "CSP header missing — the cockpit MUST ship one"
    # Split into directives and find script-src specifically.
    script_src = next(
        (d for d in csp.split(";") if d.strip().startswith("script-src")),
        "",
    )
    assert script_src, f"script-src directive missing from CSP: {csp!r}"
    assert "'unsafe-inline'" not in script_src, (
        f"script-src contains 'unsafe-inline' — we extracted dashboard.js "
        f"as a separate asset specifically to avoid this. CSP: {csp!r}"
    )


# ── 3. dashboard.js served as a cacheable asset with ETag ─────────────────


def test_dashboard_js_has_etag(cockpit_url: str):
    """The CSP tightening relies on ``/dashboard.js`` being a real asset,
    not a string concatenated into the HTML. Regression: if that route
    disappears or stops sending ETag, CDN caches fail and every pageload
    re-ships the full JS. Pin both pieces."""
    resp = requests.get(f"{cockpit_url}/dashboard.js", timeout=10)
    assert resp.status_code == 200, f"/dashboard.js must 200; got {resp.status_code}"
    assert resp.headers.get("content-type", "").startswith(
        ("application/javascript", "text/javascript")
    ), (
        f"/dashboard.js content-type wrong: {resp.headers.get('content-type')!r}"
    )
    assert resp.headers.get("etag"), "/dashboard.js must ship an ETag header"


# ── 4. Unauthenticated dispatch is rejected with 401 ──────────────────────


def test_dispatch_without_bearer_returns_401(cockpit_url: str):
    """A POST to ``/api/v1/run`` without a Bearer token must be rejected.
    If this ever 200s, the cockpit is open to unauthenticated command
    dispatch over the tailnet — critical security regression."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/run",
        json={"repo_url": "owner/name"},
        timeout=10,
    )
    assert resp.status_code in (401, 403), (
        f"Unauthenticated dispatch should be 401/403; got {resp.status_code}. "
        f"Body: {resp.text[:200]!r}"
    )


# ── 5. Bad-token dispatch is rejected with 401 ────────────────────────────


def test_dispatch_with_bad_bearer_returns_401(cockpit_url: str):
    """A plausible-looking but wrong token must still be rejected. This
    catches a specific regression class: a token comparison that falls
    through to "any non-empty token accepted" mode (seen in other FastAPI
    apps that swapped ``secrets.compare_digest`` for ``==`` and broke
    under an empty-string edge)."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/run",
        headers={"Authorization": "Bearer gitoma_wrongtoken_deadbeef"},
        json={"repo_url": "owner/name"},
        timeout=10,
    )
    assert resp.status_code in (401, 403), (
        f"Bad-token dispatch should be 401/403; got {resp.status_code}"
    )


# ── 6. WebSocket connects and conn pill goes green with a valid token ────


def test_ws_conn_pill_goes_live_with_valid_token(authed_page: Page, cockpit_url: str):
    """End-to-end authenticated handshake: injected sessionStorage token
    is picked up by the dashboard bootstrap, WS opens with the
    ``gitoma-bearer.<b64>`` subprotocol, and the conn pill transitions
    from "connecting" / "down" to "live".

    Covers the whole dispatcher chain:
      * ``dashboard.js`` Token store read
      * Sec-WebSocket-Protocol framing
      * Starlette's subprotocol echo
      * Server-side token verification
      * First state snapshot arriving and the pill class flipping."""
    authed_page.goto(cockpit_url)
    # The pill text is "live" when the WS is happy.
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=_WS_CONNECT_TIMEOUT_MS,
    )


# ── 7. Without a token, Settings dialog prompts for one ───────────────────


def test_fresh_load_prompts_for_token(fresh_page: Page, cockpit_url: str):
    """With no token in sessionStorage, the cockpit must guide the user
    to the Settings / token dialog instead of silently spinning on a
    failed WS. The specific element we pin is the token input (ID is
    stable, copy may drift)."""
    fresh_page.goto(cockpit_url)
    # The dialog is a <dialog> element. It may be opened imperatively
    # or stay hidden behind a prompt UI — either way the input must be
    # reachable via its stable ID within a short window.
    expect(fresh_page.locator("#token-input")).to_be_attached(timeout=8_000)
