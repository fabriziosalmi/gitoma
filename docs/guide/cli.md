# CLI reference

Every command is available as `gitoma <command> --help`. This page is the authoritative list.

## `gitoma run`

Launch the full autonomous pipeline on a repo.

```bash
gitoma run <repo-url> [OPTIONS]
```

| Flag | Description |
|---|---|
| `--dry-run` | Analyze + plan only. No commits, no PR. |
| `--branch TEXT` | Feature branch name. Defaults to `gitoma/improve-<timestamp>`. |
| `--base TEXT` | Base branch for the PR. Defaults to the repo's default branch. |
| `--resume` | Resume from persisted state instead of starting fresh. |
| `--reset` | Delete existing state before starting. |
| `--yes`, `-y` | Skip the "proceed with this plan?" confirmation. |
| `--no-self-review` | Skip the Phase 5 self-critic pass. |
| `--no-ci-watch` | Skip Phase 6 — no polling of GitHub Actions, no auto-remediation. |
| `--no-auto-fix-ci` | Watch CI but do **not** auto-invoke the Reflexion agent on failure. The watch still narrates pass/fail in the terminal + cockpit. |

A run acquires a kernel-held lock on `~/.gitoma/state/<owner>__<repo>.lock`. A second `gitoma run` on the same repo while the first is live exits with a message pointing at the holder PID.

## `gitoma analyze`

Read-only. Clones the repo, runs every analyzer, prints the metric report. No commits, no PR, no LLM calls.

```bash
gitoma analyze https://github.com/owner/repo
```

## `gitoma review`

Fetch external review comments (Copilot, reviewers) on the open PR. With `--integrate`, drive an LLM loop that proposes fixes and pushes them.

```bash
gitoma review <repo-url> [--integrate] [--pr NUMBER]
```

`--pr` is optional — Gitoma auto-detects the PR from its own state.

## `gitoma fix-ci`

Run the Reflexion dual-agent on a branch whose latest GitHub Actions run failed. The Fixer agent proposes a patch; the Critic agent evaluates it; approved patches are pushed. See [Architecture → Pipeline → Fix-CI](/architecture/pipeline#fix-ci-reflexion).

```bash
gitoma fix-ci <repo-url> --branch <branch-name>
```

## `gitoma status`

Print the status of a tracked run, or list all tracked runs when no URL is given.

```bash
gitoma status [<repo-url>] [--remote]
```

`--remote` adds a GitHub API call that lists any `gitoma/*` branches present on the remote (useful when rebuilding a picture after a crash).

## `gitoma list`

Short form of `gitoma status` with no URL. Prints a summary of every tracked run.

## `gitoma reset`

Delete the persisted state for a repo. The remote branch and PR (if any) are **not** touched — use this when you want the next `gitoma run` to start fresh.

```bash
gitoma reset <repo-url>
```

## `gitoma doctor`

Pre-flight health check. Always safe to run: no writes, no clones, no LLM calls unless you ask.

```bash
gitoma doctor                           # full health sweep (config + LLM + GitHub)
gitoma doctor --runs                    # list tracked runs, flag orphans
gitoma doctor --push <repo-url>         # diagnose why `git push` might fail
```

`--push` walks the entire permission chain for a repo (token kind, scopes, repo visibility, collaborator role, default-branch protection) and performs an **active write probe** — a throwaway ref is created and immediately deleted. If the probe succeeds, you can push. If it 403s, the error message tells you precisely which setting to change.

## `gitoma logs`

Tail the structured JSONL trace for a repo's most recent run.

```bash
gitoma logs <repo-url>
gitoma logs <repo-url> --follow             # stream new events
gitoma logs <repo-url> --filter phase.      # only events starting with `phase.`
gitoma logs <repo-url> --raw                # print the JSON as-is
```

Traces live at `~/.gitoma/logs/<owner>__<repo>/<timestamp>.jsonl`. See [Observability](./observability) for the event schema.

## `gitoma config`

Manage persistent configuration.

```bash
gitoma config show                                  # print effective config
gitoma config path                                  # print config.toml path
gitoma config set KEY=value                         # persist a value
gitoma config set LM_STUDIO_MODEL=qwen2.5-coder:14b
```

`config show` annotates every value with its source — shell env, `~/.gitoma/.env`, `<cwd>/.env`, or `config.toml`. `config set` warns if a higher-priority source would override your new value (the root cause of many "I rotated the token but it didn't take effect" incidents).

## `gitoma serve`

Launch the FastAPI REST server + the live web cockpit.

```bash
gitoma serve --port 8000 --host 0.0.0.0
```

| Flag | Description |
|---|---|
| `--port INT` | Port to bind. Default `8000`. |
| `--host TEXT` | Host to bind. Default `0.0.0.0`. |
| `--show-token` | Print the full API token in the startup banner. Default shows only a masked prefix/suffix. |

If `GITOMA_API_TOKEN` is not already configured, the server auto-generates one on first start, persists it to `~/.gitoma/runtime_token` (mode `0600`), and prints it in full exactly once in the banner.

If you set the token yourself (shell env, `config set`, or a dynamic value like `GITOMA_API_TOKEN=test-$(date +%s)`), the banner masks it by default so an over-the-shoulder screenshot doesn't leak it. Pass `--show-token` when you need to see the value — typical use case is pairing it with `$(date +%s)` where you can't know the exact string without the server printing it.

See [API → Authentication](/api/auth).

## `gitoma mcp`

Run the Model Context Protocol server on stdio. Point an MCP-capable client (Claude Desktop, MCP Inspector) at it to expose GitHub context + write tools. See [API → MCP server](/api/mcp).

```bash
gitoma mcp
```

## `gitoma sandbox`

Scaffolding helpers for testing Gitoma against a disposable repo on your own GitHub account.

```bash
gitoma sandbox setup       # creates gitoma-sandbox on GitHub
gitoma sandbox run         # runs the full pipeline on it
gitoma sandbox teardown    # deletes the sandbox repo
```
