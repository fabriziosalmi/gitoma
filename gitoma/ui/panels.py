"""Rich panels, tables, and banners for Gitoma's terminal UI.

Industrial-grade pass:

* Emoji downgrade to ASCII via :func:`glyph` when the terminal can't render.
* Defensive dict access — a missing GitHub field no longer crashes the
  pretty-print.
* Compact banner by default (opt-in to the full ASCII art via env var).
* Phase labels drop inline emoji to keep Rich table column widths stable.
"""

from __future__ import annotations

from rich import box
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.tree import Tree
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gitoma.core.state import AgentState

from gitoma.analyzers.base import MetricReport, score_to_bar
from gitoma.planner.task import TaskPlan
from gitoma.ui.console import (
    BANNER_COMPACT,
    BANNER_FULL,
    BANNER_SUBTITLE,
    banner_mode,
    console,
    glyph,
)


def print_banner(version: str = "0.1.0") -> None:
    """Print the Gitoma startup banner — compact by default.

    Controlled by ``GITOMA_BANNER=full|compact|off``. ``off`` is the
    default on non-TTY stdout so piping remains machine-friendly.
    """
    mode = banner_mode()
    if mode == "off":
        return
    if mode == "full":
        console.print(BANNER_FULL)
        console.print(f"  {BANNER_SUBTITLE}   [muted]v{version}[/muted]")
    else:
        console.print(f"{BANNER_COMPACT}  [muted]v{version}[/muted]  [muted]·[/muted]  {BANNER_SUBTITLE}")
    console.print()


def print_repo_info(info: dict[str, Any]) -> None:
    """Print a repo metadata panel.

    Defensive against missing fields — a private repo, unusual license, or
    API-version tweak at GitHub's side shouldn't crash the pretty-print.
    """
    full_name = info.get("full_name") or "(unknown)"
    desc = info.get("description") or "No description"
    stars = info.get("stars", "—")
    forks = info.get("forks", "—")
    issues = info.get("open_issues", "—")
    language = info.get("language") or "—"
    branch = info.get("default_branch") or "—"

    items = [
        f"[heading]{glyph('📦', '>>')} {full_name}[/heading]",
        f"[muted]{desc}[/muted]",
        "",
        f"[secondary]{glyph('★', '*')} {stars}[/secondary]  "
        f"[muted]Forks: {forks}[/muted]  "
        f"[muted]Issues: {issues}[/muted]",
        f"[muted]Language: {language} | Branch: {branch}[/muted]",
    ]
    topics = info.get("topics") or []
    if topics:
        topics_str = "  ".join(f"[info]#{t}[/info]" for t in topics[:8])
        items.append(topics_str)

    console.print(
        Panel(
            "\n".join(items),
            title=f"[primary]{glyph('🔍', '>>')} Repository[/primary]",
            border_style="primary",
            expand=False,
        )
    )
    console.print()


def print_metric_report(report: MetricReport) -> None:
    """Render the metric report as a Rich table.

    Emoji dropped from the status cells (they messed up column-width math
    on ttys that don't measure emoji width correctly). Text fallback
    means table columns stay aligned everywhere.
    """
    table = Table(
        title=f"{glyph('📊', '[metrics]')} Repo Health — [url]{report.repo_url}[/url]",
        title_style="heading",
        box=box.ROUNDED,
        border_style="primary",
        show_header=True,
        header_style="secondary",
        expand=True,
    )
    table.add_column("Metric", style="heading", min_width=22)
    table.add_column("Score", justify="center", width=12)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Details", style="muted")

    status_map = {
        "pass": "[metric.pass]pass[/metric.pass]",
        "warn": "[metric.warn]warn[/metric.warn]",
        "fail": "[metric.fail]fail[/metric.fail]",
    }

    for m in sorted(report.metrics, key=lambda x: x.score):
        bar = score_to_bar(m.score)
        score_text = f"[metric.score]{bar}[/metric.score] [dim]{m.score:.0%}[/dim]"
        table.add_row(
            f"[bold]{m.display_name}[/bold]",
            score_text,
            status_map.get(m.status, m.status),
            (m.details or "")[:70],
        )

    table.add_section()
    overall = report.overall_score
    overall_bar = score_to_bar(overall)
    table.add_row(
        "[bold heading]OVERALL[/bold heading]",
        f"[bold metric.score]{overall_bar}[/bold metric.score] [bold]{overall:.0%}[/bold]",
        "",
        f"[muted]{len(report.failing)} failing · {len(report.warning)} warning · "
        f"{len(report.passing)} passing[/muted]",
        style="bold",
    )

    console.print(table)
    console.print()


def print_task_plan(plan: TaskPlan) -> None:
    """Render the task plan as a Rich tree."""
    tree = Tree(
        f"[primary]{glyph('📋', '>>')} Task Plan[/primary] "
        f"[muted]({plan.total_tasks} tasks · {plan.total_subtasks} subtasks)[/muted]",
        guide_style="dim",
    )

    for task in plan.tasks:
        task_label = (
            f"[task.pending]P{task.priority}[/task.pending] "
            f"[bold]{task.id}[/bold] — [heading]{task.title}[/heading] "
            f"[dim](metric: {task.metric})[/dim]"
        )
        task_node = tree.add(task_label)
        if task.description:
            task_node.add(f"[muted]{task.description[:100]}[/muted]")
        for sub in task.subtasks:
            action_colors = {
                "create": f"[success]{glyph('➕', '+ ')}create[/success]",
                "modify": f"[warning]{glyph('✏️', '~ ')} modify[/warning]",
                "delete": f"[danger]{glyph('🗑', '- ')} delete[/danger]",
                "verify": f"[info]{glyph('🔍', '? ')} verify[/info]",
            }
            action = action_colors.get(sub.action, sub.action)
            hints = ", ".join(sub.file_hints[:2]) if sub.file_hints else "—"
            task_node.add(
                f"{action} [bold]{sub.id}[/bold] — {sub.title} [dim][{hints}][/dim]"
            )

    console.print(tree)
    console.print()


def print_commit(sha: str, message: str, subtask_id: str) -> None:
    """Inline commit notification."""
    console.print(
        f"  [commit]{glyph('⚡', '>>')} COMMIT[/commit] [{subtask_id}] "
        f"[code]{sha[:7]}[/code] [dim]{message[:80]}[/dim]"
    )


def print_pr_panel(pr_url: str, pr_number: int, branch: str) -> None:
    """Celebratory PR panel."""
    console.print(
        Panel(
            f"[success]{glyph('🎉', '>>')} Pull Request #{pr_number} is LIVE![/success]\n\n"
            f"  [url]{pr_url}[/url]\n\n"
            f"  Branch: [commit]{branch}[/commit]\n\n"
            f"  [muted]Review it, merge when ready, or run:[/muted]\n"
            f"  [primary]gitoma review <repo-url>[/primary]  [muted]to see Copilot feedback[/muted]",
            title=f"[pr]{glyph('🚀', '>>')} PR Opened[/pr]",
            border_style="accent",
            padding=(1, 2),
        )
    )


def print_status_panel(state: "AgentState") -> None:  # noqa: F821
    """Display agent progress status."""
    from gitoma.core.state import AgentPhase

    # No more inline emoji glyphs in phase labels — they broke column
    # alignment on tables + screen readers repeated "gear" for every tick.
    phase_colors = {
        AgentPhase.IDLE:      "[muted]IDLE[/muted]",
        AgentPhase.ANALYZING: "[warning]ANALYZING[/warning]",
        AgentPhase.PLANNING:  "[info]PLANNING[/info]",
        AgentPhase.WORKING:   "[warning]WORKING[/warning]",
        AgentPhase.PR_OPEN:   "[success]PR OPEN[/success]",
        AgentPhase.REVIEWING: "[accent]REVIEWING[/accent]",
        AgentPhase.DONE:      f"[success]DONE {glyph('✅', '')}[/success]",
    }
    try:
        phase_label = phase_colors.get(AgentPhase(state.phase), str(state.phase))
    except ValueError:
        phase_label = str(state.phase)

    started = (state.started_at or "")[:19].replace("T", " ") or "—"
    updated = (state.updated_at or "")[:19].replace("T", " ") or "—"

    lines = [
        f"[heading]{state.owner}/{state.name}[/heading]",
        f"Phase: {phase_label}",
        f"Branch: [commit]{state.branch or '—'}[/commit]",
        f"Started: [muted]{started}[/muted]",
        f"Updated: [muted]{updated}[/muted]",
    ]

    if state.task_plan:
        plan = TaskPlan.from_dict(dict(state.task_plan))
        done_sub = sum(
            1 for t in plan.tasks for s in t.subtasks if s.status == "completed"
        )
        lines.append(
            f"Progress: [success]{plan.completed_tasks}[/success]/"
            f"[heading]{plan.total_tasks}[/heading] tasks "
            f"([success]{done_sub}[/success]/"
            f"[heading]{plan.total_subtasks}[/heading] subtasks)"
        )

    if state.pr_url:
        lines.append(f"PR: [url]{state.pr_url}[/url]")

    if state.errors:
        for err in state.errors[-3:]:
            lines.append(f"[danger]{glyph('⚠', '! ')} {err[:80]}[/danger]")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[primary]{glyph('🤖', '>>')} Gitoma Agent Status[/primary]",
            border_style="primary",
            expand=False,
        )
    )


def make_analyzer_progress() -> Progress:
    """Create a progress bar for the analyzer phase."""
    return Progress(
        SpinnerColumn(spinner_name="dots", style="primary"),
        TextColumn("[progress.description]{task.description}", style="heading"),
        BarColumn(bar_width=30, style="primary", complete_style="success"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%", style="secondary"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
