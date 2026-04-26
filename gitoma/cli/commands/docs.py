"""`gitoma docs` — first vertical, narrows the full pipeline to docs.

Same pre-flight, same audit, same planner LLM call, same worker
apply pipeline as `gitoma run` — but with two scope filters
activated by `GITOMA_SCOPE=docs`:

  1. **Audit-side filter**: drop every metric except Documentation
     and README from the report BEFORE the planner sees it. The
     planner can't propose tasks for failing Build/Test/Security
     metrics that aren't the docs vertical's job to fix.

  2. **Plan-side filter**: drop every subtask whose ``file_hints``
     contain any non-doc path. Stricter than Layer-B (README-only):
     a subtask hinting both a doc and a source file is OUT.

Doc allow-list is defined in `gitoma/planner/scope_filter.py` and
covers ``.md/.mdx/.rst/.txt/.adoc`` extensions, root-level project
meta files (``README``, ``CHANGELOG``, ``CONTRIBUTING``, etc.),
and anything under ``docs/``, ``doc/``, ``documentation/``,
``website/``.

Motivation: lws dry-run on 2026-04-26 produced 6 tasks across
Security/CodeQuality/TestSuite/CI/Docs/ProjectStructure metrics,
of which 3 were hallucinated (Security false-positive on template
placeholders; TestSuite proposing JS for a pure-Python repo;
Documentation proposing MkDocs to a repo already on Jekyll). A
docs-vertical run would have skipped 5 of 6 tasks at audit time
and narrowed the planner to exactly the concern the operator
wanted.

The full-pass `gitoma run` is unchanged — vertical mode is
strictly opt-in via this command (which sets the env var) or via
explicit ``GITOMA_SCOPE=docs`` in the shell.
"""

from __future__ import annotations

import os
from typing import Annotated, Optional

import typer

from gitoma.cli._app import app
from gitoma.cli.commands.run import run as run_full_pipeline


@app.command()
def docs(
    repo_url: Annotated[
        str,
        typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)"),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Analyze + plan only — no commits or PR"),
    ] = False,
    branch: Annotated[
        str, typer.Option("--branch", help="Branch name to create"),
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
    """
    Run gitoma narrowed to the docs vertical: only doc files,
    only Documentation + README metrics. Use this when you want
    a focused docs PR without the planner wandering into source
    or config edits.

    Always run [primary]gitoma doctor[/primary] first to verify all prerequisites.
    """
    # Activate scope BEFORE entering the shared run pipeline. The
    # filters in run.py read GITOMA_SCOPE on each access, so setting
    # it here is sufficient — no API surface change needed in run.py.
    prior = os.environ.get("GITOMA_SCOPE")
    os.environ["GITOMA_SCOPE"] = "docs"
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
            no_auto_fix_ci=True,  # docs vertical never touches CI; fix-ci moot.
            skip_lm=False,
        )
    finally:
        # Restore the prior scope env (or unset it) so subsequent
        # operations in the same Python process aren't permanently
        # narrowed. Most CLI invocations are one-shot processes so
        # this is cosmetic, but tests run multiple commands per
        # process and benefit from the restore.
        if prior is None:
            os.environ.pop("GITOMA_SCOPE", None)
        else:
            os.environ["GITOMA_SCOPE"] = prior
