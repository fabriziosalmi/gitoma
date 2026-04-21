"""Helpers used only by ``gitoma doctor``.

Extracted from the monolithic cli so the doctor flow -- which is big, has
a lot of specific HTTP probing, and rarely changes -- doesn't dominate the
diff history of the rest of the CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from rich.rule import Rule
from rich.table import Table

from gitoma.cli._helpers import (
    _abort,
    _classify_github_token,
    _ok,
    _pid_alive,
    _warn,
)
from gitoma.core.config import load_config, resolve_config_source
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import AgentState, list_all_states
from gitoma.ui.console import console

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401

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


    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as exc:
        _abort(f"Invalid repo URL: {exc}")

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


