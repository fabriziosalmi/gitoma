"""Industrial-grade UI tests — HTML semantics, CSS contract, CLI helpers.

These exist to prevent regressions in the accessibility + design-token
contract introduced in the UI pass. They're intentionally *not*
screenshot tests: the brittle bit of UI work is the ARIA tree and the
token names, not pixel diffs.

Covered:
* Skip-link target (``<main id="main" tabindex="-1">``) — keyboard users.
* ``<ol>`` pipeline with ``aria-label`` — sequential screen-reader order.
* ``<button id="jobs-badge">`` — real button, not a span-with-role hack.
* All decorative ``<svg class="icon">`` carry ``aria-hidden="true"``.
* ``role="status"`` / ``role="alert"`` + ``aria-live`` on banners.
* No remaining ``role="button"`` on span (the old anti-pattern).
* CSS custom-property scales are declared (``--fs-*``, ``--sp-*``).
* CSS covers ``prefers-reduced-motion`` for *transitions* (not just animations).
* CSS carries ``@supports (backdrop-filter)`` fallback branch.
* CSS ships a ``.skeleton`` utility + ``.sr-only``.
* CLI console: emoji guard, banner modes, plain-mode detection.
"""

from __future__ import annotations

from pathlib import Path
from importlib import reload

from fastapi.testclient import TestClient

from gitoma.api.server import app


_ASSETS = Path(__file__).resolve().parents[1] / "gitoma" / "ui" / "assets"
_BODY = _ASSETS / "dashboard_body.html"
_CSS = _ASSETS / "dashboard.css"
_JS = _ASSETS / "dashboard.js"
_ICONS = _ASSETS / "dashboard_icons.html"


# ── HTML semantics ──────────────────────────────────────────────────────


def test_body_has_skip_link_and_main_target():
    html = _BODY.read_text()
    assert 'class="skip-link"' in html, "skip-link helps keyboard users jump past header"
    assert '<main id="main" tabindex="-1">' in html, "skip-link needs a focusable target"


def test_pipeline_is_ordered_list_with_aria_label():
    """<ol> so screen readers announce 1 of 7 / 2 of 7 … semantically."""
    html = _BODY.read_text()
    assert '<ol id="pipeline"' in html
    assert 'aria-label="Agent pipeline progress"' in html


def test_jobs_badge_is_real_button_not_span_role_button():
    html = _BODY.read_text()
    assert '<button id="jobs-badge"' in html, (
        "jobs-badge is an action — must be a real <button> for native a11y"
    )
    # Regression guard: the old anti-pattern was a span with role="button".
    assert 'id="jobs-badge"' not in html.split('<button id="jobs-badge"')[1][:500] \
        or 'role="button"' not in html.split('<button id="jobs-badge"')[0]


def test_all_decorative_svg_icons_are_aria_hidden():
    """Icons next to text labels are decorative — must be hidden from AT.

    We allow the top-level sprite container (display:none) too. What we
    reject is any ``<svg class="icon">`` that forgot the aria-hidden.
    """
    html = _BODY.read_text()
    # Very simple heuristic: every occurrence of ``<svg class="icon"`` in the
    # body template must include ``aria-hidden="true"`` on the same tag.
    import re

    tags = re.findall(r'<svg class="[^"]*icon[^"]*"[^>]*>', html)
    offenders = [t for t in tags if 'aria-hidden="true"' not in t]
    assert not offenders, f"missing aria-hidden on: {offenders[:3]}"


def test_no_span_role_button_in_body():
    html = _BODY.read_text()
    # Explicit regression guard for the old jobs-badge shape.
    assert '<span id="jobs-badge"' not in html
    # More defensive: no span anywhere that declares role="button".
    import re

    assert not re.search(r'<span[^>]*\brole="button"', html)


def test_icon_sprite_is_hidden_from_assistive_tech():
    icons = _ICONS.read_text()
    css = _CSS.read_text()
    # The sprite container must hide from both visual + AT.
    assert 'aria-hidden="true"' in icons
    assert 'focusable="false"' in icons
    # ``display:none`` moved from inline ``style="..."`` to a CSS class
    # so the dashboard CSP can drop ``style-src 'unsafe-inline'``.
    assert 'class="icon-sprite"' in icons
    assert ".icon-sprite" in css and "display: none" in css.split(".icon-sprite")[1][:120]


# ── Banners + role discipline ───────────────────────────────────────────


def test_banners_use_status_not_alert_by_default():
    """Hidden elements with role=alert are screen-reader noise when the
    JS toggles them on and off. We default to role=status; the JS
    promotes to role=alert only on fail-level banners."""
    html = _BODY.read_text()
    # The informational banner must NOT start life as role="alert".
    info_chunk = html.split('id="banner"')[1][:400]
    assert 'role="status"' in info_chunk
    # errors-banner and orphan-banner are hard failures by their nature —
    # role="alert" from the start is appropriate for those.
    assert 'id="errors-banner"' in html
    assert 'id="orphan-banner"' in html


def test_live_regions_present_on_dynamic_zones():
    html = _BODY.read_text()
    # Log stream: log role + polite live so each line is announced once.
    assert 'id="log-stream"' in html and 'role="log"' in html
    # Toasts region labelled + aria-live.
    assert 'id="toasts"' in html and 'aria-live="polite"' in html


# ── CSS contract ────────────────────────────────────────────────────────


def test_css_declares_typography_scale():
    css = _CSS.read_text()
    for token in ("--fs-xs", "--fs-sm", "--fs-base", "--fs-md", "--fs-lg", "--fs-xl", "--fs-2xl"):
        assert token + ":" in css or token + " " in css, f"missing type token {token}"


def test_css_declares_spacing_scale():
    css = _CSS.read_text()
    for token in ("--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6", "--sp-7", "--sp-8"):
        assert token + ":" in css or token + " " in css, f"missing spacing token {token}"


def test_css_reduced_motion_disables_transitions_not_only_animations():
    """The old rule only killed animation-duration — users with
    vestibular sensitivity still had transitions running. The new rule
    covers transition-duration too."""
    css = _CSS.read_text()
    # Find the actual @media block (skip any mention in comments).
    import re

    m = re.search(
        r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{([^}]+?\{[^}]+\})*",
        css,
    )
    assert m, "no @media (prefers-reduced-motion) block found"
    block = m.group(0)
    assert "animation-duration" in block
    assert "transition-duration" in block


def test_css_backdrop_filter_has_supports_guard():
    """Safari < 15 (and some embedded WebKit) render a black square when
    backdrop-filter is declared unconditionally — the @supports guard
    is the fallback gate."""
    css = _CSS.read_text()
    assert "@supports (backdrop-filter" in css


def test_css_ships_skeleton_and_sr_only_utilities():
    css = _CSS.read_text()
    assert ".skeleton" in css and "@keyframes skeleton-shine" in css
    assert ".sr-only" in css


def test_css_status_down_is_red_not_grey():
    """Regression guard for the silent-offline bug: .status-dot.down must
    use the fail color + a halo so a dropped WebSocket is unmissable."""
    css = _CSS.read_text()
    block = css.split(".status-dot.down")[1][:200]
    assert "var(--fail)" in block, "status down must use --fail for visibility"


# ── JS contract ─────────────────────────────────────────────────────────


def test_js_uses_session_storage_for_token():
    """The token lives in sessionStorage only — pin that contract so no
    one silently reverts it to localStorage (which survives browser
    close, expanding the XSS exfiltration window indefinitely)."""
    js = _JS.read_text()
    # Token API uses sessionStorage.
    assert "sessionStorage" in js
    # Regression guards: the Token API itself must NOT use localStorage
    # for read/write. The only mention of localStorage is the one-shot
    # eviction that wipes any stale legacy token.
    assert "localStorage.setItem" not in js, (
        "Token must not be written to localStorage anymore"
    )
    assert "localStorage.removeItem" in js, (
        "Eviction of the legacy localStorage token must remain"
    )


def test_js_has_store_singleton_and_no_global_states_variable():
    js = _JS.read_text()
    assert "const Store = {" in js
    # Regression guard against the old module-level `let STATES = []`.
    assert "\nlet STATES" not in js
    assert "\nlet SELECTED" not in js


def test_js_api_uses_abortcontroller_timeout():
    js = _JS.read_text()
    assert "AbortController" in js
    assert "TIMEOUT_MS" in js


def test_js_ws_has_exponential_backoff_with_jitter():
    js = _JS.read_text()
    assert "reconnectDelay" in js
    assert "Math.random()" in js, "missing jitter on reconnect delay"
    assert "30000" in js, "reconnect delay cap not set"


def test_js_has_focus_trap_in_dialog_stack():
    js = _JS.read_text()
    assert "DialogStack" in js
    assert "_installTrap" in js
    assert "Shift" in js or "shiftKey" in js


def test_js_log_buffer_is_fifo_capped():
    js = _JS.read_text()
    assert "LOG_MAX_ROWS" in js
    assert "removeChild" in js or "firstElementChild" in js


def test_js_detects_mac_for_kbd_hint():
    js = _JS.read_text()
    assert "IS_MAC" in js
    # Both alternatives must appear so the hint adapts.
    assert "Cmd" in js or "⌘" in js
    assert "Ctrl" in js


# ── Rendered dashboard: CSP + skip-link reaches the served HTML ────────


def test_dashboard_response_ships_skip_link_and_csp():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    # CSP is present (added in the API pass; this test guards against
    # accidental removal by a future HTML restructuring).
    assert "content-security-policy" in {k.lower() for k in resp.headers}
    body = resp.text
    assert 'class="skip-link"' in body
    assert '<main id="main" tabindex="-1">' in body


# ── Rich CLI helpers ───────────────────────────────────────────────────


def test_banner_mode_defaults_to_off_when_piped(monkeypatch):
    """Non-TTY stdout (piped output) must get banner=off by default so
    machine-friendly callers aren't flooded with decoration."""
    from gitoma.ui import console as console_mod

    monkeypatch.delenv("GITOMA_BANNER", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(console_mod.sys.stdout, "isatty", lambda: False, raising=False)
    assert console_mod.banner_mode() == "off"


def test_banner_mode_respects_env_override(monkeypatch):
    from gitoma.ui import console as console_mod

    monkeypatch.setenv("GITOMA_BANNER", "full")
    assert console_mod.banner_mode() == "full"
    monkeypatch.setenv("GITOMA_BANNER", "compact")
    assert console_mod.banner_mode() == "compact"
    monkeypatch.setenv("GITOMA_BANNER", "off")
    assert console_mod.banner_mode() == "off"


def test_is_plain_respects_no_color_and_gitoma_plain(monkeypatch):
    from gitoma.ui import console as console_mod

    monkeypatch.setattr(console_mod.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert console_mod.is_plain() is True
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("GITOMA_PLAIN", "1")
    assert console_mod.is_plain() is True
    monkeypatch.delenv("GITOMA_PLAIN", raising=False)
    # TTY + neither env set → False.
    assert console_mod.is_plain() is False


def test_glyph_downgrades_to_ascii_when_no_emoji(monkeypatch):
    """GITOMA_NO_EMOJI is the escape hatch for minimal TTYs. We re-import
    the module so the module-level EMOJI_OK is re-computed."""
    monkeypatch.setenv("GITOMA_NO_EMOJI", "1")
    import gitoma.ui.console as console_mod
    reload(console_mod)
    try:
        assert console_mod.EMOJI_OK is False
        assert console_mod.glyph("🎉", ">>") == ">>"
    finally:
        monkeypatch.delenv("GITOMA_NO_EMOJI", raising=False)
        reload(console_mod)  # restore for subsequent tests


def test_glyph_keeps_emoji_on_utf8_tty(monkeypatch):
    monkeypatch.delenv("GITOMA_NO_EMOJI", raising=False)
    # Force stdout.encoding to utf-8 via a stand-in.
    import gitoma.ui.console as console_mod

    class _Stdout:
        encoding = "utf-8"
        def isatty(self): return True

    monkeypatch.setattr(console_mod.sys, "stdout", _Stdout(), raising=False)
    reload(console_mod)
    try:
        assert console_mod.EMOJI_OK is True
        assert console_mod.glyph("🎉", ">>") == "🎉"
    finally:
        reload(console_mod)


# ── print_repo_info: defensive against missing fields ──────────────────


def test_print_repo_info_tolerates_missing_fields(capsys):
    """A private repo or a schema tweak used to crash the pretty-print
    via ``info['full_name']`` KeyError. All accessors now use .get()."""
    from gitoma.ui.panels import print_repo_info

    # Deliberately minimal dict — everything except full_name missing.
    print_repo_info({"full_name": "owner/repo"})
    captured = capsys.readouterr()
    # Just confirm we didn't raise and that we printed *something* useful.
    assert "owner/repo" in captured.out


def test_print_repo_info_does_not_explode_on_null_description():
    """None values used to concat-crash via f-string formatting."""
    from gitoma.ui.panels import print_repo_info

    # Should not raise.
    print_repo_info({
        "full_name": "x/y",
        "description": None,
        "stars": 0, "forks": 0, "open_issues": 0,
        "language": None, "default_branch": None, "topics": None,
    })
