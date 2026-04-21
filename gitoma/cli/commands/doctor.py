"""gitoma doctor command."""

from __future__ import annotations

from typing import Annotated, Optional, TYPE_CHECKING

import typer
from rich.panel import Panel
from rich.rule import Rule

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._doctor_helpers import _doctor_push, _doctor_runs
from gitoma.cli._helpers import (
    _ok,
    _warn,
)
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import parse_repo_url
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
def doctor(
    repo_url: Annotated[
        Optional[str],
        typer.Argument(help="Optional repo URL to also verify GitHub access"),
    ] = None,
    runs: Annotated[
        bool,
        typer.Option("--runs", help="Scan tracked runs for orphans (dead CLI processes)"),
    ] = False,
    push: Annotated[
        Optional[str],
        typer.Option(
            "--push",
            help="Diagnose why `git push` might fail for <url> (token type, scopes, "
            "repo visibility, collaborator role, branch protection)",
        ),
    ] = None,
) -> None:
    """
    🩺 Run a full pre-flight health check.

    Checks: config, LM Studio (connection + models + target model), GitHub token.
    With --runs: also scans ~/.gitoma/state for runs whose owning process is gone.
    With --push <url>: drills into why a push might be rejected for that repo.
    Always safe to run — no writes, no clones.
    """
    print_banner(__version__)
    console.print(Rule("[primary]🩺 Health Check[/primary]", style="primary"))

    if runs:
        _doctor_runs()
        return
    if push:
        _doctor_push(push)
        return

    all_ok = True

    # ── 1. Config ───────────────────────────────────────────────────────────
    console.print("\n[heading]① Configuration[/heading]")
    try:
        from gitoma.core.config import load_config, CONFIG_FILE
        cfg = load_config()
        _ok(f"Config loaded from {CONFIG_FILE}")
    except Exception as e:
        _warn(f"Config load error: {e}")
        all_ok = False
        cfg = None

    if cfg:
        errors = cfg.validate()
        if errors:
            for msg in errors:
                console.print(f"  [danger]✗ {msg}[/danger]")
            all_ok = False
        else:
            _ok(f"GitHub token set ({cfg.github.token[:4]}…{cfg.github.token[-4:] if len(cfg.github.token) > 8 else ''})")
            _ok(f"Bot identity: {cfg.bot.name} <{cfg.bot.email}>")
            _ok(f"LM Studio model: {cfg.lmstudio.model}")

    # ── 2. LM Studio ────────────────────────────────────────────────────────
    if cfg:
        console.print("\n[heading]② LM Studio[/heading]")
        from gitoma.planner.llm_client import check_lmstudio

        health = check_lmstudio(cfg)

        if health.ok:
            _ok(health.message)
            console.print(
                f"  [dim]Models: {', '.join(health.available_models[:5])}[/dim]"
            )
        else:
            all_ok = False
            icon = "🔴"
            console.print(f"  [danger]{icon} {health.message}[/danger]")
            for line in health.detail.split("\n"):
                if line.strip():
                    prefix = "[muted]" if line.startswith("  →") else "[dim]"
                    console.print(f"  {prefix}{line.strip()}[/{prefix[1:]}")
            if health.available_models:
                console.print(
                    f"  [muted]Currently loaded: {', '.join(health.available_models[:5])}[/muted]"
                )

    # ── 3. GitHub API ────────────────────────────────────────────────────────
    if cfg and cfg.github.token:
        console.print("\n[heading]③ GitHub API[/heading]")
        try:
            import github

            auth = github.Auth.Token(cfg.github.token)
            g = github.Github(auth=auth)
            user = g.get_user()
            _ok(f"Authenticated as: {user.login}")
        except Exception as e:
            all_ok = False
            err = str(e)
            if "401" in err or "Bad credentials" in err:
                console.print("  [danger]✗ GitHub token is invalid or expired[/danger]")
                console.print("  [muted]  → gitoma config set GITHUB_TOKEN=<new_token>[/muted]")
            else:
                console.print(f"  [danger]✗ GitHub API error: {e}[/danger]")

        # Optional: check specific repo
        if repo_url:
            owner, name = parse_repo_url(repo_url)
            console.print(f"\n[heading]④ Repo Access: {owner}/{name}[/heading]")
            try:
                info = GitHubClient(cfg).repo_info(owner, name)
                _ok(f"{info['full_name']} — {info['language']} — ★{info['stars']}")
                perms = info.get("permissions", {})
                if perms:
                    for perm, val in perms.items():
                        icon = "✓" if val else "✗"
                        color = "success" if val else "danger"
                        console.print(f"  [{color}]{icon} {perm}[/{color}]")
            except Exception as e:
                all_ok = False
                console.print(f"  [danger]✗ Cannot access {owner}/{name}: {e}[/danger]")

    # ── Summary ─────────────────────────────────────────────────────────────
    console.print()
    if all_ok:
        console.print(
            Panel(
                "[success]✅ All checks passed — Gitoma is ready to run![/success]\n\n"
                "[muted]Try: [primary]gitoma analyze <repo-url>[/primary][/muted]",
                border_style="success",
                title="[success]🩺 Health OK[/success]",
            )
        )
    else:
        console.print(
            Panel(
                "[danger]Some checks failed. Fix the issues above before running.[/danger]\n\n"
                "[muted]Run [primary]gitoma doctor[/primary] again after fixing.[/muted]",
                border_style="danger",
                title="[danger]🩺 Health: Issues Found[/danger]",
            )
        )
        raise typer.Exit(1)
