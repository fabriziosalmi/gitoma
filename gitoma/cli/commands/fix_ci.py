"""gitoma fix_ci command."""

from __future__ import annotations

from typing import Annotated, TYPE_CHECKING

import typer

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _check_config,
    _phase,
)
from gitoma.ui.console import console
from gitoma.ui.panels import (
    print_banner,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="fix-ci")
def fix_ci(
    repo_url: Annotated[str, typer.Argument(help="Repository URL")],
    branch: Annotated[str, typer.Option(help="Branch to analyze for CI failures")] = "main",
) -> None:
    """
    🛠  Auto-remediate CI/CD failures using the Reflexion Agent.
    """
    from gitoma.review.reflexion import CIDiagnosticAgent
    
    print_banner(__version__)
    config = _check_config(require_token=True)
    
    with _phase("CI Reflexion & Remediation"):
        agent = CIDiagnosticAgent(config)
        agent.analyze_and_fix(repo_url, branch)
        console.print("[success]CI Diagnostic Complete![/success]")
