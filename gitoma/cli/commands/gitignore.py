"""``gitoma gitignore <repo>`` — first **deterministic vertical**.

Bypasses the LLM-driven worker pipeline entirely. Wraps
:mod:`gitoma.integrations.occam_gitignore` (which delegates to
``occam-gitignore-core``) to produce a hash-verifiable, byte-
deterministic ``.gitignore`` for the target repo, then opens a PR
with the content via the existing GitHub machinery.

Architectural note: this is the FIRST gitoma vertical that uses
**zero LLM at runtime**. The pattern (subprocess / direct-import /
HTTP / MCP wrap of an external deterministic tool, then PR via
gitoma's git+gh wiring) generalises to future verticals: semgrep,
reuse-tool, license-checker, etc.

Flow:
  1. Pre-flight — occam-gitignore-core importable + data files
     reachable. If not → clean error message + non-zero exit.
  2. Verify GitHub access for the target repo.
  3. Clone the repo to a temp worktree.
  4. Walk the file tree → fingerprint → generate ``.gitignore``.
  5. Diff against existing ``.gitignore`` (if any).
  6. If no drift → silent success, no PR.
  7. If ``--dry-run`` → print diff + features detected, exit.
  8. Otherwise: branch + commit + push + open PR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

import typer
from rich.panel import Panel

from gitoma.cli._app import app
from gitoma.cli._helpers import _abort, _ok, _warn
from gitoma.core.config import load_config
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo, parse_repo_url
from gitoma.integrations.occam_gitignore import (
    OccamGitignoreUnavailable,
    diff_against_existing,
    generate_for_repo,
    is_available,
    version_info,
)
from gitoma.ui.console import console


@app.command()
def gitignore(
    repo_url: Annotated[
        str,
        typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)"),
    ],
    branch: Annotated[
        str, typer.Option("--branch", help="Branch name (auto-generated when empty)"),
    ] = "",
    base: Annotated[
        Optional[str],
        typer.Option("--base", help="Base branch for the PR (default: repo default)"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompts"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print diff + exit, no commit / no PR"),
    ] = False,
) -> None:
    """
    Produce a deterministic .gitignore via occam-gitignore + open a PR.

    First "deterministic vertical" — zero LLM at runtime. The output
    is hash-verifiable and reproducible: same repo state → same
    .gitignore byte-for-byte.

    Pre-flight: install ``occam-gitignore-core`` and ensure the
    templates + rules-table data files are reachable (set
    OCCAM_GITIGNORE_DATA_DIR if non-default).
    """
    # ── Pre-flight ────────────────────────────────────────────────
    if not is_available():
        _abort(
            "occam-gitignore is not available.",
            hint=(
                "Install: pip install occam-gitignore-core. "
                "Then ensure the data files (templates/, rules_table.json) "
                "are reachable via OCCAM_GITIGNORE_DATA_DIR env var if not "
                "in the default location."
            ),
        )
    versions = version_info()
    console.print(
        f"[muted]occam-gitignore: core={versions['core']} "
        f"templates={versions['templates']} "
        f"rules_table={versions['rules_table']}[/muted]"
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

        # ── Generate .gitignore ───────────────────────────────────
        try:
            result = generate_for_repo(git_repo.root)
        except OccamGitignoreUnavailable as e:
            _abort(str(e))
        console.print(
            f"[muted]Detected features: {', '.join(result.features)} "
            f"({len(result.evidence)} evidence)[/muted]"
        )
        console.print(
            f"[muted]Output hash: {result.content_hash[:30]}…[/muted]"
        )

        # ── Diff against existing ─────────────────────────────────
        diff = diff_against_existing(git_repo.root, result.content)
        if diff is None:
            console.print(Panel(
                "[success]✓ Your .gitignore is already up to date[/success]\n\n"
                f"[muted]Generated content matches the existing file "
                f"byte-for-byte (sha256: {result.content_hash[:24]}…).[/muted]",
                title="[success]Nothing to do[/success]",
                border_style="success",
            ))
            return

        diff_lines = diff.count("\n") + 1
        console.print(
            f"[warning]⚠ Drift detected: {diff_lines} diff lines[/warning]"
        )

        # ── Dry-run exit ──────────────────────────────────────────
        if dry_run:
            console.print(Panel(
                diff[:4000] + ("\n... (truncated)" if len(diff) > 4000 else ""),
                title="[primary]Proposed .gitignore (dry-run)[/primary]",
                border_style="primary",
            ))
            console.print(
                "[muted]Re-run without --dry-run to commit + open PR.[/muted]"
            )
            return

        # ── Confirmation gate ─────────────────────────────────────
        if not yes:
            ok = typer.confirm(
                f"Open a PR on {owner}/{name} replacing the .gitignore?",
                default=True,
            )
            if not ok:
                _warn("Aborted by user.")
                return

        # ── Branch + write + commit + push + PR ──────────────────
        if not branch:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            branch = f"gitoma/gitignore-{ts}"
        git_repo.create_branch(branch)
        _ok(f"Branch created: {branch} (off {base_branch})")
        git_repo.write_file(".gitignore", result.content)
        git_repo.stage_file(".gitignore")
        commit_sha = git_repo.commit(
            message=(
                f"chore: deterministic .gitignore via occam-gitignore "
                f"v{result.core_version}\n\n"
                f"Generated by occam-gitignore — same repo state → same "
                f"output, byte-for-byte.\n\n"
                f"Detected features: {', '.join(result.features)}\n"
                f"Output hash: {result.content_hash}\n"
                f"Templates version: {result.templates_version}\n"
                f"Rules table version: {result.rules_table_version}\n"
            ),
            author_name=config.bot.name,
            author_email=config.bot.email,
        )
        _ok(f"Commit {commit_sha[:8]} created")
        git_repo.push(branch)
        _ok(f"Pushed {branch} to origin")

        body = _compose_pr_body(result, diff_lines)
        title = (
            f"chore: deterministic .gitignore via occam-gitignore "
            f"({len(result.features)} features)"
        )
        pr = gh.create_pr(
            owner, name,
            title=title, body=body,
            head=branch, base=base_branch,
        )
        console.print(Panel(
            f"[success]🎉 Pull Request #{pr.number} is LIVE![/success]\n\n"
            f"  {pr.html_url}\n\n"
            f"[muted]Branch: {branch}\n"
            f"Hash: {result.content_hash}\n"
            f"Review the diff, merge when ready.[/muted]",
            title="[success]🚀 PR Opened[/success]",
            border_style="success",
        ))
    finally:
        try:
            git_repo.cleanup()
        except Exception:  # noqa: BLE001
            pass


def _compose_pr_body(result, diff_lines: int) -> str:
    """Build the PR body — emphasises determinism + provenance so a
    human reviewer can verify the patch came from a reproducible
    source, not an opaque LLM."""
    evidence_table = "\n".join(
        f"| `{feature}` | `{path}` |"
        for feature, path in result.evidence[:20]
    )
    if len(result.evidence) > 20:
        evidence_table += f"\n| _… and {len(result.evidence) - 20} more_ | |"

    return f"""## 🤖 Deterministic .gitignore via occam-gitignore

> Generated by **[occam-gitignore](https://github.com/fabriziosalmi/gitignore)** — pure deterministic tool, no LLM.
> Same repo state → same output, byte-for-byte.

---

### Reproducibility

| Field | Value |
|---|---|
| **Output hash** | `{result.content_hash}` |
| **Core version** | `{result.core_version}` |
| **Templates version** | `{result.templates_version}` |
| **Rules table version** | `{result.rules_table_version}` |

You can re-generate this exact file locally:

```bash
pip install occam-gitignore-core
occam-gitignore generate . > .gitignore
# expected hash: {result.content_hash}
```

---

### Detected features ({len(result.features)})

{', '.join(f'`{f}`' for f in result.features)}

#### Evidence

| Feature | Evidence path |
|---|---|
{evidence_table}

---

### Diff size

{diff_lines} lines changed vs the previous `.gitignore`.

---

### Why this PR

The previous `.gitignore` had drifted from the deterministic baseline.
This PR realigns it. Future drift is detectable via the same tool —
pin `occam-gitignore` in CI to fail builds when the file diverges.

---

<sub>🤖 This PR was opened by [Gitoma](https://github.com/fabriziosalmi/gitoma)
using `gitoma gitignore`. Human review encouraged before merge —
the deterministic output is opinionated; project-specific extras may
need to be added via `--extras` or appended manually.</sub>
"""
