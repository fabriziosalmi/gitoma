# Contributing

Thanks for looking. The project is intentionally small and tightly-tested. Any change must pass four gates:

1. **`pytest`** — 250+ tests including e2e + adversarial.
2. **`ruff check`** — zero warnings.
3. **`mypy`** in strict mode — zero errors.
4. **`gitoma doctor`** still exits zero with a representative config.

## Dev setup

```bash
git clone https://github.com/fabriziosalmi/gitoma
cd gitoma
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

The `[dev]` extra pulls in `pytest`, `pytest-asyncio`, `pytest-mock`, `ruff`, `mypy`, and the type stubs Gitoma needs.

## Running the checks

```bash
pytest                   # full suite
ruff check gitoma tests  # lint
mypy gitoma              # strict type-check
```

All three are fast. A full pass takes under 10 seconds on a laptop.

## Project layout at a glance

```
gitoma/
├── cli/             Typer app, one file per command
├── core/            config, state, repo, trace, github_client, sandbox
├── analyzers/       registry + one file per metric
├── planner/         LLM client + TaskPlan + prompts
├── worker/          patcher, committer, worker agent
├── pr/              PR creator + templates
├── review/          copilot watcher, integrator, reflexion, self_critic, reporter
├── api/             FastAPI app (server, routers, web, sse)
├── mcp/             FastMCP tools + LRU+TTL cache
└── ui/              Rich theme + cockpit assets
tests/
├── e2e/             FastAPI TestClient end-to-end
├── test_*.py        Unit + integration
└── conftest.py      Autouse API token-cache reset
```

## Coding conventions

- **Python 3.12 features are fair game.** `Path.is_relative_to`, PEP 695 where it helps readability, generics in stdlib collections.
- **No comments that narrate the code.** Comments should explain the *why*, especially the non-obvious constraints, security boundaries, or bug-report context. "This function returns the number of users" is not a useful comment.
- **Small files, single concern.** If a module grows past ~400 lines it's probably doing two things.
- **Side-effect-free imports.** Nothing in the import graph should hit the network, stdout, or the filesystem.
- **Tests are the documentation.** A behaviour that isn't covered by a test doesn't exist.

## How to add a new metric analyzer

1. Create `gitoma/analyzers/<metric>.py` with a subclass of `Analyzer` (see `gitoma/analyzers/base.py`).
2. Register it in `gitoma/analyzers/registry.py` under `ALL_ANALYZER_CLASSES`.
3. Add a unit test under `tests/` that runs it against a minimal fake repo.

The planner picks up new metrics automatically — it looks at `report.failing + report.warning` and produces tasks targeting those metrics.

## How to add a new MCP tool

1. Add a `@mcp.tool()`-decorated function inside `build_mcp_server` in `gitoma/mcp/server.py`.
2. If it writes: decorate with `@_with_github_retries()`, validate inputs with `_require_repo` + `_require_str_size`, bust the appropriate cache namespaces on success, return a `{"ok": True, ...}` envelope.
3. Never return `str(exc)` on failure — route through `_error(exc, **context)`.
4. Add a test under `tests/test_mcp_write_tools.py` that mocks the PyGithub layer and asserts the contract (structured success envelope, cache invalidation, size-cap refusals).

## How to add a new REST endpoint

1. Define a Pydantic request model in `gitoma/api/routers.py` with `field_validator`s for every free-form string that could become argv.
2. Define a response model. Never return a raw `dict`.
3. Tag the endpoint — `tags=["jobs"]`, `tags=["system"]`, etc. — so it groups correctly in Swagger.
4. If it dispatches a CLI subprocess, go through `_dispatch` so you inherit the scrubbed env, process-group isolation, and job tracking.
5. Test it in `tests/test_api_industrial.py` with `TestClient` — happy path, 422 on bad input, 401/403/503 on auth.

## Commit conventions

Gitoma follows a simplified Conventional Commits style:

- `feat(<scope>): …` — new feature or non-trivial change.
- `fix(<scope>): …` — bug fix.
- `refactor(<scope>): …` — internal change, no behaviour change.
- `chore: …` — tooling, deps, housekeeping.
- `docs: …` — documentation only.
- `test: …` — tests only.

Scope is the subpackage — `cli`, `api`, `mcp`, `ui`, `worker`, `review`, etc. Keep the first line under ~70 chars; put the *why* in the body.

## Releasing

Gitoma is pre-1.0. Versioning is pinned in `pyproject.toml`. To cut a release:

1. Bump `version` in `pyproject.toml`.
2. Update `docs/` wherever behaviour changed.
3. Tag: `git tag v0.x.y && git push --tags`.

The docs deploy workflow runs on every push to `main`; Pages picks it up automatically.

## Questions, ideas, bug reports

Open an issue or a discussion on the repo. For security problems, use the private advisory flow — see [Security](./security#reporting-a-vulnerability).
