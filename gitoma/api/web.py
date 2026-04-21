"""Public web UI for Gitoma — static dashboard + live state WebSocket.

Unlike the ``/api/v1/*`` router which requires a Bearer token, the web UI
is intended to run on a **trusted network** (localhost / VPN). The
dashboard itself is read-only (observes ``~/.gitoma/state/*.json``); any
write actions issued from the cockpit go through ``/api/v1/*`` with the
Bearer token the user enters once via the settings modal (stored in
browser ``localStorage``).

Hardening additions (industrial-grade pass):

* ``/ws/state`` now **validates the Origin header** before accepting the
  upgrade. Without this, a drive-by page served from ``http://attacker.local``
  could silently subscribe to live agent state (branch names, errors,
  current operation) even though the user only intended the cockpit to
  be reachable from the same host. WebSockets are not subject to CORS
  preflights, so Origin rejection is the only layer that applies.
* The dashboard HTML is **rendered once at import** instead of being
  string-joined per request. At ~90 KB of markup + inline assets this
  saves a measurable amount of allocation on every GET /.
* Every ``/`` response includes a tight **Content-Security-Policy** header
  so even if a state value ever leaks into the HTML (future risk, the
  current template is static), ``unsafe-eval`` and cross-origin fetches
  are blocked.
* ``_snapshot_states`` runs on a worker thread so the event loop doesn't
  block on disk I/O for every tick of every connected cockpit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, Response

from gitoma.core.config import load_config

logger = logging.getLogger(__name__)

web_router = APIRouter()

STATE_DIR = Path.home() / ".gitoma" / "state"
POLL_INTERVAL_S = 0.5

# A run whose `last_heartbeat` is older than this AND whose owning PID is
# gone counts as orphaned. The ~3× heartbeat-interval buffer absorbs
# scheduler jitter so a momentarily slow heartbeat isn't flagged.
ORPHAN_HEARTBEAT_GRACE_S = 90.0
# Phases considered non-terminal (still expected to be producing progress).
_NON_TERMINAL = {"IDLE", "ANALYZING", "PLANNING", "WORKING", "PR_OPEN", "REVIEWING"}

# WebSockets skip CORS preflights, so an explicit Origin allow-list is the
# only layer that stops a drive-by page from subscribing to live state.
#
# Default policy combines TWO complementary checks (defence in depth):
#
#   1. **Loopback hosts on any port** — ``localhost``, ``127.0.0.1``, ``::1``,
#      ``0.0.0.0`` are trusted regardless of port for both http/https.
#      Covers `gitoma serve --port <anything>` and every dev/test scenario.
#   2. **Same-origin** — ``Origin`` host[:port] equals the request's ``Host``
#      header. Covers VPN / LAN deployments where the cockpit reaches the
#      WS through whatever hostname the operator wired up, without needing
#      explicit env config.
#
# ``GITOMA_WS_ALLOWED_ORIGINS`` augments (does not replace) both — that way
# an operator can add a VPN-fronted origin without locking themselves out
# of localhost. Non-loopback + non-same-origin + non-listed origins stay
# rejected as before.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
})
_ALLOWED_WS_ORIGINS: set[str] = {
    o.strip()
    for o in os.getenv("GITOMA_WS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}


def _pid_alive(pid: int | None) -> bool:
    """Best-effort check that `pid` is still a running process on this host.

    `os.kill(pid, 0)` raises ProcessLookupError if gone, PermissionError if
    the process exists but belongs to a different user (treat as alive —
    we can't tell the difference from "dead" otherwise). None → False.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _enrich_liveness(state: dict[str, Any]) -> dict[str, Any]:
    """Add derived `is_alive` / `is_orphaned` fields to a state snapshot.

    Mutates and returns the dict so callers can chain. The cockpit reads
    these fields directly — keeping the logic server-side means the UI
    doesn't need to know the OS-specific PID rules.
    """
    phase = state.get("phase", "IDLE")
    pid = state.get("pid")
    heartbeat = state.get("last_heartbeat") or ""
    alive = _pid_alive(pid) if pid else False

    heartbeat_age = None
    if heartbeat:
        try:
            hb = datetime.fromisoformat(heartbeat)
            heartbeat_age = (datetime.now(timezone.utc) - hb).total_seconds()
        except ValueError:
            heartbeat_age = None

    # Orphan definition: phase says "still running" but nobody's actually
    # running it. A dead PID is conclusive; a very stale heartbeat is
    # sufficient even if the PID recycled to something else. HOWEVER, if
    # the CLI flagged a clean exit (`exit_clean=True`), the process ended
    # on purpose — not an orphan. This is the common case for phase
    # PR_OPEN after a successful `gitoma run`, where the pipeline pauses
    # waiting for the user to invoke `gitoma review`.
    stale_heartbeat = (
        heartbeat_age is not None and heartbeat_age > ORPHAN_HEARTBEAT_GRACE_S
    )
    exit_clean = bool(state.get("exit_clean"))
    orphaned = (
        phase in _NON_TERMINAL
        and not exit_clean
        and (not alive or stale_heartbeat)
    )

    state["is_alive"] = alive
    state["is_orphaned"] = orphaned
    state["heartbeat_age_s"] = heartbeat_age
    return state


def _snapshot_states() -> list[dict[str, Any]]:
    """Read every state file under STATE_DIR, newest-first by mtime.

    Each record gets liveness derivatives (`is_alive`, `is_orphaned`,
    `heartbeat_age_s`) tacked on so the cockpit can render orphan
    indicators without re-doing the OS-specific checks client-side.
    """
    if not STATE_DIR.exists():
        return []
    states: list[dict[str, Any]] = []
    for p in sorted(STATE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
            states.append(_enrich_liveness(data))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Skipping unreadable state file %s: %s", p, exc)
    return states


async def _async_snapshot_states() -> list[dict[str, Any]]:
    """Run the sync snapshot on a worker thread so the event loop isn't blocked.

    With N connected cockpits polling every ``POLL_INTERVAL_S`` seconds,
    doing the glob + per-file stat + read_text + json.loads in the event
    loop itself piles up measurable stalls under load. ``asyncio.to_thread``
    offloads it to the default ThreadPoolExecutor at effectively zero cost.
    """
    return await asyncio.to_thread(_snapshot_states)


def _is_origin_allowed(origin: str | None, host_header: str | None = None) -> bool:
    """Accept: non-browser clients, loopback hosts (any port), same-origin
    handshakes, and the explicit ``GITOMA_WS_ALLOWED_ORIGINS`` list.

    Browsers always send an ``Origin`` header on WebSocket handshakes. A
    non-browser client (e.g. ``websockets``, ``wscat``) may omit it; an
    absent origin is allowed at this layer — the bearer-token check below
    still applies whenever the server has a token configured.

    Three accept paths (defence in depth, all equivalent in trust):

    1. **Loopback hosts on any port** — ``localhost``/``127.0.0.1``/``::1``/
       ``0.0.0.0`` regardless of port. Covers ``gitoma serve --port <anything>``
       and every dev/test scenario without the operator having to set env.
    2. **Same-origin** — ``Origin`` equals ``<scheme>://<Host header>``.
       Covers VPN / LAN deployments where the cockpit reaches the WS
       through whatever hostname the operator wired up.
    3. **Explicit env allow-list** — the union with the above; operators
       who add a VPN origin via env stay free to use loopback too.
    """
    if origin is None:
        # Non-browser client (CLI test, curl, etc.) — allow.
        return True
    if origin in _ALLOWED_WS_ORIGINS:
        return True
    # Loopback acceptance via parsed scheme+hostname (handles ``http://localhost:8800``,
    # ``http://[::1]:9999``, ``https://127.0.0.1:1234``, …).
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme in ("http", "https") and parsed.hostname in _LOOPBACK_HOSTS:
        return True
    # Same-origin acceptance: lets the cockpit work behind any hostname /
    # TLS proxy the operator wired up, without hardcoding ports.
    if host_header:
        for scheme in ("http://", "https://"):
            if origin == f"{scheme}{host_header}":
                return True
    return False


# ── Bearer auth on the WebSocket handshake ──────────────────────────────────
#
# The ``/api/v1/*`` REST surface enforces a Bearer token, but ``/ws/state``
# used to be entirely unauthenticated on the assumption that the cockpit
# only runs on localhost. That assumption breaks on any LAN/VPN deploy:
# the same socket pushes PIDs, branch names, errors, and PR URLs to anyone
# who knows the URL. We close the gap by requiring the same token that
# guards the REST API — when one is configured server-side.
#
# Browsers cannot set arbitrary headers on the WebSocket handshake, so we
# accept the token via two channels:
#
#   * ``Authorization: Bearer <token>`` — non-browser clients (curl, the
#     Python ``websockets`` lib, the test client).
#   * ``Sec-WebSocket-Protocol: gitoma-bearer.<base64url(token)>`` — the
#     standard browser workaround (the cockpit JS uses this). The chosen
#     subprotocol is echoed back on accept so the browser ack is well-formed.
#
# When the server has *no* token configured (``api_auth_token == ""``), the
# WS stays open — matching the "no-config localhost" UX the rest of the
# system already exposes (the REST endpoints respond 503 in that case so
# the operator sees a clear remediation banner).

_WS_BEARER_SUBPROTOCOL_PREFIX = "gitoma-bearer."


def _b64url_decode(s: str) -> str | None:
    """Decode a base64url-no-padding string; return None on any error."""
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _extract_bearer_from_ws(ws: WebSocket) -> tuple[str | None, str | None]:
    """Return ``(presented_token, subprotocol_to_echo)`` from the handshake.

    Subprotocol is non-None only when the bearer was supplied via the
    ``Sec-WebSocket-Protocol`` header, in which case the matching value
    must be echoed back on ``accept`` per RFC 6455. ``presented_token``
    is the raw token to validate; either field may be ``None`` if the
    client supplied no credentials.
    """
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None, None

    raw = ws.headers.get("sec-websocket-protocol", "")
    if not raw:
        return None, None
    for protocol in (p.strip() for p in raw.split(",")):
        if protocol.startswith(_WS_BEARER_SUBPROTOCOL_PREFIX):
            encoded = protocol[len(_WS_BEARER_SUBPROTOCOL_PREFIX):]
            decoded = _b64url_decode(encoded)
            if decoded:
                return decoded, protocol
    return None, None


@web_router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push full ``list[state]`` snapshots whenever anything changes on disk.

    The handshake rejects browser origins outside the allow-list, then —
    if the server has a Bearer token configured — verifies the same token
    here (via ``Authorization`` header or ``gitoma-bearer.<b64url>``
    subprotocol). Both rejections close with WebSocket code ``1008``
    (policy violation) before completing ``accept()``.
    """
    origin = ws.headers.get("origin")
    host_header = ws.headers.get("host")
    if not _is_origin_allowed(origin, host_header):
        logger.warning(
            "ws_origin_rejected",
            extra={"origin": origin, "host": host_header},
        )
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    expected_token = (load_config().api_auth_token or "").strip()
    chosen_subprotocol: str | None = None
    if expected_token:
        presented, chosen_subprotocol = _extract_bearer_from_ws(ws)
        if not presented or not secrets.compare_digest(presented, expected_token):
            logger.warning(
                "ws_auth_rejected",
                extra={"origin": origin, "had_bearer": bool(presented)},
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    if chosen_subprotocol is not None:
        await ws.accept(subprotocol=chosen_subprotocol)
    else:
        await ws.accept()
    last_serialized: str | None = None
    try:
        while True:
            states = await _async_snapshot_states()
            serialized = json.dumps(states, default=str)
            if serialized != last_serialized:
                await ws.send_text(serialized)
                last_serialized = serialized
            await asyncio.sleep(POLL_INTERVAL_S)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("ws/state stream crashed")
        return


# =============================================================================
# Dashboard -- the CSS / icons / JS / body markup used to live as multi-kLoC
# triple-quoted string constants in this file. They are now on disk under
# ``gitoma/ui/assets/`` so you can edit them with a real editor (syntax
# highlight, linters, devtools mapping) instead of scrolling through 2400
# lines of Python. Loaded once at import, and the whole HTML shell is
# assembled once too -- dashboard() just hands back a pre-built bytes blob.
# =============================================================================

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "ui" / "assets"


def _load_asset(name: str) -> str:
    """Read a bundled UI asset. Fails loudly if missing -- we'd rather see
    an import-time traceback than serve a half-rendered cockpit."""
    return (_ASSETS_DIR / name).read_text(encoding="utf-8")


_DASHBOARD_CSS = _load_asset("dashboard.css")
_DASHBOARD_ICONS = _load_asset("dashboard_icons.html")
_DASHBOARD_JS = _load_asset("dashboard.js")
_DASHBOARD_BODY = _load_asset("dashboard_body.html")


def _render_dashboard_html() -> str:
    """Compose the self-contained cockpit HTML from the on-disk assets."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>\n'
        '<meta name="color-scheme" content="dark"/>\n'
        '<meta name="theme-color" content="#0a0a0b"/>\n'
        "<title>Gitoma \u2014 Cockpit</title>\n"
        f"<style>{_DASHBOARD_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{_DASHBOARD_ICONS}\n{_DASHBOARD_BODY}\n"
        '<script src="/dashboard.js" defer></script>\n'
        "</body>\n</html>\n"
    )


# The dashboard JS is served as a SEPARATE static asset (rather than
# inlined into a ``<script>`` tag) so the CSP can drop ``'unsafe-inline'``
# from ``script-src``. With ``'unsafe-inline'`` in place, any future XSS
# vector (e.g. a future template that interpolates server data into the
# inline script) instantly exfiltrates the cockpit's bearer token to any
# allowed ``connect-src`` destination. Externalising the JS is the
# structural fix; tightening the CSP is the user-visible payoff.

import hashlib

_DASHBOARD_JS_BYTES: bytes = _DASHBOARD_JS.encode("utf-8")
_DASHBOARD_JS_ETAG: str = '"' + hashlib.sha256(_DASHBOARD_JS_BYTES).hexdigest()[:16] + '"'

# Pre-rendered once at module load; served as-is on every GET /.
_DASHBOARD_BYTES: bytes = _render_dashboard_html().encode("utf-8")

# CSP for the dashboard:
#   * ``script-src 'self'`` -- no inline scripts allowed. The JS lives at
#     ``/dashboard.js``; any future ``<script>...</script>`` block would
#     be blocked at load time.
#   * ``style-src 'self' 'unsafe-hashes'`` for the inline ``<style>`` block
#     that ships the dashboard CSS, expressed as a SHA-256 hash so we
#     don't need ``'unsafe-inline'``. ``style-src-attr 'none'`` blocks
#     ``style="..."`` attributes entirely; the dashboard.js helper
#     ``el(...)`` now routes dynamic styles through ``node.style.cssText``
#     (DOM property write — gated by script-src, not style-src). All
#     formerly-inline static ``style="..."`` attributes in the body
#     template were moved to utility classes in dashboard.css.
#   * ``connect-src 'self'`` -- only same-origin XHR / WebSocket. The old
#     value (``'self' ws: wss: http://localhost:* https://localhost:*``)
#     was a token-exfiltration footgun: it allowed a hypothetical XSS
#     to ship the bearer to any localhost port and any ws/wss endpoint.
#     Same-origin is enough -- the cockpit only ever talks to its origin.
_DASHBOARD_CSS_SHA256: str = (
    "'sha256-" + hashlib.sha256(_DASHBOARD_CSS.encode("utf-8")).digest().hex()  # placeholder, replaced below
    + "'"
)
# Compute the spec-correct base64 form of the SHA-256 (CSP wants base64,
# not hex). Done at import time, included in the CSP header verbatim.
import base64 as _b64
_DASHBOARD_CSS_SHA256 = (
    "'sha256-"
    + _b64.b64encode(hashlib.sha256(_DASHBOARD_CSS.encode("utf-8")).digest()).decode()
    + "'"
)
_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    f"style-src 'self' {_DASHBOARD_CSS_SHA256}; "
    "style-src-attr 'none'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> Response:
    """Serve the live cockpit shell. JS comes from /dashboard.js."""
    return Response(
        content=_DASHBOARD_BYTES,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": _DASHBOARD_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-cache",
        },
    )


@web_router.get("/dashboard.js")
async def dashboard_js(request: Request) -> Response:
    """Serve the cockpit JS as a separate asset.

    Externalising this script is what lets the dashboard CSP drop
    ``script-src 'unsafe-inline'``. Conditional GETs honour the
    asset-hash ETag so cockpit reloads stay cheap. ``X-Content-Type-Options:
    nosniff`` is a belt-and-braces guard against a misconfigured proxy
    convincing the browser to execute it as something other than JS.
    """
    if request.headers.get("if-none-match") == _DASHBOARD_JS_ETAG:
        return Response(status_code=304, headers={"ETag": _DASHBOARD_JS_ETAG})
    return Response(
        content=_DASHBOARD_JS_BYTES,
        media_type="application/javascript; charset=utf-8",
        headers={
            "ETag": _DASHBOARD_JS_ETAG,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-cache",
        },
    )
