# MCP server

Gitoma ships an embedded [Model Context Protocol](https://modelcontextprotocol.io) server that exposes GitHub context + write tools to any MCP-capable client. On stdio:

```bash
gitoma mcp
```

Point Claude Desktop, the MCP Inspector, or your own client at that command.

## Claude Desktop config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "gitoma": {
      "command": "gitoma",
      "args": ["mcp"]
    }
  }
}
```

Reload Claude Desktop. The gitoma tools appear under the MCP server list.

## Tool surface

| Tool | Kind | Purpose |
|---|---|---|
| `list_repo_tree` | Read | All file paths in a repo, capped at `max_files`. |
| `read_github_file` | Read | One file from HEAD or a specific ref. |
| `read_github_files_batch` | Read | Parallel read of up to 30 files in a single call. |
| `get_ci_failures` | Read | Failed GitHub Actions jobs on a branch. |
| `get_open_issues` | Read | Up to `limit` open issues, with labels + body excerpt. |
| `get_pr_comments` | Read | Every review + issue comment on a PR. |
| `list_prs` | Read | PRs with state + head + base. |
| `create_branch` | **Write** | Create a ref from a base ref. |
| `commit_file` | **Write** | Create or update a file via the Contents API. |
| `open_pr` | **Write** | Open a PR, idempotent — returns the existing open PR for the head if one exists. |
| `close_pr` | **Write** | Close without merging. |
| `add_pr_comment` | **Write** | Conversation comment (not a line-level review comment). |
| `add_pr_labels` | **Write** | Add labels to a PR. |
| `invalidate_repo_cache` | Management | Bust every cache entry scoped to a repo. |

All reads are cached with an LRU + TTL strategy — see [cache below](#cache).

## Defense-in-depth

The server assumes the LLM driving it is **potentially prompt-injected** through the repository content it's asked to operate on. Several layers exist to keep that assumption safe:

### Input size limits

Every write tool validates before touching the GitHub API:

| Param | Cap |
|---|---|
| `commit_file.content` | 2 MiB (UTF-8 bytes) |
| `commit_file.message` | 10 000 chars |
| `open_pr.title` | 300 chars |
| `open_pr.body` | 65 536 chars (matches GitHub's own cap) |
| `add_pr_comment.body` | 65 536 chars |
| `add_pr_labels.labels` | 20 entries max |
| `read_github_files_batch.paths` | 30 paths max |

Violations are returned as a structured `invalid_input` error — the GitHub API is never called.

### Repo allow-list (operator kill-switch)

Set `GITOMA_MCP_REPO_ALLOWLIST="owner/repo,owner2/repo2"` before launching `gitoma mcp`. Every tool rejects calls targeting a repo outside the list, regardless of the token's own scope. Perfect for pinning an MCP server to a narrow set of projects when the underlying PAT is broader than you'd like the LLM to see.

### Idempotent `open_pr`

A flaky MCP client retrying `open_pr` used to create duplicate PRs or trigger a 422 on the retry. Gitoma now checks for an existing open PR for the same `head` branch and returns it with `already_existed: true` — one logical intent, one logical side effect.

### Rate-limit backoff

Every write tool is wrapped with exponential backoff + jitter on GitHub's secondary rate-limit (abuse detection) signal. Three attempts, base delay 2 s → 4 s → 8 s with up to 25% jitter, then surface a `rate_limited` error.

## Error envelope

Every tool returns a JSON string. Success:

```json
{ "ok": true, "number": 42, "url": "https://github.com/…/pull/42", "already_existed": false }
```

Failure:

```json
{ "ok": false, "code": "forbidden", "error": "GitHub token lacks permission or is invalid",
  "type": "GithubException", "owner": "x", "repo": "y" }
```

`code` is one of a stable enum:

| Code | Meaning |
|---|---|
| `invalid_input` | A tool validator rejected the call (size, allow-list, missing field). |
| `not_found` | GitHub returned 404. |
| `forbidden` | 401/403 — token lacks permission or is invalid. |
| `unprocessable` | 422 — stale SHA, duplicate PR, malformed body. |
| `rate_limited` | Abuse / secondary rate limit after all retries. |
| `timeout` | Upstream timed out. |
| `internal` | Everything else. The full exception is on server stderr. |

`error` is a short, sanitised message. The raw `str(exc)` is **never** echoed — it routinely contains URLs with tokens or internal paths.

## Stdio protocol hygiene

MCP over stdio uses **stdout** as the protocol channel. A stray `print()` or default `logging.basicConfig()` anywhere in the process tree corrupts the frame. Gitoma explicitly configures all logging to **stderr** at module load with `force=True`, so `stdout` is clean for the entire process lifetime. Don't add `print(...)` to any module reachable from the MCP path — prefer the module `logger` (which goes to stderr) or the structured trace.

## Cache

Reads share an in-process LRU+TTL cache with namespace invalidation:

- **O(1) invalidate-by-prefix** via a secondary `namespace → {keys}` index. A write tool that mutates a repo busts every cache entry scoped to that repo in constant time, regardless of how many total entries exist.
- **Per-namespace stats** (`hits`, `misses`, `entries`, `hit_rate`) accessible in tests and suitable for exposing via an operational endpoint.
- **TTLs**: 5 min for file content, 3 min for tree, 1 min for CI status, 2 min for PR comments, 10 min for issues.
- **Monotonic clock** (`time.monotonic`) for all TTL math — immune to NTP steps.

`invalidate_repo_cache` is also exposed as a tool so an orchestrator can force a cache bust after an external push.

## Preflight

On first invocation `gitoma mcp` classifies the configured GitHub token's kind (`classic` / `fine-grained` / `oauth` / `server`) and logs it on stderr. The **secret itself is never printed** — only which shape it is. Makes it obvious when a debugger is running with the wrong token without ever exposing it in a trace.
