"""Shared helpers for gitoma CLI commands.

All internal utilities previously at the top of ``gitoma/cli.py`` live here.
Commands import what they need from this module; tests can exercise these
helpers directly without spinning up Typer.
"""

from __future__ import annotations

import os
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, NoReturn, TYPE_CHECKING

import typer
from rich.panel import Panel
from rich.rule import Rule

from gitoma.core.github_client import GitHubClient
from gitoma.core.state import save_state
from gitoma.core.trace import open_trace
from gitoma.ui.console import console

if TYPE_CHECKING:
    from gitoma.core.config import Config
    from gitoma.core.repo import GitRepo
    from gitoma.core.state import AgentState
    from gitoma.planner.llm_client import LLMClient

# ─────────────────────────────────────────────────────────────────────────────
# Guard helpers
# ─────────────────────────────────────────────────────────────────────────────

def _abort(
    message: str,
    hint: str = "",
    code: int = 1,
    state: "AgentState | None" = None,
) -> NoReturn:
    """Print a formatted error and exit.

    If ``state`` is provided, the error is appended to ``state.errors`` and
    persisted to disk *before* exiting — so the cockpit (which observes
    ~/.gitoma/state/*.json) sees a non-silent failure instead of a stale
    phase snapshot.
    """
    lines = [f"[danger]✗ {message}[/danger]"]
    if hint:
        lines.append(f"[muted]  → {hint}[/muted]")
    console.print("\n".join(lines))
    if state is not None:
        full = message if not hint else f"{message} — {hint}"
        state.errors.append(full)
        state.current_operation = f"FAILED: {message[:120]}"
        try:
            save_state(state)
        except Exception:
            # State persistence must never block the error exit path.
            pass
    raise typer.Exit(code)


def _warn(message: str, hint: str = "") -> None:
    """Print a non-fatal warning."""
    console.print(f"[warning]⚠  {message}[/warning]")
    if hint:
        console.print(f"[muted]   → {hint}[/muted]")


def _ok(message: str) -> None:
    console.print(f"[success]✓ {message}[/success]")


@contextmanager
def _phase(
    name: str,
    cleanup: "GitRepo | None" = None,
    state: "AgentState | None" = None,
) -> Generator[None, None, None]:
    """Context manager that wraps a pipeline phase.

    On unhandled exception: prints traceback summary, persists the failure
    to ``state`` (if given) so the cockpit can surface it, calls cleanup,
    and exits 1.
    """
    console.print()
    console.print(Rule(f"[primary]{name}[/primary]", style="primary"))
    try:
        yield
    except typer.Exit:
        raise
    except Exception as exc:
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        # Show a compact, readable error — not a raw traceback dump
        console.print(
            Panel(
                f"[danger]Phase failed: {name}[/danger]\n\n"
                f"[bold]{type(exc).__name__}:[/bold] {exc}\n\n"
                f"[dim]{''.join(tb_lines[-3:]).strip()}[/dim]",
                title="[danger]💥 Unexpected Error[/danger]",
                border_style="danger",
            )
        )
        if state is not None:
            state.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            state.current_operation = f"FAILED in {name}: {str(exc)[:120]}"
            try:
                save_state(state)
            except Exception:
                pass
        if cleanup:
            _safe_cleanup(cleanup)
        raise typer.Exit(1)


def _safe_cleanup(git_repo: "GitRepo") -> None:
    """Call git_repo.cleanup() without ever raising."""
    try:
        git_repo.cleanup()
    except Exception:
        pass


_HEARTBEAT_INTERVAL_S = 30.0


def _run_self_review(
    config: "Config",
    owner: str,
    name: str,
    pr_number: int,
    state: "AgentState",
) -> None:
    """Phase 5 — adversarial LLM critic posts findings to the freshly-opened PR.

    Never re-raises: a failed self-review must not undo a successful
    Phase 4 (the PR exists and the branch is pushed). Failures are
    logged to the trace + user's terminal; state advances regardless.
    """
    from gitoma.review.self_critic import SelfCriticAgent

    console.print()
    console.print(Rule("[primary]PHASE 5 — SELF-REVIEW[/primary]", style="primary"))
    console.print(
        f"[muted]Running adversarial critic on PR #{pr_number}…[/muted]"
    )

    state.current_operation = f"Self-reviewing PR #{pr_number}"
    save_state(state)

    try:
        agent = SelfCriticAgent(config)
        result = agent.review_pr(owner, name, pr_number)
    except Exception as exc:
        _warn(
            f"Self-review failed: {exc}",
            hint="The PR is still open. Rerun with `gitoma review` when ready.",
        )
        state.current_operation = f"Self-review failed: {exc}"
        save_state(state)
        return

    n = len(result.findings)
    if result.skipped_reason:
        console.print(f"[muted]Self-review skipped — {result.skipped_reason}[/muted]")
        state.current_operation = f"Self-review skipped: {result.skipped_reason}"
    elif n == 0:
        _ok("Self-review: no issues flagged.")
        state.current_operation = "Self-review: no findings"
    else:
        by_sev = {"blocker": 0, "major": 0, "minor": 0, "nit": 0}
        for f in result.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in by_sev.items() if v)
        posted_note = "comment posted" if result.comment_posted else "post failed"
        _ok(f"Self-review: {n} finding(s) ({summary}) — {posted_note}.")
        state.current_operation = f"Self-review: {n} findings ({summary})"
    save_state(state)


def _pid_alive(pid: int | None) -> bool:
    """True iff `pid` still names a running process on this machine."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # different user, but running — treat as alive
    except OSError:
        return False
    return True


def _classify_github_token(token: str) -> str:
    """Identify the token flavor from its prefix.

    * `ghp_…`                 — classic personal access token
    * `github_pat_…`          — fine-grained PAT (per-repo scope)
    * `gho_…` / `ghu_…`       — OAuth user tokens
    * `ghs_…`                 — installation / server tokens
    Anything else → "unknown".
    """
    if not token:
        return "missing"
    if token.startswith("ghp_"):
        return "classic"
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith(("gho_", "ghu_")):
        return "oauth"
    if token.startswith("ghs_"):
        return "server"
    return "unknown"


@contextmanager
def _heartbeat(state: "AgentState", *, trace_label: str = "run") -> Generator[None, None, None]:
    """Keep ``state.last_heartbeat`` fresh AND open a structured trace file.

    Two concerns collapsed into one context manager because every use-site
    wants both: a run that's producing progress needs a live heartbeat AND
    a jsonl log of what it's doing. The trace is scoped to the same slug
    and closes on exit. Use ``gitoma.core.trace.current()`` from anywhere
    in the pipeline to append events to this trace.

    The heartbeat daemon thread refreshes ``last_heartbeat`` every
    ``_HEARTBEAT_INTERVAL_S`` seconds; when the CLI process dies by any
    signal (SIGKILL, OOM, terminal closed), the thread dies with it and
    observers can distinguish "orphaned" from "just slow".
    """
    slug = f"{state.owner}__{state.name}"
    with open_trace(slug, label=trace_label) as tr:
        tr.emit(
            "run.begin",
            repo_url=state.repo_url,
            branch=state.branch,
            phase=state.phase,
            pid=os.getpid(),
        )

        state.pid = os.getpid()
        state.last_heartbeat = datetime.now(timezone.utc).isoformat()
        # Reset any leftover clean-exit flag from a prior run on the same
        # slug. Without this, a new run inheriting a state whose previous
        # CLI ended at PR_OPEN (exit_clean=True) would be invisible to
        # orphan detection if the *new* run were SIGKILL'd before the
        # finally below could flip the flag — the stale True would still
        # be on disk.
        state.exit_clean = False
        save_state(state)

        stop = threading.Event()

        def _tick() -> None:
            while not stop.wait(_HEARTBEAT_INTERVAL_S):
                state.last_heartbeat = datetime.now(timezone.utc).isoformat()
                try:
                    save_state(state)
                except Exception:
                    # File locked / removed mid-write — retry on the next tick.
                    pass

        thread = threading.Thread(
            target=_tick, daemon=True, name="gitoma-heartbeat"
        )
        thread.start()
        caught: BaseException | None = None
        try:
            yield
        except typer.Exit as e:
            code = getattr(e, "exit_code", 0) or 0
            if code != 0:
                caught = e
                tr.emit("run.aborted", level="warn", exit_code=code)
            raise
        except BaseException as e:  # KeyboardInterrupt, SystemExit, errors
            caught = e
            tr.exception("run.crashed", e)
            raise
        finally:
            stop.set()
            thread.join(timeout=2.0)
            if caught is None:
                # Clean exit — mark so the orphan detector doesn't flag a
                # successfully-ended run (e.g. phase=PR_OPEN after `run`).
                state.exit_clean = True
                try:
                    save_state(state)
                except Exception:
                    pass
                tr.emit("run.exit_clean", phase=state.phase)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks (reusable across commands)
# ─────────────────────────────────────────────────────────────────────────────

def _check_config(require_token: bool = True) -> "Config":
    """Load and validate config. Aborts with friendly message on error."""
    from gitoma.core.config import load_config

    try:
        cfg = load_config()
    except Exception as e:
        _abort(
            f"Failed to load configuration: {e}",
            hint="Run 'gitoma config show' to inspect your config.",
        )

    if require_token:
        errors = cfg.validate()
        if errors:
            for msg in errors:
                console.print(f"[danger]✗ {msg}[/danger]")
            console.print(
                "\n[muted]Fix with: [primary]gitoma config set GITHUB_TOKEN=<token>[/primary][/muted]"
            )
            raise typer.Exit(1)

    return cfg


def _check_github(config: "Config", owner: str, name: str) -> "dict[str, Any]":
    """Verify GitHub access and repo existence. Returns repo_info dict."""
    console.print(f"[muted]Verifying GitHub access for {owner}/{name}…[/muted]")
    gh = GitHubClient(config)
    try:
        info = gh.repo_info(owner, name)
        _ok(f"GitHub → {info['full_name']} ({info['language']})")
        return info
    except Exception as e:
        err_str = str(e)
        if "401" in err_str or "Bad credentials" in err_str:
            _abort(
                "GitHub token is invalid or expired",
                hint="Set a valid token: gitoma config set GITHUB_TOKEN=<token>",
            )
        elif "404" in err_str or "Not Found" in err_str:
            _abort(
                f"Repository {owner}/{name} not found or not accessible",
                hint=(
                    "Check the URL and token scopes. "
                    "Token needs: contents:write, pull-requests:write"
                ),
            )
        elif "403" in err_str or "Forbidden" in err_str:
            _abort(
                "GitHub API access forbidden",
                hint="Check token permissions: contents:write + pull-requests:write required",
            )
        else:
            _abort(f"GitHub API error: {e}", hint="Check your token and network connection")
    return {}  # unreachable but satisfies type checker


def _check_lmstudio(config: "Config") -> "LLMClient":
    """
    Perform 3-level LM Studio health check.
    Prints a detailed diagnostic panel on failure.
    Returns an LLMClient on success.
    """
    from gitoma.planner.llm_client import LLMClient, check_lmstudio, HealthLevel

    console.print(
        f"[muted]Checking LM Studio at {config.lmstudio.base_url} "
        f"(model: {config.lmstudio.model})…[/muted]"
    )

    health = check_lmstudio(config)

    if health.level == HealthLevel.OK:
        _ok(f"LM Studio ready — {health.message}")
        if health.available_models:
            console.print(
                f"[dim]   Models visible: {', '.join(health.available_models[:4])}[/dim]"
            )
        return LLMClient(config)

    # Build a rich error panel
    icon = "🔴" if health.failed else "🟡"
    panel_body = (
        f"[danger]{health.message}[/danger]\n\n"
        + "\n".join(
            f"[muted]{line}[/muted]" if line.startswith("  →") else line
            for line in health.detail.split("\n")
        )
    )

    if health.available_models:
        panel_body += (
            "\n\n[muted]Currently loaded:[/muted] "
            + ", ".join(f"[code]{m}[/code]" for m in health.available_models[:5])
        )

    console.print(
        Panel(
            panel_body,
            title=f"[danger]{icon} LM Studio Health Check Failed[/danger]",
            border_style="danger",
        )
    )
    raise typer.Exit(1)


def _clone_repo(repo_url: str, config: "Config") -> "GitRepo":
    """Clone repo to temp dir with clear error messages."""
    console.print(f"[muted]Cloning {repo_url}…[/muted]")
    git_repo = GitRepo(repo_url, config)
    try:
        git_repo.clone()
        _ok(f"Cloned to {git_repo.root}")
        return git_repo
    except Exception as e:
        err_str = str(e)
        if "Authentication" in err_str or "could not read" in err_str.lower():
            _abort(
                "Git clone failed: authentication error",
                hint=(
                    "Ensure your GitHub token has 'contents:read' permission "
                    "and the bot user has access to this repo."
                ),
            )
        elif "Repository not found" in err_str or "does not exist" in err_str:
            _abort(
                "Git clone failed: repository not found",
                hint="Check the URL is correct and the repo is accessible by the bot user.",
            )
        else:
            _abort(f"Git clone failed: {e}", hint="Check your network connection and GitHub token.")
    return None  # unreachable


