"""``gitoma scaffold <repo> --stack <s> --level <n>`` — third
deterministic vertical.

Wraps :mod:`gitoma.integrations.occam_trees` (HTTP client to a
local occam-trees server) to materialise the canonical file tree
for a `(stack, complexity-level)` pair into the target repo, then
opens a PR. Zero LLM at runtime — Occam-Trees is a pure dataset
lookup oracle (1000 deterministic scaffolds).

Architectural note: this is the THIRD gitoma deterministic
vertical, after `gitoma gitignore` (occam-gitignore) and the
read/write Layer0 substrate. Pattern is now well-grooved:

  1. Pre-flight — leg-tool reachable + healthy.
  2. Verify GitHub access for the target repo.
  3. Clone the repo to a temp worktree.
  4. Call the leg tool to produce the canonical content.
  5. Diff against the repo's current state.
  6. If no missing/changed files → silent success, no PR.
  7. If ``--dry-run`` → print the proposed tree + exit.
  8. Otherwise: branch + commit + push + open PR.

Why this matters
----------------
Yesterday's `gitoma-bench-generation` 5-way bench (entries on
fabgpt-coder/log) proved gitoma's LLM planner cannot generate a
project from zero — it ignores README intent, spec files, and
even failing tests with import errors. This vertical is the
upstream fix: scaffold the canonical tree deterministically
FIRST, then let the LLM-driven `gitoma run` polish the result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.panel import Panel

from gitoma.cli._app import app
from gitoma.cli._helpers import _abort, _ok, _warn
from gitoma.core.config import load_config
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo, parse_repo_url
from gitoma.integrations.occam_trees import (
    OccamTreesClient,
    OccamTreesUnavailable,
    ResolvedScaffold,
)
from gitoma.ui.console import console


# Stub content per role — minimal, valid, idiomatic. The point of
# this vertical is to materialise SHAPE (which files exist, with
# which roles), not to author meaningful content. The polish-agent
# (`gitoma run`) is what fills these in semantically afterwards.
_ROLE_STUBS: dict[str, str] = {
    "manifest": "{}\n",
    "framework-config": "// framework configuration — TODO: implement\n",
    "secrets-template": "# Secrets template — copy to .env and fill in\n",
    "edge-middleware": "// edge middleware — TODO: implement\n",
    "root-layout": "// root layout — TODO: implement\n",
    "home-page": "// home page — TODO: implement\n",
    "auth-endpoint": "// auth endpoint — TODO: implement\n",
    "webhook-handler": "// webhook handler — TODO: implement\n",
    "protected-page": "// protected page — TODO: implement\n",
    "settings-page": "// settings page — TODO: implement\n",
    "streaming-fallback": "// loading state — TODO: implement\n",
    "primitive-component": "// UI primitive — TODO: implement\n",
    "data-component": "// data component — TODO: implement\n",
    "notification-component": "// notification component — TODO: implement\n",
    "navigation": "// navigation — TODO: implement\n",
    "directory": "",  # empty-dir marker handled separately
}


def _stub_for(path: str, role: str) -> str:
    """Return a tiny stub body suitable for an empty placeholder
    file at `path` with semantic `role`. Falls back to a TODO
    comment that matches the file's apparent extension."""
    if role in _ROLE_STUBS:
        return _ROLE_STUBS[role]
    # Extension-aware fallback. Goal: file parses / lints clean
    # in its native toolchain so the post-scaffold polish-agent
    # can read it without choking.
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    comment_styles = {
        "py":   "# TODO: implement\n",
        "js":   "// TODO: implement\n",
        "jsx":  "// TODO: implement\n",
        "ts":   "// TODO: implement\n",
        "tsx":  "// TODO: implement\n",
        "go":   "// TODO: implement\npackage main\n",
        "rs":   "// TODO: implement\n",
        "rb":   "# TODO: implement\n",
        "php":  "<?php\n// TODO: implement\n",
        "css":  "/* TODO: implement */\n",
        "html": "<!-- TODO: implement -->\n<!DOCTYPE html>\n<html><body></body></html>\n",
        "md":   f"# {path.rsplit('/', 1)[-1].rsplit('.', 1)[0].title()}\n\nTODO\n",
        "yml":  "# TODO\n",
        "yaml": "# TODO\n",
        "toml": "# TODO\n",
        "json": "{}\n",
        "txt":  "TODO\n",
        "sh":   "#!/usr/bin/env bash\n# TODO: implement\n",
    }
    return comment_styles.get(ext, "# TODO\n")


@app.command()
def scaffold(
    repo_url: Annotated[
        str,
        typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)"),
    ],
    stack: Annotated[
        str,
        typer.Option(
            "--stack",
            help="Stack id (e.g. mern, t3, django-react). "
            "List with: occam-trees stacks",
        ),
    ],
    level: Annotated[
        int,
        typer.Option(
            "--level",
            help="Complexity level 1-10 (1=static-docs, 4=fullstack-monolith, "
            "10=decentralized-mesh). List with: occam-trees archetypes",
        ),
    ],
    branch: Annotated[
        str, typer.Option("--branch", help="Branch name (auto-generated when empty)"),
    ] = "",
    base: Annotated[
        Optional[str],
        typer.Option("--base", help="Base branch for the PR (default: repo default)"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompts"),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print proposed tree + exit, no PR"),
    ] = False,
) -> None:
    """
    Materialise a deterministic project scaffold via occam-trees + open a PR.

    Third deterministic vertical. The output is a stub-filled file
    tree: every canonical file for the (stack, level) pair is
    created (with a TODO body matching role + extension), every
    canonical directory is created. Existing files are NEVER
    overwritten — you get only the missing pieces, additive PR.

    Pre-flight: occam-trees REST server must be reachable. Default:
    OCCAM_TREES_URL=http://localhost:8420. Run it with:
      cd <occam-trees-repo> && uvicorn occam_trees.api:app --port 8420
    """
    # ── Pre-flight ────────────────────────────────────────────────
    client = OccamTreesClient()
    if not client.enabled:
        _abort(
            "occam-trees is not configured.",
            hint=(
                "Set OCCAM_TREES_URL=http://localhost:8420 (or your "
                "deploy URL). Run the server with: cd <occam-trees-repo> "
                "&& uvicorn occam_trees.api:app --port 8420"
            ),
        )

    archetypes = client.list_archetypes()
    if not archetypes:
        _abort(
            f"occam-trees server at {client.config.base_url} is unreachable.",
            hint="Verify with: curl http://localhost:8420/health",
        )
    console.print(
        f"[muted]occam-trees: {len(client.list_stacks())} stacks, "
        f"{len(archetypes)} archetypes loaded[/muted]"
    )

    # ── Validate (stack, level) early ─────────────────────────────
    if level < 1 or level > 10:
        _abort(f"--level must be 1-10 (got {level}).")
    resolved = client.resolve(stack, level)
    if resolved is None:
        # Try to give the operator a useful error: was the stack
        # unknown, or did the resolve genuinely fail?
        known_stacks = {s.get("id") for s in client.list_stacks()}
        if stack not in known_stacks:
            sample = sorted(known_stacks)[:10]
            _abort(
                f"Unknown stack '{stack}'.",
                hint=f"Examples: {', '.join(sample)} … "
                f"(see GET {client.config.base_url}/v1/stacks for the full list)",
            )
        _abort(
            f"occam-trees could not resolve stack={stack} level={level}.",
            hint="The combination might not be supported by the dataset.",
        )

    leaf_files = [t for t in resolved.flatten() if not t[0].endswith("/")]
    console.print(
        f"[muted]Resolved {resolved.stack_name} × L{resolved.archetype_level} "
        f"({resolved.archetype_name}) → {len(leaf_files)} canonical files[/muted]"
    )

    # ── Validate repo URL + GitHub access ─────────────────────────
    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as e:
        _abort(f"Invalid repo URL: {e}")

    config = load_config()
    gh = GitHubClient(config)
    try:
        info = gh.repo_info(owner, name)
    except Exception as e:  # noqa: BLE001
        _abort(
            f"Cannot access {owner}/{name}: {e}",
            hint="Check the URL + your GITHUB_TOKEN scopes (`repo` for private).",
        )
    base_branch = base or info.get("default_branch", "main")
    _ok(f"GitHub → {owner}/{name} (default: {base_branch})")

    # ── Clone repo ────────────────────────────────────────────────
    git_repo = GitRepo(repo_url, config)
    try:
        local_root = git_repo.clone()
        _ok(f"Cloned to {local_root}")

        # ── Diff against existing tree ────────────────────────────
        # Additive only: never overwrite existing files. Compute the
        # set of files that DON'T already exist in the worktree.
        missing_files: list[tuple[str, str]] = []
        existing_count = 0
        for path, role in resolved.flatten():
            if path.endswith("/"):
                # Directory marker — only "missing" if the dir doesn't
                # exist. Empty dirs aren't tracked by git so we skip
                # the marker emission unless it has explicit role.
                continue
            full = git_repo.root / path
            if full.exists():
                existing_count += 1
                continue
            missing_files.append((path, role))

        if not missing_files:
            console.print(Panel(
                f"[success]✓ The repo already has all {len(leaf_files)} "
                f"canonical files for {resolved.stack_name} × L{resolved.archetype_level}[/success]\n\n"
                f"[muted]Nothing to scaffold. The polish-agent (`gitoma run`) "
                f"is the right next step.[/muted]",
                title="[success]Nothing to do[/success]",
                border_style="success",
            ))
            return

        console.print(
            f"[warning]⚠ {len(missing_files)} file(s) missing "
            f"({existing_count} already present)[/warning]"
        )

        # ── Dry-run exit ──────────────────────────────────────────
        if dry_run:
            preview_lines = [
                f"  + {path}  [{role}]"
                for path, role in missing_files[:50]
            ]
            if len(missing_files) > 50:
                preview_lines.append(
                    f"  … and {len(missing_files) - 50} more"
                )
            console.print(Panel(
                "\n".join(preview_lines),
                title=(
                    f"[primary]Files to create (dry-run): "
                    f"{resolved.stack_name} × L{resolved.archetype_level}[/primary]"
                ),
                border_style="primary",
            ))
            console.print(
                "[muted]Re-run without --dry-run to commit + open PR.[/muted]"
            )
            return

        # ── Confirmation gate ─────────────────────────────────────
        if not yes:
            ok = typer.confirm(
                f"Open a PR on {owner}/{name} adding {len(missing_files)} "
                f"scaffold file(s)?",
                default=True,
            )
            if not ok:
                _warn("Aborted by user.")
                return

        # ── Branch + write + commit + push + PR ──────────────────
        if not branch:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            branch = (
                f"gitoma/scaffold-{resolved.stack_id}-L{resolved.archetype_level}-{ts}"
            )
        git_repo.create_branch(branch)
        _ok(f"Branch created: {branch} (off {base_branch})")

        for path, role in missing_files:
            content = _stub_for(path, role)
            # Ensure parent dirs exist on disk before write
            (git_repo.root / path).parent.mkdir(parents=True, exist_ok=True)
            git_repo.write_file(path, content)
            git_repo.stage_file(path)

        commit_sha = git_repo.commit(
            message=(
                f"scaffold: {resolved.stack_name} × L{resolved.archetype_level} "
                f"via occam-trees ({len(missing_files)} files)\n\n"
                f"Deterministic file tree from occam-trees — same (stack, level) "
                f"input → same output, byte-for-byte.\n\n"
                f"Stack: {resolved.stack_name} ({resolved.stack_id})\n"
                f"Components: {', '.join(resolved.stack_components)}\n"
                f"Archetype: {resolved.archetype_name} (L{resolved.archetype_level})\n"
                f"Files added: {len(missing_files)} of {len(leaf_files)} canonical "
                f"({existing_count} already present, untouched)\n\n"
                f"Each file body is a TODO stub. Run `gitoma run "
                f"{repo_url}` next to have the polish-agent fill them in.\n"
            ),
            author_name=config.bot.name,
            author_email=config.bot.email,
        )
        _ok(f"Commit {commit_sha[:8]} created")
        git_repo.push(branch)
        _ok(f"Pushed {branch} to origin")

        body = _compose_pr_body(resolved, missing_files, existing_count, repo_url)
        title = (
            f"scaffold: {resolved.stack_name} × L{resolved.archetype_level} "
            f"({len(missing_files)} files)"
        )
        pr = gh.create_pr(
            owner, name,
            title=title, body=body,
            head=branch, base=base_branch,
        )
        console.print(Panel(
            f"[success]🎉 Pull Request #{pr.number} is LIVE![/success]\n\n"
            f"  {pr.url}\n\n"
            f"[muted]Branch: {branch}\n"
            f"Stack: {resolved.stack_name} × L{resolved.archetype_level}\n"
            f"Files added: {len(missing_files)}. Stubs are TODO-only — run "
            f"`gitoma run {repo_url}` next.[/muted]",
            title="[success]🚀 PR Opened[/success]",
            border_style="success",
        ))
    finally:
        try:
            git_repo.cleanup()
        except Exception:  # noqa: BLE001
            pass
        client.close()


def _compose_pr_body(
    resolved: ResolvedScaffold,
    missing_files: list[tuple[str, str]],
    existing_count: int,
    repo_url: str,
) -> str:
    """PR body emphasises determinism + tells the reviewer how to
    re-derive the patch locally."""
    file_table = "\n".join(
        f"| `{path}` | `{role}` |"
        for path, role in missing_files[:30]
    )
    if len(missing_files) > 30:
        file_table += f"\n| _… and {len(missing_files) - 30} more_ | |"

    return f"""## 🤖 Deterministic project scaffold via occam-trees

> Generated by **[occam-trees](https://github.com/fabriziosalmi/occam-trees)** — pure dataset lookup, no LLM.
> Same `(stack, level)` input → same file tree, byte-for-byte.

---

### Reproducibility

| Field | Value |
|---|---|
| **Stack** | `{resolved.stack_id}` ({resolved.stack_name}) |
| **Components** | {', '.join(resolved.stack_components)} |
| **Archetype** | `{resolved.archetype_id}` ({resolved.archetype_name}) |
| **Level** | {resolved.archetype_level}/10 |
| **Files added** | {len(missing_files)} (out of {len(missing_files) + existing_count} canonical) |
| **Existing files** | {existing_count} (untouched) |

### How to re-derive locally

```bash
# Resolve the same scaffold via occam-trees CLI
occam-trees resolve {resolved.stack_id} {resolved.archetype_level} --json
```

### Files added

| Path | Role |
|---|---|
{file_table}

---

### Next step

Each file body is a tiny TODO stub. To have gitoma's polish-agent
fill them in semantically:

```bash
gitoma run {repo_url}
```

The polish-agent's critic stack (G1–G20 + Ψ-full + orphan-symbol
family) will guard the LLM-driven content.

🤖 *Auto-generated by gitoma — third deterministic vertical (after `gitignore` + Layer0 substrate). Existing files were preserved untouched.*
"""
