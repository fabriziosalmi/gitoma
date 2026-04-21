# Quickstart

Three commands to a pull request.

## 1. Install + configure

```bash
pipx install gitoma
gitoma config set GITHUB_TOKEN=ghp_your_fine_grained_token
```

The token needs `contents:write`, `pull-requests:write`, and `issues:read` on the target repo. See [Prerequisites](./prerequisites) for details.

## 2. Check everything is wired

```bash
gitoma doctor
```

You should see green check marks for **Configuration**, **LM Studio**, and **GitHub API**. If any of them are red, the error message will tell you exactly what to fix.

## 3. Run

```bash
gitoma run https://github.com/owner/repo
```

Gitoma will:

1. **Clone** the repo into a temp directory.
2. **Analyze** it with nine metric analyzers.
3. **Plan** improvements — a local LLM turns failing metrics into a `TaskPlan`.
4. **Ask for confirmation** (unless you pass `--yes`).
5. **Execute** each subtask as a separate commit on a `gitoma/improve-<timestamp>` branch.
6. **Push** the branch and **open a pull request**.
7. **Self-review** the diff and post a structured comment on the PR.

The terminal narrates every phase; you can tail structured events in a second pane with `gitoma logs <repo-url> --follow`.

## Watch it live

The cockpit is a read-only dashboard that streams agent state over WebSocket:

```bash
# Start the server in a background process.
gitoma serve &

# Open in your browser.
open http://localhost:8000
```

The first time you launch `gitoma serve` it auto-generates a Bearer token and prints it once. Paste it into the cockpit's **Settings → API Token** dialog to enable command dispatch from the UI.

::: tip Keyboard shortcuts
Press <kbd>Cmd</kbd><kbd>K</kbd> (or <kbd>Ctrl</kbd><kbd>K</kbd>) to open the command palette. Single-key shortcuts: <kbd>R</kbd> run, <kbd>A</kbd> analyze, <kbd>V</kbd> review, <kbd>F</kbd> fix-ci.
:::

## Common next steps

### The plan looks wrong — iterate without committing

```bash
gitoma run https://github.com/owner/repo --dry-run
```

`--dry-run` stops after the plan is generated. Nothing is pushed, nothing is committed.

### Something crashed — resume

```bash
gitoma run https://github.com/owner/repo --resume
```

State under `~/.gitoma/state/<owner>__<repo>.json` is replayed; the worker restarts at the last uncommitted subtask.

### Start over cleanly

```bash
gitoma reset https://github.com/owner/repo     # deletes the local state
gitoma run https://github.com/owner/repo       # fresh run
```

### Review Copilot feedback on the PR

Once Copilot (or any reviewer) has commented:

```bash
gitoma review https://github.com/owner/repo --integrate
```

`--integrate` drives an LLM loop that reads each comment, proposes a patch, commits it, and pushes. Without `--integrate` it prints the comments without touching the branch.

### Fix a broken CI run

```bash
gitoma fix-ci https://github.com/owner/repo --branch gitoma/improve-2026-04-21
```

The [Reflexion agent](/architecture/pipeline#fix-ci-reflexion) streams the failing job logs to the LLM, a critic evaluates the proposed patch, and the approved fix is pushed to the branch.

Read the full [CLI reference](./cli) for every command and flag.
