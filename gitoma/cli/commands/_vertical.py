"""Vertical CLI command factory — Castelletto Taglio A.

Single source of truth for the per-vertical CLI surface. Iterating
:data:`gitoma.verticals.VERTICALS` and calling
:func:`register_vertical_command` once per record produces a
``gitoma <name>`` Typer command for every registered vertical, all
with the same flag surface as ``gitoma run`` minus ``--no-auto-fix-ci``
(which the vertical's own ``no_auto_fix_ci`` field controls).

Adding a new vertical = drop a module under :mod:`gitoma.verticals`,
add the constant to :data:`VERTICALS` — the CLI command appears
automatically.
"""

from __future__ import annotations

import os
from typing import Annotated, Optional

import typer

from gitoma.cli._app import app
from gitoma.cli.commands.run import run as run_full_pipeline
from gitoma.verticals._base import Vertical


def register_vertical_command(vertical: Vertical) -> None:
    """Register ``gitoma <vertical.name>`` against the global app.

    The generated command is a thin wrapper that:

    1. sets ``GITOMA_SCOPE=<vertical.name>`` so
       :func:`gitoma.planner.scope_filter.active_scope` picks it up,
    2. calls the shared ``run`` pipeline with ``no_auto_fix_ci`` set
       from the vertical's declarative spec,
    3. restores the prior scope env on exit so multi-command CLI
       sessions (and tests) aren't permanently narrowed.

    The flag surface mirrors ``gitoma run`` except ``no_auto_fix_ci``,
    which is taken from the vertical's spec rather than a flag.
    """
    name = vertical.name
    summary = vertical.summary
    no_auto_fix_ci = vertical.no_auto_fix_ci

    @app.command(name=name, help=summary)
    def _vertical_cmd(  # noqa: PLR0913 — flag surface mirrors `run`
        repo_url: Annotated[
            str,
            typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)"),
        ],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", help="Analyze + plan only — no commits or PR"),
        ] = False,
        branch: Annotated[
            str,
            typer.Option("--branch", help="Branch name to create"),
        ] = "",
        base: Annotated[
            Optional[str],
            typer.Option("--base", help="Base branch for the PR"),
        ] = None,
        resume: Annotated[
            bool,
            typer.Option("--resume", help="Resume an existing agent run"),
        ] = False,
        reset_state: Annotated[
            bool,
            typer.Option("--reset", help="Delete existing state and start fresh"),
        ] = False,
        yes: Annotated[
            bool,
            typer.Option("--yes", "-y", help="Skip confirmation prompts"),
        ] = False,
        no_self_review: Annotated[
            bool,
            typer.Option(
                "--no-self-review",
                help="Skip the self-critic pass that posts a review comment on the PR",
            ),
        ] = False,
        no_ci_watch: Annotated[
            bool,
            typer.Option(
                "--no-ci-watch",
                help="Skip the CI-watch + auto fix-ci phases",
            ),
        ] = False,
    ) -> None:
        prior = os.environ.get("GITOMA_SCOPE")
        os.environ["GITOMA_SCOPE"] = name
        try:
            run_full_pipeline(
                repo_url=repo_url,
                dry_run=dry_run,
                branch=branch,
                base=base,
                resume=resume,
                reset_state=reset_state,
                yes=yes,
                no_self_review=no_self_review,
                no_ci_watch=no_ci_watch,
                no_auto_fix_ci=no_auto_fix_ci,
                skip_lm=False,
            )
        finally:
            if prior is None:
                os.environ.pop("GITOMA_SCOPE", None)
            else:
                os.environ["GITOMA_SCOPE"] = prior

    # Stash a reference for tests / introspection — Typer hides the
    # underlying callable behind its registry, so attaching it on the
    # module under a stable attribute name makes test access cheap.
    globals()[f"_cmd_{name}"] = _vertical_cmd


def register_all() -> None:
    """Register every vertical in the registry. Called from the
    commands package ``__init__`` so import-side-effects stay
    centralised."""
    # Imported here to avoid a circular import at module load time
    # (gitoma.verticals → gitoma.cli.commands._vertical via tests).
    from gitoma.verticals import VERTICALS
    for vertical in VERTICALS.values():
        register_vertical_command(vertical)
