"""gitoma review command."""

from __future__ import annotations

from typing import Annotated, Optional, TYPE_CHECKING

import typer
from rich.rule import Rule

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _check_config,
    _check_lmstudio,
    _clone_repo,
    _ok,
    _safe_cleanup,
)
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import (
    AgentPhase,
    load_state,
    save_state,
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

@app.command()
def review(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL")],
    integrate: Annotated[bool, typer.Option("--integrate", help="Auto-fix Copilot comments and push")] = False,
    pr_number: Annotated[Optional[int], typer.Option("--pr", help="PR number (auto-detected from state)")] = None,
) -> None:
    """
    🔍 Show Copilot/reviewer comments on the agent's open PR.

    Use [primary]--integrate[/primary] to auto-fix all comments and push the fixes.
    """
    print_banner(__version__)

    config = _check_config()
    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as e:
        _abort(f"Invalid repo URL: {e}")

    state = load_state(owner, name)

    # Resolve PR number
    if not pr_number:
        if not state or not state.pr_number:
            _abort(
                "No PR number found in local state",
                hint=(
                    "Pass [primary]--pr <number>[/primary] explicitly, "
                    "or run [primary]gitoma run[/primary] first to create a PR."
                ),
            )
        pr_number = state.pr_number

    pr_url = (
        state.pr_url
        if (state and state.pr_url)
        else f"https://github.com/{owner}/{name}/pull/{pr_number}"
    )

    console.print(f"[muted]Fetching review status for PR #{pr_number}…[/muted]\n")

    gh = GitHubClient(config)
    from gitoma.review.watcher import CopilotWatcher
    from gitoma.review.reporter import display_review_status

    watcher = CopilotWatcher(gh, owner, name)

    try:
        review_status = watcher.fetch(pr_number, pr_url)
    except Exception as e:
        _abort(
            f"Could not fetch PR #{pr_number}: {e}",
            hint="Check the PR number and your token's pull-requests:read permission.",
        )

    display_review_status(review_status)

    if not integrate:
        return

    # ── Integrate mode ──────────────────────────────────────────────────────
    if not review_status.all_comments:
        console.print("[muted]No comments to integrate yet.[/muted]")
        return

    if not state:
        _abort(
            "Cannot integrate without local state (we need the branch name)",
            hint="Pass [primary]--pr[/primary] if you lost state, but the branch name must also be known.",
        )

    console.print(Rule("[primary]INTEGRATING REVIEW COMMENTS[/primary]", style="primary"))

    # Validate PR state on GitHub BEFORE cloning. A merged/closed PR means
    # its branch is often already auto-deleted (GitHub's default post-merge
    # behaviour), and even when the branch survives, pushing new fixes onto
    # a finalised PR is wrong — the maintainer moved past it. Mirror of the
    # ``pr_finalised`` guard in ``gitoma run`` but at the review entry point,
    # since ``review --integrate`` is the other code path that relies on a
    # mutable agent branch.
    try:
        from github import GithubException
        gh_pr = gh.get_pr(owner, name, pr_number)
        if gh_pr.merged:
            console.print(
                f"\n[info]PR #{pr_number} was merged on GitHub — "
                "nothing to integrate (branch is finalised).[/info]"
            )
            if state:
                state.current_operation = f"PR #{pr_number} merged — review integration skipped"
                state.advance(AgentPhase.DONE)
                save_state(state)
            return
        if gh_pr.state == "closed":
            console.print(
                f"\n[info]PR #{pr_number} is closed on GitHub — "
                "nothing to integrate.[/info]"
            )
            if state:
                state.current_operation = f"PR #{pr_number} closed — review integration skipped"
                state.advance(AgentPhase.DONE)
                save_state(state)
            return
    except GithubException as exc:
        if getattr(exc, "status", None) == 404:
            _abort(
                f"PR #{pr_number} not found on GitHub",
                hint=(
                    "The PR or its repository may have been deleted. "
                    "Run [primary]gitoma status --remote[/primary] to verify, "
                    "or pass a different [primary]--pr[/primary] number."
                ),
            )
        # Other API errors — warn and continue. Worst case the checkout
        # below still fails with its own clearer diagnostic.
        console.print(
            f"[warning]⚠ Could not verify PR #{pr_number} state "
            f"({exc}); attempting integration anyway.[/warning]"
        )
    except Exception as exc:
        console.print(
            f"[warning]⚠ Unexpected error verifying PR #{pr_number}: "
            f"{exc}; attempting integration anyway.[/warning]"
        )

    # LM Studio check before integrating
    llm = _check_lmstudio(config)

    console.print(f"[muted]Cloning repo to apply fixes on branch {state.branch}…[/muted]")
    git_repo = _clone_repo(repo_url, config)

    # Checkout agent branch. At this point the PR is open on GitHub per
    # the pre-flight above, so a pathspec failure here is the narrow
    # case: PR is open but its head branch was deleted out from under it
    # (manual delete, force-pushed to a different ref, …). Sharpen the
    # hint for that specific shape.
    try:
        git_repo.repo.git.checkout(state.branch)
        _ok(f"Checked out branch: {state.branch}")
    except Exception as e:
        _safe_cleanup(git_repo)
        _abort(
            f"Could not checkout branch '{state.branch}': {e}",
            hint=(
                f"PR #{pr_number} is open on GitHub but its branch "
                f"'{state.branch}' no longer exists on the remote. "
                "Someone likely deleted it. Options: restore the branch "
                "from the PR's commit history, or run "
                "[primary]gitoma run --reset[/primary] to start fresh."
            ),
        )

    from gitoma.review.integrator import ReviewIntegrator

    integrator = ReviewIntegrator(llm=llm, git_repo=git_repo, config=config, state=state)

    # Advance to REVIEWING *before* the LLM loop so the cockpit shows the
    # correct phase for the entire duration of the work — not for a
    # <500 ms flash after the fact. The previous order set REVIEWING
    # only after push, right before DONE, making the phase effectively
    # invisible to any cockpit that sampled the WS outside that window.
    state.current_operation = (
        f"Reviewing PR #{pr_number} — integrating {len(review_status.all_comments)} comment(s)"
    )
    state.advance(AgentPhase.REVIEWING)
    save_state(state)

    from gitoma.core.github_client import ReviewComment

    def on_comment_start(c: ReviewComment) -> None:
        console.print(f"  [info]◌ Comment #{c.id} by [bold]{c.author}[/bold]…[/info]")
        if c.path:
            console.print(f"    [dim]File: {c.path}" + (f":{c.line}" if c.line else "") + "[/dim]")
        console.print(f"    [dim]{c.body[:100].strip()}…[/dim]" if len(c.body) > 100 else f"    [dim]{c.body.strip()}[/dim]")

    def on_comment_done(c: ReviewComment, sha: str | None) -> None:
        if sha:
            console.print(f"  [commit]⚡ Fixed → commit {sha[:7]}[/commit]")
        else:
            console.print(f"  [warning]◎ Comment #{c.id} — no file changes needed[/warning]")

    def on_comment_error(c: ReviewComment, err: str) -> None:
        console.print(f"  [danger]✗ Comment #{c.id} failed: {err[:120]}[/danger]")

    results = integrator.integrate(
        review_status.all_comments,
        on_comment_start=on_comment_start,
        on_comment_done=on_comment_done,
        on_comment_error=on_comment_error,
    )

    fixed = sum(1 for r in results if r["sha"])
    failed = sum(1 for r in results if r["error"])

    console.print(f"\n[muted]Results: [success]{fixed} fixed[/success] · [danger]{failed} failed[/danger][/muted]")

    if fixed > 0:
        console.print(f"\n[muted]Pushing fixes to {state.branch}…[/muted]")
        try:
            git_repo.push(state.branch)
            _ok(f"Pushed review fixes to {state.branch}")
        except Exception as e:
            _safe_cleanup(git_repo)
            _abort(
                f"Push failed: {e}",
                hint="Check token permissions (contents:write) and that the branch still exists.",
            )

        console.print(
            f"\n[muted]PR updated: [url]{pr_url}[/url][/muted]"
        )
    else:
        console.print("[warning]⚠ No fixes pushed (nothing committed).[/warning]")

    _safe_cleanup(git_repo)

    # Terminal advance: ``gitoma review`` has finished. State must move
    # to DONE so the cockpit's Pipeline strip lights up the final step
    # and the orphan detector stops considering this run "in flight".
    state.current_operation = (
        f"Review integration complete — {fixed} fix(es) pushed"
        if fixed > 0
        else "Review integration complete — no fixes needed"
    )
    state.advance(AgentPhase.DONE)
    save_state(state)
