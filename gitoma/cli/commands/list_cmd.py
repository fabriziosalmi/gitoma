"""gitoma list command."""

from __future__ import annotations

from typing import TYPE_CHECKING


from gitoma import __version__
from gitoma.cli._app import app
from gitoma.core.state import (
    list_all_states,
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

@app.command(name="list")
def list_cmd() -> None:
    """
    📋 List all active agent runs across all repos.
    """
    print_banner(__version__)
    states = list_all_states()
    if not states:
        console.print(
            "[muted]No active runs.\n"
            "Start one with: [primary]gitoma run <url>[/primary][/muted]"
        )
        return
    console.print(f"[heading]Active agent runs ({len(states)}):[/heading]\n")
    for s in states:
        print_status_panel(s)
        console.print()
