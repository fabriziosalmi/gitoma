"""Public web UI for Gitoma — static dashboard + live state WebSocket.

Unlike the `/api/v1/*` router which requires a Bearer token, the web UI is
intended to run on a trusted network (localhost / VPN). The dashboard itself
is read-only (observes `~/.gitoma/state/*.json`); any write actions issued
from the cockpit go through `/api/v1/*` with the Bearer token the user
enters once via the settings modal (stored in browser `localStorage`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

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


@web_router.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Push full `list[state]` snapshots whenever anything changes on disk.

    Clients reconnect on drop; the first frame is always a full snapshot so
    late joiners don't need history.
    """
    await ws.accept()
    last_serialized: str | None = None
    try:
        while True:
            states = _snapshot_states()
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
# lines of Python. Loaded once at import, cached in module-level strings.
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
    """Compose the self-contained cockpit HTML from the on-disk assets.

    Called on every dashboard request, but this is a plain string join --
    the four assets are already in memory from import time.
    """
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


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the self-contained live cockpit."""
    return HTMLResponse(_render_dashboard_html())
