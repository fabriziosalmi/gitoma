"""Browser-level UI flow coverage.

Where ``test_smoke.py`` pins the shell + auth handshake, this file
exercises interactive flows the operator actually hits in daily use:

  * Command palette open/close (⌘K, Ctrl+K)
  * Settings / token dialog save → sessionStorage persisted → pill live
  * Extended CSP: style-src + style-src-attr + connect-src + frame-
    ancestors (the directives we explicitly ship to lock the cockpit
    against obvious XSS/clickjacking shapes)
  * Repos sidebar populates from the live jobs dict

None of these mutate cockpit state; the Settings-dialog test cleans
up by clearing the token back from sessionStorage at teardown.
"""

from __future__ import annotations

import json
import re

import pytest
import requests
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e_cockpit

_WS_CONNECT_TIMEOUT_MS = 12_000


# ── Command palette ────────────────────────────────────────────────────────


def test_command_palette_opens_via_keyboard(authed_page: Page, cockpit_url: str):
    """Ctrl+K / ⌘K must open the palette dialog. The cockpit's UX
    contract: operators never need the mouse to dispatch a command."""
    authed_page.goto(cockpit_url)
    # Wait for the shell to be interactive; the palette handler is
    # registered on DOMContentLoaded but the input only focuses after
    # the dialog animation starts.
    expect(authed_page.locator("#palette-btn")).to_be_visible()
    # Press the platform-appropriate shortcut. Playwright normalises
    # ``ControlOrMeta`` to Cmd on macOS, Ctrl elsewhere.
    authed_page.keyboard.press("ControlOrMeta+k")
    expect(authed_page.locator("#palette-dialog")).to_have_attribute("open", "", timeout=3000)
    expect(authed_page.locator("#palette-input")).to_be_focused(timeout=3000)


def test_command_palette_closes_on_escape(authed_page: Page, cockpit_url: str):
    """Escape must close the palette. A regression that traps focus
    here would be the kind of UX paper-cut that drives operators to
    the mouse."""
    authed_page.goto(cockpit_url)
    authed_page.keyboard.press("ControlOrMeta+k")
    expect(authed_page.locator("#palette-dialog")).to_have_attribute("open", "")
    authed_page.keyboard.press("Escape")
    # ``open`` attribute should be removed once closed. Use not.to_have
    # with a reasonable timeout because the close is animated.
    expect(authed_page.locator("#palette-dialog")).not_to_have_attribute("open", "", timeout=3000)


# ── Settings / token dialog ───────────────────────────────────────────────


def test_settings_button_opens_token_dialog(authed_page: Page, cockpit_url: str):
    """Clicking the gear in the header must open the token dialog.
    The dialog's ``<dialog>`` element exposes ``open`` once shown."""
    authed_page.goto(cockpit_url)
    expect(authed_page.locator("#settings-btn")).to_be_visible()
    authed_page.locator("#settings-btn").click()
    expect(authed_page.locator("#token-dialog")).to_have_attribute(
        "open", "", timeout=3000
    )
    expect(authed_page.locator("#token-input")).to_be_focused(timeout=3000)


def test_token_save_persists_and_ws_stays_live(fresh_page: Page, cockpit_url: str, cockpit_token: str):
    """The full paste-token flow: no token → open settings → paste →
    save → sessionStorage persists → WS reconnects → conn pill 'live'.
    This is the first-time operator onboarding flow and must be
    bulletproof."""
    fresh_page.goto(cockpit_url)
    # Dialog should already be attached; open it if not auto-opened.
    if not fresh_page.locator("#token-dialog[open]").count():
        fresh_page.locator("#settings-btn").click()
    expect(fresh_page.locator("#token-dialog")).to_have_attribute("open", "")
    fresh_page.locator("#token-input").fill(cockpit_token)
    # The save button is the submit. Submit the form.
    fresh_page.locator("#token-form button[type=submit]").click()
    # Dialog closes on save.
    expect(fresh_page.locator("#token-dialog")).not_to_have_attribute(
        "open", "", timeout=3000
    )
    # sessionStorage now has the v2 key.
    stored = fresh_page.evaluate("sessionStorage.getItem('gitoma.api_token.v2')")
    assert stored == cockpit_token, (
        f"sessionStorage should hold the saved token; got {stored!r}"
    )
    # And the WS reconnects with the new token.
    expect(fresh_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=_WS_CONNECT_TIMEOUT_MS,
    )


# ── Extended CSP ──────────────────────────────────────────────────────────


def _get_csp(cockpit_url: str) -> str:
    resp = requests.get(f"{cockpit_url}/", timeout=10)
    return resp.headers.get("content-security-policy", "")


def _directive(csp: str, name: str) -> str:
    """Return the body of a single CSP directive (empty string if absent)."""
    for d in csp.split(";"):
        d = d.strip()
        if d.startswith(f"{name} "):
            return d
    return ""


def test_csp_has_style_src_with_sha256_hash(cockpit_url: str):
    """The cockpit ships one inline ``<style>`` that we explicitly hash
    (SHA-256) into the CSP. A regression that either (a) re-inlines new
    styles without updating the hash or (b) drops the style-src
    directive entirely would break rendering for CSP-respecting browsers.
    Pin the hash-based directive."""
    csp = _get_csp(cockpit_url)
    style_src = _directive(csp, "style-src")
    assert style_src, f"style-src directive missing from CSP: {csp!r}"
    assert "'sha256-" in style_src, (
        f"style-src must whitelist the inline <style> via sha256 hash "
        f"(no blanket unsafe-inline); got: {style_src!r}"
    )
    assert "'unsafe-inline'" not in style_src, (
        f"style-src must NOT fall back to 'unsafe-inline'; got: {style_src!r}"
    )


def test_csp_has_style_src_attr(cockpit_url: str):
    """``style-src-attr`` controls inline ``style=""`` attributes —
    distinct from ``style-src`` which governs ``<style>`` blocks. We
    set it explicitly so a future framework addition that starts
    writing ``style="..."`` fails loudly instead of appearing to work
    in permissive browsers and breaking in strict ones."""
    csp = _get_csp(cockpit_url)
    style_src_attr = _directive(csp, "style-src-attr")
    assert style_src_attr, f"style-src-attr missing from CSP: {csp!r}"


def test_csp_frame_ancestors_denies_embedding(cockpit_url: str):
    """The cockpit is not meant to be iframed. ``frame-ancestors`` must
    explicitly reject embedding — otherwise a tailnet-adjacent attacker
    with a cross-site endpoint could clickjack the Run button."""
    csp = _get_csp(cockpit_url)
    fa = _directive(csp, "frame-ancestors")
    assert fa, f"frame-ancestors missing — cockpit embeddable via iframe: {csp!r}"
    # Either ``'none'`` or ``'self'`` is acceptable; a bare ``*`` is not.
    assert "'none'" in fa or "'self'" in fa, (
        f"frame-ancestors must be 'none' or 'self', not {fa!r}"
    )
    assert " *" not in fa and fa != "frame-ancestors *", (
        f"frame-ancestors must not whitelist wildcards: {fa!r}"
    )


def test_csp_connect_src_allows_ws_same_origin(cockpit_url: str):
    """``connect-src`` must allow WebSocket connections to the same
    origin (``'self'`` or explicit ws:/wss: schemes). Regression that
    forgets the ws scheme has made the cockpit blank-screen in the
    past — CSP blocks the upgrade silently."""
    csp = _get_csp(cockpit_url)
    cs = _directive(csp, "connect-src")
    assert cs, f"connect-src missing from CSP: {csp!r}"
    # 'self' implicitly allows ws:/wss: in modern browsers. Either
    # 'self' OR an explicit ws(s): token is acceptable.
    lowered = cs.lower()
    assert "'self'" in lowered or "ws:" in lowered or "wss:" in lowered, (
        f"connect-src must permit WebSocket to same origin: {cs!r}"
    )


# ── Sidebar / repos panel ─────────────────────────────────────────────────


def test_repos_sidebar_renders(authed_page: Page, cockpit_url: str):
    """With any persisted state on the server, the Repositories card
    must render with a repo-count pill. Empty = '0' is still a pass —
    the bug we pin against is 'pill missing entirely' (cockpit thinks
    the state fetch failed and leaves the section blank)."""
    authed_page.goto(cockpit_url)
    expect(authed_page.locator("#repo-count")).to_be_attached(timeout=5000)
    # The WS-populated section must render within a few seconds.
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=_WS_CONNECT_TIMEOUT_MS,
    )


# ── No console errors on authenticated load ───────────────────────────────


def test_authenticated_load_produces_no_console_errors(authed_page: Page, cockpit_url: str):
    """The strict cutoff: after the cockpit is loaded with a valid
    bearer token, ZERO console errors must fire during the first
    5 seconds. This catches the regression class where a minor JS
    refactor introduces a silent ReferenceError that only the console
    catches (the UI appears to work, but a specific button is dead)."""
    errors: list[str] = []

    def _on_msg(msg):
        if msg.type == "error":
            errors.append(msg.text)

    authed_page.on("console", _on_msg)

    # Also catch uncaught page-level errors (throwables not caught by
    # ``try { ... }``).  Playwright exposes this as ``pageerror``.
    def _on_page_error(exc):
        errors.append(f"pageerror: {exc}")

    authed_page.on("pageerror", _on_page_error)

    authed_page.goto(cockpit_url)
    # Wait for the WS to be live — that's the "fully booted" signal.
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=_WS_CONNECT_TIMEOUT_MS,
    )
    # Give the post-boot tick a moment to settle (ping, first jobs
    # poll, repo-count update). If any of those error, we catch it.
    authed_page.wait_for_timeout(2000)

    # Some errors are environmental and unavoidable on a dev box (a
    # favicon 404, a stray network hiccup) — be strict about gitoma's
    # own JS but tolerant of those. If the error mentions a gitoma
    # module / handler name, fail; otherwise log and skip.
    _benign = ("favicon", "net::err_internet_disconnected", "net::err_failed")
    fatal = [e for e in errors if not any(b in e.lower() for b in _benign)]
    assert not fatal, (
        "Cockpit produced console/page errors during authenticated boot:\n"
        + "\n".join(f"  - {e}" for e in fatal)
    )
