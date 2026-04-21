"""Shared helpers for gitoma CLI commands.

All internal utilities previously at the top of ``gitoma/cli.py`` live here.
Commands import what they need from this module; tests can exercise these
helpers directly without spinning up Typer.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, NoReturn, TYPE_CHECKING

import typer
from rich.panel import Panel
from rich.rule import Rule

from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo
from gitoma.core.state import save_state
from gitoma.core.trace import open_trace
from gitoma.ui.console import console

if TYPE_CHECKING:
    from gitoma.core.config import Config
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


# ─────────────────────────────────────────────────────────────────────────────
# CI watch-and-maybe-fix — closes the loop between PR open and a merge-ready branch
# ─────────────────────────────────────────────────────────────────────────────
#
# After the PR is opened and the adversarial self-review has posted its
# comment, GitHub Actions is almost certainly running on the feature
# branch. A human-in-the-loop workflow would now sit and wait — and if
# CI fails, manually invoke `gitoma fix-ci`. That's exactly what this
# helper automates:
#
#   1. Poll the latest workflow run for ``branch`` every
#      ``poll_interval_s`` seconds, up to ``timeout_s`` total.
#   2. Success → narrate + return.
#   3. Failure → invoke the existing ``CIDiagnosticAgent`` (same one
#      ``gitoma fix-ci`` uses) once; the agent streams logs to the LLM,
#      a critic reviews, the approved patch is pushed.
#   4. Re-poll once more so the user sees the remediated CI either pass
#      or surface as a final failure (bound attempts prevent ping-pong).
#
# The whole routine is failure-tolerant: a network blip or a hiccup in
# the GitHub API logs a warning and keeps polling until the budget is
# exhausted. ``state.current_operation`` narrates live so the cockpit
# reflects "Watching CI (2m 30s)…" and the trace captures every poll.


# 20 min matches the median GitHub Actions build time across the set of
# repos gitoma is typically used on. Configurable per-invocation from
# the ``run`` command if someone has an unusually long test matrix.
_CI_WATCH_TIMEOUT_S = 1200.0
_CI_WATCH_POLL_INTERVAL_S = 30.0
_CI_WATCH_MAX_FIX_ATTEMPTS = 1


def _watch_ci_and_maybe_fix(
    config: "Config",
    owner: str,
    name: str,
    branch: str,
    repo_url: str,
    state: "AgentState",
    *,
    timeout_s: float = _CI_WATCH_TIMEOUT_S,
    poll_interval_s: float = _CI_WATCH_POLL_INTERVAL_S,
    auto_fix: bool = True,
    max_fix_attempts: int = _CI_WATCH_MAX_FIX_ATTEMPTS,
) -> str:
    """Watch GitHub Actions for ``branch``; if it fails, auto-invoke fix-ci.

    Returns the final state as one of:

    * ``"success"``  — CI passed (either first try or after remediation).
    * ``"failure"``  — CI failed and either auto-fix was off, the fix-ci
      attempt also failed, or the post-fix CI still failed.
    * ``"timeout"``  — the poll budget expired before CI reached a
      terminal state. The run is not aborted; the user can decide.
    * ``"no_runs"``  — no GitHub Actions workflow triggered on the
      branch during the watch window. Treated as non-blocking.
    * ``"skipped"``  — caller asked to skip (returned without polling).

    Never re-raises. A failed watch does not undo a successful PR open;
    it just annotates ``state.current_operation`` and the trace with
    the outcome so the cockpit + `gitoma logs` reflect reality.
    """
    import gitoma.core.trace as trace_mod

    # `current()` always returns a Trace — a no-op one when there is no
    # active run, so we can call `.emit()` unconditionally without guards.
    tr = trace_mod.current()
    gh = GitHubClient(config)

    console.print()
    from rich.rule import Rule  # noqa: WPS433 — local import to avoid rich at module import time
    console.print(Rule("[primary]PHASE 6 — CI WATCH[/primary]", style="primary"))
    console.print(
        f"[muted]Polling GitHub Actions on [bold]{branch}[/bold] "
        f"(every {int(poll_interval_s)}s, up to {int(timeout_s // 60)} min)…[/muted]"
    )
    tr.emit("ci.watch.begin", branch=branch, timeout_s=timeout_s,
            poll_interval_s=poll_interval_s, auto_fix=auto_fix)

    fix_attempts = 0
    start = time.monotonic()

    def _poll_once() -> dict[str, Any]:
        try:
            status = gh.get_latest_ci_status(owner, name, branch)
        except Exception as exc:
            # Transient API error — log + return a synthetic "pending" so
            # the outer loop keeps polling until the budget runs out.
            _warn(f"CI status probe failed: {exc}")
            tr.emit("ci.watch.probe_error", level="warn", error=type(exc).__name__)
            return {"state": "pending", "run_id": None, "conclusion": None}
        return status

    # One retry loop per fix attempt (including the initial "no fix yet"
    # attempt as iteration zero). ``max_fix_attempts + 1`` total passes
    # through the poll: the first one watches the initial CI, every
    # subsequent pass watches the post-fix-ci rerun.
    for attempt in range(max_fix_attempts + 1):
        deadline = start + timeout_s
        result: dict[str, Any] = {"state": "pending"}
        while time.monotonic() < deadline:
            result = _poll_once()
            elapsed = int(time.monotonic() - start)
            state.current_operation = (
                f"Watching CI — {result.get('state', 'pending')} "
                f"({elapsed // 60}m {elapsed % 60}s)"
            )
            try:
                save_state(state)
            except Exception:
                pass
            tr.emit(
                "ci.watch.poll",
                state=result.get("state"),
                conclusion=result.get("conclusion"),
                run_id=result.get("run_id"),
                elapsed_s=elapsed,
            )
            if result["state"] in ("success", "failure", "no_runs"):
                break
            time.sleep(poll_interval_s)
        else:
            # The ``while`` loop fell through because deadline elapsed.
            _warn(
                f"CI watch timed out after {int(timeout_s // 60)} min",
                hint="The PR is still open — check GitHub manually or run `gitoma fix-ci` later.",
            )
            state.current_operation = "CI watch timed out"
            save_state(state)
            tr.emit("ci.watch.timeout", level="warn")
            return "timeout"

        final_state = result["state"]
        if final_state == "success":
            run_url = result.get("run_url") or ""
            _ok(f"CI passed{' — ' + str(run_url) if run_url else ''}.")
            state.current_operation = "CI passed"
            save_state(state)
            tr.emit("ci.watch.success", run_url=run_url)
            return "success"

        if final_state == "no_runs":
            _warn(
                "No GitHub Actions workflows ran on this branch",
                hint="If this repo has no CI, that's fine. Otherwise check that workflows are configured to run on pushes.",
            )
            state.current_operation = "CI: no workflows triggered"
            save_state(state)
            tr.emit("ci.watch.no_runs", level="warn")
            return "no_runs"

        # final_state == "failure"
        console.print(
            f"[danger]CI failed[/danger] — workflow `{result.get('workflow') or 'unknown'}` "
            f"ended as `{result.get('conclusion')}`."
        )
        tr.emit("ci.watch.failure", level="warn",
                conclusion=result.get("conclusion"),
                run_url=result.get("run_url"))

        if not auto_fix or fix_attempts >= max_fix_attempts:
            hint = (
                "Re-run with `gitoma fix-ci <repo-url> --branch "
                f"{branch}` to try again."
                if auto_fix
                else "Auto-remediation was disabled (--no-ci-watch / --no-auto-fix-ci)."
            )
            _warn("CI failure not auto-remediated.", hint=hint)
            state.current_operation = f"CI failed ({result.get('conclusion')})"
            save_state(state)
            return "failure"

        # Auto-remediate: invoke the same agent `gitoma fix-ci` uses.
        fix_attempts += 1
        from gitoma.review.reflexion import CIDiagnosticAgent

        console.print(
            f"[info]Invoking Reflexion auto-remediation (attempt "
            f"{fix_attempts}/{max_fix_attempts})…[/info]"
        )
        state.current_operation = f"Auto fix-ci (attempt {fix_attempts})"
        save_state(state)
        tr.emit("ci.watch.fix.attempt", attempt=fix_attempts)

        try:
            CIDiagnosticAgent(config).analyze_and_fix(repo_url, branch)
        except Exception as exc:
            _warn(
                f"fix-ci attempt failed: {exc}",
                hint="The PR is still open; try `gitoma fix-ci` manually.",
            )
            tr.emit(
                "ci.watch.fix.error",
                level="error",
                attempt=fix_attempts,
                error=type(exc).__name__,
            )
            state.current_operation = "CI fix-ci failed"
            save_state(state)
            return "failure"

        # After fix-ci pushes, GitHub starts a fresh workflow run. Reset
        # the start timestamp so the second poll gets a fresh budget
        # rather than inheriting what's left of the first one.
        console.print("[muted]Waiting for post-remediation CI run to start…[/muted]")
        time.sleep(poll_interval_s)  # tiny grace window for GitHub to kick the run
        start = time.monotonic()

    # Exhausted all attempts without success.
    state.current_operation = "CI still failing after auto-remediation"
    save_state(state)
    tr.emit("ci.watch.failure.final", level="error", attempts=fix_attempts)
    return "failure"


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

    Hardening (Swiss-watch pass):

    * **Tick survives transient crashes.** The previous tick wrapped only
      ``save_state()`` in try/except; if ``datetime.now()`` or the
      ``state.last_heartbeat = …`` assignment ever raised (clock jump,
      thread-local exhaustion, …), the loop died silently and observers
      flagged a still-running CLI as orphaned. We now wrap the whole tick
      body and log every exception via the trace, so the heartbeat
      degrades to "stale but alive" instead of "silently dead".
    * **Save_state serialized.** Both the main thread and the heartbeat
      thread call ``save_state(state)`` against the same shared object.
      Concurrent writes are atomic at the file-rename level (no torn
      reads), but interleaved field mutations could leave a brief on-disk
      window where one writer's snapshot misses the other's update. A
      module-level lock funnels both writers through one serial point —
      cheap (only contended a few times per minute) and removes a class
      of "why did the cockpit briefly show the old phase?" puzzles.
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
            # Wrap the whole loop body so a single transient failure (FS
            # error, clock anomaly, race during a state mutation) never
            # silently kills the heartbeat. We log via the trace and
            # continue — the next tick will retry.
            while not stop.wait(_HEARTBEAT_INTERVAL_S):
                try:
                    state.last_heartbeat = datetime.now(timezone.utc).isoformat()
                    save_state(state)
                except BaseException as exc:  # noqa: BLE001 - daemon must not die
                    # Log but DON'T re-raise: the daemon thread crashing
                    # silently is the failure mode we're guarding against.
                    try:
                        tr.exception("heartbeat.tick_failed", exc)
                    except Exception:
                        # Even the trace failed — last-resort: do nothing
                        # rather than spin in an exception loop.
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


