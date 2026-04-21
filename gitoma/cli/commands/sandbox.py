"""gitoma sandbox command."""

from __future__ import annotations

from typing import Annotated, TYPE_CHECKING

import typer

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _check_config,
    _ok,
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

@app.command(name="sandbox")
def sandbox_cmd(
    action: Annotated[str, typer.Argument(help="Action: setup | teardown | run")],
) -> None:
    """
    🧪 Manage a Gitoma test repository.

    [bold]Examples:[/bold]
      gitoma sandbox setup
      gitoma sandbox run
      gitoma sandbox teardown
    """
    print_banner(__version__)
    config = _check_config(require_token=True)

    from gitoma.core.sandbox import setup_sandbox, teardown_sandbox

    if action == "setup":
        with _phase("Creating Sandbox Repository"):
            console.print("[muted]Clearing and scaffolding 'gitoma-sandbox' on GitHub...[/muted]")
            try:
                repo_url = setup_sandbox(config)
                _ok(f"Sandbox created: {repo_url}")
                console.print("\n[muted]Ready! Now run: [primary]gitoma sandbox run[/primary][/muted]")
            except Exception as e:
                _abort(f"Failed to setup sandbox: {e}")

    elif action == "teardown":
        with _phase("Tearing down Sandbox"):
            console.print("[muted]Deleting 'gitoma-sandbox'...[/muted]")
            try:
                teardown_sandbox(config)
                _ok("Sandbox repo deleted from GitHub.")
            except Exception as e:
                _abort(f"Failed to teardown sandbox: {e}")

    elif action == "run":
        # Launch the run command on the sandbox repo directly. Imported
        # lazily so we don't pay for worker/planner module load on every
        # sandbox setup/teardown invocation.
        from gitoma.cli.commands.run import run as run_command

        owner = config.bot.github_user
        repo_url = f"https://github.com/{owner}/gitoma-sandbox"
        console.print(f"[success]Launching Gitoma agent on {repo_url}...[/success]\n")

        try:
            run_command(
                repo_url=repo_url,
                dry_run=False,
                branch="",
                base=None,
                resume=True,
                reset_state=False,
                yes=True,
                skip_lm=False,
            )
        except Exception as e:
            if not isinstance(e, typer.Exit):
                _abort(f"Sandbox run failed: {e}")

    else:
        _abort(f"Unknown sandbox action: {action}. Use setup, run, or teardown.")
