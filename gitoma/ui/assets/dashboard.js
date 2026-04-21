/* =============================================================================
 * Gitoma Cockpit — client runtime
 *
 * Industrial-grade pass:
 *   - Store singleton (single source of truth, drives renderAll).
 *   - Api wrapper with AbortController timeout + classified errors.
 *   - WebSocket reconnect: exponential backoff + jitter, cap at 30 s,
 *     pauses when the tab is hidden (no pointless battery drain).
 *   - Focus trap in every dialog (WCAG 2.4.3) + Esc close + focus return.
 *   - Zero innerHTML for dynamic content — DOM API end-to-end.
 *   - Mac/Ctrl detection for the palette kbd hint.
 *   - Log FIFO capped at 5000 rows; buffer never grows unbounded.
 *   - Cancel button waits for the server's __END__:cancelled sentinel
 *     before transitioning the stream state.
 *   - Token migrated to sessionStorage (still readable from legacy
 *     localStorage key for one release).
 *   - Skip-link target: <main> is tabindex=-1 so it can receive focus.
 * ============================================================================= */

// ── Constants & tiny utils ───────────────────────────────────────────────
const PHASES = ["IDLE","ANALYZING","PLANNING","WORKING","PR_OPEN","REVIEWING","DONE"];
const PHASE_ICON = {
  IDLE:"icon-phase-idle", ANALYZING:"icon-phase-analyzing",
  PLANNING:"icon-phase-planning", WORKING:"icon-phase-working",
  PR_OPEN:"icon-phase-pr", REVIEWING:"icon-phase-review", DONE:"icon-phase-done",
};
const $ = (id) => document.getElementById(id);

// Mac / non-Mac detection — used for the palette kbd hint label AND for
// distinguishing ⌘ vs Ctrl in keyboard shortcuts. navigator.platform is
// deprecated but still the best heuristic; userAgentData covers fresh Chromium.
const IS_MAC = (() => {
  const platform = (navigator.userAgentData && navigator.userAgentData.platform)
    || navigator.platform || "";
  return /Mac|iPhone|iPad|iPod/i.test(platform);
})();

/** Build an SVG <use> sprite reference as a real DOM element (not a string). */
function svgEl(id, cls = "icon") {
  const NS = "http://www.w3.org/2000/svg";
  const XLINK = "http://www.w3.org/1999/xlink";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(NS, "use");
  use.setAttribute("href", `#${id}`);
  // Legacy browsers (pre-Chromium Edge) honoured xlink:href — harmless now.
  use.setAttributeNS(XLINK, "xlink:href", `#${id}`);
  svg.appendChild(use);
  return svg;
}

/** createElement shorthand. Pass attrs as object; children as vararg.
 *
 * The `style` key is handled via the DOM property setter
 * (`node.style.cssText = v`) instead of `setAttribute("style", v)`.
 * The two paths look equivalent but they aren't from CSP's point of
 * view: ``setAttribute("style", ...)`` is treated by the parser as an
 * inline style attribute and gated by ``style-src 'unsafe-inline'``;
 * the DOM property write executes under the script's own permissions
 * (``script-src``) and is allowed under a strict ``style-src 'self'``.
 * Routing through the property setter is what lets the cockpit CSP
 * drop ``'unsafe-inline'`` from ``style-src``.
 */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class")      node.className = v;
    else if (k === "text")  node.textContent = v;
    else if (k === "html")  throw new Error("el(): use textContent, not html");
    else if (k === "style") node.style.cssText = String(v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else                    node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

/** Remove all children of a node. */
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// ── Token store: sessionStorage only ────────────────────────────────────
// sessionStorage is scoped to the tab and cleared on tab close — a more
// defensive default for a local cockpit. The legacy localStorage key
// (``gitoma.api_token``) was migrated for one release with no expiry; we
// now do a one-shot eviction at module load (best-effort, no migration:
// any stale value is wiped, the user re-enters the token once). This
// shrinks the persistent attack surface — a token in localStorage
// survived browser close and could be exfiltrated by a future XSS at
// any later session.
const Token = {
  key: "gitoma.api_token.v2",
  get() { return sessionStorage.getItem(this.key) || ""; },
  set(v) {
    if (v) sessionStorage.setItem(this.key, v);
    else sessionStorage.removeItem(this.key);
  },
  has() { return !!this.get(); },
};
// One-shot eviction of the long-deprecated localStorage token. Best-effort
// (private-mode browsers can throw on storage access); the intent is to
// remove any leftover persistent copy regardless of whether the user has
// already migrated to sessionStorage.
try { localStorage.removeItem("gitoma.api_token"); } catch {}

// ── API client: AbortController timeout, classified errors ───────────────
const API = {
  TIMEOUT_MS: 15000,
  async _fetch(method, path, body) {
    const token = Token.get();
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.TIMEOUT_MS);
    let res;
    try {
      res = await fetch("/api/v1" + path, {
        method, headers, body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timeoutId);
      if (err.name === "AbortError") {
        const e = new Error("Request timed out after " + (this.TIMEOUT_MS / 1000) + "s");
        e.status = 0; e.kind = "timeout";
        throw e;
      }
      const e = new Error(err.message || "Network error");
      e.status = 0; e.kind = "network";
      throw e;
    }
    clearTimeout(timeoutId);
    let data = null;
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      const detail = (data && (data.detail || data.error_id)) || res.statusText || "Request failed";
      const err = new Error(typeof detail === "string" ? detail : "Request failed");
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

// ── Store: single source of truth ────────────────────────────────────────
//
// The WebSocket pushes a fresh snapshot every 500 ms when *anything* on
// disk changes. Without diffing, every push triggered a full re-render of
// the repo list, metrics, agents, task plan and detail card — wiping
// keyboard focus, scroll position and screen-reader cursor every tick.
// We dedupe at two levels:
//   1. setStates() compares the serialized snapshot against the last one
//      and skips emit entirely if nothing moved (JSON.stringify is O(n)
//      but n is small — ~1-100 KB — and dramatically cheaper than a DOM
//      rebuild).
//   2. Each section render function (renderRepoList, renderMetrics, …)
//      memoises its own input slice and bails when unchanged. So a frame
//      that only changes `updated_at` repaints only the timestamp, not
//      the entire repo list.
//
// _stableStringify ensures we don't get a false-positive diff from key
// reordering on the server — JSON.stringify already preserves insertion
// order for plain objects, but defensive sorting keeps the diff stable
// across server-side dict implementation churn.
function _stableStringify(value) {
  return JSON.stringify(value, (_, v) =>
    (v && typeof v === "object" && !Array.isArray(v))
      ? Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]]))
      : v
  );
}

const Store = {
  _states: [],
  _selected: 0,
  _listeners: new Set(),
  _lastSerialized: null,

  get states() { return this._states; },
  get selected() { return this._selected; },
  get current() { return this._states[this._selected] || null; },

  setStates(next) {
    if (!Array.isArray(next)) return;
    const serialized = _stableStringify(next);
    if (serialized === this._lastSerialized) return;  // no-op tick
    this._lastSerialized = serialized;
    this._states = next;
    if (this._selected >= this._states.length) this._selected = 0;
    this._emit();
  },
  select(i) {
    const clamped = Math.max(0, Math.min(i, Math.max(0, this._states.length - 1)));
    if (clamped === this._selected) return;
    this._selected = clamped;
    this._emit();
  },
  subscribe(fn) { this._listeners.add(fn); return () => this._listeners.delete(fn); },
  _emit() {
    for (const fn of this._listeners) {
      try { fn(); } catch (e) { console.error("store listener", e); }
    }
  },
};

// Per-section render memo: each render function calls _memo(key, payload, fn)
// and the body only runs if the payload's serialized form differs from the
// last call under the same key.
const _renderMemo = new Map();
function _memo(key, payload, fn) {
  const sig = _stableStringify(payload);
  if (_renderMemo.get(key) === sig) return false;
  _renderMemo.set(key, sig);
  fn();
  return true;
}
function _invalidateMemo(key) { _renderMemo.delete(key); }

// ── Toast system ────────────────────────────────────────────────────────
const Toast = {
  _seen: new Map(),   // key → timestamp of last emission (for de-dup)
  _dedupMs: 2500,

  host() { return $("toasts"); },
  show(level, title, msg = "") {
    const key = `${level}:${title}:${msg}`;
    const now = Date.now();
    const last = this._seen.get(key) || 0;
    if (now - last < this._dedupMs) return;
    this._seen.set(key, now);

    const icons = { success: "icon-check", error: "icon-alert", info: "icon-info" };
    const node = el("div", { class: `toast ${level}`, role: "status" },
      svgEl(icons[level] || icons.info),
      el("div", { class: "body" },
        el("div", { class: "title", text: title }),
        msg ? el("div", { class: "msg", text: msg }) : null,
      ),
    );
    this.host().appendChild(node);
    setTimeout(() => {
      node.classList.add("leaving");
      node.addEventListener("animationend", () => node.remove(), { once: true });
    }, 4200);
  },
  success(t, m) { this.show("success", t, m); },
  error(t, m)   { this.show("error", t, m); },
  info(t, m)    { this.show("info", t, m); },
};

// ── Dialog helpers: focus trap + restore + Esc + click-outside ──────────
const DialogStack = {
  _openers: new Map(),   // dialog element → element that was focused before open
  _trapHandlers: new Map(),

  open(id) {
    const dlg = $(id);
    if (!dlg || dlg.open) return;
    this._openers.set(dlg, document.activeElement);
    dlg.showModal();
    this._installTrap(dlg);
    // Focus the first focusable control — or the dialog itself as fallback.
    const focusables = this._focusableIn(dlg);
    (focusables[0] || dlg).focus();
  },
  close(id) {
    const dlg = $(id);
    if (!dlg || !dlg.open) return;
    this._uninstallTrap(dlg);
    dlg.close();
    const opener = this._openers.get(dlg);
    this._openers.delete(dlg);
    if (opener && typeof opener.focus === "function") {
      try { opener.focus(); } catch {}
    }
  },
  _focusableIn(dlg) {
    return Array.from(dlg.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(el => el.offsetParent !== null || el === document.activeElement);
  },
  _installTrap(dlg) {
    const handler = (ev) => {
      if (ev.key !== "Tab") return;
      const focusables = this._focusableIn(dlg);
      if (focusables.length === 0) { ev.preventDefault(); return; }
      const first = focusables[0];
      const last  = focusables[focusables.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault(); last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault(); first.focus();
      }
    };
    dlg.addEventListener("keydown", handler);
    this._trapHandlers.set(dlg, handler);
  },
  _uninstallTrap(dlg) {
    const h = this._trapHandlers.get(dlg);
    if (h) dlg.removeEventListener("keydown", h);
    this._trapHandlers.delete(dlg);
  },
};

function openDialog(id)  { DialogStack.open(id); }
function closeDialog(id) { DialogStack.close(id); }

document.addEventListener("click", (e) => {
  const t = e.target;
  // Click on the <dialog> element itself = click on its backdrop (native).
  if (t instanceof HTMLDialogElement && t.open) DialogStack.close(t.id);
  if (t && t.dataset && t.dataset.closeDialog) closeDialog(t.dataset.closeDialog);
});

// ── Connection state ────────────────────────────────────────────────────
const Conn = {
  state: "connecting",  // "live" | "reconnecting" | "offline"
  _wasLiveOnce: false,
  set(kind) {
    if (kind === this.state) return;
    const prev = this.state;
    this.state = kind;
    const dot = $("conn-dot");
    const lbl = $("conn-label");
    dot.classList.remove("live", "down");
    if (kind === "live") {
      dot.classList.add("live");
      lbl.textContent = "live";
      if (this._wasLiveOnce && prev === "reconnecting") {
        Toast.success("Connection restored");
      }
      this._wasLiveOnce = true;
    } else {
      dot.classList.add("down");
      lbl.textContent = kind === "reconnecting" ? "reconnecting" : "offline";
    }
    // The /api/v1/jobs poll cadence depends on whether the WS is the
    // active source of truth. When the WS is live, slow the poller
    // down (it's just a freshness/heartbeat ping); when the WS is
    // down, speed it up so the cockpit recovers visibly.
    if (typeof _retuneJobPolling === "function") _retuneJobPolling();
  },
};

// ── Live log stream ──────────────────────────────────────────────────────
const LOG_MAX_ROWS = 5000;

const LogStream = {
  controller: null,
  jobId: null,
  _autoScroll: true,
  _scrollHandler: null,
  _cancelled: false,

  open(jobId, label) {
    this.close();
    this.jobId = jobId;
    this._cancelled = false;
    $("log-card").classList.add("open");
    $("log-title").textContent = `Live Output — ${label}`;
    this._setStatus("running");
    $("log-stop").hidden = false;
    const pre = $("log-stream");
    clear(pre);
    this._autoScroll = true;
    if (this._scrollHandler) pre.removeEventListener("scroll", this._scrollHandler);
    this._scrollHandler = () => {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 12;
      this._autoScroll = atBottom;
    };
    pre.addEventListener("scroll", this._scrollHandler);
    this._consume(jobId).catch((err) => {
      this._appendRaw(`[stream error] ${err.message || err}`, "error");
      this._setStatus("fail");
    });
  },

  async stop() {
    if (!this.jobId || this._cancelled) return;
    this._cancelled = true;
    try {
      await API.cancel(this.jobId);
      Toast.info("Cancel requested", `Signal sent to job ${this.jobId.slice(0, 8)}`);
      // The transition to "cancelled" is driven by the __END__:cancelled
      // sentinel arriving through the SSE stream — so we intentionally do
      // NOT touch the status pill here. This closes the UX loop the old
      // implementation left open (user saw "Cancelling…" forever).
    } catch (err) {
      this._cancelled = false;
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
          let payload;
          try {
            payload = JSON.parse(line.slice(5).trim());
          } catch {
            // Ignore malformed frames — don't kill the stream on a single
            // corrupt chunk (used to silently stop tailing).
            continue;
          }
          const text = payload.line || "";
          if (text.startsWith("__END__")) {
            const status = text.split(":").slice(1).join(":") || "completed";
            const kind = status === "cancelled" ? "cancelled"
                       : status.startsWith("failed") || status === "timed_out" ? "fail"
                       : "done";
            this._setStatus(kind, status);
            return;
          }
          this._appendLine(text);
        }
      }
    }
    // Stream ended without __END__ (network drop, server shutdown…).
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
    const row = el("span", { class: "row" + (cls ? " " + cls : ""), text: text + "\n" });
    pre.appendChild(row);

    // FIFO cap: DOM with 50k+ rows trashes scroll on long runs.
    while (pre.childElementCount > LOG_MAX_ROWS) {
      pre.removeChild(pre.firstElementChild);
    }
    if (this._autoScroll) pre.scrollTop = pre.scrollHeight;
  },

  _setStatus(kind, label) {
    const pill = $("log-status");
    pill.className = "status-pill " + (
      kind === "done" ? "done"
      : kind === "fail" ? "fail"
      : kind === "cancelled" ? "fail"
      : ""
    );
    $("log-status-label").textContent = label || kind;
    if (kind !== "running") $("log-stop").hidden = true;
  },
};

// ── Rendering ───────────────────────────────────────────────────────────
function renderPipeline(phase) {
  const el_ = $("pipeline");
  const idx = Math.max(0, PHASES.indexOf(phase || "IDLE"));

  // Diff-based render: re-use existing <li> children if the phase list
  // matches (it always does — PHASES is constant). Only class names
  // change, no layout thrash.
  if (el_.childElementCount !== PHASES.length) {
    clear(el_);
    for (const p of PHASES) {
      const li = el("li", { class: "step", "aria-label": p.replace("_", " ") },
        svgEl(PHASE_ICON[p]),
        el("span", { class: "label", text: p.replace("_", " ") }),
      );
      el_.appendChild(li);
    }
  }
  Array.from(el_.children).forEach((step, i) => {
    step.className = "step" + (i < idx ? " done" : i === idx ? " active" : "");
    step.setAttribute("aria-current", i === idx ? "step" : "false");
  });

  const chip = $("phase-chip-top");
  chip.className = `phase-chip ${phase || "IDLE"}`;
  $("phase-chip-label").textContent = (phase || "IDLE").replace("_", " ");
}

function renderRepoList() {
  // Slice the inputs that actually drive this section. If they haven't
  // changed, do nothing — DOM identity is preserved, focus + scroll
  // survive, screen readers stop chattering.
  const slice = Store.states.map(s => ({
    owner: s.owner, name: s.name, phase: s.phase,
    is_orphaned: !!s.is_orphaned,
  }));
  const ranOnce = _memo("repo-list", { slice, selected: Store.selected }, () => {
    const host = $("repo-list");
    $("repo-count").textContent = Store.states.length;
    // Capture the focused index before clearing so we can restore focus
    // to the equivalent item after rebuild — otherwise a WS tick during
    // keyboard navigation silently sends focus back to <body>.
    const prevFocus = document.activeElement;
    const prevFocusIdx = (prevFocus && prevFocus.classList && prevFocus.classList.contains("repo-item"))
      ? parseInt(prevFocus.dataset.idx, 10)
      : -1;
    clear(host);
    if (!Store.states.length) {
      const empty = el("div", { class: "empty", role: "status" },
        el("span", { text: "No tracked runs yet." }),
        el("button", {
          class: "btn btn--sm empty-cta",
          type: "button",
          onclick: () => onCommandTile("run"),
        }, svgEl("icon-play"), "Launch first run"),
      );
      host.appendChild(empty);
      return;
    }
    Store.states.forEach((s, i) => {
      const slug = `${s.owner}/${s.name}`;
      const phase = s.phase || "IDLE";
      const isOrphan = !!s.is_orphaned;
      const chipClass = isOrphan ? "ORPHANED" : phase;
      const chipLabel = isOrphan ? "ORPHANED" : phase.replace("_", " ");
      const item = el("div", {
        class: "repo-item" + (i === Store.selected ? " active" : ""),
        role: "option",
        "aria-selected": i === Store.selected ? "true" : "false",
        tabindex: i === Store.selected ? "0" : "-1",
        dataset: { idx: String(i) },
      },
        svgEl("icon-repo"),
        el("span", { class: "slug", text: slug }),
        el("span", { class: `phase-chip ${chipClass}` },
          el("span", { class: "dot" }),
          chipLabel,
        ),
      );
      host.appendChild(item);
    });
    // Restore focus to the equivalent item if the user was navigating.
    if (prevFocusIdx >= 0 && prevFocusIdx < Store.states.length) {
      const restored = host.querySelector(`.repo-item[data-idx="${prevFocusIdx}"]`);
      if (restored) {
        try { restored.focus({ preventScroll: true }); } catch {}
      }
    }
  });
  // No-op when memoised — but the repo-count badge is the only thing
  // that's cheap enough to refresh unconditionally. Keep it in sync if
  // the count changed beneath us via a non-list-affecting field.
  if (!ranOnce) {
    $("repo-count").textContent = Store.states.length;
  }
}

function renderMetrics(state) {
  const report = state && state.metric_report;
  // Memo on the report itself (the only input that affects this render).
  // A repo-list selection change still passes a different `report` so it
  // re-renders correctly.
  _memo("metrics", report || null, () => {
    const host = $("metrics");
    clear(host);
    if (!report || !report.metrics || !report.metrics.length) {
      const empty = el("div", { class: "empty", role: "status" },
        el("span", { text: "No metric report yet." }),
        el("button", {
          class: "btn btn--sm empty-cta",
          type: "button",
          onclick: () => onCommandTile("analyze"),
        }, svgEl("icon-phase-analyzing"), "Analyze repository"),
      );
      host.appendChild(empty);
      $("score").textContent = "—";
      return;
    }
    const metrics = [...report.metrics].sort((a, b) => (a.score || 0) - (b.score || 0));
    for (const m of metrics) {
      const pct = Math.max(0, Math.min(100, Math.round((m.score || 0) * 100)));
      const cls = m.status === "fail" ? "fail" : m.status === "warn" ? "warn" : "";
      const row = el("div", { class: "metric-row", role: "listitem" },
        el("div", { class: "metric-name", text: (m.display_name || m.key || "—") }),
        el("div", { class: "metric-bar" },
          el("div", { class: `fill${cls ? " " + cls : ""}`, style: `width:${pct}%` }),
        ),
        el("div", { class: "metric-score", text: `${pct}%` }),
      );
      host.appendChild(row);
    }
    $("score").textContent = Math.round((report.overall_score || 0) * 100) + "%";
  });
}

function renderDetail(state) {
  const keys = ["repo", "branch", "tasks", "subtasks", "pr", "updated"];
  if (!state) {
    keys.forEach((k) => ($("info-" + k).textContent = "—"));
    renderPipeline("IDLE");
    $("reset-btn").disabled = true;
    $("current-op-row").hidden = true;
    $("task-plan-card").hidden = true;
    return;
  }
  $("info-repo").textContent   = `${state.owner || "?"}/${state.name || "?"}`;
  $("info-branch").textContent = state.branch || "—";
  const plan = state.task_plan || {};
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  const doneTasks = tasks.filter((t) => t.status === "completed").length;
  const subtasks = tasks.flatMap((t) => Array.isArray(t.subtasks) ? t.subtasks : []);
  const doneSubs = subtasks.filter((s) => s.status === "completed").length;
  $("info-tasks").textContent    = tasks.length    ? `${doneTasks} / ${tasks.length}` : "—";
  $("info-subtasks").textContent = subtasks.length ? `${doneSubs} / ${subtasks.length}` : "—";

  const prHost = $("info-pr");
  clear(prHost);
  if (state.pr_url) {
    const link = el("a", {
      href: state.pr_url, target: "_blank", rel: "noreferrer",
      "aria-label": `Pull request number ${state.pr_number || "unknown"} (opens in a new tab)`,
    },
      document.createTextNode(`#${state.pr_number || "?"} `),
      svgEl("icon-external"),
    );
    prHost.appendChild(link);
  } else {
    prHost.textContent = "—";
  }

  // Localised updated_at — handles timezone + absent server timezone.
  const updatedRaw = state.updated_at || "";
  if (updatedRaw) {
    const dt = new Date(updatedRaw);
    $("info-updated").textContent = isNaN(dt.getTime())
      ? updatedRaw.slice(11, 19)
      : dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } else {
    $("info-updated").textContent = "—";
  }

  renderPipeline(state.phase);
  $("reset-btn").disabled = false;
  renderCurrentOp(state);
  renderTaskPlan(state);
}

function _ageBucket(ms) {
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
  const hasErrors = Array.isArray(state.errors) && state.errors.length > 0;

  if (!op && terminal && !hasErrors) { row.hidden = true; return; }
  row.hidden = false;

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

  const icon = $("current-op-icon");
  icon.classList.toggle("spin", !terminal && !hasErrors);
}

function renderTaskPlan(state) {
  const card = $("task-plan-card");
  const plan = state.task_plan || {};
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  if (!tasks.length) { card.hidden = true; _invalidateMemo("task-plan"); return; }
  // Memoise on the task list itself — a different repo selection passes
  // a different `tasks` reference so the re-render still fires when needed.
  _memo("task-plan", tasks, () => {
    card.hidden = false;
    $("task-plan-count").textContent =
      `${tasks.filter((t) => t.status === "completed").length}/${tasks.length}`;

    const host = $("task-list");
    clear(host);
    tasks.forEach((t, i) => {
      const status = t.status || "pending";
      const subs = Array.isArray(t.subtasks) ? t.subtasks : [];
      const doneSubs = subs.filter((s) => s.status === "completed").length;
      const failedSubs = subs.filter((s) => s.status === "failed").length;
      const progressText = subs.length
        ? `${doneSubs}/${subs.length}${failedSubs ? ` · ${failedSubs} failed` : ""}`
        : "";
      const pillLabel = status === "in_progress" ? "RUNNING"
                      : status === "completed"   ? "DONE"
                      : status === "failed"      ? "FAILED"
                      : status === "skipped"     ? "SKIPPED"
                      : "PENDING";
      const row = el("div", {
        class: `task-row ${status}`,
        role: "listitem",
        title: t.description || "",
      },
        el("span", { class: "badge", text: String(i + 1) }),
        el("span", { class: "title", text: (t.title || t.id || "—") }),
        el("span", { class: "progress", text: progressText }),
        el("span", { class: "status-pill" },
          el("span", { class: "dot" }),
          pillLabel,
        ),
      );
      host.appendChild(row);
    });
  });
}

function renderAgents(state) {
  const card = $("agents-card");
  if (!state) { card.hidden = true; _invalidateMemo("agents"); return; }

  const phase = state.phase || "IDLE";
  const plan = state.task_plan || {};
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  const subs = tasks.flatMap((t) => Array.isArray(t.subtasks) ? t.subtasks : []);
  const allSubsTerminal = subs.length > 0
    && subs.every((s) => ["completed", "skipped", "failed"].includes(s.status));
  const hasPR = !!state.pr_url;
  const hasErrors = Array.isArray(state.errors) && state.errors.length > 0;
  // Memoise on the (small) tuple of inputs that actually drive the cells.
  // The full state changes on every WS frame; these five fields don't.
  const memoKey = { phase, hasPR, hasErrors, allSubsTerminal, taskCount: tasks.length };
  if (_renderMemo.get("agents") === _stableStringify(memoKey)) return;
  _renderMemo.set("agents", _stableStringify(memoKey));
  card.hidden = false;

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

  const host = $("agents-row");
  clear(host);
  for (const r of roles) {
    host.appendChild(el("div", {
      class: "agent-cell",
      role: "listitem",
      "aria-label": `${r.name}: ${r.label}`,
      dataset: { state: r.state, role: r.id },
      title: `${r.name} — ${r.label}`,
    },
      svgEl(r.icon),
      el("span", { class: "name", text: r.name }),
      el("span", { class: "dot" }),
    ));
  }
}

function renderErrors(state) {
  const banner = $("errors-banner");
  const errors = (state && Array.isArray(state.errors)) ? state.errors : [];
  if (!errors.length) { banner.hidden = true; return; }
  banner.hidden = false;
  const phase = state.phase || "IDLE";
  $("errors-title").textContent = `Run failed during ${phase.replace("_", " ")}`;
  const list = $("errors-list");
  clear(list);
  for (const err of errors) {
    list.appendChild(el("li", { text: String(err) }));
  }
  $("errors-hint").textContent =
    "Fix the underlying issue and re-run with `gitoma run <url> --reset` to start fresh, or `--resume` to continue.";
}

function renderOrphan(state) {
  const banner = $("orphan-banner");
  if (!state || !state.is_orphaned) { banner.hidden = true; return; }
  banner.hidden = false;

  const phase = state.phase || "UNKNOWN";
  const pid = state.pid;
  const ageS = state.heartbeat_age_s;
  const ageText = ageS == null
    ? "never reported a heartbeat"
    : ageS < 60   ? `last heartbeat ${Math.round(ageS)}s ago`
    : ageS < 3600 ? `last heartbeat ${Math.round(ageS / 60)}m ago`
    : `last heartbeat ${Math.round(ageS / 3600)}h ago`;

  $("orphan-title").textContent = `Run orphaned in ${phase.replace("_", " ")}`;
  // Build the message as a DOM fragment — no innerHTML — so even a future
  // injection into `pid` or `ageText` would be harmlessly text.
  const msg = $("orphan-msg");
  clear(msg);
  msg.append(
    document.createTextNode("The CLI process "),
    el("code", { text: pid ? `pid ${pid}` : "(unknown pid)" }),
    document.createTextNode(
      ` owning this run is no longer alive (${ageText}). The state file is frozen — nothing is actively progressing. Use `,
    ),
    el("strong", { text: "Reset state" }),
    document.createTextNode(
      " and relaunch, or inspect the gitoma CLI terminal for what happened.",
    ),
  );
}

function renderAll() {
  const state = Store.current;
  renderRepoList();
  renderDetail(state);
  renderMetrics(state);
  renderAgents(state);
  renderErrors(state);
  renderOrphan(state);
}
Store.subscribe(renderAll);

// ── Banner (persistent, actionable, non-modal) ──────────────────────────
const Banner = {
  show({ title, msg = "", actionLabel = null, actionFn = null, level = "warn" }) {
    const banner = $("banner");
    banner.classList.toggle("fail", level === "fail");
    // A failure banner is an assertion the user must notice; a warning is
    // a gentler status. Toggle role accordingly.
    banner.setAttribute("role", level === "fail" ? "alert" : "status");
    banner.setAttribute("aria-live", level === "fail" ? "assertive" : "polite");
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
    banner.hidden = false;
  },
  hide() { $("banner").hidden = true; },
};

// ── Jobs badge (polled when anything is live) ───────────────────────────
let JOB_POLL = null;

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
      // Usually means the server was restarted between two polls (deploy,
      // launchctl reload, service crash). Offer Retry so the user isn't
      // stuck on a stale banner — the next poll against a recovered
      // server hides the banner automatically via the success path above.
      Banner.show({
        title: "Server not configured",
        msg: "GITOMA_API_TOKEN is missing on the server. Set it in ~/.gitoma/.env (or via `gitoma config set GITOMA_API_TOKEN=…`) and restart. If you just redeployed, click Retry once uvicorn is back.",
        actionLabel: "Retry",
        actionFn: () => startJobPolling(),
        level: "fail",
      });
    } else if (err.kind === "timeout") {
      Banner.show({
        title: "API timed out",
        msg: "Server isn't responding within 15 s.",
        actionLabel: "Retry",
        actionFn: () => startJobPolling(),
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

// Polling cadence:
//   * WS live  → slow heartbeat (the WS already pushes state changes,
//                so the badge only needs a stale-detector + freshness ping).
//   * WS down  → faster fallback so a server restart shows up quickly
//                in the cockpit.
//   * On dispatch / user action → immediate refresh (callers invoke
//                ``refreshJobs()`` explicitly).
//
// The previous unconditional 3 s poll generated 1200 req/h per open
// cockpit, each round-tripping through ``load_config`` if the cache
// was cold. The slow cadence cuts that to ~240 req/h with no UX cost
// because the WS is the actual source of state change.
const JOB_POLL_INTERVAL_FAST_MS = 3000;
const JOB_POLL_INTERVAL_SLOW_MS = 15000;

function _jobPollIntervalForCurrentConn() {
  return Conn.state === "live" ? JOB_POLL_INTERVAL_SLOW_MS : JOB_POLL_INTERVAL_FAST_MS;
}

function startJobPolling() {
  if (JOB_POLL) return;
  refreshJobs();
  JOB_POLL = setInterval(refreshJobs, _jobPollIntervalForCurrentConn());
}
function stopJobPolling() {
  if (JOB_POLL) { clearInterval(JOB_POLL); JOB_POLL = null; }
}
// Re-rate the poller when the WS connection state flips. Cheaper than
// adding a permanent listener: we just call this from Conn.set().
function _retuneJobPolling() {
  if (!JOB_POLL) return;
  clearInterval(JOB_POLL);
  JOB_POLL = setInterval(refreshJobs, _jobPollIntervalForCurrentConn());
}

// Age tick: refresh the "Xm ago" label every 10 s even if the WS frame
// hasn't changed, so a stalled run visibly ages.
setInterval(() => {
  const state = Store.current;
  if (state) renderCurrentOp(state);
}, 10000);

// ── WebSocket connection: exponential backoff + jitter + tab-visibility ─
//
// Auth: the server requires the same Bearer token as /api/v1/* whenever one
// is configured. Browsers can't set custom headers on WebSocket handshakes,
// so we tunnel the token via the ``Sec-WebSocket-Protocol`` subprotocol —
// the standard workaround. The encoding (base64url, no padding) keeps the
// raw token out of every server access log AND fits the RFC 6455 token
// grammar (which forbids the ``+ / =`` characters of vanilla base64).
function _b64urlEncodeUtf8(s) {
  // btoa() only handles latin-1; route UTF-8 through TextEncoder for
  // future-proofing (today the token is base64url-no-padding from
  // secrets.token_urlsafe → ASCII, but operator-supplied tokens may not be).
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const WS = {
  socket: null,
  reconnectDelay: 1500,
  reconnectTimer: null,
  manualClose: false,

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/state`;
    $("ws-url").textContent = url;
    const token = Token.get();
    const protocols = token ? [`gitoma-bearer.${_b64urlEncodeUtf8(token)}`] : [];
    try {
      this.socket = protocols.length
        ? new WebSocket(url, protocols)
        : new WebSocket(url);
    } catch (err) {
      console.error("ws ctor", err);
      this._scheduleReconnect();
      return;
    }
    // Capture the socket reference at handler-attach time. This lets
    // each handler tell whether it still owns `this.socket` when it
    // fires — an orphan socket (from a prior failed attempt, closed via
    // WS.disconnect() right before we opened this one) will otherwise
    // fire `onclose` AFTER the new one is already live and flip the
    // Conn pill back to "reconnecting". Root cause of the "live but
    // UI says reconnecting" bug visible after pasting the token.
    const sock = this.socket;
    sock.onopen = () => {
      if (this.socket !== sock) return;  // orphan
      Conn.set("live");
      this.reconnectDelay = 1500;   // reset on successful connect
    };
    sock.onclose = () => {
      if (this.socket !== sock) return;  // orphan — stale close event
      if (this.manualClose) return;
      Conn.set("reconnecting");
      this._scheduleReconnect();
    };
    sock.onerror = () => {
      if (this.socket !== sock) return;  // orphan
      try { sock.close(); } catch {}
    };
    sock.onmessage = (evt) => {
      if (this.socket !== sock) return;  // orphan
      try {
        const parsed = JSON.parse(evt.data);
        if (Array.isArray(parsed)) Store.setStates(parsed);
      } catch (e) {
        console.warn("bad frame", e);
      }
    };
  },

  _scheduleReconnect() {
    if (this.reconnectTimer) return;
    if (document.hidden) return;  // paused until the tab is visible
    const jitter = Math.random() * 500;
    const delay = Math.min(this.reconnectDelay + jitter, 30000);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 30000);
  },

  disconnect() {
    this.manualClose = true;
    if (this.socket) {
      // Detach handlers BEFORE close() so any queued onclose from a
      // still-failing connection attempt (which can fire even after we
      // clear this.socket) doesn't re-enter _scheduleReconnect and
      // clobber the pill state. The orphan guard on the handlers
      // themselves already handles the race; this just makes the
      // detachment explicit and helps the GC drop the socket cleanly.
      this.socket.onopen = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
      try { this.socket.close(); } catch {}
      this.socket = null;
    }
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  },

  resume() {
    this.manualClose = false;
    if (!this.socket || this.socket.readyState >= WebSocket.CLOSING) {
      this.connect();
    }
  },
};

// Pause WS + polling when the tab is hidden; resume on return. Saves
// power and reduces server load for inactive cockpit tabs.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    WS.disconnect();
    stopJobPolling();
  } else {
    WS.resume();
    startJobPolling();
  }
});

// ── Command dispatch ────────────────────────────────────────────────────
let __pendingAction = null;

function requireToken(nextFn) {
  if (Token.has()) { nextFn(); return; }
  __pendingAction = nextFn;
  openDialog("token-dialog");
}

async function submitCommand(name, fn) {
  try {
    const res = await fn();
    Toast.success(name + " dispatched", res?.job_id ? `Job ${res.job_id.slice(0, 8)}` : "");
    startJobPolling();
    if (res?.job_id) LogStream.open(res.job_id, name);
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      Toast.error("Auth failed", "Please re-enter the API token.");
      openDialog("token-dialog");
      return;
    }
    if (err.status === 503) {
      Toast.error("Server not configured", "GITOMA_API_TOKEN is missing server-side.");
      refreshJobs();
      return;
    }
    if (err.status === 422) {
      Toast.error(`${name} rejected`, "Check that the URL and branch match the allowed format.");
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
    // The WebSocket handshake reads the token at connect time. Force a
    // reconnect so the just-saved token is used — otherwise the cockpit
    // stays "reconnecting" against a server that requires auth.
    WS.disconnect();
    WS.resume();
    if (__pendingAction) {
      const fn = __pendingAction;
      __pendingAction = null;
      fn();
    }
  });
  $("token-clear").addEventListener("click", () => {
    Token.set("");
    $("token-input").value = "";
    closeDialog("token-dialog");
    Toast.info("Token cleared");
    stopJobPolling();
    refreshJobs();
    // Also drop and reattempt the WS so the server sees a clean handshake;
    // it will fail-close if the server requires auth, which surfaces in the
    // connection-status pill.
    WS.disconnect();
    WS.resume();
  });

  // Show/hide the token input — a small UX win that stops users pasting
  // a wrong token and never being able to verify it.
  $("token-reveal").addEventListener("click", () => {
    const input = $("token-input");
    const btn = $("token-reveal");
    const revealed = input.type === "text";
    input.type = revealed ? "password" : "text";
    btn.setAttribute("aria-pressed", revealed ? "false" : "true");
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
    const s = Store.current;
    return [
      { id: "run",      label: "Run full pipeline",   icon: "icon-play",             act: () => onCommandTile("run") },
      { id: "analyze",  label: "Analyze repository",  icon: "icon-phase-analyzing",  act: () => onCommandTile("analyze") },
      { id: "review",   label: "Review PR comments",  icon: "icon-eye",              act: () => onCommandTile("review") },
      { id: "fix-ci",   label: "Fix broken CI",       icon: "icon-tool",             act: () => onCommandTile("fix-ci") },
      { id: "settings", label: "Configure API token", icon: "icon-settings",         act: () => $("settings-btn").click() },
      { id: "refresh",  label: "Refresh jobs",        icon: "icon-zap",              act: () => refreshJobs() },
      {
        id: "reset",
        label: `Reset state for ${s ? s.owner + "/" + s.name : "…"}`,
        icon: "icon-trash", destructive: true,
        when: () => !!Store.current, act: () => confirmAndReset(),
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
    input.focus();
  },

  filter(q) {
    q = q.trim().toLowerCase();
    this.filtered = q ? this.items.filter((c) => c.label.toLowerCase().includes(q)) : this.items.slice();
    this.selected = 0;
    this.render();
  },

  render() {
    const host = $("palette-list");
    clear(host);
    if (!this.filtered.length) {
      host.appendChild(el("div", { class: "empty", role: "status", text: "No matching commands." }));
      return;
    }
    this.filtered.forEach((c, i) => {
      const item = el("div", {
        class: "palette-item" + (c.destructive ? " destructive" : "") + (i === this.selected ? " selected" : ""),
        role: "option",
        "aria-selected": i === this.selected ? "true" : "false",
        dataset: { idx: String(i) },
      },
        svgEl(c.icon),
        el("span", { class: "label", text: c.label }),
      );
      host.appendChild(item);
    });
    const selected = host.querySelector(".palette-item.selected");
    if (selected) selected.scrollIntoView({ block: "nearest" });
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
  $("palette-kbd").textContent = IS_MAC ? "⌘K" : "Ctrl+K";

  $("palette-btn").addEventListener("click", () => Palette.open());
  $("palette-input").addEventListener("input", (e) => Palette.filter(e.target.value));
  $("palette-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown")    { e.preventDefault(); Palette.move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); Palette.move(-1); }
    else if (e.key === "Enter")   { e.preventDefault(); Palette.run(); }
  });

  // Delegated click + mousemove on the palette list.
  $("palette-list").addEventListener("click", (e) => {
    const item = e.target.closest(".palette-item");
    if (!item) return;
    Palette.selected = parseInt(item.dataset.idx, 10);
    Palette.run();
  });
  $("palette-list").addEventListener("mousemove", (e) => {
    const item = e.target.closest(".palette-item");
    if (!item) return;
    const idx = parseInt(item.dataset.idx, 10);
    if (idx !== Palette.selected) { Palette.selected = idx; Palette.render(); }
  });

  // Global shortcuts — mod detection honours the host platform.
  document.addEventListener("keydown", (e) => {
    const mod = IS_MAC ? e.metaKey : e.ctrlKey;
    if (mod && e.key.toLowerCase() === "k") {
      e.preventDefault();
      Palette.open();
      return;
    }
    // Ignore single-letter shortcuts when typing / any dialog is open.
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

// ── Repo list: delegated click + arrow-key navigation ───────────────────
function wireRepoList() {
  const host = $("repo-list");
  host.addEventListener("click", (e) => {
    const item = e.target.closest(".repo-item");
    if (!item) return;
    const idx = parseInt(item.dataset.idx, 10);
    if (!Number.isNaN(idx)) Store.select(idx);
  });
  host.addEventListener("keydown", (e) => {
    const items = Array.from(host.querySelectorAll(".repo-item"));
    if (!items.length) return;
    let idx = items.findIndex((it) => it === document.activeElement);
    if (idx < 0) idx = Store.selected;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = Math.min(items.length - 1, idx + 1);
      Store.select(next);
      items[next] && items[next].focus();
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = Math.max(0, idx - 1);
      Store.select(prev);
      items[prev] && items[prev].focus();
    } else if (e.key === "Home") {
      e.preventDefault(); Store.select(0); items[0] && items[0].focus();
    } else if (e.key === "End") {
      e.preventDefault(); Store.select(items.length - 1); items.at(-1) && items.at(-1).focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const item = e.target.closest(".repo-item");
      if (item) Store.select(parseInt(item.dataset.idx, 10));
    }
  });
}

function confirmAndReset() {
  const s = Store.current;
  if (!s) return;
  $("confirm-title").textContent = "Reset state?";
  $("confirm-body").textContent =
    `Delete the persisted state for ${s.owner}/${s.name}? This only removes the local progress — no changes are pushed.`;
  $("confirm-ok").textContent = "Reset state";
  $("confirm-ok").className = "btn btn--danger btn--sm";
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
    $("token-input").type = "password";
    $("token-reveal").setAttribute("aria-pressed", "false");
    openDialog("token-dialog");
  });
  $("jobs-badge").addEventListener("click", refreshJobs);
  $("reset-btn").addEventListener("click", confirmAndReset);
  $("log-close").addEventListener("click", () => LogStream.hide());
  $("log-stop").addEventListener("click", () => LogStream.stop());

  wireDialogs();
  wirePalette();
  wireRepoList();
  WS.connect();
  renderAll();
  startJobPolling();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
