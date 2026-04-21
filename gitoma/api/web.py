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
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, Response

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
# Default policy: any loopback origin on any port is trusted (localhost,
# 127.0.0.1, ::1, 0.0.0.0). Hard-coding port 8000 — as the first cut
# did — breaks `gitoma serve --port 8800` and every dev/test scenario
# that doesn't happen to pick the default port.
#
# Operators who need a LAN frontend can provide an explicit list via env;
# that list REPLACES the loopback default, so it's a conscious decision.
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


def _is_origin_allowed(origin: str | None) -> bool:
    """Accept: non-browser clients, loopback origins on any port, and the
    explicit ``GITOMA_WS_ALLOWED_ORIGINS`` list (if the operator set one).

    Browsers always send an ``Origin`` header on WebSocket handshakes. A
    non-browser client (e.g. ``websockets``, ``wscat``) may omit it; we
    accept an absent origin because our ``/api/v1/*`` endpoints already
    enforce the serious security boundary via Bearer auth — ``/ws/state``
    is read-only derived data and exists primarily for the browser
    cockpit anyway.

    Loopback hosts are trusted regardless of port because:
      1. ``gitoma serve --port <anything>`` should just work.
      2. The threat model explicitly says the cockpit runs on localhost /
         VPN; an attacker with access to loopback can already do worse.

    Explicit env list augments (does not restrict) loopback acceptance —
    that way an operator can add a VPN origin without accidentally
    locking themselves out of localhost.
    """
    if origin is None:
        # Non-browser client (CLI test, curl, etc.) — allow.
        return True
    if origin in _ALLOWED_WS_ORIGINS:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme in ("http", "https") and parsed.hostname in _LOOPBACK_HOSTS:
        return True
    return False


@web_router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push full ``list[state]`` snapshots whenever anything changes on disk.

    The handshake rejects browser origins outside the allow-list with
    WebSocket close code ``1008`` (policy violation) before completing
    ``accept()``. Clients reconnect on drop; the first frame is always a
    full snapshot so late joiners don't need history.
    """
    origin = ws.headers.get("origin")
    if not _is_origin_allowed(origin):
        logger.warning("ws_origin_rejected", extra={"origin": origin})
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

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
        f"<script>{_DASHBOARD_JS}</script>\n"
        "</body>\n</html>\n"
    )


# Pre-rendered once at module load; served as-is on every GET /.
_DASHBOARD_BYTES: bytes = _render_dashboard_html().encode("utf-8")

# Tight CSP header for the dashboard: the assets are fully self-contained
# (no CDNs, no external scripts), so we allow only inline style/script from
# the page itself and restrict connections to the same origin + our local
# WebSocket. If a future contributor adds external resources, the browser
# console will blast them with CSP violations — that's the point.
_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss: http://localhost:* https://localhost:*; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> Response:
    """Serve the self-contained live cockpit."""
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
