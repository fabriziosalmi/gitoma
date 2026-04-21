"""gitoma analyze command."""

from __future__ import annotations

from typing import Annotated, TYPE_CHECKING

import typer

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _check_config,
    _check_github,
    _clone_repo,
    _safe_cleanup,
)
from gitoma.core.repo import parse_repo_url
from gitoma.ui.console import console
from gitoma.ui.panels import (
    make_analyzer_progress,
    print_banner,
    print_metric_report,
    print_repo_info,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def analyze(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL")],
) -> None:
    """
    🔬 Analyze a repository and display health metrics.

    Safe read-only — no commits, no PR, no LLM calls.
    """
    print_banner(__version__)

    config = _check_config()
    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as e:
        _abort(f"Invalid repo URL: {e}")

    repo_info = _check_github(config, owner, name)
    print_repo_info(repo_info)

    git_repo = _clone_repo(repo_url, config)
    languages = git_repo.detect_languages() or ["Unknown"]
    console.print(f"[muted]Languages: {', '.join(languages)}[/muted]\n")

    from gitoma.analyzers.registry import AnalyzerRegistry, ALL_ANALYZER_CLASSES

    registry = AnalyzerRegistry(
        root=git_repo.root,
        languages=languages,
        repo_url=repo_url,
        owner=owner,
        name=name,
        default_branch=repo_info["default_branch"],
    )

    with make_analyzer_progress() as progress:
        task_id = progress.add_task("[heading]Analyzing…[/heading]", total=len(ALL_ANALYZER_CLASSES))

        def on_progress(a_name: str, idx: int, total: int) -> None:
            progress.update(task_id, description=f"[heading]{a_name}[/heading]", advance=1)

        report = registry.run(on_progress=on_progress)

    _safe_cleanup(git_repo)
    print_metric_report(report)

    # Actionable summary
    if report.failing:
        n = len(report.failing)
        console.print(
            f"\n[muted]{n} metric(s) failing. Fix them with:[/muted]\n"
            f"  [primary]gitoma run {repo_url}[/primary]"
        )
    elif report.warning:
        n = len(report.warning)
        console.print(
            f"\n[muted]{n} metric(s) need attention. Run:[/muted]\n"
            f"  [primary]gitoma run {repo_url}[/primary]"
        )
    else:
        console.print("\n[success]✅ All metrics pass![/success]")
