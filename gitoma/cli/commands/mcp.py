"""gitoma mcp command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _check_config,
)
from gitoma.ui.console import console

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="mcp")
def mcp_cmd() -> None:
    """
    🔗  Run the Gitoma GitHub MCP server on stdio.

    Exposes read_github_file, list_repo_tree, get_ci_failures and other GitHub
    context tools to any MCP-capable client (Claude Desktop, MCP Inspector, ...).
    """
    try:
        from gitoma.mcp.server import get_mcp_server
    except ImportError as exc:
        console.print(
            f"[danger]MCP server unavailable: {exc}[/danger]\n"
            "[muted]Install it with: [primary]pip install 'mcp[cli]>=1.0'[/primary][/muted]"
        )
        raise typer.Exit(1)

    _check_config()
    console.print("[info]🔗 Gitoma GitHub MCP server running on stdio[/info]")
    console.print("[muted]  Ctrl-C to stop.[/muted]")
    try:
        get_mcp_server().run()
    except KeyboardInterrupt:
        console.print("\n[muted]MCP server stopped.[/muted]")
