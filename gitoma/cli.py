"""Gitoma CLI — main entry point with all commands and per-phase error guards."""

from __future__ import annotations

import atexit
import json
import os
import threading
import traceback
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Generator, Optional, TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from gitoma.core.config import Config
    from gitoma.planner.llm_client import LLMClient
    from gitoma.core.repo import GitRepo

# ── Silence noisy library warnings before any imports ──────────────────────────
# urllib3 v2 warns about LibreSSL on macOS — not actionable for end users
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.rule import Rule

from gitoma import __version__
from gitoma.core.config import load_config, save_config_value
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo, parse_repo_url
from gitoma.core.state import (
    AgentPhase,
    AgentState,
    acquire_run_lock,
    delete_state,
    list_all_states,
    load_state,
    release_run_lock,
    save_state,
)
from gitoma.core.trace import open_trace
from gitoma.ui.console import console
from gitoma.ui.panels import (
    make_analyzer_progress,
    print_banner,
    print_commit,
    print_metric_report,
    print_pr_panel,
    print_repo_info,
    print_status_panel,
    print_task_plan,
)

app = typer.Typer(
    name="gitoma",
    help="🤖 AI-powered GitHub repository improvement agent",
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,  # we handle our own error presentation
)


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


def _doctor_push(repo_url: str) -> None:
    """Ordered diagnostic pass for push-permission failures.

    Walks every layer that can say `403 Permission denied`:
      1. Token present + classifiable
      2. Token authenticates as SOME user (GET /user)
      3. Token user matches the configured BOT_GITHUB_USER
      4. Token has `repo` scope (classic) or the repo is in its allowed list
         (fine-grained)
      5. User has push permission on the repo
      6. Target default branch protection
    Each step prints a verdict and, on failure, a concrete remediation.
    """
    import requests

    from gitoma.core.config import load_config
    from gitoma.core.repo import parse_repo_url

    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as exc:
        _abort(f"Invalid repo URL: {exc}")

    from gitoma.core.config import resolve_config_source
    cfg = load_config()
    token = cfg.github.token
    bot_user = cfg.bot.github_user
    kind = _classify_github_token(token)
    _, source = resolve_config_source("GITHUB_TOKEN", "github", "token")
    source_display = (
        source.replace(str(Path.home()), "~") if source not in ("env", "default") else source
    )

    console.print(f"\n[heading]Target[/heading]:            {owner}/{name}")
    console.print(f"[heading]Token kind[/heading]:        {kind}")
    console.print(f"[heading]Token source[/heading]:      {source_display}")
    console.print(f"[heading]Configured bot user[/heading]: {bot_user}")

    # ── 1. Token presence ────────────────────────────────────────────────
    if kind == "missing":
        _abort(
            "GITHUB_TOKEN is not configured.",
            hint="gitoma config set GITHUB_TOKEN=<token>",
        )

    # ── 2. Token authenticates ───────────────────────────────────────────
    console.print("\n[heading]① Token identity[/heading]")
    try:
        user_resp = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _abort(f"Network error contacting GitHub API: {exc}")
    if user_resp.status_code == 401:
        _abort(
            "Token is invalid or expired (401).",
            hint="Rotate the token on GitHub and update with `gitoma config set GITHUB_TOKEN=<new>`.",
        )
    if user_resp.status_code != 200:
        _abort(f"GitHub API /user returned {user_resp.status_code}: {user_resp.text[:200]}")

    token_user = user_resp.json().get("login", "?")
    _ok(f"Authenticated as [bold]{token_user}[/bold]")

    # ── 3. Bot user match ────────────────────────────────────────────────
    if token_user != bot_user:
        _warn(
            f"BOT_GITHUB_USER is '{bot_user}' but the token authenticates as '{token_user}'.",
            hint=(
                f"Either set BOT_GITHUB_USER={token_user} (recommended), or "
                f"replace the token with one owned by {bot_user}."
            ),
        )

    # ── 4. Scopes (classic) / repo-scope (fine-grained) ──────────────────
    console.print("\n[heading]② Token scope[/heading]")
    if kind == "classic":
        scopes = [s.strip() for s in user_resp.headers.get("X-OAuth-Scopes", "").split(",") if s.strip()]
        console.print(f"  scopes: {', '.join(scopes) if scopes else '(none)'}")
        if "repo" not in scopes and "public_repo" not in scopes:
            console.print(
                "[danger]✗ Classic token is missing the [bold]repo[/bold] scope.[/danger]"
            )
            console.print(
                "[muted]  → Regenerate at github.com/settings/tokens, tick 'repo' "
                "(full control of private repositories).[/muted]"
            )
            return
        _ok(f"'repo' scope present — {', '.join(scopes)}")
    elif kind == "fine-grained":
        console.print(
            "[muted]  fine-grained PAT — scopes are declared per-token in GitHub UI. "
            "Verifying via repo probe below.[/muted]"
        )
    else:
        _warn(f"Unusual token kind '{kind}' — proceeding but results may vary.")

    # ── 5. Repo visibility + collaborator permission ────────────────────
    console.print("\n[heading]③ Repository access[/heading]")
    repo_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{name}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if repo_resp.status_code == 404:
        console.print(f"[danger]✗ 404 — the token cannot see {owner}/{name}.[/danger]")
        if kind == "fine-grained":
            console.print(
                "[muted]  → Most common cause: the fine-grained PAT wasn't granted "
                "access to this repo.\n"
                f"     Edit the token at github.com/settings/personal-access-tokens, "
                f"add [bold]{owner}/{name}[/bold] to 'Repository access', set\n"
                "     'Repository permissions → Contents: Read and write' and "
                "'Pull requests: Read and write'.[/muted]"
            )
        else:
            console.print(
                f"[muted]  → Confirm [bold]{token_user}[/bold] is a collaborator on "
                f"[bold]{owner}/{name}[/bold] (the invite must be accepted), then retry.[/muted]"
            )
        return
    if repo_resp.status_code != 200:
        _abort(f"/repos returned {repo_resp.status_code}: {repo_resp.text[:200]}")

    repo_info = repo_resp.json()
    visibility = "private" if repo_info.get("private") else "public"
    _ok(f"Visible: {repo_info['full_name']} ({visibility})")

    perms = repo_info.get("permissions") or {}
    pull_ok = perms.get("pull", False)
    push_ok = perms.get("push", False)
    admin_ok = perms.get("admin", False)
    console.print(f"  permissions: pull={pull_ok}  push={push_ok}  admin={admin_ok}")

    if not push_ok:
        console.print(
            f"\n[danger]✗ {token_user} has NO push permission on {owner}/{name}.[/danger]"
        )
        if kind == "fine-grained":
            console.print(
                "[muted]  → Fine-grained PATs default to read-only per-repo. Edit the token:\n"
                "     Repository permissions → [bold]Contents: Read and write[/bold]\n"
                "     Repository permissions → [bold]Pull requests: Read and write[/bold][/muted]"
            )
        elif kind == "classic":
            console.print(
                f"[muted]  → The token can see the repo but {token_user} is either not a "
                f"collaborator or has only Read role.\n"
                f"     Invite as Write or Admin at "
                f"github.com/{owner}/{name}/settings/access.[/muted]"
            )
        else:
            console.print("[muted]  → Role is below Write. Ask the repo owner to raise it.[/muted]")
        return

    _ok(f"{token_user} has push permission.")

    # ── 5b. Active write-probe ──────────────────────────────────────────
    # The /repos `permissions` map reflects the USER's rights, not the
    # TOKEN's. Fine-grained PATs commonly have Contents: Read (default) even
    # though the user has write — and the only way to be sure is to actually
    # try a write. We create a temporary ref that points to the current
    # default-branch HEAD, then immediately delete it.
    console.print("\n[heading]④ Write probe (create + delete throwaway ref)[/heading]")
    import uuid as _uuid
    default_branch = repo_info.get("default_branch", "main")
    head_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{name}/git/refs/heads/{default_branch}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if head_resp.status_code != 200:
        _warn(
            f"Could not read '{default_branch}' HEAD ({head_resp.status_code}) — skipping probe."
        )
    else:
        sha = head_resp.json().get("object", {}).get("sha")
        probe_name = f"gitoma-doctor-probe-{_uuid.uuid4().hex[:8]}"
        create_resp = requests.post(
            f"https://api.github.com/repos/{owner}/{name}/git/refs",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"ref": f"refs/heads/{probe_name}", "sha": sha},
            timeout=10,
        )
        if create_resp.status_code == 201:
            _ok("Write probe succeeded — the token CAN write to this repo.")
            # Cleanup best-effort.
            requests.delete(
                f"https://api.github.com/repos/{owner}/{name}/git/refs/heads/{probe_name}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                timeout=10,
            )
        elif create_resp.status_code == 403:
            console.print(
                "[danger]✗ 403 on write probe — /repos lied: the TOKEN itself lacks write permission.[/danger]"
            )
            if kind == "fine-grained":
                console.print(
                    "[muted]  → Root cause for your fine-grained PAT: edit the token at\n"
                    "     github.com/settings/personal-access-tokens, select the token,\n"
                    "     [bold]Repository permissions[/bold] →\n"
                    "       Contents:      set to [bold]Read and write[/bold]\n"
                    "       Pull requests: set to [bold]Read and write[/bold]\n"
                    "     Save. The user has access; the token didn't.[/muted]"
                )
            else:
                console.print(
                    "[muted]  → Token lacks 'repo' scope or the user's role is below Write.[/muted]"
                )
            return
        else:
            _warn(
                f"Write probe returned {create_resp.status_code}: "
                f"{create_resp.text[:200]} — skipping."
            )

    # ── 6. Branch protection on default branch ──────────────────────────
    console.print("\n[heading]⑤ Default-branch protection[/heading]")
    default_branch = repo_info.get("default_branch", "main")
    prot_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{name}/branches/{default_branch}/protection",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if prot_resp.status_code == 404:
        _ok(f"'{default_branch}' has no protection rules — feature branches push freely.")
    elif prot_resp.status_code == 200:
        rules = prot_resp.json()
        _warn(
            f"'{default_branch}' is PROTECTED.",
            hint=(
                "Feature-branch pushes (gitoma/improve-*) should still succeed, "
                "but PR merge may require review. Rules snapshot below."
            ),
        )
        console.print(f"  [dim]{json.dumps({k: v for k, v in rules.items() if not k.startswith('url')}, default=str)[:200]}…[/dim]")
    else:
        _warn(f"Branch-protection probe returned {prot_resp.status_code} — skipping.")

    # ── Verdict ─────────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[primary]Verdict[/primary]", style="primary"))
    if push_ok:
        console.print(
            "[success]✓ Every layer checked says push should succeed.[/success]\n"
            "[muted]If `git push` still 403s, the problem is below the HTTP API: "
            "check git's credential helper, SSO enforcement on an org, or a personal "
            "mirror that points to the wrong fork.[/muted]"
        )
    else:
        console.print("[danger]✗ Push will fail until the step above is fixed.[/danger]")


def _doctor_runs() -> None:
    """Scan ~/.gitoma/state/ and classify each run as live / orphaned / done."""
    from rich.table import Table

    states = list_all_states()
    if not states:
        console.print("\n[muted]No tracked runs.[/muted]\n")
        return

    table = Table(title="Tracked runs", border_style="dim", pad_edge=False)
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Phase", style="dim")
    table.add_column("PID", justify="right")
    table.add_column("Heartbeat")
    table.add_column("Verdict")

    now = datetime.now(timezone.utc)
    orphans: list[AgentState] = []
    for s in states:
        alive = _pid_alive(s.pid)
        age_s: float | None = None
        if s.last_heartbeat:
            try:
                age_s = (now - datetime.fromisoformat(s.last_heartbeat)).total_seconds()
            except ValueError:
                age_s = None

        terminal = s.phase in ("DONE",) or bool(s.errors)
        orphaned = (
            not terminal
            and (not alive or (age_s is not None and age_s > 90.0))
        )

        if s.phase == "DONE":
            verdict = "[success]done[/success]"
        elif s.errors:
            verdict = "[danger]failed[/danger]"
        elif orphaned:
            verdict = "[warning]ORPHANED[/warning]"
            orphans.append(s)
        elif alive:
            verdict = "[success]live[/success]"
        else:
            verdict = "[muted]idle[/muted]"

        hb_text = "never"
        if age_s is not None:
            if age_s < 60:
                hb_text = f"{int(age_s)}s ago"
            elif age_s < 3600:
                hb_text = f"{int(age_s / 60)}m ago"
            else:
                hb_text = f"{int(age_s / 3600)}h ago"

        table.add_row(
            f"{s.owner}/{s.name}",
            s.phase,
            str(s.pid) if s.pid else "—",
            hb_text,
            verdict,
        )

    console.print()
    console.print(table)

    if orphans:
        console.print(
            f"\n[warning]⚠  {len(orphans)} orphaned run(s) detected.[/warning]"
        )
        for s in orphans:
            console.print(
                f"  [muted]→ Reset with:[/muted] "
                f"[primary]gitoma reset https://github.com/{s.owner}/{s.name}[/primary]"
            )
        console.print()
    else:
        console.print("\n[success]✓ All tracked runs look healthy.[/success]\n")


@contextmanager
def _heartbeat(state: "AgentState", *, trace_label: str = "run") -> Generator[None, None, None]:
    """Keep ``state.last_heartbeat`` fresh AND open a structured trace file.

    Two concerns collapsed into one context manager because every use-site
    wants both: a run that's producing progress needs a live heartbeat AND
    a jsonl log of what it's doing. The trace is scoped to the same slug
    and closes on exit. Use ``gitoma.core.trace.current()`` from anywhere
    in the pipeline to append events to this trace.
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


# ─────────────────────────────────────────────────────────────────────────────
# gitoma doctor
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


# ─────────────────────────────────────────────────────────────────────────────
# gitoma run
# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def run(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Analyze and plan only — no commits or PR")] = False,
    branch: Annotated[str, typer.Option("--branch", help="Branch name to create")] = "",
    base: Annotated[Optional[str], typer.Option("--base", help="Base branch for the PR")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume an existing agent run")] = False,
    reset_state: Annotated[bool, typer.Option("--reset", help="Delete existing state and start fresh")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
    skip_lm: Annotated[bool, typer.Option("--skip-lm-check", hidden=True, help="Skip LM Studio check (testing)")] = False,
) -> None:
    """
    🚀 Run the full autonomous improvement pipeline on a GitHub repo.

    Pipeline: Analyze → Plan (LLM) → Execute (LLM + commits) → Open PR

    Always run [primary]gitoma doctor[/primary] first to verify all prerequisites.
    """
    print_banner(__version__)

    # ── Pre-flight ──────────────────────────────────────────────────────────
    config = _check_config()
    owner, name = parse_repo_url(repo_url)

    # ── Concurrent-run lock ────────────────────────────────────────────────
    # Prevent two parallel `gitoma run` invocations on the same repo from
    # corrupting each other's state. The lock is released at the end of
    # this function (and cleaned up automatically if this PID dies).
    acquired, holder_pid = acquire_run_lock(owner, name)
    if not acquired:
        _abort(
            f"Another gitoma run is already active for {owner}/{name} (pid {holder_pid}).",
            hint=(
                "Wait for it to finish, or delete "
                f"~/.gitoma/state/{owner}__{name}.lock if you know the other process is gone."
            ),
        )
    # atexit guarantees release on every normal exit path (typer.Exit, sys.exit,
    # graceful SIGTERM). Hard kills leave a stale lock behind which the next
    # `acquire_run_lock` will detect and take over.
    atexit.register(release_run_lock, owner, name)

    # ── Existing state guard ────────────────────────────────────────────────
    existing_state = load_state(owner, name)
    if existing_state:
        if reset_state:
            delete_state(owner, name)
            existing_state = None
            _warn("Existing state deleted — starting fresh")
        elif resume:
            console.print(
                f"[info]↩ Resuming from phase: [bold]{existing_state.phase}[/bold][/info]"
            )
        else:
            print_status_panel(existing_state)
            console.print(
                "\n[warning]⚠ An agent run already exists for this repo.[/warning]\n"
                "[muted]Options:[/muted]\n"
                "  [primary]--resume[/primary]  Continue from last checkpoint\n"
                "  [primary]--reset[/primary]   Delete state and restart\n"
                "  [primary]gitoma status <url>[/primary]  Inspect current progress"
            )
            raise typer.Exit(0)

    # ── Branch name ─────────────────────────────────────────────────────────
    if not branch:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"gitoma/improve-{ts}"

    # ── GitHub check ────────────────────────────────────────────────────────
    gh = GitHubClient(config)
    repo_info = _check_github(config, owner, name)
    print_repo_info(repo_info)
    base_branch = base or repo_info["default_branch"]

    # Verify the target branch doesn't already exist remotely
    try:
        remote_branches = gh.list_branches(owner, name)
        if branch in remote_branches:
            _warn(
                f"Branch '{branch}' already exists on GitHub",
                hint="A new timestamped branch will be used instead.",
            )
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            branch = f"gitoma/improve-{ts}"
            console.print(f"[muted]  New branch: {branch}[/muted]")
    except Exception:
        pass  # non-fatal — continue

    # ── LM Studio check ─────────────────────────────────────────────────────
    if not skip_lm:
        llm = _check_lmstudio(config)
    else:
        from gitoma.planner.llm_client import LLMClient
        llm = LLMClient(config)
        _warn("LM Studio check skipped (--skip-lm-check)")

    console.print()

    # ── Clone ───────────────────────────────────────────────────────────────
    git_repo = _clone_repo(repo_url, config)
    languages = git_repo.detect_languages() or ["Unknown"]
    console.print(f"[muted]Languages detected: {', '.join(languages)}[/muted]")

    # ── Initialize state ────────────────────────────────────────────────────
    state = AgentState(
        repo_url=repo_url,
        owner=owner,
        name=name,
        branch=branch,
        phase=AgentPhase.ANALYZING,
    )
    save_state(state)

    with _heartbeat(state):

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 1 — ANALYZE
        # ────────────────────────────────────────────────────────────────────────
        with _phase("PHASE 1 — ANALYSIS", cleanup=git_repo, state=state):
            from gitoma.analyzers.registry import AnalyzerRegistry, ALL_ANALYZER_CLASSES

            registry = AnalyzerRegistry(
                root=git_repo.root,
                languages=languages,
                repo_url=repo_url,
                owner=owner,
                name=name,
                default_branch=base_branch,
            )

            n_analyzers = len(ALL_ANALYZER_CLASSES)
            with make_analyzer_progress() as progress:
                task_id = progress.add_task(
                    "[heading]Analyzing repository…[/heading]", total=n_analyzers
                )

                def on_progress(analyzer_name: str, idx: int, total: int) -> None:
                    progress.update(
                        task_id,
                        description=f"[heading]{analyzer_name}[/heading]",
                        advance=1,
                    )
                    state.current_operation = f"Analyzing: {analyzer_name}"
                    save_state(state)

                report = registry.run(on_progress=on_progress)

            state.metric_report = report.to_dict()
            state.current_operation = "Analysis complete"
            state.advance(AgentPhase.PLANNING)
            save_state(state)

        print_metric_report(report)

        if not report.failing and not report.warning:
            console.print(
                Panel(
                    "[success]✅ All metrics pass! This repo is already in great shape.[/success]",
                    border_style="success",
                )
            )
            _safe_cleanup(git_repo)
            state.current_operation = "All metrics already pass"
            state.advance(AgentPhase.DONE)
            save_state(state)
            return

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 2 — PLAN
        # ────────────────────────────────────────────────────────────────────────
        with _phase("PHASE 2 — PLANNING", cleanup=git_repo, state=state):
            from gitoma.planner.planner import PlannerAgent
            from gitoma.planner.llm_client import LLMError

            console.print(
                f"[muted]Asking {config.lmstudio.model} to generate improvement plan…[/muted]"
            )
            state.current_operation = f"Planning with {config.lmstudio.model}"
            save_state(state)
            file_tree = git_repo.file_tree(max_files=100)
            planner = PlannerAgent(llm)

            try:
                plan = planner.plan(report, file_tree)
            except LLMError as e:
                # LLM-specific error — give actionable hint
                console.print(
                    Panel(
                        f"[danger]LLM planning failed:[/danger] {e}\n\n"
                        "[muted]Possible causes:\n"
                        "  → LM Studio was closed during inference\n"
                        "  → Model context window exceeded (try a smaller repo)\n"
                        "  → Model returned malformed JSON (retry with --reset)[/muted]",
                        title="[danger]🤖 LLM Error[/danger]",
                        border_style="danger",
                    )
                )
                _safe_cleanup(git_repo)
                raise typer.Exit(1)

            if not plan.tasks:
                console.print("[warning]⚠ LLM returned an empty task plan. Nothing to do.[/warning]")
                _safe_cleanup(git_repo)
                return

            state.task_plan = plan.to_dict()
            state.current_operation = f"Plan ready — {plan.total_tasks} tasks, {plan.total_subtasks} subtasks"
            state.advance(AgentPhase.WORKING)
            save_state(state)

        print_task_plan(plan)

        if dry_run:
            console.print(
                Panel(
                    "[warning]DRY RUN — plan generated but no commits or PR will be created.[/warning]\n"
                    "[muted]Remove [primary]--dry-run[/primary] to execute.[/muted]",
                    border_style="warning",
                    title="[warning]🧪 Dry Run[/warning]",
                )
            )
            _safe_cleanup(git_repo)
            return

        if not yes:
            if not Confirm.ask(
                f"\n[primary]Proceed? ({plan.total_tasks} tasks · "
                f"{plan.total_subtasks} subtasks on branch [bold]{branch}[/bold])[/primary]",
                default=True,
            ):
                console.print("[muted]Aborted by user.[/muted]")
                _safe_cleanup(git_repo)
                return

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 3 — EXECUTE
        # ────────────────────────────────────────────────────────────────────────
        with _phase("PHASE 3 — EXECUTION", cleanup=git_repo, state=state):
            from gitoma.worker.worker import WorkerAgent

            # Create branch
            try:
                git_repo.create_branch(branch)
                _ok(f"Branch created: {branch}")
            except Exception as e:
                _abort(
                    f"Failed to create branch '{branch}': {e}",
                    hint="The branch may already exist locally. Use --reset to start fresh.",
                    state=state,
                )

            console.print()
            worker = WorkerAgent(llm=llm, git_repo=git_repo, config=config, state=state)

            from gitoma.planner.task import SubTask, Task

            def on_task_start(task: Task) -> None:
                state.current_operation = f"Task {task.id}: {task.title}"
                save_state(state)
                console.print(
                    f"\n[task.current]▶ {task.id}[/task.current] "
                    f"[bold heading]{task.title}[/bold heading]"
                )

            def on_subtask_start(task: Task, sub: SubTask) -> None:
                state.current_operation = f"{sub.id}: {sub.title} — {config.lmstudio.model} generating"
                save_state(state)
                console.print(
                    f"  [muted]◌ {sub.id}[/muted] [info]{sub.title}[/info] "
                    f"[dim]({config.lmstudio.model} generating…)[/dim]"
                )

            def on_subtask_done(task: Task, sub: SubTask, sha: str | None) -> None:
                if sha:
                    state.current_operation = f"{sub.id} committed → {sha[:7]}"
                    save_state(state)
                    print_commit(sha, sub.title, sub.id)
                else:
                    state.current_operation = f"{sub.id} skipped (no changes)"
                    save_state(state)
                    console.print(f"  [warning]◎ {sub.id} — skipped (no file changes)[/warning]")

            def on_subtask_error(task: Task, sub: SubTask, error: str) -> None:
                state.current_operation = f"{sub.id} FAILED: {error[:80]}"
                save_state(state)
                console.print(f"  [danger]✗ {sub.id} failed: {error[:120]}[/danger]")

            plan = worker.execute(
                plan,
                on_task_start=on_task_start,
                on_subtask_start=on_subtask_start,
                on_subtask_done=on_subtask_done,
                on_subtask_error=on_subtask_error,
            )

        completed = plan.completed_tasks
        console.print(
            f"\n[success]✓ Execution complete — "
            f"{completed}/{plan.total_tasks} tasks done "
            f"({sum(s.status=='completed' for t in plan.tasks for s in t.subtasks)}/"
            f"{plan.total_subtasks} subtasks)[/success]"
        )

        if completed == 0:
            console.print(
                Panel(
                    "[danger]No tasks completed — aborting PR creation.[/danger]\n\n"
                    "[muted]All subtasks failed. Check LM Studio model output and retry.\n"
                    "Run with [primary]--dry-run[/primary] to inspect the plan without committing.[/muted]",
                    border_style="danger",
                    title="[danger]💥 Execution Failed[/danger]",
                )
            )
            _safe_cleanup(git_repo)
            raise typer.Exit(1)

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 4 — PULL REQUEST
        # ────────────────────────────────────────────────────────────────────────
        with _phase("PHASE 4 — PULL REQUEST", cleanup=git_repo, state=state):
            from gitoma.pr.pr_agent import PRAgent

            console.print(f"[muted]Pushing {branch} to origin and opening PR…[/muted]")
            state.current_operation = f"Pushing branch {branch} to origin"
            save_state(state)
            pr_agent = PRAgent(git_repo=git_repo, gh_client=gh, config=config, state=state)

            try:
                pr_info = pr_agent.push_and_open_pr(
                    report=report,
                    plan=plan,
                    branch=branch,
                    base=base_branch,
                )
            except Exception as e:
                err_str = str(e)
                if "push" in err_str.lower() or "rejected" in err_str.lower():
                    _abort(
                        f"Git push failed: {e}",
                        hint=(
                            "Ensure your token has 'contents:write' permission "
                            "on this repo. Also check the branch isn't protected."
                        ),
                        state=state,
                    )
                elif "422" in err_str or "Unprocessable" in err_str:
                    _abort(
                        "GitHub rejected the PR (422 Unprocessable Entity)",
                        hint=(
                            "Possible causes: PR already exists, branch not pushed, "
                            "or head/base branch names are wrong."
                        ),
                        state=state,
                    )
                else:
                    raise  # re-raise for the _phase guard to catch

        print_pr_panel(pr_info.url, pr_info.number, branch)

        state.current_operation = f"PR #{pr_info.number} opened"
        state.advance(AgentPhase.PR_OPEN)
        save_state(state)
        _safe_cleanup(git_repo)

        console.print(
            f"\n[muted]Next: run [primary]gitoma review {repo_url}[/primary] "
            "once Copilot reviews the PR.[/muted]"
        )


# ─────────────────────────────────────────────────────────────────────────────
# gitoma status
# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def status(
    repo_url: Annotated[
        Optional[str],
        typer.Argument(help="GitHub repo URL. Omit to list all tracked repos."),
    ] = None,
    remote: Annotated[bool, typer.Option("--remote", help="Also query GitHub for gitoma/* branches")] = False,
) -> None:
    """
    📊 Show agent progress for a repo (or all tracked repos).
    """
    print_banner(__version__)

    if repo_url:
        try:
            owner, name = parse_repo_url(repo_url)
        except ValueError as e:
            _abort(f"Invalid repo URL: {e}")

        state = load_state(owner, name)
        if not state:
            console.print(f"[muted]No local agent state for {owner}/{name}.[/muted]")
        else:
            print_status_panel(state)

        if remote:
            config = _check_config(require_token=True)
            gh = GitHubClient(config)
            try:
                branches = gh.gitoma_branches(owner, name)
                if branches:
                    console.print("\n[secondary]Remote gitoma/* branches:[/secondary]")
                    for b in branches:
                        # Try to find matching state
                        state_match = (state and state.branch == b)
                        suffix = " [dim](this run)[/dim]" if state_match else ""
                        console.print(f"  [commit]{b}[/commit]{suffix}")
                else:
                    console.print("[muted]No gitoma/* branches found on GitHub.[/muted]")
            except Exception as e:
                _warn(f"Could not query remote branches: {e}")
    else:
        states = list_all_states()
        if not states:
            console.print(
                "[muted]No active agent runs.\n"
                "Start one with: [primary]gitoma run <url>[/primary][/muted]"
            )
            return
        console.print(f"[heading]Active agent runs ({len(states)}):[/heading]\n")
        for s in states:
            print_status_panel(s)
            console.print()


# ─────────────────────────────────────────────────────────────────────────────
# gitoma review
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

    # LM Studio check before integrating
    llm = _check_lmstudio(config)

    console.print(f"[muted]Cloning repo to apply fixes on branch {state.branch}…[/muted]")
    git_repo = _clone_repo(repo_url, config)

    # Checkout agent branch
    try:
        git_repo.repo.git.checkout(state.branch)
        _ok(f"Checked out branch: {state.branch}")
    except Exception as e:
        _safe_cleanup(git_repo)
        _abort(
            f"Could not checkout branch '{state.branch}': {e}",
            hint="The branch may have been deleted on remote. Use gitoma status --remote to verify.",
        )

    from gitoma.review.integrator import ReviewIntegrator

    integrator = ReviewIntegrator(llm=llm, git_repo=git_repo, config=config, state=state)

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

        state.advance(AgentPhase.REVIEWING)
        save_state(state)
        console.print(
            f"\n[muted]PR updated: [url]{pr_url}[/url][/muted]"
        )
    else:
        console.print("[warning]⚠ No fixes pushed (nothing committed).[/warning]")

    _safe_cleanup(git_repo)


# ─────────────────────────────────────────────────────────────────────────────
# gitoma analyze
# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def analyze(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL")],
) -> None:
    """
    🔬 Analyze a repository and display health metrics.

    Safe read-only — no commits, no PR, no LLM calls.
    """
    print_banner(__version__)

    config = _check_config()
    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as e:
        _abort(f"Invalid repo URL: {e}")

    repo_info = _check_github(config, owner, name)
    print_repo_info(repo_info)

    git_repo = _clone_repo(repo_url, config)
    languages = git_repo.detect_languages() or ["Unknown"]
    console.print(f"[muted]Languages: {', '.join(languages)}[/muted]\n")

    from gitoma.analyzers.registry import AnalyzerRegistry, ALL_ANALYZER_CLASSES

    registry = AnalyzerRegistry(
        root=git_repo.root,
        languages=languages,
        repo_url=repo_url,
        owner=owner,
        name=name,
        default_branch=repo_info["default_branch"],
    )

    with make_analyzer_progress() as progress:
        task_id = progress.add_task("[heading]Analyzing…[/heading]", total=len(ALL_ANALYZER_CLASSES))

        def on_progress(a_name: str, idx: int, total: int) -> None:
            progress.update(task_id, description=f"[heading]{a_name}[/heading]", advance=1)

        report = registry.run(on_progress=on_progress)

    _safe_cleanup(git_repo)
    print_metric_report(report)

    # Actionable summary
    if report.failing:
        n = len(report.failing)
        console.print(
            f"\n[muted]{n} metric(s) failing. Fix them with:[/muted]\n"
            f"  [primary]gitoma run {repo_url}[/primary]"
        )
    elif report.warning:
        n = len(report.warning)
        console.print(
            f"\n[muted]{n} metric(s) need attention. Run:[/muted]\n"
            f"  [primary]gitoma run {repo_url}[/primary]"
        )
    else:
        console.print("\n[success]✅ All metrics pass![/success]")


# ─────────────────────────────────────────────────────────────────────────────
# gitoma config
# ─────────────────────────────────────────────────────────────────────────────

@app.command(
    name="config",
    # Allow unknown extra args so KEY=VALUE is never mis-parsed by Click/Typer
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def config_cmd(
    ctx: typer.Context,
    action: Annotated[str, typer.Argument(help="Action: set | show | path")],
) -> None:
    """
    ⚙️  Manage Gitoma configuration.

    [bold]Examples:[/bold]
      gitoma config set GITHUB_TOKEN=ghp_xxx
      gitoma config set LM_STUDIO_MODEL=gemma-4-e2b-it
      gitoma config show
      gitoma config path
    """
    from gitoma.core.config import CONFIG_FILE

    if action == "path":
        console.print(f"[code]{CONFIG_FILE}[/code]")
        return

    if action == "show":
        from gitoma.core.config import resolve_config_source
        cfg = load_config()
        token_display = (
            f"{'*' * 8}{cfg.github.token[-4:]}"
            if len(cfg.github.token) > 8
            else "(not set — run: gitoma config set GITHUB_TOKEN=...)"
        )

        # (env_var_name, toml_section, toml_key, display_row)
        keys = [
            ("GITHUB_TOKEN",           "github",   "token",        ("GitHub", "token", token_display)),
            ("BOT_NAME",               "bot",      "name",         ("Bot Identity", "name", cfg.bot.name)),
            ("BOT_EMAIL",              "bot",      "email",        ("Bot Identity", "email", cfg.bot.email)),
            ("BOT_GITHUB_USER",        "bot",      "github_user",  ("Bot Identity", "github_user", cfg.bot.github_user)),
            ("LM_STUDIO_BASE_URL",     "lmstudio", "base_url",     ("LM Studio", "base_url", cfg.lmstudio.base_url)),
            ("LM_STUDIO_MODEL",        "lmstudio", "model",        ("LM Studio", "model", cfg.lmstudio.model)),
            ("LM_STUDIO_TEMPERATURE",  "lmstudio", "temperature",  ("LM Studio", "temperature", str(cfg.lmstudio.temperature))),
            ("LM_STUDIO_MAX_TOKENS",   "lmstudio", "max_tokens",   ("LM Studio", "max_tokens", str(cfg.lmstudio.max_tokens))),
            ("GITOMA_API_TOKEN",       "api",      "token",        ("Cockpit API", "token",
                f"{'*' * 8}{cfg.api_auth_token[-4:]}" if len(cfg.api_auth_token) > 8 else "(auto-generated)")),
        ]

        def _fmt_source(src: str) -> str:
            home = str(Path.home())
            if src == "env":
                return "[warning]$ENV[/warning]"
            if src == "default":
                return "[muted]default[/muted]"
            # Collapse $HOME for readability.
            shown = src.replace(home, "~")
            return f"[code]{shown}[/code]"

        console.print("\n[heading]Gitoma Configuration[/heading]\n")
        current_section = None
        for env_key, tsec, tkey, (section, field_name, shown) in keys:
            if section != current_section:
                console.print(f"[muted]─ {section} {'─' * (50 - len(section))}[/muted]")
                current_section = section
            _, src = resolve_config_source(env_key, tsec, tkey)
            console.print(
                f"  {field_name:<14}[code]{shown}[/code]   {_fmt_source(src)}"
            )
        console.print(
            f"\n[muted]Precedence: $ENV > ~/.gitoma/.env > <cwd>/.env > {CONFIG_FILE}[/muted]"
        )
        return

    if action == "set":
        # Reconstruct KEY=VALUE from context.args.
        # This handles all shell edge cases:
        #   gitoma config set GITHUB_TOKEN=ghp_xxx      → ctx.args = ['GITHUB_TOKEN=ghp_xxx']
        #   gitoma config set GITHUB_TOKEN ghp_xxx      → ctx.args = ['GITHUB_TOKEN', 'ghp_xxx']
        #   gitoma config set GITHUB_TOKEN = ghp_xxx    → ctx.args = ['GITHUB_TOKEN', '=', 'ghp_xxx']
        raw_args = ctx.args  # list of remaining tokens Click didn't consume

        if not raw_args:
            console.print(
                "[danger]✗ Missing argument.[/danger]\n"
                "[muted]Usage: [primary]gitoma config set KEY=value[/primary]\n"
                "Example: [primary]gitoma config set GITHUB_TOKEN=ghp_xxx[/primary][/muted]"
            )
            raise typer.Exit(1)

        # Rejoin and normalize: handle 'KEY=VAL', 'KEY = VAL', or 'KEY VAL'
        joined = "".join(raw_args)         # remove spaces around '=': KEY=VAL
        if "=" not in joined:
            # Fallback: first token is KEY, rest is VALUE
            key = raw_args[0]
            value = " ".join(raw_args[1:]) if len(raw_args) > 1 else ""
        else:
            key, _, value = joined.partition("=")

        key = key.strip().upper()
        value = value.strip()

        if not key:
            _abort("Empty key. Usage: gitoma config set KEY=value")
        if not value:
            _abort(
                f"Empty value for key '{key}'.",
                hint=f"Usage: gitoma config set {key}=<your-value>",
            )

        # Intelligence: warn BEFORE writing when a higher-priority source
        # would silently override the new value. Root cause of many 'I
        # updated the token but it didn't take effect' incidents.
        from gitoma.core.config import find_overriding_sources
        overriding = find_overriding_sources(key)
        if overriding:
            console.print()
            _warn(
                f"Your new {key} will be overridden at load time by:",
            )
            for src in overriding:
                shown = src if src == "env" else src.replace(str(Path.home()), "~")
                label = "[warning]$ENV[/warning]" if src == "env" else f"[code]{shown}[/code]"
                console.print(f"  → {label}")
            console.print(
                "[muted]  Remove/rename that source first, or edit it "
                "directly. Writing to config.toml anyway.[/muted]\n"
            )

        try:
            save_config_value(key, value)
            _ok(f"Saved {key} → {CONFIG_FILE}")
        except ValueError as e:
            _abort(str(e))
        return

    _abort(f"Unknown action '{action}'", hint="Valid actions: set | show | path")


# ─────────────────────────────────────────────────────────────────────────────
# gitoma list
# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="list")
def list_cmd() -> None:
    """
    📋 List all active agent runs across all repos.
    """
    print_banner(__version__)
    states = list_all_states()
    if not states:
        console.print(
            "[muted]No active runs.\n"
            "Start one with: [primary]gitoma run <url>[/primary][/muted]"
        )
        return
    console.print(f"[heading]Active agent runs ({len(states)}):[/heading]\n")
    for s in states:
        print_status_panel(s)
        console.print()


# ─────────────────────────────────────────────────────────────────────────────
# gitoma logs
# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="logs")
def logs_cmd(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new events as they arrive")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Print raw JSONL instead of the pretty summary")] = False,
    filter_event: Annotated[Optional[str], typer.Option("--filter", help="Only show events whose 'event' field starts with this prefix (e.g. 'run.', 'phase.')")] = None,
) -> None:
    """
    📜 Tail the structured trace for a repo's latest run.

    Every ``gitoma run`` / ``review`` / ``fix-ci`` writes one JSONL file
    per invocation under ``~/.gitoma/logs/<slug>/``. This command finds
    the most recent one and prints it — live with ``--follow``, grepped
    with ``--filter=phase.``, or verbatim with ``--raw``.
    """
    from gitoma.core.trace import latest_log_path

    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as exc:
        _abort(f"Invalid repo URL: {exc}")

    slug = f"{owner}__{name}"
    path = latest_log_path(slug)
    if path is None:
        _abort(
            f"No trace logs for {slug}.",
            hint="The trace is written on first `gitoma run`. Start one and re-try.",
        )

    console.print(f"[muted]Tailing {path}[/muted]\n")

    _stream_log_file(path, follow=follow, raw=raw, filter_prefix=filter_event)


def _stream_log_file(
    path: "Path",
    *,
    follow: bool,
    raw: bool,
    filter_prefix: Optional[str],
) -> None:
    """Print every record, optionally streaming new ones as they're appended."""
    import json as _json
    import time as _time

    with path.open("r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                if not follow:
                    return
                _time.sleep(0.25)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if raw:
                console.print(stripped)
                continue
            try:
                rec = _json.loads(stripped)
            except _json.JSONDecodeError:
                console.print(f"[dim]{stripped}[/dim]")
                continue
            event = rec.get("event", "")
            if filter_prefix and not event.startswith(filter_prefix):
                continue
            ts = rec.get("ts", "")[11:19]  # HH:MM:SS
            level = rec.get("level", "info")
            phase = rec.get("phase", "")
            data = rec.get("data", {})
            level_style = {"warn": "warning", "error": "danger", "debug": "dim"}.get(level, "info")
            phase_chip = f"[muted]{phase}[/muted] " if phase else ""
            detail = " ".join(f"{k}={v}" for k, v in data.items() if v not in ("", None))
            console.print(
                f"[dim]{ts}[/dim] {phase_chip}[{level_style}]{event}[/{level_style}] "
                f"[muted]{detail}[/muted]"
            )


# ─────────────────────────────────────────────────────────────────────────────
# gitoma reset
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


# ─────────────────────────────────────────────────────────────────────────────
# gitoma sandbox
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
        # Launch run_cmd on the sandbox repo dynamically
        owner = config.bot.github_user
        repo_url = f"https://github.com/{owner}/gitoma-sandbox"
        console.print(f"[success]Launching Gitoma agent on {repo_url}...[/success]\n")
        
        try:
            run(
                repo_url=repo_url,
                dry_run=False,
                branch="",
                base=None,
                resume=True,
                reset_state=False,
                yes=True,
                skip_lm=False
            )
        except Exception as e:
            if not isinstance(e, typer.Exit):
                _abort(f"Sandbox run failed: {e}")

    else:
        _abort(f"Unknown sandbox action: {action}. Use setup, run, or teardown.")

# ─────────────────────────────────────────────────────────────────────────────
# gitoma fix-ci
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

# ─────────────────────────────────────────────────────────────────────────────
# gitoma serve
# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="serve")
def serve(
    port: Annotated[int, typer.Option(help="Port to run the REST API on")] = 8000,
    host: Annotated[str, typer.Option(help="Host to bind the server to")] = "0.0.0.0",
) -> None:
    """
    🌐  Launch the Gitoma FastAPI REST Server.
    """
    import os

    import uvicorn

    from gitoma.core.config import RUNTIME_TOKEN_FILE, ensure_runtime_api_token

    print_banner(__version__)
    _check_config(require_token=False)

    token, generated = ensure_runtime_api_token()
    # Publish to the process env so `load_config()` picks it up in every
    # request handler (verify_token calls it per request).
    os.environ["GITOMA_API_TOKEN"] = token

    masked = (token[:6] + "…" + token[-4:]) if len(token) > 12 else "***"
    if generated:
        console.print(
            Panel(
                f"[bold]{token}[/bold]\n\n"
                f"[muted]Persisted to {RUNTIME_TOKEN_FILE} (mode 0600).[/muted]\n"
                f"[muted]Paste into the cockpit Settings dialog when prompted.[/muted]\n"
                f"[muted]Delete that file and restart to rotate.[/muted]",
                title="[primary]◉ New API token generated[/primary]",
                border_style="info",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            f"[success]API secured[/success] "
            f"[muted](token {masked})[/muted]"
        )

    console.print(f"Starting server on [primary]http://{host}:{port}[/primary]")
    console.print(f"Cockpit:         [primary]http://{host}:{port}/[/primary]")
    console.print(f"Swagger docs:    [primary]http://{host}:{port}/docs[/primary]\n")

    uvicorn.run("gitoma.api.server:app", host=host, port=port, log_level="info")

# ─────────────────────────────────────────────────────────────────────────────
# gitoma mcp
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


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
