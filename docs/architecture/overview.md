# Overview

Gitoma is a Python 3.12 package laid out as a small set of focused subpackages, each owning one concern.

```
gitoma/
├── cli/              Typer app — one file per command, thin entry points.
├── core/             Cross-cutting primitives (config, state, repo, trace, GitHub client).
├── analyzers/        Metric scanners. One per metric. Pluggable via a registry.
├── planner/          LLM client + TaskPlan model + prompt assembly.
├── worker/           Patch applier, committer, worker agent.
├── pr/               GitHub PR creation + templating.
├── review/           Copilot watcher, integrator, reflexion dual-agent, self-critic.
├── api/              FastAPI server (REST + web cockpit router + SSE).
├── mcp/              Model Context Protocol server + cache.
└── ui/
    ├── console.py    Rich console + theme + banner modes + emoji guard.
    ├── panels.py     Rich panels + tables + trees.
    └── assets/       Cockpit HTML + CSS + JS + icon sprite (served by api/web.py).
```

## Key primitives

**Config** (`core/config.py`) — layered precedence: shell env > `~/.gitoma/.env` > `<cwd>/.env` > `~/.gitoma/config.toml`. Helpers: `resolve_config_source`, `find_overriding_sources`, `ensure_runtime_api_token` (fail-closed on FS that can't honour `0o600`).

**State** (`core/state.py`) — per-repo state file under `~/.gitoma/state/<slug>.json`. Atomic writes via `tempfile + os.replace`. The concurrent-run lock is a **kernel-held `fcntl.flock`** — released automatically on any process death, including SIGKILL. PID in the file is UX-only, not ownership data.

**Trace** (`core/trace.py`) — structured JSONL per invocation under `~/.gitoma/logs/<slug>/<ts>.jsonl`. Events carry phase, level, dotted namespace, and a free-form data object. Retention: 20 most recent per slug.

**GitRepo** (`core/repo.py`) — thin wrapper over GitPython. Owns the clone temp dir, authed remote URL, and push/commit semantics.

**LLMClient** (`planner/llm_client.py`) — OpenAI-compatible client with `chat` and `chat_json` methods and a three-level health check (`check_lmstudio` returns OK/WARN/FAIL with actionable details).

**Analyzer registry** (`analyzers/registry.py`) — every metric is a subclass of `Analyzer`. Registration is by class; the CLI iterates them. Adding a metric is one file.

## Shape of a run

`gitoma run <url>` drives a finite state machine:

```
IDLE  →  ANALYZING  →  PLANNING  →  WORKING  →  PR_OPEN  →  [REVIEWING]  →  DONE
```

- **ANALYZING**: the full analyzer registry runs. `MetricReport` collects scores.
- **PLANNING**: the planner turns failing metrics into a `TaskPlan` (tasks → subtasks).
- **WORKING**: the worker iterates subtasks, calls the LLM for patches, commits each.
- **PR_OPEN**: branch is pushed, PR is created, self-critic posts a review comment.
- **REVIEWING**: optional — user invokes `gitoma review` later to integrate Copilot feedback.
- **DONE**: terminal.

Each transition persists state atomically. A SIGKILL mid-phase leaves the file at the last phase; `--resume` picks up from there.

See [Pipeline + state machine](./pipeline) for the full graph, including `fix-ci`.

## The three surfaces

Everything Gitoma does is available through three interchangeable surfaces:

1. **CLI** (`gitoma …`) — humans at a terminal.
2. **REST** (`/api/v1/*`) — scripts, CI, the web cockpit, or anything that speaks HTTP+JSON. Bearer-protected, SSE for logs.
3. **MCP** (`gitoma mcp`) — LLM clients via the Model Context Protocol. Read tools for context, write tools for mutations.

The three share everything below them: the CLI is the authoritative layer, the REST endpoints dispatch `asyncio.subprocess` calls to the same CLI (process-group isolated, env-scrubbed), and the MCP server touches the GitHub API directly with a shared cache.

## Test matrix

250+ tests across unit, integration, and end-to-end layers.

| Layer | Tests |
|---|---|
| `tests/test_core_units.py` | Config, state, patcher, cache, task model |
| `tests/test_security_hardening.py` | Path traversal, denylist, timing-safe auth, runtime-token perms, heartbeat reset |
| `tests/test_worker_and_reflexion.py` | Worker control flow, Reflexion approve/reject |
| `tests/test_self_critic.py` | Self-review agent |
| `tests/test_trace.py` | JSONL structure, retention, span timings |
| `tests/test_mcp_write_tools.py` | MCP contract + size caps + idempotency |
| `tests/test_api_industrial.py` | REST hardening (SSE heartbeat, backpressure, env scrub, error_id) |
| `tests/test_ui_industrial.py` | HTML semantics, CSS contract, JS architecture, CLI glyph/banner |
| `tests/e2e/*` | Full-server tests with `TestClient`, subprocess SIGKILL → orphan detection |

Every PR must pass all tests + ruff + mypy strict.
