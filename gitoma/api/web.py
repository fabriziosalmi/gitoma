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
<title>Gitoma · Live Cockpit</title>
<style>
  :root {
    --bg: #07070c;
    --bg-elev: #0e0e16;
    --bg-card: #11111b;
    --fg: #e8e8f0;
    --fg-dim: #8a8a9e;
    --accent: #c084fc;
    --accent-2: #7dd3fc;
    --ok: #4ade80;
    --warn: #facc15;
    --fail: #f87171;
    --grid: #1a1a28;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--fg);
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }
  body::before {
    content: "";
    position: fixed; inset: 0;
    background:
      radial-gradient(ellipse at 10% 0%, rgba(192,132,252,0.12), transparent 50%),
      radial-gradient(ellipse at 90% 100%, rgba(125,211,252,0.10), transparent 50%),
      repeating-linear-gradient(
        0deg,
        transparent 0,
        transparent 3px,
        rgba(255,255,255,0.012) 3px,
        rgba(255,255,255,0.012) 4px
      );
    pointer-events: none;
    z-index: 0;
  }
  #particles { position: fixed; inset: 0; z-index: 0; pointer-events: none; }
  header {
    position: relative; z-index: 2;
    padding: 18px 28px;
    display: flex; align-items: center; gap: 18px;
    border-bottom: 1px solid var(--grid);
    background: linear-gradient(180deg, rgba(192,132,252,0.04), transparent);
  }
  header h1 {
    margin: 0; font-size: 18px; letter-spacing: .5px;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    font-weight: 700;
  }
  header .sub { color: var(--fg-dim); font-size: 11px; }
  header .status {
    margin-left: auto; display: flex; align-items: center; gap: 10px;
    font-size: 11px;
  }
  .pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--ok); box-shadow: 0 0 10px var(--ok);
    animation: pulse 1.5s ease-in-out infinite;
  }
  .pulse.off { background: var(--fail); box-shadow: 0 0 10px var(--fail); }
  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50% { opacity: .4; transform: scale(.7); }
  }
  main {
    position: relative; z-index: 2;
    display: grid; gap: 18px;
    grid-template-columns: minmax(280px, 320px) 1fr;
    padding: 24px 28px;
  }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--grid);
    border-radius: 8px;
    padding: 16px 18px;
    position: relative;
  }
  .card h2 {
    margin: 0 0 12px 0; font-size: 11px; letter-spacing: 1.5px;
    color: var(--fg-dim); text-transform: uppercase; font-weight: 600;
  }
  .empty { color: var(--fg-dim); font-size: 12px; padding: 6px 0; }
  .repo-item {
    padding: 10px 12px; margin: 4px 0;
    border-radius: 6px; cursor: pointer;
    border: 1px solid transparent;
    display: flex; align-items: center; gap: 10px;
    transition: background .15s, border-color .15s;
  }
  .repo-item:hover { background: rgba(192,132,252,0.06); }
  .repo-item.active { border-color: var(--accent); background: rgba(192,132,252,0.08); }
  .repo-item .slug { flex: 1; font-size: 12px; }
  .phase-chip {
    font-size: 10px; padding: 2px 6px; border-radius: 3px;
    background: var(--bg-elev); color: var(--fg-dim);
    border: 1px solid var(--grid);
  }
  .phase-chip.DONE { color: var(--ok); border-color: var(--ok); }
  .phase-chip.WORKING, .phase-chip.ANALYZING, .phase-chip.PLANNING { color: var(--warn); border-color: var(--warn); }
  .phase-chip.PR_OPEN { color: var(--accent-2); border-color: var(--accent-2); }
  .pipeline { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }
  .step {
    flex: 1 1 110px; min-width: 100px; padding: 12px;
    background: var(--bg-elev); border-radius: 6px;
    border: 1px solid var(--grid); text-align: center;
    transition: all .25s ease;
  }
  .step .label {
    font-size: 10px; letter-spacing: 1.3px;
    color: var(--fg-dim); text-transform: uppercase;
  }
  .step .icon { font-size: 20px; margin: 6px 0; display: block; }
  .step.active {
    border-color: var(--accent);
    background: linear-gradient(180deg, rgba(192,132,252,0.15), rgba(192,132,252,0.02));
    box-shadow: 0 0 24px rgba(192,132,252,0.25);
    transform: translateY(-2px);
  }
  .step.active .label { color: var(--accent); }
  .step.active .icon { animation: spin 2s linear infinite; }
  .step.done { border-color: var(--ok); background: rgba(74,222,128,0.05); }
  .step.done .icon { color: var(--ok); }
  .step.done .label { color: var(--ok); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .info-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px; margin: 12px 0;
  }
  .info-cell {
    background: var(--bg-elev); border: 1px solid var(--grid);
    border-radius: 6px; padding: 10px 12px;
  }
  .info-cell .k {
    font-size: 10px; letter-spacing: 1.2px; text-transform: uppercase;
    color: var(--fg-dim); margin-bottom: 4px;
  }
  .info-cell .v {
    font-size: 13px; color: var(--fg);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .info-cell .v.big { font-size: 18px; font-weight: 600; letter-spacing: .5px; }
  .info-cell a { color: var(--accent-2); text-decoration: none; }
  .info-cell a:hover { text-decoration: underline; }
  .metrics { display: grid; gap: 10px; }
  .metric-row {
    display: grid; grid-template-columns: 140px 1fr 56px; align-items: center; gap: 10px;
  }
  .metric-name { font-size: 12px; color: var(--fg-dim); }
  .metric-bar {
    position: relative; height: 8px;
    background: var(--bg-elev); border-radius: 4px; overflow: hidden;
  }
  .metric-bar .fill {
    position: absolute; top: 0; left: 0; bottom: 0;
    background: linear-gradient(90deg, var(--accent-2), var(--accent));
    transition: width .6s cubic-bezier(.2,.8,.2,1);
    box-shadow: 0 0 10px var(--accent);
  }
  .metric-bar .fill.fail { background: linear-gradient(90deg, var(--fail), #fb923c); box-shadow: 0 0 10px var(--fail); }
  .metric-bar .fill.warn { background: linear-gradient(90deg, var(--warn), #fb923c); box-shadow: 0 0 10px var(--warn); }
  .metric-score { text-align: right; font-variant-numeric: tabular-nums; font-size: 12px; }
  .score-big {
    font-size: 64px; font-weight: 800; line-height: 1;
    background: linear-gradient(180deg, var(--accent), var(--accent-2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    letter-spacing: -2px;
  }
  footer {
    text-align: center; padding: 16px; color: var(--fg-dim); font-size: 11px;
    border-top: 1px solid var(--grid);
  }
</style>
</head>
<body>
<canvas id="particles"></canvas>
<header>
  <h1>◈  GITOMA</h1>
  <span class="sub">Autonomous GitHub Agent · Live Cockpit</span>
  <div class="status">
    <span id="conn-dot" class="pulse off"></span>
    <span id="conn-label">connecting…</span>
  </div>
</header>
<main>
  <section class="card" id="repo-panel">
    <h2>◉  Tracked Repositories</h2>
    <div id="repo-list"><div class="empty">No tracked runs yet.</div></div>
  </section>
  <section>
    <div class="card">
      <h2>⬡  Pipeline</h2>
      <div id="pipeline" class="pipeline"></div>
      <div class="info-grid">
        <div class="info-cell">
          <div class="k">Repo</div>
          <div class="v" id="info-repo">—</div>
        </div>
        <div class="info-cell">
          <div class="k">Branch</div>
          <div class="v" id="info-branch">—</div>
        </div>
        <div class="info-cell">
          <div class="k">Phase</div>
          <div class="v" id="info-phase">—</div>
        </div>
        <div class="info-cell">
          <div class="k">Tasks</div>
          <div class="v big" id="info-tasks">—</div>
        </div>
        <div class="info-cell">
          <div class="k">Subtasks</div>
          <div class="v big" id="info-subtasks">—</div>
        </div>
        <div class="info-cell">
          <div class="k">Pull Request</div>
          <div class="v" id="info-pr">—</div>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top: 18px;">
      <h2>📊  Repo Health</h2>
      <div style="display: grid; grid-template-columns: 180px 1fr; gap: 24px; align-items: center;">
        <div style="text-align: center;">
          <div class="score-big" id="score">—</div>
          <div style="font-size: 10px; color: var(--fg-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-top: 4px;">Overall Score</div>
        </div>
        <div id="metrics" class="metrics"><div class="empty">No metric report yet.</div></div>
      </div>
    </div>
  </section>
</main>
<footer>◦ wss <code id="ws-url"></code> · polling every 500ms</footer>
<script>
const PHASES = ["IDLE", "ANALYZING", "PLANNING", "WORKING", "PR_OPEN", "REVIEWING", "DONE"];
const ICONS = {IDLE:"○", ANALYZING:"⟳", PLANNING:"⬡", WORKING:"⚙", PR_OPEN:"🚀", REVIEWING:"🔍", DONE:"✅"};

let states = [];
let selected = 0;

function renderPipeline(phase) {
  const container = document.getElementById("pipeline");
  const activeIdx = PHASES.indexOf(phase || "IDLE");
  container.innerHTML = PHASES.map((p, i) => {
    let cls = "step";
    if (i < activeIdx) cls += " done";
    else if (i === activeIdx) cls += " active";
    return `<div class="${cls}"><span class="icon">${ICONS[p]}</span><div class="label">${p}</div></div>`;
  }).join("");
}

function renderRepoList() {
  const host = document.getElementById("repo-list");
  if (!states.length) { host.innerHTML = '<div class="empty">No tracked runs yet.</div>'; return; }
  host.innerHTML = states.map((s, i) => {
    const slug = `${s.owner}/${s.name}`;
    const phase = s.phase || "IDLE";
    const active = i === selected ? " active" : "";
    return `<div class="repo-item${active}" data-idx="${i}">
      <span class="slug">${slug}</span>
      <span class="phase-chip ${phase}">${phase}</span>
    </div>`;
  }).join("");
  host.querySelectorAll(".repo-item").forEach(el => {
    el.addEventListener("click", () => {
      selected = parseInt(el.dataset.idx, 10);
      renderAll();
    });
  });
}

function renderMetrics(state) {
  const host = document.getElementById("metrics");
  const report = state && state.metric_report;
  if (!report || !report.metrics || !report.metrics.length) {
    host.innerHTML = '<div class="empty">No metric report yet.</div>';
    document.getElementById("score").textContent = "—";
    return;
  }
  const metrics = [...report.metrics].sort((a,b) => (a.score||0) - (b.score||0));
  host.innerHTML = metrics.map(m => {
    const pct = Math.round((m.score || 0) * 100);
    const cls = m.status === "fail" ? "fail" : m.status === "warn" ? "warn" : "";
    return `<div class="metric-row">
      <div class="metric-name">${m.display_name || m.key}</div>
      <div class="metric-bar"><div class="fill ${cls}" style="width:${pct}%"></div></div>
      <div class="metric-score">${pct}%</div>
    </div>`;
  }).join("");
  const overall = Math.round((report.overall_score || 0) * 100);
  document.getElementById("score").textContent = overall + "%";
}

function renderDetail(state) {
  if (!state) {
    ["repo","branch","phase","tasks","subtasks","pr"].forEach(k => {
      document.getElementById("info-" + k).textContent = "—";
    });
    renderPipeline("IDLE");
    return;
  }
  document.getElementById("info-repo").textContent = `${state.owner}/${state.name}`;
  document.getElementById("info-branch").textContent = state.branch || "—";
  document.getElementById("info-phase").textContent = state.phase || "IDLE";
  const plan = state.task_plan || {};
  const tasks = plan.tasks || [];
  const doneTasks = tasks.filter(t => t.status === "completed").length;
  const subtasks = tasks.flatMap(t => t.subtasks || []);
  const doneSubs = subtasks.filter(s => s.status === "completed").length;
  document.getElementById("info-tasks").textContent = tasks.length ? `${doneTasks}/${tasks.length}` : "—";
  document.getElementById("info-subtasks").textContent = subtasks.length ? `${doneSubs}/${subtasks.length}` : "—";
  const pr = state.pr_url;
  document.getElementById("info-pr").innerHTML = pr
    ? `<a href="${pr}" target="_blank" rel="noreferrer">#${state.pr_number || "?"}</a>`
    : "—";
  renderPipeline(state.phase);
}

function renderAll() {
  if (selected >= states.length) selected = 0;
  const state = states[selected] || null;
  renderRepoList();
  renderDetail(state);
  renderMetrics(state);
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/state`;
  document.getElementById("ws-url").textContent = url;
  const ws = new WebSocket(url);
  const dot = document.getElementById("conn-dot");
  const label = document.getElementById("conn-label");
  ws.onopen = () => { dot.classList.remove("off"); label.textContent = "live"; };
  ws.onclose = () => {
    dot.classList.add("off"); label.textContent = "reconnecting…";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (evt) => {
    try {
      states = JSON.parse(evt.data);
      renderAll();
    } catch (e) { console.warn("bad frame", e); }
  };
}
connect();
renderAll();

// ── Particle field (pure deco) ─────────────────────────────────────────────
(function particles() {
  const canvas = document.getElementById("particles");
  const ctx = canvas.getContext("2d");
  let w, h, pts = [];
  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
    const count = Math.floor((w * h) / 22000);
    pts = Array.from({length: count}, () => ({
      x: Math.random()*w, y: Math.random()*h,
      vx: (Math.random()-.5)*0.22, vy: (Math.random()-.5)*0.22,
      r: Math.random()*1.6 + .4,
    }));
  }
  function step() {
    ctx.clearRect(0,0,w,h);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x<0||p.x>w) p.vx *= -1;
      if (p.y<0||p.y>h) p.vy *= -1;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI*2);
      ctx.fillStyle = "rgba(192,132,252,0.5)";
      ctx.fill();
    }
    for (let i=0;i<pts.length;i++) for (let j=i+1;j<pts.length;j++) {
      const dx = pts[i].x-pts[j].x, dy = pts[i].y-pts[j].y;
      const d = Math.hypot(dx,dy);
      if (d < 110) {
        ctx.strokeStyle = `rgba(125,211,252,${(1-d/110)*0.18})`;
        ctx.lineWidth = 0.6;
        ctx.beginPath(); ctx.moveTo(pts[i].x,pts[i].y); ctx.lineTo(pts[j].x,pts[j].y); ctx.stroke();
      }
    }
    requestAnimationFrame(step);
  }
  window.addEventListener("resize", resize);
  resize(); step();
})();
</script>
</body>
</html>
"""


@web_router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the self-contained live cockpit."""
    return HTMLResponse(_DASHBOARD_HTML)
