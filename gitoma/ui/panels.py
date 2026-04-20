"""Rich panels, tables, and banners for Gitoma's terminal UI."""

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
from gitoma.ui.console import BANNER, BANNER_SUBTITLE, console


def print_banner(version: str = "0.1.0") -> None:
    """Print the Gitoma startup banner."""
    console.print(BANNER)
    console.print(f"  {BANNER_SUBTITLE}   [muted]v{version}[/muted]")
    console.print()


def print_repo_info(info: dict[str, Any]) -> None:
    """Print a repo metadata panel."""
    items = [
        f"[heading]📦 {info['full_name']}[/heading]",
        f"[muted]{info.get('description', 'No description')}[/muted]",
        "",
        f"[secondary]★ {info['stars']}[/secondary]  "
        f"[muted]Forks: {info['forks']}[/muted]  "
        f"[muted]Issues: {info['open_issues']}[/muted]",
        f"[muted]Language: {info['language']} | Branch: {info['default_branch']}[/muted]",
    ]
    if info.get("topics"):
        topics_str = "  ".join(f"[info]#{t}[/info]" for t in info["topics"][:8])
        items.append(topics_str)

    console.print(
        Panel(
            "\n".join(items),
            title="[primary]🔍 Repository[/primary]",
            border_style="primary",
            expand=False,
        )
    )
    console.print()


def print_metric_report(report: MetricReport) -> None:
    """Render the metric report as a Rich table."""
    table = Table(
        title=f"📊 Repo Health — [url]{report.repo_url}[/url]",
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
        "pass": "[metric.pass]✅ pass[/metric.pass]",
        "warn": "[metric.warn]⚠️  warn[/metric.warn]",
        "fail": "[metric.fail]❌ fail[/metric.fail]",
    }

    for m in sorted(report.metrics, key=lambda x: x.score):
        bar = score_to_bar(m.score)
        score_text = f"[metric.score]{bar}[/metric.score] [dim]{m.score:.0%}[/dim]"
        table.add_row(
            f"[bold]{m.display_name}[/bold]",
            score_text,
            status_map.get(m.status, m.status),
            m.details[:70],
        )

    # Overall score row
    table.add_section()
    overall = report.overall_score
    overall_bar = score_to_bar(overall)
    table.add_row(
        "[bold heading]OVERALL[/bold heading]",
        f"[bold metric.score]{overall_bar}[/bold metric.score] [bold]{overall:.0%}[/bold]",
        "",
        f"[muted]{len(report.failing)} failing • {len(report.warning)} warning • "
        f"{len(report.passing)} passing[/muted]",
        style="bold",
    )

    console.print(table)
    console.print()


def print_task_plan(plan: TaskPlan) -> None:
    """Render the task plan as a Rich tree."""
    tree = Tree(
        f"[primary]📋 Task Plan[/primary] "
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
                "create": "[success]➕ create[/success]",
                "modify": "[warning]✏️  modify[/warning]",
                "delete": "[danger]🗑  delete[/danger]",
                "verify": "[info]🔍 verify[/info]",
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
        f"  [commit]⚡ COMMIT[/commit] [{subtask_id}] "
        f"[code]{sha[:7]}[/code] [dim]{message[:80]}[/dim]"
    )


def print_pr_panel(pr_url: str, pr_number: int, branch: str) -> None:
    """Celebratory PR panel."""
    console.print(
        Panel(
            f"[success]🎉 Pull Request #{pr_number} is LIVE![/success]\n\n"
            f"  [url]{pr_url}[/url]\n\n"
            f"  Branch: [commit]{branch}[/commit]\n\n"
            f"  [muted]Review it, merge when ready, or run:[/muted]\n"
            f"  [primary]gitoma review <repo-url>[/primary]  [muted]to see Copilot feedback[/muted]",
            title="[pr]🚀 PR Opened[/pr]",
            border_style="accent",
            padding=(1, 2),
        )
    )


def print_status_panel(state: "AgentState") -> None:  # noqa: F821
    """Display agent progress status."""
    from gitoma.core.state import AgentPhase

    phase_colors = {
        AgentPhase.IDLE: "[muted]IDLE[/muted]",
        AgentPhase.ANALYZING: "[warning]ANALYZING[/warning]",
        AgentPhase.PLANNING: "[info]PLANNING[/info]",
        AgentPhase.WORKING: "[warning]⚙️  WORKING[/warning]",
        AgentPhase.PR_OPEN: "[success]PR OPEN[/success]",
        AgentPhase.REVIEWING: "[accent]REVIEWING[/accent]",
        AgentPhase.DONE: "[success]DONE ✅[/success]",
    }

    phase_label = phase_colors.get(AgentPhase(state.phase), str(state.phase))

    lines = [
        f"[heading]{state.owner}/{state.name}[/heading]",
        f"Phase: {phase_label}",
        f"Branch: [commit]{state.branch}[/commit]",
        f"Started: [muted]{state.started_at[:19].replace('T', ' ')}[/muted]",
        f"Updated: [muted]{state.updated_at[:19].replace('T', ' ')}[/muted]",
    ]

    if state.task_plan:
        from gitoma.planner.task import TaskPlan
        plan = TaskPlan.from_dict(dict(state.task_plan))
        lines.append(
            f"Progress: [success]{plan.completed_tasks}[/success]/"
            f"[heading]{plan.total_tasks}[/heading] tasks "
            f"([success]{sum(s.status=='completed' for t in plan.tasks for s in t.subtasks)}[/success]/"
            f"[heading]{plan.total_subtasks}[/heading] subtasks)"
        )

    if state.pr_url:
        lines.append(f"PR: [url]{state.pr_url}[/url]")

    if state.errors:
        for err in state.errors[-3:]:
            lines.append(f"[danger]⚠ {err[:80]}[/danger]")

    console.print(
        Panel(
            "\n".join(lines),
            title="[primary]🤖 Gitoma Agent Status[/primary]",
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
