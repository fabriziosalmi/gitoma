"""gitoma reset command."""

from __future__ import annotations

from typing import Annotated, TYPE_CHECKING

import typer

from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _ok,
)
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import (
    delete_state,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def reset(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL to reset state for")],
) -> None:
    """
    🗑  Delete the saved agent state for a repo (start fresh next run).
    """
    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as e:
        _abort(f"Invalid repo URL: {e}")

    delete_state(owner, name)
    _ok(f"State cleared for {owner}/{name}")
