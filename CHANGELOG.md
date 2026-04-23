# Changelog

All notable changes to gitoma are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning is
[SemVer](https://semver.org/).

## [0.2.0] — 2026-04-23

The "guards stack + Occam feedback loop" release. Six fully-green
rung-3 PRs out of 27 attempts on the day, three of which were fully
engineered (every guard fired on its intended target). See
[`docs/architecture/critic-stack.md`](docs/architecture/critic-stack.md)
for the chronological narrative.

### Added

- **G7 — AST-diff guard** (`gitoma/worker/patcher.py`): every "modify"
  patch on a `.py` file must preserve all top-level functions and
  classes from the original. Wired into both worker retry loop and
  refiner apply path. Catches the rung-3 v17/v18 helper-deletion
  pattern (worker dropping `db` pytest fixture + sibling tests).
- **G8 — Runtime test regression gate** (`gitoma/worker/worker.py` +
  `gitoma/cli/commands/run.py`): captures a baseline failing-test set,
  re-runs tests after every subtask, reverts patches that introduce
  new failures. Wired to BOTH worker (with retry feedback) AND refiner
  (with v0 reset). Catches body-level semantic regressions that AST
  guards miss by design — rung-3 v20/v21 `row_factory` drop, v16/v24
  refiner `>`-injection in SQL string.
- **G9 — Deterministic post-plan filter**
  (`gitoma/planner/occam_filter.py`): drops subtasks whose
  `file_hints` overlap with paths failing ≥ threshold times in the
  Occam agent-log. Threshold via `GITOMA_OCCAM_FILTER_THRESHOLD`
  (default 2). Two-window design: planner prompt fetches 24h/20 (fresh
  bullets), G9 fetches 7d/200 (full failure history). Catches the
  rung-3 v24 pattern where soft prompt injection wasn't enough — 4B
  planner reshaped task title but kept identical `file_hints`.
- **`worker.subtask.failed` trace event**: visible JSONL event for
  every silent worker failure (LLM JSON-emit, all-patches-rejected,
  sensitive-path denylist, build/syntax/AST/test retry exhaustion,
  git refused). Replaces "diff state.json across runs" post-mortem
  workflow.
- **Q&A crash → PR body annotation**: the post-meta Q&A
  self-consistency phase now annotates the PR body with a ⚠ block
  when it crashes mid-flight ("Treat this PR as ungated"). Closes
  the silent-absence-≠-all-clear gap.
- **In-process JSON repair** (`gitoma/planner/llm_client.py`):
  `_attempt_json_repair` runs ONCE between `json.loads` failure and
  the LLM correction round-trip. Two passes: trailing-comma strip
  (string-aware) + bare-quote escape (state-machine). Saves ~30s of
  round-trip latency per recovered call on a 4B-class model.
- **Worker prompt SCOPE BOUNDARIES** (`gitoma/planner/prompts.py`):
  six numbered rules injected before the JSON schema — file-fence,
  no new top-level imports, no signature changes, no helper deletion
  (with WRONG/RIGHT examples), no phantom imports, minimal-change
  wins. Each rule cites its rung-3 evidence by name (sqlite3→psycopg2,
  `get_conn`/`init_schema`/`seed`).
- **Always-on patcher build-manifest hard-block**
  (`gitoma/worker/patcher.py`): build manifests (`pyproject.toml`,
  `go.mod`, `Cargo.toml`, `package.json`, `Gemfile`, `pom.xml`, …)
  are blocked unless either `compile_fix_mode=True` (always reject)
  or the planner explicitly allow-lists them via the subtask's
  `file_hints`.
- **Per-file post-write syntax check** (`validate_post_write_syntax`
  in `gitoma/worker/patcher.py`): TOML/JSON/YAML/Python parsers run
  on every touched file before the build check. Failures revert +
  retry with parser error injected as feedback.
- **Refiner-phase syntax check + AST-diff + test gate**: the refiner
  apply path now goes through G2/G6/G7/G8 just like the worker, with
  `phase="refiner"` tagged on the trace events. Closes the silent
  refiner-corruption path that shipped pre-v17.
- **Occam Observer P1 integration**
  (`gitoma/context/occam_client.py`): HTTP client to the local
  Occam Observer gateway. Two directions:
  - `POST /observation` after every subtask in
    `on_subtask_done`/`on_subtask_error` callbacks. Payload includes
    `run_id` (= branch name), `agent`, `subtask_id`, `model`,
    `outcome` (success/fail/skipped), `touched_files`,
    `failure_modes` (closed-set 11-label enum), `confidence`.
  - `GET /repo/agent-log?since=24h&limit=20` right before
    `planner.plan()`, rendered into the planner user prompt as
    `== PRIOR RUNS CONTEXT ==`.
  - Silent fail-open contract on every network / schema / gateway
    error. `OCCAM_URL` env var unset → feature off, pipeline runs
    unchanged.
- **`docs/architecture/critic-stack.md`**: chronological catalogue
  of every guard with its rung-3 evidence, topology diagram, and
  composability model. Living document.
- **Bench ladder** (separate repo
  [`gitoma-bench-ladder`](https://github.com/fabriziosalmi/gitoma-bench-ladder)):
  rungs 0–4 with deterministic synthetic bugs across Python /
  Go / Rust / JS, immutable scorecards on `main`, GitHub Pages
  dashboard. 27 PRs / 8 scorecards from today's iteration.

### Environment knobs

All new vars are opt-in / default-off. Documented in `SSOT.md`.

- `OCCAM_URL` — Occam gateway URL (e.g. `http://127.0.0.1:9999`).
  Unset = feature off.
- `GITOMA_OCCAM_FILTER_THRESHOLD` — G9 fail-count threshold
  (default 2, clamped ≥ 1).
- `GITOMA_TEST_REGRESSION_GATE=off` — disable G8 runtime test gate.
- `LM_STUDIO_DISABLE_THINKING=true` — append `/no_think` to last
  user message (Qwen3 family soft-switch, ~30× speedup).
- `LM_STUDIO_TIMEOUT` — per-call HTTP timeout (default 120s,
  clamped 10–600).
- `GITOMA_WORKER_BUILD_RETRIES` — retries after compile/syntax/
  AST/test-regression failure (default 1, i.e. 2 attempts total).

### Trace events

New events emitted to `~/.gitoma/logs/<slug>/<timestamp>-run.jsonl`:

- `worker.subtask.failed` — visibility for every subtask failure
- `critic_syntax_check.fail` — G2/G6 fired (carries `phase` =
  `worker` or `refiner`)
- `critic_ast_diff.fail` — G7 fired
- `critic_test_regression.fail` — G8 fired (carries `phase`)
- `critic_build_retry.success` — G2/G7/G8 retry recovered
- `plan.occam_filter` — G9 dropped subtasks at plan time
- `critic_qa.crashed` — Q&A phase exception (paired with PR-body
  annotation)

### Tests

- 744 passing (was 597 at 0.1.0). +147 new tests covering every guard
  in isolation + the end-to-end rung-3 scenarios.

### Known gaps (open observations)

- **Intra-run sequencing**: `.gitignore`-creating subtask runs first;
  later subtasks try to write paths the new `.gitignore` blocks →
  git refuses. G9 (inter-run) and Occam (inter-run) don't help.
  Candidate next guard.
- **qwen3-8b TOML authoring ceiling**: same TOML mistakes recur
  (`Invalid value at line N col M`); G2 catches them but the worker
  rarely recovers on retry. Switch worker model or bigger param
  count would help more than another guard.
- **JSON-emit failures**: ~10–20% of subtasks on qwen3-8b. JSON
  repair helps some; the rest need either prompt strengthening or
  a more reliable model.

## [0.1.0]

Initial release.
