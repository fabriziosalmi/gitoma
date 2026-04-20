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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

web_router = APIRouter()

STATE_DIR = Path.home() / ".gitoma" / "state"
POLL_INTERVAL_S = 0.5


def _snapshot_states() -> list[dict[str, Any]]:
    """Read every state file under STATE_DIR, newest-first by mtime."""
    if not STATE_DIR.exists():
        return []
    states: list[dict[str, Any]] = []
    for p in sorted(STATE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            states.append(json.loads(p.read_text()))
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


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard — CSS / ICONS / JS / HTML are assembled from Python constants so
# the file stays self-contained (no external assets, no build step) while
# remaining readable and modular.
# ═════════════════════════════════════════════════════════════════════════════

_DASHBOARD_CSS = """
:root {
  --bg:            #0a0a0b;
  --bg-elev:       #111113;
  --bg-card:       #131316;
  --bg-subtle:     #17171b;
  --bg-hover:      #1c1c21;
  --border:        #1e1e23;
  --border-strong: #2a2a30;
  --border-focus:  #3b82f6;
  --fg:            #ededed;
  --fg-mid:        #a0a0a8;
  --fg-dim:        #6b6b75;
  --fg-faint:      #45454e;
  --accent:        #3b82f6;
  --accent-soft:   rgba(59,130,246,0.12);
  --accent-hover:  #4c8df6;
  --ok:            #22c55e;
  --ok-soft:       rgba(34,197,94,0.12);
  --warn:          #eab308;
  --warn-soft:     rgba(234,179,8,0.12);
  --fail:          #ef4444;
  --fail-soft:     rgba(239,68,68,0.12);
  --radius:        6px;
  --radius-lg:     8px;
  --shadow-sm:     0 1px 2px rgba(0,0,0,0.3);
  --shadow-md:     0 8px 24px rgba(0,0,0,0.4), 0 2px 6px rgba(0,0,0,0.3);
  --font-ui:       -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --font-mono:     ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --tap:           44px;
  --ease:          cubic-bezier(.3,.7,.2,1);
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-ui);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  min-height: 100vh;
}
code, .mono { font-family: var(--font-mono); }
button { font-family: inherit; font-size: inherit; color: inherit; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Layout ────────────────────────────────────────────────────────────── */
.app { display: grid; grid-template-rows: auto 1fr auto; min-height: 100vh; }
header {
  display: flex; align-items: center; gap: 16px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky; top: 0; z-index: 5;
  min-height: 52px;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-logo { width: 22px; height: 22px; color: var(--fg); flex-shrink: 0; }
.brand-name {
  font-size: 13px; font-weight: 600; letter-spacing: -0.01em;
  color: var(--fg);
}
.brand-badge {
  font-size: 10px; font-weight: 500; letter-spacing: 0.4px;
  color: var(--fg-dim); padding: 2px 6px;
  border: 1px solid var(--border-strong); border-radius: 3px;
  text-transform: uppercase;
}
.header-spacer { flex: 1; }
.header-actions { display: flex; align-items: center; gap: 6px; }
.status {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: var(--fg-mid);
  padding: 0 10px;
}
.status-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--fg-faint);
  transition: background .2s, box-shadow .2s;
}
.status-dot.live { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.status-dot.down { background: var(--fail); }

main {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  padding: 16px 20px 24px;
  max-width: 1400px; width: 100%; margin: 0 auto;
}
@media (max-width: 900px) {
  main { grid-template-columns: 1fr; padding: 12px; gap: 12px; }
}
.stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.aside { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

/* ── Card ──────────────────────────────────────────────────────────────── */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.card-head {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  min-height: 46px;
}
.card-head .icon { width: 14px; height: 14px; color: var(--fg-dim); flex-shrink: 0; }
.card-head .title {
  font-size: 11px; font-weight: 600; letter-spacing: 0.5px;
  color: var(--fg-mid); text-transform: uppercase;
}
.card-head .count {
  margin-left: auto;
  font-size: 11px; color: var(--fg-dim);
  padding: 2px 7px; border-radius: 10px;
  background: var(--bg-subtle); border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}
.card-body { padding: 14px 16px; }
.card-body.dense { padding: 8px; }
.card-body.flush { padding: 0; }
.empty {
  color: var(--fg-dim); font-size: 12px;
  padding: 20px 8px; text-align: center;
}

/* ── Button ────────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: var(--tap);
  padding: 0 14px;
  font-size: 13px; font-weight: 500;
  background: var(--bg-subtle);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background .12s, border-color .12s, transform .06s var(--ease);
  white-space: nowrap;
}
.btn:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.btn:active { transform: translateY(1px); }
.btn:focus-visible { outline: none; border-color: var(--border-focus); box-shadow: 0 0 0 3px var(--accent-soft); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.btn .icon { width: 14px; height: 14px; }
.btn--primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn--primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.btn--danger { background: var(--fail); border-color: var(--fail); color: #fff; }
.btn--danger:hover { background: #dc2626; border-color: #dc2626; }
.btn--ghost { background: transparent; border-color: transparent; }
.btn--ghost:hover { background: var(--bg-subtle); border-color: var(--border); }
.btn--sm { min-height: 32px; padding: 0 10px; font-size: 12px; border-radius: 5px; }
.btn--icon { min-height: var(--tap); min-width: var(--tap); padding: 0; }
.btn--icon.btn--sm { min-height: 32px; min-width: 32px; }
.btn--block { width: 100%; }

/* ── Command panel grid ────────────────────────────────────────────────── */
.cmd-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.cmd-tile {
  display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
  padding: 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  text-align: left;
  min-height: 76px;
  transition: background .12s, border-color .12s, transform .06s var(--ease);
}
.cmd-tile:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.cmd-tile:active { transform: translateY(1px); }
.cmd-tile:focus-visible { outline: none; border-color: var(--border-focus); box-shadow: 0 0 0 3px var(--accent-soft); }
.cmd-tile .icon { width: 16px; height: 16px; color: var(--fg-mid); }
.cmd-tile .cmd-name { font-size: 12px; font-weight: 500; color: var(--fg); }
.cmd-tile .cmd-hint { font-size: 10.5px; color: var(--fg-dim); line-height: 1.3; }
.cmd-tile[data-kind="destructive"] .icon { color: var(--fail); }

/* ── Repo list ─────────────────────────────────────────────────────────── */
.repo-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; margin: 0;
  border-radius: var(--radius); cursor: pointer;
  border: 1px solid transparent;
  transition: background .12s, border-color .12s;
  min-height: var(--tap);
}
.repo-item + .repo-item { margin-top: 2px; }
.repo-item:hover { background: var(--bg-subtle); }
.repo-item.active { background: var(--bg-subtle); border-color: var(--border-strong); }
.repo-item .icon { width: 14px; height: 14px; color: var(--fg-dim); flex-shrink: 0; }
.repo-item .slug {
  flex: 1; font-size: 12px; color: var(--fg);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.phase-chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10px; font-weight: 500; letter-spacing: 0.3px;
  padding: 2px 7px; border-radius: 3px;
  background: var(--bg-subtle); color: var(--fg-mid);
  border: 1px solid var(--border);
  text-transform: uppercase;
}
.phase-chip .dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: var(--fg-faint);
}
.phase-chip.IDLE      .dot { background: var(--fg-faint); }
.phase-chip.ANALYZING .dot,
.phase-chip.PLANNING  .dot,
.phase-chip.WORKING   .dot { background: var(--warn); }
.phase-chip.PR_OPEN   .dot,
.phase-chip.REVIEWING .dot { background: var(--accent); }
.phase-chip.DONE      .dot { background: var(--ok); }

/* ── Jobs badge ────────────────────────────────────────────────────────── */
.jobs-badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--fg-mid);
  padding: 4px 8px; border-radius: 10px;
  background: var(--bg-subtle); border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: background .12s, border-color .12s;
}
.jobs-badge:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.jobs-badge .icon { width: 12px; height: 12px; color: var(--fg-dim); }
.jobs-badge.busy .icon { color: var(--warn); animation: spin 2s linear infinite; }
.jobs-badge.busy { color: var(--fg); }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Pipeline stepper ──────────────────────────────────────────────────── */
.pipeline { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }
@media (max-width: 700px) { .pipeline { grid-template-columns: repeat(2, 1fr); } }
.step {
  position: relative;
  display: flex; flex-direction: column; gap: 8px;
  padding: 14px 10px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  transition: all .2s var(--ease);
}
.step .icon { width: 18px; height: 18px; color: var(--fg-dim); }
.step .label {
  font-size: 10px; font-weight: 500; letter-spacing: 0.5px;
  color: var(--fg-dim); text-transform: uppercase;
}
.step.done { border-color: rgba(34,197,94,0.3); background: var(--ok-soft); }
.step.done .icon { color: var(--ok); }
.step.done .label { color: var(--ok); }
.step.active { border-color: var(--accent); background: var(--accent-soft); }
.step.active .icon { color: var(--accent); animation: spin 2.5s linear infinite; }
.step.active .label { color: var(--accent); }

/* ── KPI grid ──────────────────────────────────────────────────────────── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1px;
  background: var(--border);
}
.kpi { background: var(--bg-card); padding: 12px 14px; }
.kpi .k {
  font-size: 10px; font-weight: 500; letter-spacing: 0.5px;
  color: var(--fg-dim); text-transform: uppercase;
  margin-bottom: 6px;
}
.kpi .v {
  font-size: 14px; font-weight: 500; color: var(--fg);
  font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.kpi .v.xl { font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
.kpi .v a { display: inline-flex; align-items: center; gap: 4px; }
.kpi .v a .icon { width: 11px; height: 11px; }

.card-actions {
  display: flex; gap: 8px; justify-content: flex-end;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
}

/* ── Metrics ───────────────────────────────────────────────────────────── */
.score-lead {
  display: flex; align-items: baseline; gap: 14px;
  padding: 4px 0 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.score-value {
  font-size: 40px; font-weight: 600; letter-spacing: -0.03em;
  color: var(--fg); line-height: 1;
  font-variant-numeric: tabular-nums;
}
.score-label {
  font-size: 11px; font-weight: 500; letter-spacing: 0.5px;
  color: var(--fg-dim); text-transform: uppercase;
}
.metrics { display: flex; flex-direction: column; gap: 10px; }
.metric-row {
  display: grid; grid-template-columns: 130px 1fr 44px;
  align-items: center; gap: 12px;
}
@media (max-width: 480px) {
  .metric-row { grid-template-columns: 1fr 44px; }
  .metric-row .metric-bar { grid-column: 1 / -1; }
}
.metric-name { font-size: 12px; color: var(--fg-mid); }
.metric-bar {
  position: relative; height: 6px;
  background: var(--bg-subtle); border-radius: 3px; overflow: hidden;
}
.metric-bar .fill {
  position: absolute; top: 0; left: 0; bottom: 0;
  background: var(--accent); border-radius: 3px;
  transition: width .5s var(--ease);
}
.metric-bar .fill.warn { background: var(--warn); }
.metric-bar .fill.fail { background: var(--fail); }
.metric-score {
  text-align: right; font-size: 12px; color: var(--fg-mid);
  font-variant-numeric: tabular-nums;
}

/* ── Live log stream ───────────────────────────────────────────────────── */
.log-card { display: none; }
.log-card.open { display: block; }
.log-card .card-head .status-pill {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 10px; font-weight: 500; letter-spacing: 0.3px; text-transform: uppercase;
  padding: 2px 8px; border-radius: 3px;
  background: var(--bg-subtle); color: var(--fg-mid);
  border: 1px solid var(--border);
}
.log-card .status-pill .dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: var(--warn);
  animation: pulse 1.1s ease-in-out infinite;
}
.log-card .status-pill.done .dot { background: var(--ok); animation: none; }
.log-card .status-pill.fail .dot { background: var(--fail); animation: none; }
.log-card .status-pill.done { color: var(--ok); border-color: rgba(34,197,94,0.3); }
.log-card .status-pill.fail { color: var(--fail); border-color: rgba(239,68,68,0.3); }
@keyframes pulse {
  0%,100% { opacity: 1; transform: scale(1); }
  50%     { opacity: .35; transform: scale(.6); }
}
.log-card .btn--icon.close-log { margin-left: 6px; }
.log-stream {
  margin: 0;
  background: #050506;
  color: #c9d1d9;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  padding: 12px 14px;
  max-height: 360px;
  overflow-y: auto;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 2;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}
.log-stream::-webkit-scrollbar { width: 8px; height: 8px; }
.log-stream::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
.log-stream::-webkit-scrollbar-track { background: transparent; }
.log-stream .row { display: block; }
.log-stream .row.system { color: var(--accent); opacity: 0.85; }
.log-stream .row.error { color: var(--fail); }
.log-stream .row.dim   { color: var(--fg-dim); }
.log-stream .empty {
  color: var(--fg-dim);
  padding: 10px 0;
  text-align: center;
  font-family: var(--font-ui);
  font-size: 11px;
}

/* ── Footer ────────────────────────────────────────────────────────────── */
footer {
  padding: 10px 20px;
  border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  font-size: 11px; color: var(--fg-dim);
}
footer .mono { color: var(--fg-mid); }
footer .sep { color: var(--fg-faint); }

/* ── Dialog (native <dialog>) ──────────────────────────────────────────── */
dialog.modal {
  padding: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  color: var(--fg);
  width: min(440px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  box-shadow: var(--shadow-md);
}
dialog.modal::backdrop { background: rgba(0,0,0,0.6); backdrop-filter: blur(2px); }
dialog.modal[open] {
  animation: modal-in .18s var(--ease);
}
@keyframes modal-in {
  from { opacity: 0; transform: translateY(8px) scale(.98); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
.modal-head {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
.modal-head .icon { width: 16px; height: 16px; color: var(--fg-mid); }
.modal-head .title { font-size: 13px; font-weight: 600; color: var(--fg); }
.modal-head .sub { font-size: 11px; color: var(--fg-dim); margin-left: auto; }
.modal-body { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.modal-foot {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg-elev);
}

/* ── Form fields ───────────────────────────────────────────────────────── */
.field { display: flex; flex-direction: column; gap: 5px; }
.field label {
  font-size: 11px; font-weight: 500; letter-spacing: 0.3px;
  color: var(--fg-dim); text-transform: uppercase;
}
.field input[type="text"], .field input[type="password"], .field input[type="url"] {
  min-height: var(--tap);
  padding: 0 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 13px;
}
.field input:focus {
  outline: none;
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.field .hint { font-size: 11px; color: var(--fg-dim); line-height: 1.4; }
.check {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  min-height: var(--tap);
}
.check input { accent-color: var(--accent); width: 16px; height: 16px; }
.check span { font-size: 12px; color: var(--fg); }

/* ── Toasts ───────────────────────────────────────────────────────────── */
#toasts {
  position: fixed;
  bottom: 16px; right: 16px;
  display: flex; flex-direction: column-reverse; gap: 8px;
  z-index: 50;
  pointer-events: none;
  max-width: calc(100vw - 32px);
}
@media (max-width: 640px) { #toasts { left: 16px; right: 16px; } }
.toast {
  pointer-events: auto;
  display: flex; align-items: flex-start; gap: 10px;
  padding: 12px 14px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--fg-dim);
  border-radius: var(--radius);
  box-shadow: var(--shadow-md);
  font-size: 12.5px;
  max-width: 380px;
  animation: toast-in .22s var(--ease);
}
.toast.success { border-left-color: var(--ok); }
.toast.error   { border-left-color: var(--fail); }
.toast.info    { border-left-color: var(--accent); }
.toast .icon { width: 14px; height: 14px; color: var(--fg-mid); margin-top: 2px; flex-shrink: 0; }
.toast.success .icon { color: var(--ok); }
.toast.error   .icon { color: var(--fail); }
.toast.info    .icon { color: var(--accent); }
.toast .body { flex: 1; min-width: 0; }
.toast .title { font-weight: 500; color: var(--fg); margin-bottom: 2px; }
.toast .msg { color: var(--fg-mid); word-wrap: break-word; }
@keyframes toast-in {
  from { opacity: 0; transform: translateX(20px); }
  to   { opacity: 1; transform: translateX(0); }
}
.toast.leaving { animation: toast-out .18s var(--ease) forwards; }
@keyframes toast-out {
  to { opacity: 0; transform: translateX(20px); }
}
"""


_DASHBOARD_ICONS = """
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <defs>
    <symbol id="icon-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <line x1="6" x2="6" y1="3" y2="15"/>
      <circle cx="18" cy="6" r="3"/>
      <circle cx="6" cy="18" r="3"/>
      <path d="M18 9a9 9 0 0 1-9 9"/>
    </symbol>
    <symbol id="icon-folder" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
    </symbol>
    <symbol id="icon-repo" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6 3h11a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1 0-4h11"/>
      <path d="M6 3v14"/>
    </symbol>
    <symbol id="icon-branch" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="7" r="2"/>
      <path d="M6 7v10M18 9a5 5 0 0 1-5 5H9"/>
    </symbol>
    <symbol id="icon-activity" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
    </symbol>
    <symbol id="icon-chart" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 3v18h18"/>
      <rect x="7" y="12" width="3" height="6"/>
      <rect x="12" y="8" width="3" height="10"/>
      <rect x="17" y="4" width="3" height="14"/>
    </symbol>
    <symbol id="icon-external" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M15 3h6v6M10 14 21 3M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/>
    </symbol>
    <symbol id="icon-settings" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </symbol>
    <symbol id="icon-play" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="6 4 20 12 6 20 6 4" fill="currentColor"/>
    </symbol>
    <symbol id="icon-tool" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"/>
    </symbol>
    <symbol id="icon-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M10 11v6M14 11v6"/>
    </symbol>
    <symbol id="icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="20 6 9 17 4 12"/>
    </symbol>
    <symbol id="icon-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </symbol>
    <symbol id="icon-info" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="16" x2="12" y2="12"/>
      <line x1="12" y1="8" x2="12.01" y2="8"/>
    </symbol>
    <symbol id="icon-zap" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
    </symbol>
    <symbol id="icon-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </symbol>
    <symbol id="icon-x" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </symbol>
    <symbol id="icon-terminal" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="4 17 10 11 4 5"/>
      <line x1="12" y1="19" x2="20" y2="19"/>
    </symbol>
    <!-- Phase icons -->
    <symbol id="icon-phase-idle" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"/>
    </symbol>
    <symbol id="icon-phase-analyzing" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="7"/>
      <path d="m21 21-4.35-4.35"/>
    </symbol>
    <symbol id="icon-phase-planning" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 6h13M8 12h13M8 18h13"/>
      <circle cx="4" cy="6" r="1" fill="currentColor"/>
      <circle cx="4" cy="12" r="1" fill="currentColor"/>
      <circle cx="4" cy="18" r="1" fill="currentColor"/>
    </symbol>
    <symbol id="icon-phase-working" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </symbol>
    <symbol id="icon-phase-pr" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="6" cy="18" r="2"/><circle cx="6" cy="6" r="2"/><circle cx="18" cy="18" r="2"/>
      <path d="M6 8v8M18 8V7a3 3 0 0 0-3-3h-4l3 3m0-6-3 3"/>
    </symbol>
    <symbol id="icon-phase-review" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </symbol>
    <symbol id="icon-phase-done" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"/>
      <path d="m8 12 3 3 5-6"/>
    </symbol>
  </defs>
</svg>
"""


_DASHBOARD_JS = r"""
// ── Constants & tiny utils ───────────────────────────────────────────────
const PHASES = ["IDLE","ANALYZING","PLANNING","WORKING","PR_OPEN","REVIEWING","DONE"];
const PHASE_ICON = {
  IDLE:"icon-phase-idle", ANALYZING:"icon-phase-analyzing",
  PLANNING:"icon-phase-planning", WORKING:"icon-phase-working",
  PR_OPEN:"icon-phase-pr", REVIEWING:"icon-phase-review", DONE:"icon-phase-done",
};
const $ = (id) => document.getElementById(id);
const svg = (id, cls="icon") => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
}[c]));

// ── Token store (localStorage) ──────────────────────────────────────────
const Token = {
  key: "gitoma.api_token",
  get() { return localStorage.getItem(this.key) || ""; },
  set(v) { v ? localStorage.setItem(this.key, v) : localStorage.removeItem(this.key); },
  has() { return !!this.get(); },
};

// ── API client ──────────────────────────────────────────────────────────
const API = {
  async _fetch(method, path, body) {
    const token = Token.get();
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch("/api/v1" + path, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      const detail = (data && data.detail) || res.statusText || "Request failed";
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return data;
  },
  run(repoUrl, { branch = "", dryRun = false } = {}) {
    return this._fetch("POST", "/run", { repo_url: repoUrl, branch: branch || null, dry_run: !!dryRun });
  },
  analyze(repoUrl) { return this._fetch("POST", "/analyze", { repo_url: repoUrl }); },
  review(repoUrl, integrate) { return this._fetch("POST", "/review", { repo_url: repoUrl, integrate: !!integrate }); },
  fixCi(repoUrl, branch) { return this._fetch("POST", "/fix-ci", { repo_url: repoUrl, branch }); },
  reset(owner, name) { return this._fetch("DELETE", `/state/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`); },
  jobs() { return this._fetch("GET", "/jobs"); },
  health() { return this._fetch("GET", "/health"); },
};

// ── Toast system ────────────────────────────────────────────────────────
const Toast = {
  host() { return $("toasts"); },
  show(level, title, msg = "") {
    const host = this.host();
    const icons = { success: "icon-check", error: "icon-alert", info: "icon-info" };
    const el = document.createElement("div");
    el.className = `toast ${level}`;
    el.innerHTML = `${svg(icons[level] || icons.info)}
      <div class="body">
        <div class="title">${escape(title)}</div>
        ${msg ? `<div class="msg">${escape(msg)}</div>` : ""}
      </div>`;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add("leaving");
      el.addEventListener("animationend", () => el.remove(), { once: true });
    }, 4200);
  },
  success(t, m) { this.show("success", t, m); },
  error(t, m)   { this.show("error", t, m); },
  info(t, m)    { this.show("info", t, m); },
};

// ── Dialog helpers (native <dialog>) ────────────────────────────────────
function openDialog(id) {
  const d = $(id);
  if (!d) return;
  if (!d.open) d.showModal();
  const first = d.querySelector("input, button, textarea");
  if (first) first.focus();
}
function closeDialog(id) {
  const d = $(id);
  if (d && d.open) d.close();
}
document.addEventListener("click", (e) => {
  // click outside dialog content closes it
  const t = e.target;
  if (t instanceof HTMLDialogElement && t.open) t.close();
  if (t.dataset && t.dataset.closeDialog) closeDialog(t.dataset.closeDialog);
});

// ── Cockpit state ───────────────────────────────────────────────────────
let STATES = [];
let SELECTED = 0;
let JOB_POLL = null;

// ── Live log stream (SSE via fetch, so we can pass Bearer header) ───────
const LogStream = {
  controller: null,
  jobId: null,

  open(jobId, label) {
    this.close();
    this.jobId = jobId;
    $("log-card").classList.add("open");
    $("log-title").textContent = `Live Output — ${label}`;
    this._setStatus("running");
    const pre = $("log-stream");
    pre.innerHTML = "";
    this._autoScroll = true;
    pre.addEventListener("scroll", () => {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 12;
      this._autoScroll = atBottom;
    });
    this._consume(jobId).catch((err) => {
      this._appendRaw(`[stream error] ${err.message || err}`, "error");
      this._setStatus("fail");
    });
  },

  close() {
    if (this.controller) {
      try { this.controller.abort(); } catch {}
      this.controller = null;
    }
    this.jobId = null;
  },

  hide() {
    this.close();
    $("log-card").classList.remove("open");
  },

  async _consume(jobId) {
    this.controller = new AbortController();
    const token = Token.get();
    const res = await fetch(`/api/v1/stream/${encodeURIComponent(jobId)}`, {
      headers: token ? { "Authorization": `Bearer ${token}` } : {},
      signal: this.controller.signal,
    });
    if (!res.ok) {
      if (res.status === 401 || res.status === 403) {
        this._appendRaw("[auth required] token rejected by server", "error");
        this._setStatus("fail");
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split(/\n\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) {
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          try {
            const payload = JSON.parse(line.slice(5).trim());
            const text = payload.line || "";
            if (text.startsWith("__END__")) {
              const status = text.split(":").slice(1).join(":") || "completed";
              this._setStatus(status.startsWith("failed") ? "fail" : "done", status);
              return;
            }
            this._appendLine(text);
          } catch { /* ignore malformed */ }
        }
      }
    }
    // Stream ended without __END__
    this._setStatus("done", "closed");
  },

  _appendLine(text) {
    let cls = "";
    if (text.startsWith("$ ")) cls = "system";
    else if (/\b(error|failed|traceback)/i.test(text)) cls = "error";
    else if (text.startsWith("[") && text.endsWith("]")) cls = "dim";
    this._appendRaw(text, cls);
  },

  _appendRaw(text, cls = "") {
    const pre = $("log-stream");
    const row = document.createElement("span");
    row.className = "row" + (cls ? " " + cls : "");
    row.textContent = text + "\n";
    pre.appendChild(row);
    if (this._autoScroll) pre.scrollTop = pre.scrollHeight;
  },

  _setStatus(kind, label) {
    const pill = $("log-status");
    pill.className = "status-pill " + (kind === "done" ? "done" : kind === "fail" ? "fail" : "");
    $("log-status-label").textContent = label || kind;
  },
};

// ── Rendering ───────────────────────────────────────────────────────────
function renderPipeline(phase) {
  const el = $("pipeline");
  const idx = Math.max(0, PHASES.indexOf(phase || "IDLE"));
  el.innerHTML = PHASES.map((p, i) => {
    let cls = "step";
    if (i < idx) cls += " done";
    else if (i === idx) cls += " active";
    return `<div class="${cls}">${svg(PHASE_ICON[p])}<span class="label">${p.replace("_"," ")}</span></div>`;
  }).join("");
  const chip = $("phase-chip-top");
  chip.className = `phase-chip ${phase || "IDLE"}`;
  $("phase-chip-label").textContent = (phase || "IDLE").replace("_"," ");
}

function renderRepoList() {
  const host = $("repo-list");
  $("repo-count").textContent = STATES.length;
  if (!STATES.length) {
    host.innerHTML = '<div class="empty">No tracked runs yet.<br/>Launch one via the command panel.</div>';
    return;
  }
  host.innerHTML = STATES.map((s, i) => {
    const slug = `${s.owner}/${s.name}`;
    const phase = s.phase || "IDLE";
    return `<div class="repo-item${i === SELECTED ? " active" : ""}" data-idx="${i}" role="button" tabindex="0">
      ${svg("icon-repo")}
      <span class="slug">${escape(slug)}</span>
      <span class="phase-chip ${phase}"><span class="dot"></span>${phase.replace("_"," ")}</span>
    </div>`;
  }).join("");
  host.querySelectorAll(".repo-item").forEach((el) => {
    el.addEventListener("click", () => { SELECTED = parseInt(el.dataset.idx, 10); renderAll(); });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.click(); }
    });
  });
}

function renderMetrics(state) {
  const host = $("metrics");
  const report = state && state.metric_report;
  if (!report || !report.metrics || !report.metrics.length) {
    host.innerHTML = '<div class="empty">No metric report yet.</div>';
    $("score").textContent = "—";
    return;
  }
  const metrics = [...report.metrics].sort((a, b) => (a.score || 0) - (b.score || 0));
  host.innerHTML = metrics.map((m) => {
    const pct = Math.round((m.score || 0) * 100);
    const cls = m.status === "fail" ? "fail" : m.status === "warn" ? "warn" : "";
    return `<div class="metric-row">
      <div class="metric-name">${escape(m.display_name || m.key || "—")}</div>
      <div class="metric-bar"><div class="fill ${cls}" style="width:${pct}%"></div></div>
      <div class="metric-score">${pct}%</div>
    </div>`;
  }).join("");
  $("score").textContent = Math.round((report.overall_score || 0) * 100) + "%";
}

function renderDetail(state) {
  const keys = ["repo","branch","tasks","subtasks","pr","updated"];
  if (!state) {
    keys.forEach((k) => ($("info-" + k).textContent = "—"));
    renderPipeline("IDLE");
    $("reset-btn").disabled = true;
    return;
  }
  $("info-repo").textContent = `${state.owner}/${state.name}`;
  $("info-branch").textContent = state.branch || "—";
  const plan = state.task_plan || {};
  const tasks = plan.tasks || [];
  const doneTasks = tasks.filter((t) => t.status === "completed").length;
  const subtasks = tasks.flatMap((t) => t.subtasks || []);
  const doneSubs = subtasks.filter((s) => s.status === "completed").length;
  $("info-tasks").textContent = tasks.length ? `${doneTasks} / ${tasks.length}` : "—";
  $("info-subtasks").textContent = subtasks.length ? `${doneSubs} / ${subtasks.length}` : "—";
  const pr = state.pr_url;
  $("info-pr").innerHTML = pr
    ? `<a href="${escape(pr)}" target="_blank" rel="noreferrer">#${escape(state.pr_number || "?")} ${svg("icon-external")}</a>`
    : "—";
  const updated = state.updated_at || "";
  $("info-updated").textContent = updated ? updated.slice(11, 19) : "—";
  renderPipeline(state.phase);
  $("reset-btn").disabled = false;
}

function renderAll() {
  if (SELECTED >= STATES.length) SELECTED = 0;
  const state = STATES[SELECTED] || null;
  renderRepoList();
  renderDetail(state);
  renderMetrics(state);
}

// ── Jobs badge (polled when anything is live) ───────────────────────────
async function refreshJobs() {
  try {
    const jobs = await API.jobs();
    const entries = Object.entries(jobs || {});
    const running = entries.filter(([, v]) => v.status === "running").length;
    const badge = $("jobs-badge");
    badge.classList.toggle("busy", running > 0);
    $("jobs-count").textContent = running ? `${running} running` : `${entries.length} total`;
  } catch (err) {
    // token might be unset / invalid — fail silent in the header
    $("jobs-count").textContent = "—";
    $("jobs-badge").classList.remove("busy");
  }
}

function startJobPolling() {
  if (JOB_POLL) return;
  refreshJobs();
  JOB_POLL = setInterval(refreshJobs, 3000);
}

// ── WebSocket connection ────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/state`;
  $("ws-url").textContent = url;
  const ws = new WebSocket(url);
  const dot = $("conn-dot");
  const label = $("conn-label");
  ws.onopen = () => { dot.classList.remove("down"); dot.classList.add("live"); label.textContent = "live"; };
  ws.onclose = () => {
    dot.classList.remove("live"); dot.classList.add("down"); label.textContent = "reconnecting";
    setTimeout(connectWS, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (evt) => {
    try { STATES = JSON.parse(evt.data); renderAll(); }
    catch (e) { console.warn("bad frame", e); }
  };
}

// ── Command dispatch ────────────────────────────────────────────────────
function requireToken(nextFn) {
  if (Token.has()) { nextFn(); return; }
  $("token-next").value = nextFn.name || "";
  window.__pendingAction = nextFn;
  openDialog("token-dialog");
}

async function submitCommand(name, fn) {
  try {
    const res = await fn();
    Toast.success(name + " dispatched", res?.job_id ? `Job ${res.job_id.slice(0, 8)}` : "");
    refreshJobs();
    if (res?.job_id) LogStream.open(res.job_id, name);
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      Token.set("");
      Toast.error("Auth failed", "Please re-enter the API token.");
      openDialog("token-dialog");
      return;
    }
    Toast.error(name + " failed", err.message || "Unknown error");
  }
}

function onCommandTile(kind) {
  switch (kind) {
    case "run":     requireToken(() => openDialog("run-dialog")); break;
    case "analyze": requireToken(() => openDialog("analyze-dialog")); break;
    case "review":  requireToken(() => openDialog("review-dialog")); break;
    case "fix-ci":  requireToken(() => openDialog("fixci-dialog")); break;
  }
}

function wireDialogs() {
  $("token-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("token-input").value.trim();
    Token.set(t);
    closeDialog("token-dialog");
    Toast.success("Token saved", "Authorization ready.");
    refreshJobs();
    if (window.__pendingAction) {
      const fn = window.__pendingAction;
      window.__pendingAction = null;
      fn();
    }
  });
  $("token-clear").addEventListener("click", () => {
    Token.set("");
    $("token-input").value = "";
    closeDialog("token-dialog");
    Toast.info("Token cleared");
    refreshJobs();
  });

  $("run-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("run-url").value.trim();
    const branch = $("run-branch").value.trim();
    const dry = $("run-dry").checked;
    if (!url) return;
    closeDialog("run-dialog");
    submitCommand("Run", () => API.run(url, { branch, dryRun: dry }));
  });

  $("analyze-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("analyze-url").value.trim();
    if (!url) return;
    closeDialog("analyze-dialog");
    submitCommand("Analyze", () => API.analyze(url));
  });

  $("review-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("review-url").value.trim();
    const integrate = $("review-integrate").checked;
    if (!url) return;
    closeDialog("review-dialog");
    submitCommand(integrate ? "Review integrate" : "Review", () => API.review(url, integrate));
  });

  $("fixci-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("fixci-url").value.trim();
    const branch = $("fixci-branch").value.trim();
    if (!url || !branch) return;
    closeDialog("fixci-dialog");
    submitCommand("Fix-CI", () => API.fixCi(url, branch));
  });

  $("confirm-ok").addEventListener("click", () => {
    const fn = window.__confirmAction;
    closeDialog("confirm-dialog");
    if (fn) fn();
    window.__confirmAction = null;
  });
}

function confirmAndReset() {
  const s = STATES[SELECTED];
  if (!s) return;
  $("confirm-title").textContent = "Reset state?";
  $("confirm-body").textContent =
    `Delete the persisted state for ${s.owner}/${s.name}? This only removes the local progress — no changes are pushed.`;
  $("confirm-ok").textContent = "Reset state";
  $("confirm-ok").className = "btn btn--danger";
  window.__confirmAction = () => {
    requireToken(async () => {
      try {
        await API.reset(s.owner, s.name);
        Toast.success("State reset", `${s.owner}/${s.name}`);
      } catch (err) {
        if (err.status === 401 || err.status === 403) {
          Token.set(""); openDialog("token-dialog");
          Toast.error("Auth failed", "Please re-enter the API token.");
        } else {
          Toast.error("Reset failed", err.message || "Unknown error");
        }
      }
    });
  };
  openDialog("confirm-dialog");
}

// ── Entrypoint ──────────────────────────────────────────────────────────
function init() {
  // Command tiles
  document.querySelectorAll("[data-cmd]").forEach((el) => {
    el.addEventListener("click", () => onCommandTile(el.dataset.cmd));
  });
  $("settings-btn").addEventListener("click", () => {
    $("token-input").value = Token.get();
    openDialog("token-dialog");
  });
  $("jobs-badge").addEventListener("click", refreshJobs);
  $("reset-btn").addEventListener("click", confirmAndReset);
  $("log-close").addEventListener("click", () => LogStream.hide());

  wireDialogs();
  connectWS();
  renderAll();
  startJobPolling();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
"""


_DASHBOARD_BODY = """
<div class="app">
  <header>
    <div class="brand">
      <svg class="brand-logo"><use href="#icon-logo"/></svg>
      <span class="brand-name">Gitoma</span>
      <span class="brand-badge">Cockpit</span>
    </div>
    <div class="header-spacer"></div>
    <div class="header-actions">
      <span id="jobs-badge" class="jobs-badge" role="button" tabindex="0" title="Refresh jobs">
        <svg class="icon"><use href="#icon-zap"/></svg>
        <span id="jobs-count">—</span>
      </span>
      <button id="settings-btn" class="btn btn--ghost btn--icon btn--sm" aria-label="Settings" title="API token">
        <svg class="icon"><use href="#icon-settings"/></svg>
      </button>
      <span class="status">
        <span id="conn-dot" class="status-dot down"></span>
        <span id="conn-label">connecting</span>
      </span>
    </div>
  </header>

  <main>
    <aside class="aside">
      <section class="card">
        <div class="card-head">
          <svg class="icon"><use href="#icon-zap"/></svg>
          <span class="title">Commands</span>
        </div>
        <div class="card-body">
          <div class="cmd-grid">
            <button class="cmd-tile" data-cmd="run" type="button">
              <svg class="icon"><use href="#icon-play"/></svg>
              <span class="cmd-name">Run</span>
              <span class="cmd-hint">Full autonomous pipeline</span>
            </button>
            <button class="cmd-tile" data-cmd="analyze" type="button">
              <svg class="icon"><use href="#icon-phase-analyzing"/></svg>
              <span class="cmd-name">Analyze</span>
              <span class="cmd-hint">Read-only metric scan</span>
            </button>
            <button class="cmd-tile" data-cmd="review" type="button">
              <svg class="icon"><use href="#icon-eye"/></svg>
              <span class="cmd-name">Review</span>
              <span class="cmd-hint">Copilot comments + integrate</span>
            </button>
            <button class="cmd-tile" data-cmd="fix-ci" type="button">
              <svg class="icon"><use href="#icon-tool"/></svg>
              <span class="cmd-name">Fix CI</span>
              <span class="cmd-hint">Reflexion agent on a branch</span>
            </button>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <svg class="icon"><use href="#icon-folder"/></svg>
          <span class="title">Repositories</span>
          <span id="repo-count" class="count">0</span>
        </div>
        <div class="card-body dense">
          <div id="repo-list"><div class="empty">No tracked runs yet.</div></div>
        </div>
      </section>
    </aside>

    <div class="stack">
      <section id="log-card" class="card log-card" aria-live="polite">
        <div class="card-head">
          <svg class="icon"><use href="#icon-terminal"/></svg>
          <span class="title" id="log-title">Live Output</span>
          <span id="log-status" class="status-pill"><span class="dot"></span><span id="log-status-label">running</span></span>
          <button id="log-close" class="btn btn--ghost btn--icon btn--sm close-log" aria-label="Close live output" title="Close">
            <svg class="icon"><use href="#icon-x"/></svg>
          </button>
        </div>
        <pre id="log-stream" class="log-stream" role="log" aria-label="Live subprocess output"></pre>
      </section>

      <section class="card">
        <div class="card-head">
          <svg class="icon"><use href="#icon-activity"/></svg>
          <span class="title">Pipeline</span>
          <span id="phase-chip-top" class="phase-chip IDLE" style="margin-left:auto">
            <span class="dot"></span><span id="phase-chip-label">IDLE</span>
          </span>
        </div>
        <div class="card-body">
          <div id="pipeline" class="pipeline"></div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <svg class="icon"><use href="#icon-repo"/></svg>
          <span class="title">Run Details</span>
        </div>
        <div class="card-body flush">
          <div class="kpi-grid">
            <div class="kpi"><div class="k">Repository</div><div class="v mono" id="info-repo">—</div></div>
            <div class="kpi"><div class="k">Branch</div><div class="v mono" id="info-branch">—</div></div>
            <div class="kpi"><div class="k">Tasks</div><div class="v xl" id="info-tasks">—</div></div>
            <div class="kpi"><div class="k">Subtasks</div><div class="v xl" id="info-subtasks">—</div></div>
            <div class="kpi"><div class="k">Pull Request</div><div class="v" id="info-pr">—</div></div>
            <div class="kpi"><div class="k">Updated</div><div class="v mono" id="info-updated">—</div></div>
          </div>
        </div>
        <div class="card-actions">
          <button id="reset-btn" class="btn btn--sm" disabled>
            <svg class="icon"><use href="#icon-trash"/></svg>
            Reset state
          </button>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <svg class="icon"><use href="#icon-chart"/></svg>
          <span class="title">Health Metrics</span>
        </div>
        <div class="card-body">
          <div class="score-lead">
            <span class="score-value" id="score">—</span>
            <span class="score-label">Overall</span>
          </div>
          <div id="metrics" class="metrics"><div class="empty">No metric report yet.</div></div>
        </div>
      </section>
    </div>
  </main>

  <footer>
    <span>WebSocket</span>
    <span class="mono" id="ws-url">—</span>
    <span class="sep">·</span>
    <span>Poll 500 ms</span>
    <span class="sep">·</span>
    <span>Read-only stream · Bearer-auth commands</span>
  </footer>
</div>

<!-- Toasts ────────────────────────────────────────────────────────────── -->
<div id="toasts" aria-live="polite" aria-atomic="false"></div>

<!-- ── Dialogs ──────────────────────────────────────────────────────────── -->

<dialog id="token-dialog" class="modal" aria-labelledby="token-title">
  <form id="token-form" method="dialog">
    <div class="modal-head">
      <svg class="icon"><use href="#icon-settings"/></svg>
      <span id="token-title" class="title">API Token</span>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="token-input">Bearer token</label>
        <input id="token-input" type="password" autocomplete="off" spellcheck="false"
               placeholder="Paste GITOMA_API_TOKEN value" required/>
        <div class="hint">
          Stored in your browser only (localStorage). Matches the
          <code>GITOMA_API_TOKEN</code> configured on the server.
        </div>
      </div>
      <input type="hidden" id="token-next"/>
    </div>
    <div class="modal-foot">
      <button type="button" id="token-clear" class="btn btn--sm">Clear</button>
      <button type="button" class="btn btn--sm" data-close-dialog="token-dialog">Cancel</button>
      <button type="submit" class="btn btn--primary btn--sm">Save token</button>
    </div>
  </form>
</dialog>

<dialog id="run-dialog" class="modal" aria-labelledby="run-title">
  <form id="run-form" method="dialog">
    <div class="modal-head">
      <svg class="icon"><use href="#icon-play"/></svg>
      <span id="run-title" class="title">Launch full run</span>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="run-url">Repository URL</label>
        <input id="run-url" type="url" required placeholder="https://github.com/owner/repo"/>
      </div>
      <div class="field">
        <label for="run-branch">Branch (optional)</label>
        <input id="run-branch" type="text" placeholder="gitoma/improve-2026…"/>
      </div>
      <label class="check">
        <input id="run-dry" type="checkbox"/>
        <span>Dry run — analyze &amp; plan only, no commits</span>
      </label>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn--sm" data-close-dialog="run-dialog">Cancel</button>
      <button type="submit" class="btn btn--primary btn--sm">Launch</button>
    </div>
  </form>
</dialog>

<dialog id="analyze-dialog" class="modal" aria-labelledby="analyze-title">
  <form id="analyze-form" method="dialog">
    <div class="modal-head">
      <svg class="icon"><use href="#icon-phase-analyzing"/></svg>
      <span id="analyze-title" class="title">Analyze repository</span>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="analyze-url">Repository URL</label>
        <input id="analyze-url" type="url" required placeholder="https://github.com/owner/repo"/>
        <div class="hint">Runs all metric analyzers. Nothing is committed.</div>
      </div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn--sm" data-close-dialog="analyze-dialog">Cancel</button>
      <button type="submit" class="btn btn--primary btn--sm">Analyze</button>
    </div>
  </form>
</dialog>

<dialog id="review-dialog" class="modal" aria-labelledby="review-title">
  <form id="review-form" method="dialog">
    <div class="modal-head">
      <svg class="icon"><use href="#icon-eye"/></svg>
      <span id="review-title" class="title">Review comments</span>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="review-url">Repository URL</label>
        <input id="review-url" type="url" required placeholder="https://github.com/owner/repo"/>
      </div>
      <label class="check">
        <input id="review-integrate" type="checkbox"/>
        <span>Auto-integrate — LLM applies fixes and pushes new commits</span>
      </label>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn--sm" data-close-dialog="review-dialog">Cancel</button>
      <button type="submit" class="btn btn--primary btn--sm">Review</button>
    </div>
  </form>
</dialog>

<dialog id="fixci-dialog" class="modal" aria-labelledby="fixci-title">
  <form id="fixci-form" method="dialog">
    <div class="modal-head">
      <svg class="icon"><use href="#icon-tool"/></svg>
      <span id="fixci-title" class="title">Fix broken CI</span>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="fixci-url">Repository URL</label>
        <input id="fixci-url" type="url" required placeholder="https://github.com/owner/repo"/>
      </div>
      <div class="field">
        <label for="fixci-branch">Target branch</label>
        <input id="fixci-branch" type="text" required placeholder="gitoma/improve-2026…"/>
        <div class="hint">The Reflexion agent streams CI logs to the LLM, then proposes + pushes fixes.</div>
      </div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn--sm" data-close-dialog="fixci-dialog">Cancel</button>
      <button type="submit" class="btn btn--primary btn--sm">Run Fix-CI</button>
    </div>
  </form>
</dialog>

<dialog id="confirm-dialog" class="modal" aria-labelledby="confirm-title">
  <div class="modal-head">
    <svg class="icon"><use href="#icon-alert"/></svg>
    <span id="confirm-title" class="title">Are you sure?</span>
  </div>
  <div class="modal-body">
    <p id="confirm-body" style="margin:0;color:var(--fg-mid);font-size:12.5px;"></p>
  </div>
  <div class="modal-foot">
    <button type="button" class="btn btn--sm" data-close-dialog="confirm-dialog">Cancel</button>
    <button type="button" id="confirm-ok" class="btn btn--danger btn--sm">Confirm</button>
  </div>
</dialog>
"""


_DASHBOARD_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="color-scheme" content="dark"/>
<meta name="theme-color" content="#0a0a0b"/>
<title>Gitoma — Cockpit</title>
<style>{_DASHBOARD_CSS}</style>
</head>
<body>
{_DASHBOARD_ICONS}
{_DASHBOARD_BODY}
<script>{_DASHBOARD_JS}</script>
</body>
</html>
"""


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the self-contained live cockpit."""
    return HTMLResponse(_DASHBOARD_HTML)
