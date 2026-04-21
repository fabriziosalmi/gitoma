"""gitoma status command."""

from __future__ import annotations

from typing import Annotated, Optional, TYPE_CHECKING

import typer

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _check_config,
    _warn,
)
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import (
    list_all_states,
    load_state,
)
from gitoma.ui.console import console
from gitoma.ui.panels import (
    print_banner,
    print_status_panel,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def status(
    repo_url: Annotated[
        Optional[str],
        typer.Argument(help="GitHub repo URL. Omit to list all tracked repos."),
    ] = None,
    remote: Annotated[bool, typer.Option("--remote", help="Also query GitHub for gitoma/* branches")] = False,
) -> None:
    """
    📊 Show agent progress for a repo (or all tracked repos).
    """
    print_banner(__version__)

    if repo_url:
        try:
            owner, name = parse_repo_url(repo_url)
        except ValueError as e:
            _abort(f"Invalid repo URL: {e}")

        state = load_state(owner, name)
        if not state:
            console.print(f"[muted]No local agent state for {owner}/{name}.[/muted]")
        else:
            print_status_panel(state)

        if remote:
            config = _check_config(require_token=True)
            gh = GitHubClient(config)
            try:
                branches = gh.gitoma_branches(owner, name)
                if branches:
                    console.print("\n[secondary]Remote gitoma/* branches:[/secondary]")
                    for b in branches:
                        # Try to find matching state
                        state_match = (state and state.branch == b)
                        suffix = " [dim](this run)[/dim]" if state_match else ""
                        console.print(f"  [commit]{b}[/commit]{suffix}")
                else:
                    console.print("[muted]No gitoma/* branches found on GitHub.[/muted]")
            except Exception as e:
                _warn(f"Could not query remote branches: {e}")
    else:
        states = list_all_states()
        if not states:
            console.print(
                "[muted]No active agent runs.\n"
                "Start one with: [primary]gitoma run <url>[/primary][/muted]"
            )
            return
        console.print(f"[heading]Active agent runs ({len(states)}):[/heading]\n")
        for s in states:
            print_status_panel(s)
            console.print()
