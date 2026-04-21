# Web cockpit

The cockpit is the read-only dashboard served at `/` by `gitoma serve`. It reflects the state of every tracked run as the CLI progresses, with live log streaming, a command palette, and full keyboard navigation.

```bash
gitoma serve &
open http://localhost:8000
```

## What you see

| Panel | What it shows |
|---|---|
| **Header status** | A green dot when the WebSocket is alive, a **red pulsing dot + "OFFLINE"** when the connection is lost. |
| **Commands** | Four buttons: Run, Analyze, Review, Fix-CI. Each opens a form-driven dialog, validates the URL/branch before dispatch, and streams the result. |
| **Repositories** | Every `~/.gitoma/state/*.json` as a keyboard-navigable list (Arrow keys, Home/End). |
| **Pipeline** | A 7-step progress tracker — IDLE → ANALYZING → PLANNING → WORKING → PR_OPEN → REVIEWING → DONE — with semantic `<ol>` markup for screen readers. |
| **Agents** | Five personas (Analyzer, Planner, Worker, PR agent, Reviewer) with live state dots. |
| **Run Details** | Repo slug, branch, task/subtask counts, PR link, last-updated timestamp. |
| **Current operation** | What the agent is doing *right now*, with an ageing indicator that turns amber then red if progress stalls. |
| **Task Plan** | Expanded tree of tasks and subtasks, with per-row status pills. |
| **Health Metrics** | Latest analyzer report — bar chart per metric + overall score. |
| **Live Output** | SSE stream of the running subprocess, line-by-line. Auto-scroll follows the tail until you scroll up. |

## Banners

The cockpit surfaces three levels of situation:

- **Info banner** (top, `role="status"`) — "Token required to use commands", "Server not configured", etc. Informational, non-disruptive.
- **Orphan banner** (orange, `role="alert"`) — the CLI process owning a run is no longer alive. The state file is frozen; nothing is actively progressing. Includes the dead PID and heartbeat age; the fix is **Reset state** + relaunch.
- **Errors banner** (red, `role="alert"`) — the most recent run failed; the list shows every error accumulated during the pipeline. Hint text points at `--reset` / `--resume`.

## Keyboard shortcuts

Focus anywhere not inside an input:

| Key | Action |
|---|---|
| <kbd>R</kbd> | Launch a new run |
| <kbd>A</kbd> | Analyze |
| <kbd>V</kbd> | Review |
| <kbd>F</kbd> | Fix CI |
| <kbd>Cmd/Ctrl</kbd><kbd>K</kbd> | Open the command palette |

Inside the command palette:

| Key | Action |
|---|---|
| <kbd>↑</kbd> <kbd>↓</kbd> | Navigate |
| <kbd>↵</kbd> | Run the selected command |
| <kbd>Esc</kbd> | Close |

Inside any modal:

- <kbd>Tab</kbd> and <kbd>Shift</kbd>+<kbd>Tab</kbd> cycle focus within the dialog (focus trap).
- <kbd>Esc</kbd> closes the dialog.
- Focus returns to the element that opened the dialog on close.

Press <kbd>Tab</kbd> on a fresh load to reveal the **Skip to main content** link — one press jumps past the header chrome straight into the main panel.

## Accessibility

- **WCAG AA** contrast on every foreground token (dim log rows, status chips, hints).
- **Full keyboard navigation** — every interactive element is reachable; visible focus rings are accent-coloured.
- **Screen readers** — semantic landmarks (`<main>`, `<nav>`, `<aside>`, `<footer>`), `aria-live` regions on the log stream, phase chip, task plan, and toasts. `aria-hidden="true"` on every decorative SVG icon.
- **Motion** — `@media (prefers-reduced-motion: reduce)` zeroes animations **and** transitions. The pulsing status-dot also honours it.
- **High contrast** — `@media (prefers-contrast: more)` strengthens borders and widens focus outlines.

## Settings + token

Click the gear icon or press <kbd>Cmd/Ctrl</kbd><kbd>K</kbd> → **Configure API token**.

The token lives in `sessionStorage` scoped to the tab (cleared when you close it). The input has a show/hide adornment — paste once, toggle to verify, save. If you previously used an older Gitoma version that stored the token in `localStorage`, it is automatically migrated on first load.

## Security posture (in brief)

- `/` and `/ws/state` are **public, read-only** on the assumption that the server runs on localhost or a trusted VPN. The state snapshots contain no credentials.
- `/api/v1/*` requires a Bearer token (the one in Settings). Timing-safe compare; wrong token returns 403, missing header returns 401 with `WWW-Authenticate`.
- `/ws/state` refuses WebSocket handshakes from browser origins outside `GITOMA_WS_ALLOWED_ORIGINS` (defaults to localhost). WebSockets skip CORS preflight — this is the only layer that stops a drive-by page from subscribing.
- Every response ships `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`.

Full details in [Architecture → Security](/architecture/security).
