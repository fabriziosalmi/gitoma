"""Gitoma TUI — Fullscreen interactive dashboard powered by Textual.

Launch with: gitoma tui
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Optional, Tuple

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Log,
    Markdown,
    ProgressBar,
    Static,
)

# ── Constants ──────────────────────────────────────────────────────────────────

STATE_DIR = Path.home() / ".gitoma" / "state"
TELEMETRY_DIR = Path.home() / ".gitoma" / "telemetry"

_PHASES = [
    ("IDLE", "○ IDLE"),
    ("ANALYZING", "◉ ANALYZING"),
    ("PLANNING", "◉ PLANNING"),
    ("WORKING", "◉ WORKING"),
    ("PR_OPEN", "◉ PR OPEN"),
    ("REVIEWING", "◉ REVIEWING"),
    ("DONE", "◉ DONE"),
]

_PHASE_ICONS = {
    "IDLE": "⬡",
    "ANALYZING": "⟳",
    "PLANNING": "⬡",
    "WORKING": "⚙",
    "PR_OPEN": "🚀",
    "REVIEWING": "🔍",
    "DONE": "✅",
}

_VERSION = "0.1.0"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_all_states() -> list[dict]:
    """Load all agent states from disk."""
    states: list[dict] = []
    if STATE_DIR.exists():
        for p in sorted(STATE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                states.append(json.loads(p.read_text()))
            except Exception:
                pass
    return states


def _latest_telemetry() -> str | None:
    """Return content of the most recent telemetry report."""
    if not TELEMETRY_DIR.exists():
        return None
    reports = sorted(TELEMETRY_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
    if reports:
        return reports[0].read_text()
    return None


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── Modals ──────────────────────────────────────────────────────────────────────

class InputModal(ModalScreen):
    """Modal to capture repo URL + optional branch."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str = "New Run", show_branch: bool = True) -> None:
        super().__init__()
        self._title = title
        self._show_branch = show_branch

    def compose(self) -> ComposeResult:
        with Container(id="modal-container"):
            yield Static(f"◈  {self._title}", id="modal-title")
            yield Static("GitHub Repository URL:", id="modal-body")
            yield Input(placeholder="https://github.com/owner/repo", id="url-input")
            if self._show_branch:
                yield Static("Branch (optional for CI fix):", id="modal-body")
                yield Input(placeholder="gitoma/my-branch", id="branch-input")
            with Horizontal(id="modal-buttons"):
                yield Button("✕  Cancel", id="cancel-btn", variant="default")
                yield Button("▶  Launch", id="confirm-btn", classes="primary")

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        url = self.query_one("#url-input", Input).value.strip()
        if not url:
            return
        branch = ""
        if self._show_branch:
            try:
                branch = self.query_one("#branch-input", Input).value.strip()
            except Exception:
                pass
        self.dismiss((url, branch))  # type: ignore[arg-type]

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen):
    """Simple yes/no confirmation dialog."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, message: str = "Are you sure?") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="modal-container"):
            yield Static("⚠  Confirm Action", id="modal-title")
            yield Static(self._message, id="modal-body")
            with Horizontal(id="modal-buttons"):
                yield Button("✕  Cancel", id="cancel-btn", variant="default")
                yield Button("✓  Confirm", id="confirm-btn", classes="danger")

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.dismiss(False)


class TelemetryModal(ModalScreen):
    """Full-screen telemetry viewer for Observer Agent reports."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("q", "dismiss(None)", "Close"),
    ]

    def __init__(self, content: str) -> None:
        super().__init__()
        self._content = content

    def compose(self) -> ComposeResult:
        with Container(id="telemetry-container"):
            yield Static("👁  Observer Agent — Meta-Cognitive Telemetry", id="modal-title")
            with ScrollableContainer(id="telemetry-scroll"):
                yield Markdown(self._content)
            with Horizontal(id="modal-buttons"):
                yield Button("✕  Close", id="close-btn", classes="danger")

    @on(Button.Pressed, "#close-btn")
    def _close(self) -> None:
        self.dismiss(None)


class ConfigModal(ModalScreen):
    """Shows current config key/value pairs."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        try:
            from gitoma.core.config import load_config
            cfg = load_config()
            lines = [
                f"**GitHub Token:** `{cfg.github.token[:6]}…` (set)" if cfg.github.token else "**GitHub Token:** ⚠ NOT SET",
                f"**Bot Name:** `{cfg.bot.name}`",
                f"**Bot Email:** `{cfg.bot.email}`",
                f"**LM Studio URL:** `{cfg.lmstudio.base_url}`",
                f"**LM Studio Model:** `{cfg.lmstudio.model}`",
                f"**Critic Model:** `{cfg.lmstudio.critic_model or '(same as main)'}`",
                f"**Temperature:** `{cfg.lmstudio.temperature}`",
                f"**Max Tokens:** `{cfg.lmstudio.max_tokens}`",
            ]
            content = "\n\n".join(lines)
        except Exception as e:
            content = f"⚠ Could not load config: {e}"

        with Container(id="modal-container"):
            yield Static("⚙  Gitoma Configuration", id="modal-title")
            with ScrollableContainer(id="telemetry-scroll"):
                yield Markdown(content)
            yield Static(
                "[dim]Edit: gitoma config set KEY=value[/dim]\n"
                "[dim]Config file: ~/.gitoma/config.toml[/dim]",
                id="modal-body",
            )
            with Horizontal(id="modal-buttons"):
                yield Button("✕  Close", id="close-btn")

    @on(Button.Pressed, "#close-btn")
    def _close(self) -> None:
        self.dismiss(None)


# ── Widget: Repo List ─────────────────────────────────────────────────────────

class RepoListPanel(Vertical):
    """Left panel: navigable list of tracked repos."""

    selected_slug: reactive[Optional[str]] = reactive(None)

    def compose(self) -> ComposeResult:
        yield Static("◈  REPOS", id="repo-panel-title")
        yield ListView(id="repo-list")
        yield Button("＋  Add Repo", id="repo-add-btn")

    def refresh_repos(self, states: list[dict], select_slug: str | None = None) -> None:
        lv = self.query_one("#repo-list", ListView)
        lv.clear()
        if not states:
            lv.append(ListItem(Static("[dim]No active runs[/dim]")))
            return
        for s in states:
            slug = s.get("owner", "") + "/" + s.get("name", "")
            phase = s.get("phase", "IDLE")
            icon = _PHASE_ICONS.get(phase, "○")
            phase_color = {
                "DONE": "green",
                "PR_OPEN": "cyan",
                "WORKING": "yellow",
                "ANALYZING": "yellow",
                "PLANNING": "yellow",
                "IDLE": "dim",
            }.get(phase, "dim")
            lv.append(
                ListItem(
                    Static(f" {icon} [{phase_color}]{slug}[/{phase_color}]"),
                )
            )

    @on(ListView.Selected, "#repo-list")
    def _selected(self, event: ListView.Selected) -> None:
        # Bubble up to app via message
        self.post_message(self.RepoSelected(event.item.index))

    class RepoSelected:
        def __init__(self, index: int) -> None:
            self.index = index


# ── Widget: Pipeline Status ───────────────────────────────────────────────────

class PipelinePanel(Vertical):
    """Center panel top: animated pipeline stepper."""

    def compose(self) -> ComposeResult:
        with Container(id="pipeline-container"):
            yield Static("⬡  PIPELINE", id="pipeline-title")
            for phase_key, _ in _PHASES[1:]:  # skip IDLE
                yield Static(
                    f"  ○  {phase_key}",
                    id=f"step-{phase_key}",
                    classes="pipeline-step step-pending",
                )
            yield ProgressBar(total=100, show_eta=False, id="pipeline-progress-bar")

    def update_phase(self, phase: str, pct: int = 0) -> None:
        """Highlight the active phase and grey out past/future ones."""
        phase_order = [p[0] for p in _PHASES]
        try:
            current_idx = phase_order.index(phase)
        except ValueError:
            current_idx = 0

        for i, (key, _) in enumerate(_PHASES[1:], start=1):
            try:
                widget = self.query_one(f"#step-{key}", Static)
            except Exception:
                continue

            if i < current_idx:
                widget.update(f"  ✓  {key}")
                widget.set_classes("pipeline-step step-done")
            elif i == current_idx:
                spinner_frames = ["⟳", "↻", "↺", "⟲"]
                icon = spinner_frames[int(datetime.now().second / 2) % 4] if phase not in ("DONE", "PR_OPEN") else "✅"
                widget.update(f"  {icon}  {key}")
                widget.set_classes("pipeline-step step-active")
            else:
                widget.update(f"  ○  {key}")
                widget.set_classes("pipeline-step step-pending")

        try:
            pb = self.query_one("#pipeline-progress-bar", ProgressBar)
            pb.progress = pct
        except Exception:
            pass


# ── Widget: Metrics ───────────────────────────────────────────────────────────

class MetricsPanel(Vertical):
    """Center panel middle: health metrics with progress bars."""

    _METRIC_DISPLAY_NAMES: ClassVar[dict[str, str]] = {
        "readme": "README",
        "ci_cd": "CI/CD",
        "code_quality": "Code Quality",
        "security": "Security",
        "license": "License",
        "contributing": "Contributing",
        "tests": "Tests",
        "documentation": "Docs",
        "gitignore": ".gitignore",
        "changelog": "Changelog",
        "topics": "Topics",
        "description": "Description",
    }

    def compose(self) -> ComposeResult:
        with Container(id="metrics-container"):
            yield Static("📊  REPO HEALTH", id="metrics-title")
            # Placeholder rows — updated dynamically
            for key in ["Code Quality", "CI/CD", "Docs", "Security", "Tests"]:
                with Horizontal(classes="metric-row"):
                    yield Static(key[:14], classes="metric-label")
                    yield ProgressBar(total=100, show_eta=False, classes="metric-bar")
                    yield Static("—%", classes="metric-score")

    def update_metrics(self, metric_report: Optional[dict]) -> None:
        if not metric_report:
            return
        metrics = metric_report.get("metrics", [])
        if not metrics:
            return

        # Sort and pick top-5 by score (ascending → worst first)
        sorted_m = sorted(metrics, key=lambda x: x.get("score", 0))[:5]

        rows = self.query(".metric-row")
        for i, row in enumerate(rows):
            if i >= len(sorted_m):
                break
            m = sorted_m[i]
            name = self._METRIC_DISPLAY_NAMES.get(m.get("key", ""), m.get("display_name", "?"))
            score_pct = int(m.get("score", 0) * 100)
            status = m.get("status", "pass")

            score_color = {"pass": "green", "warn": "yellow", "fail": "red"}.get(status, "white")

            try:
                row.query_one(".metric-label", Static).update(name[:14])
                pb = row.query_one(".metric-bar", ProgressBar)
                pb.progress = score_pct
                row.query_one(".metric-score", Static).update(
                    f"[{score_color}]{score_pct}%[/{score_color}]"
                )
            except Exception:
                pass


# ── Widget: Agent Info ────────────────────────────────────────────────────────

class AgentInfoPanel(Vertical):
    """Center panel bottom: branch, model, task progress."""

    def compose(self) -> ComposeResult:
        with Container(id="agent-info"):
            yield Static("🤖  AGENT INFO", id="agent-info-title")
            with Horizontal(classes="info-row"):
                yield Static("Branch", classes="info-key")
                yield Static("—", id="info-branch", classes="info-val")
            with Horizontal(classes="info-row"):
                yield Static("Model", classes="info-key")
                yield Static("—", id="info-model", classes="info-val")
            with Horizontal(classes="info-row"):
                yield Static("Tasks", classes="info-key")
                yield Static("—", id="info-tasks", classes="info-val")
            with Horizontal(classes="info-row"):
                yield Static("Subtasks", classes="info-key")
                yield Static("—", id="info-subtasks", classes="info-val")
            with Horizontal(classes="info-row"):
                yield Static("PR", classes="info-key")
                yield Static("—", id="info-pr", classes="info-val")
            with Horizontal(classes="info-row"):
                yield Static("Updated", classes="info-key")
                yield Static("—", id="info-updated", classes="info-val")

    def update_info(self, state: Optional[dict]) -> None:
        if not state:
            for wid in ["info-branch", "info-model", "info-tasks", "info-subtasks", "info-pr", "info-updated"]:
                try:
                    self.query_one(f"#{wid}", Static).update("—")
                except Exception:
                    pass
            return

        try:
            from gitoma.core.config import load_config
            cfg = load_config()
            model = cfg.lmstudio.model
        except Exception:
            model = "?"

        branch = state.get("branch", "—")
        pr_url = state.get("pr_url") or "—"
        updated = state.get("updated_at", "—")
        if updated != "—":
            updated = updated[11:19]  # HH:MM:SS

        task_plan = state.get("task_plan")
        tasks_str = "—"
        subtasks_str = "—"
        if task_plan:
            tasks = task_plan.get("tasks", [])
            total_t = len(tasks)
            done_t = sum(1 for t in tasks if t.get("status") == "completed")
            subtasks = [s for t in tasks for s in t.get("subtasks", [])]
            total_s = len(subtasks)
            done_s = sum(1 for s in subtasks if s.get("status") == "completed")
            tasks_str = f"[green]{done_t}[/green]/[bold]{total_t}[/bold]"
            subtasks_str = f"[green]{done_s}[/green]/[bold]{total_s}[/bold]"

        info = {
            "info-branch": f"[cyan]{branch}[/cyan]",
            "info-model": f"[violet]{model}[/violet]",
            "info-tasks": tasks_str,
            "info-subtasks": subtasks_str,
            "info-pr": f"[underline cyan]{pr_url}[/underline cyan]" if pr_url != "—" else "—",
            "info-updated": f"[dim]{updated}[/dim]",
        }
        for wid, val in info.items():
            try:
                self.query_one(f"#{wid}", Static).update(val)
            except Exception:
                pass


# ── Widget: Event Feed ─────────────────────────────────────────────────────────

class EventFeed(ScrollableContainer):
    """Right panel bottom: recent agent events."""

    MAX_EVENTS: ClassVar[int] = 50
    _events: list[str]

    def __init__(self) -> None:
        super().__init__(id="event-feed")
        self._events = []

    def compose(self) -> ComposeResult:
        yield Static("", id="event-content")

    def push_event(self, icon: str, message: str, color: str = "cyan") -> None:
        ts = _ts()
        entry = f"[dim]{ts}[/dim] {icon}  [{color}]{message}[/{color}]"
        self._events.append(entry)
        if len(self._events) > self.MAX_EVENTS:
            self._events = self._events[-self.MAX_EVENTS :]
        try:
            self.query_one("#event-content", Static).update("\n".join(self._events[-12:]))
        except Exception:
            pass
        self.scroll_end(animate=False)


# ── Main App ───────────────────────────────────────────────────────────────────

class GitomaTUI(App):
    """Gitoma — AI Agent Cockpit TUI."""

    CSS_PATH = Path(__file__).parent / "tui_styles.tcss"

    BINDINGS = [
        Binding("r", "run_agent", "Run", show=True),
        Binding("d", "doctor", "Doctor", show=True),
        Binding("f", "fix_ci", "Fix-CI", show=True),
        Binding("t", "telemetry", "Telemetry", show=True),
        Binding("c", "show_config", "Config", show=True),
        Binding("s", "stop_agent", "Stop", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    # Reactive: currently selected state index
    _selected_idx: reactive[int] = reactive(0)
    _states: list[dict]
    _subprocess: Optional[subprocess.Popen]  # type: ignore[type-arg]

    def __init__(self) -> None:
        super().__init__()
        self._states = []
        self._subprocess = None

    # ── Compose layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # ── Header ──────────────────────────────────────────────────────────
        with Container(id="header"):
            yield Static(
                f"◈  [bold #C084FC]GITOMA[/bold #C084FC] [dim]v{_VERSION}[/dim]",
                id="header-logo",
            )
            yield Static(
                "AI-Powered Autonomous GitHub Agent",
                id="header-status",
            )
            yield Static(
                "[dim][Q]uit  [H]elp[/dim]",
                id="header-hints",
            )

        # ── Main 3-column grid ───────────────────────────────────────────────
        with Container(id="main-grid"):
            # Left — repo navigator
            with Vertical(id="left-panel"):
                yield RepoListPanel()

            # Center — pipeline + metrics + info
            with Vertical(id="center-panel"):
                yield PipelinePanel()
                yield MetricsPanel()
                yield AgentInfoPanel()

            # Right — log + events
            with Vertical(id="right-panel"):
                yield Static("●  LIVE LOG", id="log-title")
                yield Log(id="live-log", highlight=True, max_lines=500)
                yield Static("⚡  EVENTS", id="events-title")
                yield EventFeed()

        # ── Footer ───────────────────────────────────────────────────────────
        with Container(id="footer"):
            for key, label in [
                ("R", "Run"),
                ("D", "Doctor"),
                ("F", "Fix-CI"),
                ("T", "Telemetry"),
                ("C", "Config"),
                ("S", "Stop"),
                ("Q", "Quit"),
            ]:
                yield Static(key, classes="footer-key")
                yield Static(label, classes="footer-desc")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._log("◈ Gitoma TUI started", "cyan")
        self._log(f"◦ Version {_VERSION} · Textual 8.x", "dim")
        self._log("◦ Press [R] to run agent, [D] for doctor check", "dim")
        self.set_interval(0.5, self._poll_states)
        self._poll_states()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_states(self) -> None:
        """Reload all states from disk and refresh UI."""
        new_states = _load_all_states()
        changed = json.dumps(new_states) != json.dumps(self._states)
        self._states = new_states

        # Refresh repo list
        try:
            repo_panel = self.query_one(RepoListPanel)
            repo_panel.refresh_repos(new_states)
        except Exception:
            pass

        # Update selected state view
        state = self._get_selected_state()
        self._refresh_center(state)

        # Log new phase changes
        if changed and state:
            phase = state.get("phase", "IDLE")
            self._push_event(_PHASE_ICONS.get(phase, "○"), f"Phase → {phase}", "yellow")

    def _get_selected_state(self) -> Optional[dict]:
        if not self._states:
            return None
        idx = min(self._selected_idx, len(self._states) - 1)
        return self._states[idx]

    def _refresh_center(self, state: Optional[dict]) -> None:
        phase = state.get("phase", "IDLE") if state else "IDLE"

        # Pipeline progress estimation
        phase_pct = {
            "IDLE": 0, "ANALYZING": 25, "PLANNING": 45,
            "WORKING": 70, "PR_OPEN": 90, "REVIEWING": 95, "DONE": 100,
        }
        pct = phase_pct.get(phase, 0)

        try:
            self.query_one(PipelinePanel).update_phase(phase, pct)
            self.query_one(MetricsPanel).update_metrics(state.get("metric_report") if state else None)
            self.query_one(AgentInfoPanel).update_info(state)
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_run_agent(self) -> None:
        """Open modal and launch a gitoma run in background."""
        self.push_screen(InputModal("Launch Agent Run", show_branch=False), self._do_run)

    def _do_run(self, result: Optional[Tuple[str, str]]) -> None:
        if not result:
            return
        url, _ = result
        self._log(f"▶ Launching agent run for [cyan]{url}[/cyan]", "yellow")
        self._push_event("🚀", f"Run started: {url}", "yellow")
        self._launch_subprocess(["gitoma", "run", url, "--yes"])

    def action_fix_ci(self) -> None:
        """Open modal and launch CI fix agent."""
        self.push_screen(InputModal("Fix CI Pipeline", show_branch=True), self._do_fix_ci)

    def _do_fix_ci(self, result: Optional[Tuple[str, str]]) -> None:
        if not result:
            return
        url, branch = result
        if not branch:
            self._log_warn("⚠  Branch is required for fix-ci")
            return
        self._log(f"🔧 Launching CI fix for [cyan]{url}[/cyan] @ [yellow]{branch}[/yellow]", "yellow")
        self._push_event("🔧", f"Fix-CI: {url}@{branch}", "yellow")
        self._launch_subprocess(["gitoma", "fix-ci", url, "--branch", branch])

    def action_doctor(self) -> None:
        """Run gitoma doctor and stream output to LiveLog."""
        self._log("🩺 Running doctor check…", "cyan")
        self._push_event("🩺", "Doctor check started", "cyan")
        self._launch_subprocess(["gitoma", "doctor"])

    def action_stop_agent(self) -> None:
        """Stop any running subprocess."""
        if self._subprocess and self._subprocess.poll() is None:
            self.push_screen(ConfirmModal("Stop the currently running agent?"), self._do_stop)
        else:
            self._log_warn("⚠  No agent is currently running")

    def _do_stop(self, confirmed: bool) -> None:
        if not confirmed:
            return
        if self._subprocess:
            self._subprocess.terminate()
            self._log("⛔ Agent process terminated", "red")
            self._push_event("⛔", "Agent stopped by user", "red")

    def action_telemetry(self) -> None:
        """Show latest Observer telemetry report."""
        content = _latest_telemetry()
        if not content:
            self._log_warn("⚠  No telemetry reports found in ~/.gitoma/telemetry/")
            return
        self.push_screen(TelemetryModal(content))

    def action_show_config(self) -> None:
        """Show config modal."""
        self.push_screen(ConfigModal())

    # ── Subprocess runner ─────────────────────────────────────────────────────

    @work(thread=True)
    def _launch_subprocess(self, cmd: list[str]) -> None:
        """Run a gitoma CLI command and stream output to the LiveLog."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._subprocess = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.call_from_thread(self._log, line, "dim")
            proc.wait()
            rc = proc.returncode
            if rc == 0:
                self.call_from_thread(self._log, f"✅ Command finished OK (rc={rc})", "green")
                self.call_from_thread(self._push_event, "✅", "Command completed", "green")
            else:
                self.call_from_thread(self._log, f"⚠  Command exited with rc={rc}", "red")
                self.call_from_thread(self._push_event, "⚠", f"Command failed (rc={rc})", "red")
        except FileNotFoundError:
            self.call_from_thread(
                self._log,
                "⚠  'gitoma' not found in PATH. Is the package installed?",
                "red",
            )
        except Exception as e:
            self.call_from_thread(self._log, f"⚠  Error: {e}", "red")
        finally:
            self._subprocess = None

    # ── Repo selection ────────────────────────────────────────────────────────

    @on(RepoListPanel.RepoSelected)
    def _on_repo_selected(self, event: RepoListPanel.RepoSelected) -> None:
        self._selected_idx = event.index
        self._refresh_center(self._get_selected_state())

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, message: str, color: str = "white") -> None:
        try:
            log = self.query_one("#live-log", Log)
            ts = _ts()
            log.write_line(f"[dim]{ts}[/dim] [{color}]{message}[/{color}]")
        except Exception:
            pass

    def _log_warn(self, message: str) -> None:
        self._log(message, "yellow")
        self.notify(message, severity="warning")

    def _push_event(self, icon: str, message: str, color: str = "cyan") -> None:
        try:
            self.query_one(EventFeed).push_event(icon, message, color)
        except Exception:
            pass


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def run_tui() -> None:
    """Launch the Gitoma TUI. Called from CLI."""
    app = GitomaTUI()
    app.run()


if __name__ == "__main__":
    run_tui()
