"""Public web UI for Gitoma — static dashboard + live state WebSocket.

Unlike the `/api/v1/*` router which requires a Bearer token, the web UI is
intended to run on a trusted network (localhost / VPN). The dashboard is
read-only and streams the same `~/.gitoma/state/*.json` files that the CLI
writes after every phase.
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


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Gitoma — Cockpit</title>
<style>
  :root {
    --bg:            #0a0a0b;
    --bg-elev:       #111113;
    --bg-card:       #131316;
    --bg-subtle:     #17171b;
    --border:        #1e1e23;
    --border-strong: #2a2a30;
    --fg:            #ededed;
    --fg-mid:        #a0a0a8;
    --fg-dim:        #6b6b75;
    --fg-faint:      #45454e;
    --accent:        #3b82f6;
    --accent-soft:   rgba(59,130,246,0.12);
    --ok:            #22c55e;
    --ok-soft:       rgba(34,197,94,0.12);
    --warn:          #eab308;
    --warn-soft:     rgba(234,179,8,0.12);
    --fail:          #ef4444;
    --fail-soft:     rgba(239,68,68,0.12);
    --radius:        6px;
    --radius-lg:     8px;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
  }
  code, .mono { font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }

  /* ── Layout ────────────────────────────────────────────────────────────── */
  .app { display: grid; grid-template-rows: auto 1fr auto; min-height: 100vh; }

  header {
    display: flex; align-items: center; gap: 20px;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-logo {
    width: 22px; height: 22px;
    color: var(--fg);
  }
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
  .status {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: var(--fg-mid);
  }
  .status-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--fg-faint);
    transition: background .2s;
  }
  .status-dot.live {
    background: var(--ok);
    box-shadow: 0 0 0 3px var(--ok-soft);
  }
  .status-dot.down { background: var(--fail); }

  main {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 16px;
    padding: 16px 24px 24px;
  }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }

  .stack { display: flex; flex-direction: column; gap: 16px; }

  /* ── Card ──────────────────────────────────────────────────────────────── */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
  }
  .card-head {
    display: flex; align-items: center; gap: 8px;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
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
  }
  .card-body { padding: 14px 16px; }
  .card-body.dense { padding: 8px; }
  .empty {
    color: var(--fg-dim); font-size: 12px;
    padding: 20px 8px; text-align: center;
  }

  /* ── Repo list ─────────────────────────────────────────────────────────── */
  .repo-item {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; margin: 0;
    border-radius: var(--radius); cursor: pointer;
    border: 1px solid transparent;
    transition: background .12s, border-color .12s;
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
    transition: all .2s ease;
  }
  .step .icon {
    width: 18px; height: 18px;
    color: var(--fg-dim);
  }
  .step .label {
    font-size: 10px; font-weight: 500; letter-spacing: 0.5px;
    color: var(--fg-dim); text-transform: uppercase;
  }
  .step.done { border-color: rgba(34,197,94,0.3); background: var(--ok-soft); }
  .step.done .icon { color: var(--ok); }
  .step.done .label { color: var(--ok); }
  .step.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .step.active .icon { color: var(--accent); animation: spin 2.5s linear infinite; }
  .step.active .label { color: var(--accent); }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── KPI grid ──────────────────────────────────────────────────────────── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 1px;
    background: var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .kpi {
    background: var(--bg-card);
    padding: 12px 14px;
  }
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
  .kpi .v a {
    color: var(--accent); text-decoration: none;
    display: inline-flex; align-items: center; gap: 4px;
  }
  .kpi .v a:hover { text-decoration: underline; }
  .kpi .v a .icon { width: 11px; height: 11px; }

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
  .metric-name { font-size: 12px; color: var(--fg-mid); }
  .metric-bar {
    position: relative; height: 6px;
    background: var(--bg-subtle); border-radius: 3px; overflow: hidden;
  }
  .metric-bar .fill {
    position: absolute; top: 0; left: 0; bottom: 0;
    background: var(--accent); border-radius: 3px;
    transition: width .5s cubic-bezier(.3,.7,.2,1);
  }
  .metric-bar .fill.warn { background: var(--warn); }
  .metric-bar .fill.fail { background: var(--fail); }
  .metric-score {
    text-align: right; font-size: 12px; color: var(--fg-mid);
    font-variant-numeric: tabular-nums;
  }

  /* ── Footer ────────────────────────────────────────────────────────────── */
  footer {
    padding: 10px 24px;
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px;
    font-size: 11px; color: var(--fg-dim);
  }
  footer .mono { color: var(--fg-mid); }
  footer .sep { color: var(--fg-faint); }
</style>
</head>
<body>
<!-- ── Icon sprite (referenced via <use href="#icon-..."/>) ─────────────── -->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <defs>
    <symbol id="icon-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"/>
      <path d="M3 12h18M12 3v18"/>
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

<div class="app">
  <header>
    <div class="brand">
      <svg class="brand-logo"><use href="#icon-logo"/></svg>
      <span class="brand-name">Gitoma</span>
      <span class="brand-badge">Cockpit</span>
    </div>
    <div class="header-spacer"></div>
    <div class="status">
      <span id="conn-dot" class="status-dot down"></span>
      <span id="conn-label">connecting</span>
    </div>
  </header>

  <main>
    <!-- Left: repo list ──────────────────────────────────────────────────── -->
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

    <!-- Right: pipeline + KPIs + metrics ────────────────────────────────── -->
    <div class="stack">
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
        <div class="card-body" style="padding: 0;">
          <div class="kpi-grid">
            <div class="kpi">
              <div class="k">Repository</div>
              <div class="v mono" id="info-repo">—</div>
            </div>
            <div class="kpi">
              <div class="k">Branch</div>
              <div class="v mono" id="info-branch">—</div>
            </div>
            <div class="kpi">
              <div class="k">Tasks</div>
              <div class="v xl" id="info-tasks">—</div>
            </div>
            <div class="kpi">
              <div class="k">Subtasks</div>
              <div class="v xl" id="info-subtasks">—</div>
            </div>
            <div class="kpi">
              <div class="k">Pull Request</div>
              <div class="v" id="info-pr">—</div>
            </div>
            <div class="kpi">
              <div class="k">Updated</div>
              <div class="v mono" id="info-updated">—</div>
            </div>
          </div>
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
          <div id="metrics" class="metrics">
            <div class="empty">No metric report yet.</div>
          </div>
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
    <span>Read-only</span>
  </footer>
</div>

<script>
const PHASES = ["IDLE","ANALYZING","PLANNING","WORKING","PR_OPEN","REVIEWING","DONE"];
const PHASE_ICON = {
  IDLE:      "icon-phase-idle",
  ANALYZING: "icon-phase-analyzing",
  PLANNING:  "icon-phase-planning",
  WORKING:   "icon-phase-working",
  PR_OPEN:   "icon-phase-pr",
  REVIEWING: "icon-phase-review",
  DONE:      "icon-phase-done",
};

let states = [];
let selected = 0;

const $ = (id) => document.getElementById(id);

function svg(iconId, cls = "icon") {
  return `<svg class="${cls}"><use href="#${iconId}"/></svg>`;
}

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
  $("repo-count").textContent = states.length;
  if (!states.length) {
    host.innerHTML = '<div class="empty">No tracked runs yet.</div>';
    return;
  }
  host.innerHTML = states.map((s, i) => {
    const slug = `${s.owner}/${s.name}`;
    const phase = s.phase || "IDLE";
    return `<div class="repo-item${i === selected ? " active" : ""}" data-idx="${i}">
      ${svg("icon-repo")}
      <span class="slug">${slug}</span>
      <span class="phase-chip ${phase}"><span class="dot"></span>${phase.replace("_"," ")}</span>
    </div>`;
  }).join("");
  host.querySelectorAll(".repo-item").forEach((el) => {
    el.addEventListener("click", () => {
      selected = parseInt(el.dataset.idx, 10);
      renderAll();
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
      <div class="metric-name">${m.display_name || m.key}</div>
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
    ? `<a href="${pr}" target="_blank" rel="noreferrer">#${state.pr_number || "?"} ${svg("icon-external")}</a>`
    : "—";

  const updated = state.updated_at || "";
  $("info-updated").textContent = updated ? updated.slice(11, 19) : "—";

  renderPipeline(state.phase);
}

function renderAll() {
  if (selected >= states.length) selected = 0;
  const state = states[selected] || null;
  renderRepoList();
  renderDetail(state);
  renderMetrics(state);
}

// ── WebSocket ────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/state`;
  $("ws-url").textContent = url;
  const ws = new WebSocket(url);
  const dot = $("conn-dot");
  const label = $("conn-label");
  ws.onopen = () => { dot.classList.remove("down"); dot.classList.add("live"); label.textContent = "live"; };
  ws.onclose = () => {
    dot.classList.remove("live"); dot.classList.add("down"); label.textContent = "reconnecting";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (evt) => {
    try {
      states = JSON.parse(evt.data);
      renderAll();
    } catch (e) {
      console.warn("bad frame", e);
    }
  };
}
connect();
renderAll();
</script>
</body>
</html>
"""


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the self-contained live cockpit."""
    return HTMLResponse(_DASHBOARD_HTML)
