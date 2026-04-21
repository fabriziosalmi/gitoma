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
  cancel(jobId) { return this._fetch("POST", `/jobs/${encodeURIComponent(jobId)}/cancel`); },
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
    $("log-stop").hidden = false;
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

  async stop() {
    if (!this.jobId) return;
    try {
      await API.cancel(this.jobId);
      Toast.info("Cancelling…", `Signal sent to job ${this.jobId.slice(0, 8)}`);
    } catch (err) {
      if (err.status === 409) {
        Toast.info("Already finished", err.message || "");
      } else if (err.status === 401 || err.status === 403) {
        Toast.error("Auth failed", "Re-enter the API token.");
        openDialog("token-dialog");
      } else {
        Toast.error("Cancel failed", err.message || "Unknown error");
      }
    }
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
    // Hide Stop button whenever we leave the running state.
    if (kind !== "running") $("log-stop").hidden = true;
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
    host.innerHTML = `
      <div class="empty" style="padding:24px 12px;">
        <div style="margin-bottom:10px;">No tracked runs yet.</div>
        <button class="btn btn--sm" id="empty-run-btn" type="button">
          ${svg("icon-play")}
          Launch first run
        </button>
      </div>`;
    const btn = $("empty-run-btn");
    if (btn) btn.addEventListener("click", () => onCommandTile("run"));
    return;
  }
  host.innerHTML = STATES.map((s, i) => {
    const slug = `${s.owner}/${s.name}`;
    const phase = s.phase || "IDLE";
    // An orphaned run surfaces as its own chip rather than the phase chip,
    // because the phase is stale by definition.
    const chipClass = s.is_orphaned ? "ORPHANED" : phase;
    const chipLabel = s.is_orphaned ? "ORPHANED" : phase.replace("_", " ");
    return `<div class="repo-item${i === SELECTED ? " active" : ""}" data-idx="${i}" role="button" tabindex="0">
      ${svg("icon-repo")}
      <span class="slug">${escape(slug)}</span>
      <span class="phase-chip ${chipClass}"><span class="dot"></span>${chipLabel}</span>
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
    host.innerHTML = `
      <div class="empty" style="padding:20px 8px;">
        <div style="margin-bottom:10px;">No metric report yet.</div>
        <button class="btn btn--sm" id="empty-analyze-btn" type="button">
          ${svg("icon-phase-analyzing")}
          Analyze repository
        </button>
      </div>`;
    $("score").textContent = "—";
    const btn = $("empty-analyze-btn");
    if (btn) btn.addEventListener("click", () => onCommandTile("analyze"));
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
    $("current-op-row").hidden = true;
    $("task-plan-card").hidden = true;
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
  renderCurrentOp(state);
  renderTaskPlan(state);
}

function _ageBucket(ms) {
  // returns {text, kind} — kind is "" | "warn" | "fail"
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return { text: `${secs}s ago`, kind: "" };
  const mins = Math.floor(secs / 60);
  if (mins < 3) return { text: `${mins}m ago`, kind: "" };
  if (mins < 10) return { text: `${mins}m ago`, kind: "warn" };
  if (mins < 60) return { text: `${mins}m ago`, kind: "fail" };
  const hrs = Math.floor(mins / 60);
  return { text: `${hrs}h ago`, kind: "fail" };
}

function renderCurrentOp(state) {
  const row = $("current-op-row");
  const phase = state.phase || "IDLE";
  const terminal = phase === "DONE";
  const op = state.current_operation || "";
  const hasErrors = (state.errors || []).length > 0;

  // Hide the row if there's no activity signal and we're in a terminal state.
  if (!op && terminal && !hasErrors) {
    row.hidden = true;
    return;
  }
  row.hidden = false;

  // Default label — but if phase is non-terminal and updated_at is old and no
  // explicit operation is recorded, tell the user the run looks stalled.
  let label = op || `${phase.replace("_", " ")} — awaiting`;
  const ageInfo = state.updated_at
    ? _ageBucket(Date.now() - new Date(state.updated_at).getTime())
    : { text: "—", kind: "" };
  if (!op && !terminal && !hasErrors && ageInfo.kind === "fail") {
    label = `${phase.replace("_", " ")} — appears stalled (no updates)`;
  }
  $("current-op-text").textContent = label;

  const ageEl = $("current-op-age");
  ageEl.textContent = ageInfo.text;
  ageEl.className = "age" + (ageInfo.kind ? " " + ageInfo.kind : "");

  // Spin the icon only while actively progressing (non-terminal, no errors).
  const icon = $("current-op-icon");
  icon.classList.toggle("spin", !terminal && !hasErrors);
}

function renderTaskPlan(state) {
  const card = $("task-plan-card");
  const plan = state.task_plan || {};
  const tasks = plan.tasks || [];
  if (!tasks.length) { card.hidden = true; return; }

  card.hidden = false;
  $("task-plan-count").textContent = `${tasks.filter(t => t.status === "completed").length}/${tasks.length}`;

  const host = $("task-list");
  host.innerHTML = tasks.map((t, i) => {
    const status = t.status || "pending";
    const subs = t.subtasks || [];
    const doneSubs = subs.filter(s => s.status === "completed").length;
    const failedSubs = subs.filter(s => s.status === "failed").length;
    const rowCls = "task-row " + status;
    const pillLabel = status === "in_progress" ? "RUNNING"
                    : status === "completed"  ? "DONE"
                    : status === "failed"     ? "FAILED"
                    : status === "skipped"    ? "SKIPPED"
                    : "PENDING";
    const progressText = subs.length ? `${doneSubs}/${subs.length}${failedSubs ? ` · ${failedSubs} failed` : ""}` : "";
    return `<div class="${rowCls}" title="${escape(t.description || "")}">
      <span class="badge">${i + 1}</span>
      <span class="title">${escape(t.title || t.id || "—")}</span>
      <span class="progress">${progressText}</span>
      <span class="status-pill"><span class="dot"></span>${pillLabel}</span>
    </div>`;
  }).join("");
}

function renderAgents(state) {
  const card = $("agents-card");
  if (!state) { card.hidden = true; return; }
  card.hidden = false;

  const phase = state.phase || "IDLE";
  const plan = state.task_plan || {};
  const tasks = plan.tasks || [];
  const subs = tasks.flatMap(t => t.subtasks || []);
  const allSubsTerminal = subs.length > 0 && subs.every(s => ["completed", "skipped", "failed"].includes(s.status));
  const hasPR = !!state.pr_url;
  const hasErrors = (state.errors || []).length > 0;

  const pastAnalyzing = ["PLANNING","WORKING","PR_OPEN","REVIEWING","DONE"].includes(phase);
  const pastPlanning  = ["WORKING","PR_OPEN","REVIEWING","DONE"].includes(phase);
  const workerDone    = (phase === "WORKING" && allSubsTerminal) || ["PR_OPEN","REVIEWING","DONE"].includes(phase);
  const prDone        = hasPR || ["REVIEWING","DONE"].includes(phase);
  const reviewerDone  = phase === "DONE";

  function cell(isActive, isDone) {
    if (isDone) return { state: "done", label: "done" };
    if (hasErrors && isActive) return { state: "failed", label: "failed" };
    if (isActive) return { state: "active", label: "active" };
    return { state: "idle", label: "idle" };
  }

  const roles = [
    { id: "analyzer", name: "Analyzer", icon: "icon-phase-analyzing",
      ...cell(phase === "ANALYZING", pastAnalyzing) },
    { id: "planner",  name: "Planner",  icon: "icon-phase-planning",
      ...cell(phase === "PLANNING",  pastPlanning) },
    { id: "worker",   name: "Worker",   icon: "icon-phase-working",
      ...cell(phase === "WORKING" && !allSubsTerminal, workerDone) },
    { id: "pr",       name: "PR Agent", icon: "icon-phase-pr",
      ...cell(phase === "WORKING" && allSubsTerminal && !hasPR, prDone) },
    { id: "reviewer", name: "Reviewer", icon: "icon-eye",
      ...cell(phase === "REVIEWING", reviewerDone) },
  ];

  $("agents-row").innerHTML = roles.map(r => `
    <div class="agent-cell" data-state="${r.state}" data-role="${r.id}" title="${r.name} — ${r.label}">
      ${svg(r.icon)}
      <span class="name">${r.name}</span>
      <span class="dot"></span>
    </div>`).join("");
}

function renderErrors(state) {
  const el = $("errors-banner");
  const errors = (state && state.errors) || [];
  if (!errors.length) { el.hidden = true; return; }

  el.hidden = false;
  const phase = state.phase || "IDLE";
  $("errors-title").textContent = `Run failed during ${phase.replace("_", " ")}`;
  $("errors-list").innerHTML = errors.map(err => `<li>${escape(String(err))}</li>`).join("");
  $("errors-hint").textContent =
    "Fix the underlying issue and re-run with `gitoma run <url> --reset` to start fresh, or `--resume` to continue.";
}

function renderOrphan(state) {
  const el = $("orphan-banner");
  if (!state || !state.is_orphaned) { el.hidden = true; return; }
  el.hidden = false;

  const phase = state.phase || "UNKNOWN";
  const pid = state.pid;
  const ageS = state.heartbeat_age_s;
  const ageText = ageS == null
    ? "never reported a heartbeat"
    : ageS < 60 ? `last heartbeat ${Math.round(ageS)}s ago`
    : ageS < 3600 ? `last heartbeat ${Math.round(ageS / 60)}m ago`
    : `last heartbeat ${Math.round(ageS / 3600)}h ago`;

  $("orphan-title").textContent =
    `Run orphaned in ${phase.replace("_", " ")}`;
  $("orphan-msg").innerHTML =
    `The CLI process <code>${pid ? "pid " + escape(String(pid)) : "(unknown pid)"}</code> owning this run ` +
    `is no longer alive (${escape(ageText)}). The state file is frozen — nothing is actively progressing. ` +
    `Use <strong>Reset state</strong> and relaunch, or inspect the gitoma CLI terminal for what happened.`;
}

function renderAll() {
  if (SELECTED >= STATES.length) SELECTED = 0;
  const state = STATES[SELECTED] || null;
  renderRepoList();
  renderDetail(state);
  renderMetrics(state);
  renderAgents(state);
  renderErrors(state);
  renderOrphan(state);
}

// ── Banner (persistent, actionable, non-modal) ──────────────────────────
const Banner = {
  show({ title, msg = "", actionLabel = null, actionFn = null, level = "warn" }) {
    const el = $("banner");
    el.classList.toggle("fail", level === "fail");
    $("banner-title").textContent = title;
    $("banner-msg").textContent = msg;
    const btn = $("banner-action");
    if (actionLabel && actionFn) {
      btn.textContent = actionLabel;
      btn.onclick = actionFn;
      btn.hidden = false;
    } else {
      btn.hidden = true;
      btn.onclick = null;
    }
    el.hidden = false;
  },
  hide() { $("banner").hidden = true; },
};

// ── Jobs badge (polled when anything is live) ───────────────────────────
async function refreshJobs() {
  try {
    const jobs = await API.jobs();
    const entries = Object.entries(jobs || {});
    const running = entries.filter(([, v]) => v.status === "running").length;
    const badge = $("jobs-badge");
    badge.classList.toggle("busy", running > 0);
    $("jobs-count").textContent = running ? `${running} running` : `${entries.length} total`;
    Banner.hide();
  } catch (err) {
    // Auth / server misconfig → stop polling and surface an actionable banner.
    stopJobPolling();
    $("jobs-count").textContent = "—";
    $("jobs-badge").classList.remove("busy");
    if (err.status === 401) {
      Banner.show({
        title: "API token required",
        msg: "Configure a Bearer token to issue commands.",
        actionLabel: "Configure",
        actionFn: () => $("settings-btn").click(),
      });
    } else if (err.status === 403) {
      Banner.show({
        title: "API token rejected",
        msg: "The token doesn't match the one on the server.",
        actionLabel: "Update token",
        actionFn: () => $("settings-btn").click(),
        level: "fail",
      });
    } else if (err.status === 503) {
      Banner.show({
        title: "Server not configured",
        msg: "GITOMA_API_TOKEN is missing on the server. Set it in ~/.gitoma/.env (or via `gitoma config set GITOMA_API_TOKEN=…`) and restart the server.",
        level: "fail",
      });
    } else {
      Banner.show({
        title: "Connection error",
        msg: err.message || "Unable to reach the API.",
        actionLabel: "Retry",
        actionFn: () => startJobPolling(),
        level: "fail",
      });
    }
  }
}

function startJobPolling() {
  if (JOB_POLL) return;
  refreshJobs();
  JOB_POLL = setInterval(refreshJobs, 3000);
}

// Refresh the "Xm ago" age indicator every 10 s even if the WS frame
// hasn't changed, so a stuck run visibly ages.
setInterval(() => {
  const state = STATES[SELECTED];
  if (state) renderCurrentOp(state);
}, 10000);

function stopJobPolling() {
  if (JOB_POLL) { clearInterval(JOB_POLL); JOB_POLL = null; }
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
    startJobPolling();  // re-enable if previously stopped by a 4xx/5xx
    if (res?.job_id) LogStream.open(res.job_id, name);
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      Toast.error("Auth failed", "Please re-enter the API token.");
      openDialog("token-dialog");
      return;
    }
    if (err.status === 503) {
      Toast.error("Server not configured", "GITOMA_API_TOKEN is missing server-side.");
      refreshJobs();  // re-raises the banner
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
    Banner.hide();
    startJobPolling();
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
    stopJobPolling();
    refreshJobs();  // one probe to re-show the appropriate banner
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

// ── Command palette ─────────────────────────────────────────────────────
const Palette = {
  items: [],
  filtered: [],
  selected: 0,

  commands() {
    return [
      { id: "run",      label: "Run full pipeline",   icon: "icon-play",             act: () => onCommandTile("run") },
      { id: "analyze",  label: "Analyze repository",  icon: "icon-phase-analyzing",  act: () => onCommandTile("analyze") },
      { id: "review",   label: "Review PR comments",  icon: "icon-eye",              act: () => onCommandTile("review") },
      { id: "fix-ci",   label: "Fix broken CI",       icon: "icon-tool",             act: () => onCommandTile("fix-ci") },
      { id: "settings", label: "Configure API token", icon: "icon-settings",         act: () => $("settings-btn").click() },
      { id: "refresh",  label: "Refresh jobs",        icon: "icon-zap",              act: () => refreshJobs() },
      {
        id: "reset", label: `Reset state for ${STATES[SELECTED] ? STATES[SELECTED].owner + "/" + STATES[SELECTED].name : "…"}`,
        icon: "icon-trash", destructive: true,
        when: () => !!STATES[SELECTED], act: () => confirmAndReset(),
      },
      {
        id: "close-log", label: "Close live output",
        icon: "icon-x", when: () => $("log-card").classList.contains("open"),
        act: () => LogStream.hide(),
      },
    ];
  },

  open() {
    this.items = this.commands().filter((c) => !c.when || c.when());
    this.filtered = this.items.slice();
    this.selected = 0;
    const input = $("palette-input");
    input.value = "";
    this.render();
    openDialog("palette-dialog");
    setTimeout(() => input.focus(), 0);
  },

  filter(query) {
    const q = query.trim().toLowerCase();
    if (!q) { this.filtered = this.items.slice(); }
    else {
      this.filtered = this.items.filter((c) => c.label.toLowerCase().includes(q));
    }
    this.selected = 0;
    this.render();
  },

  render() {
    const host = $("palette-list");
    if (!this.filtered.length) {
      host.innerHTML = '<div class="empty">No matching commands.</div>';
      return;
    }
    host.innerHTML = this.filtered.map((c, i) => {
      const cls = "palette-item" + (c.destructive ? " destructive" : "") + (i === this.selected ? " selected" : "");
      return `<div class="${cls}" role="option" data-idx="${i}">
        ${svg(c.icon)}
        <span class="label">${escape(c.label)}</span>
      </div>`;
    }).join("");
    host.querySelectorAll(".palette-item").forEach((el) => {
      el.addEventListener("click", () => {
        this.selected = parseInt(el.dataset.idx, 10);
        this.run();
      });
      el.addEventListener("mousemove", () => {
        const idx = parseInt(el.dataset.idx, 10);
        if (idx !== this.selected) { this.selected = idx; this.render(); }
      });
    });
    const current = host.querySelector(".palette-item.selected");
    if (current) current.scrollIntoView({ block: "nearest" });
  },

  move(delta) {
    if (!this.filtered.length) return;
    this.selected = (this.selected + delta + this.filtered.length) % this.filtered.length;
    this.render();
  },

  run() {
    const cmd = this.filtered[this.selected];
    if (!cmd) return;
    closeDialog("palette-dialog");
    setTimeout(() => cmd.act(), 50);
  },
};

function wirePalette() {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  $("palette-kbd").textContent = isMac ? "⌘K" : "Ctrl K";

  $("palette-btn").addEventListener("click", () => Palette.open());
  $("palette-input").addEventListener("input", (e) => Palette.filter(e.target.value));
  $("palette-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown")      { e.preventDefault(); Palette.move(1); }
    else if (e.key === "ArrowUp")   { e.preventDefault(); Palette.move(-1); }
    else if (e.key === "Enter")     { e.preventDefault(); Palette.run(); }
  });

  // Global shortcuts
  document.addEventListener("keydown", (e) => {
    // ⌘K / Ctrl+K → open palette
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      Palette.open();
      return;
    }
    // Ignore single-letter shortcuts when typing, modifiers held, or any dialog is open
    const focus = document.activeElement;
    const tag = focus && focus.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || (focus && focus.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const k = e.key.toLowerCase();
    const map = { r: "run", a: "analyze", v: "review", f: "fix-ci" };
    if (map[k]) {
      e.preventDefault();
      onCommandTile(map[k]);
    }
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
          openDialog("token-dialog");
          Toast.error("Auth failed", "Please re-enter the API token.");
        } else if (err.status === 503) {
          Toast.error("Server not configured", "GITOMA_API_TOKEN is missing server-side.");
          refreshJobs();
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
  $("log-stop").addEventListener("click", () => LogStream.stop());

  wireDialogs();
  wirePalette();
  connectWS();
  renderAll();
  startJobPolling();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
