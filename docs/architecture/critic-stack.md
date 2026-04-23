# Critic stack — composable guards against LLM patch slop

> Living document. Every guard here was added to fix a specific class of
> failure observed live on the [gitoma-bench-ladder](https://github.com/fabriziosalmi/gitoma-bench-ladder)
> repo. New guards are added in the same shape: name the failure, name
> the guard, link the bench evidence.

## What this document is

The bench ladder is gitoma's stochasticity treadmill: a series of
synthetic repos (rung-0 through rung-N) with known bugs, run repeatedly
through gitoma with different worker models. Every PR is scored against
a `golden.json` and committed as an immutable scorecard on the ladder
repo's `main` branch.

This doc is the engineering counterpart: for every scorecard regression
we shipped a guard. The order is chronological — each guard was a
response to the prior bench's worst remaining failure.

## Topology of guards

```
                    ┌─────────────────────────────────────────────┐
                    │  WORKER PROMPT                              │
                    │  ─ SCOPE BOUNDARIES (6 rules + WRONG/RIGHT) │
                    └─────────────────┬───────────────────────────┘
                                      │ LLM call
                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │  LLMClient.chat_json                        │
                    │  ─ JSON repair (trailing commas, bare        │
                    │     quotes, bare newlines)                   │
                    │  ─ /no_think suffix (Qwen3 family)           │
                    └─────────────────┬───────────────────────────┘
                                      │ patches dict
                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │  patcher.apply_patches                      │
                    │  ─ Containment, denylist, size cap          │
                    │  ─ Build-manifest hard-block (always-on,    │
                    │     opt-in via subtask.file_hints)          │
                    └─────────────────┬───────────────────────────┘
                                      │ touched paths
                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │  patcher.validate_post_write_syntax         │
                    │  ─ Per-extension parse: TOML, JSON, YAML,    │
                    │     Python (compile)                         │
                    │  ─ On fail: revert + retry-with-feedback     │
                    └─────────────────┬───────────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │  worker._post_write_build_check             │
                    │  ─ Cross-file analyzer (BuildAnalyzer)       │
                    │  ─ Same revert+retry loop as syntax check    │
                    └─────────────────┬───────────────────────────┘
                                      │ commit + on to next subtask
                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │  Critic phases (panel, devil, refiner, Q&A) │
                    │  ─ refiner: SAME syntax check applied to    │
                    │     its output before commit (rung-3 v17)    │
                    │  ─ Q&A: round-trip Defender, gated apply     │
                    └─────────────────────────────────────────────┘
```

## Observability primitives

Two universal events hold the stack visible:

| Event | Emitted when | Why it matters |
|-------|--------------|----------------|
| `worker.subtask.failed` | A subtask raises (any cause: JSON parse, patcher rejection, syntax/build retry exhaustion, git refusal) | Pre-2026-04-23 these failures only landed in `state.json`, invisible in the trace JSONL. Reviewers had no way to grep failure modes across runs. |
| `critic_syntax_check.fail` | `validate_post_write_syntax` rejects a patch | Carries `path`, `error`, `attempt`, and `phase` (`worker` or `refiner`). The `phase` tag was added when v16 caught the refiner silently shipping a corrupted file the worker had written correctly. |

Both events compose with the existing critic events
(`critic_panel.review.end`, `critic_devil.finding`, `critic_refiner.kept`,
`critic_qa.answers`) to give a complete picture of why a subtask landed
or failed.

## Guard catalogue (chronological)

Each entry: **what it catches**, **how it works**, **bench evidence**.

### G1 — Build-manifest hard-block

**Catches**: worker over-scoping into build manifests (`pyproject.toml`,
`go.mod`, `Cargo.toml`, `package.json`, `Gemfile`, `pom.xml`, etc.) on
subtasks that didn't ask for it.

**How**: `patcher.apply_patches(allowed_manifests=...)` rejects any
`BUILD_MANIFESTS` filename unless either (a) `compile_fix_mode=True`
forces ALWAYS-reject (the run is supposed to restore source, not
reshape deps), or (b) the planner explicitly puts the manifest filename
in the subtask's `file_hints`, which the worker passes through as the
allow-list.

**Evidence**: rung-3 v11 — worker's correct SQLi fix in `src/db.py`
shipped alongside collateral damage to `pyproject.toml` (broken at
line 19); pytest config-parse failed before any test could run.
Without this guard, scope drift into manifests was the dominant
failure family. Default-on rejection (since rung-3 v11) closed it.

### G2 — Per-file post-write syntax check

**Catches**: syntactically-invalid manifest / config / source files the
LLM wrote, including the planner-SANCTIONED case where a manifest edit
was allowed but the LLM's TOML was still malformed.

**How**: `patcher.validate_post_write_syntax(root, touched)` routes by
extension to a stdlib parser:

- `.toml` → `tomllib`
- `.json` → `json`
- `.yml` / `.yaml` → `yaml.safe_load` (skipped if PyYAML absent)
- `.py` → builtin `compile()`

Returns `(rel_path, parser_msg)` on the FIRST failure, or `None` on
clean. Wired into `worker._apply_with_build_retry` BEFORE the
BuildAnalyzer pass. Treated identically to a build failure: revert +
retry with the parser error injected as feedback.

**Evidence**: rung-3 v12 — planner-SANCTIONED T004 edit on
`pyproject.toml` shipped `source = src` (bare identifier instead of
quoted string). The manifest sanction allow-list let it through; no
parser ran at write time; pytest config-parse failed at runtime.
The post-write syntax check intercepts regardless of sanction state.
v17 caught the same TOML error on retry, then qwen3-8b actually
fixed it on attempt 2 (`critic_build_retry.success`) — first time
the parser-feedback retry recovered.

### G3 — `worker.subtask.failed` trace event

**Catches**: silent worker failures (LLM JSON-emit failure,
all-patches-rejected, syntax/build retry exhaustion, git refusal).

**How**: `run.py`'s `on_subtask_error` callback emits a
`worker.subtask.failed` trace event with `task_id`, `subtask_id`,
`title`, `file_hints`, and the truncated error string. Wrapped in
`try/except` so a trace failure can never kill the run.

**Evidence**: rung-3 v12 — T001-S01 + T001-S02 both failed with
`Could not obtain valid JSON from LLM after 3 attempts` but the
JSONL trace had ZERO worker events. Only `state.json` recorded the
errors. Post-mortem cost was "diff state.json across runs"; now it's
"grep `worker.subtask.failed` in jsonl". v13 immediately surfaced
4 distinct failure modes (JSON-emit, sensitive-path denylist,
syntax-retry-exhausted, unsanctioned-manifest).

### G4 — Worker prompt SCOPE BOUNDARIES

**Catches**: worker over-scoping a "fix bug X in function Y" subtask
into "rewrite the entire module to use a different driver".

**How**: a six-rule block injected into the worker user prompt
BEFORE the JSON schema (so the model has the constraints
internalised before it composes patches):

1. File-fence — don't patch outside `Files to touch`.
2. No new top-level `import`/`use`/`require` unless the task
   explicitly requests a new dependency.
3. No public function signature changes.
4. Don't delete unrelated helpers.
5. Cross-module imports must reference symbols that ACTUALLY EXIST.
6. Minimal-change wins.

Each rule cites its rung-3 evidence by name (e.g. rule 2 names
`sqlite3` → `psycopg2`; rule 4 names `get_conn`/`init_schema`/`seed`).

**Evidence**: rung-3 v13 — qwen3-8b interpreted "Verify Test
Coverage for SQL Injection Fix" as "rewrite db.py from stdlib
sqlite3 to psycopg2". The patch was syntactically valid, the SQL
was correctly parameterised, devil even confirmed the fix — but
psycopg2 wasn't installed and the worker had also deleted helpers
the test fixture relies on. v14 with the scope-fence in place:
worker stayed on stdlib sqlite3 (rule 2 held).

### G5 — Rule 4 WRONG/RIGHT examples

**Catches**: worker emitting a "modify" patch that drops helper
functions because its rewrite "didn't need them", then waving at
the deletion with a comment like `# init_schema and seed remain
unchanged` ABOVE a file body where they're nowhere to be found.

**How**: rule 4 of the SCOPE BOUNDARIES block now contains literal
WRONG and RIGHT example blocks. The WRONG block reproduces the
v14 actual output (the lying comment annotated `← LIE`). The
RIGHT block shows every helper preserved verbatim with the edited
function as the only change. Anti-comment-as-substitute language
follows: *"If you write a comment like `# X remains unchanged` and
X is NOT in your file content above the comment, you are lying.
The parser doesn't read comments; tests will fail at import time."*

**Evidence**: rung-3 v14 → v15. v14 had rule 4 in abstract form;
qwen3-8b read it, agreed, then deleted helpers anyway. v15 with
WRONG/RIGHT examples: same model, same task, every helper
preserved verbatim. **First fully-passing rung-3 PR** (4/4 tests
green). Same WRONG/RIGHT pattern that hardened the Defender
prompt (commit `fc4365e`) ports cleanly to the worker prompt.

### G7 — AST-diff guard (top-level def preservation)

**Catches**: "modify" patches that drop top-level functions / classes
from a Python file. Exactly the rung-3 v14/v17/v18 pattern: worker
emits new content for a file, drops helpers its rewrite "doesn't
need", often with a lying comment like `# X remains unchanged`. New
file parses cleanly → syntax check silent → import/collection breaks
at runtime.

**How**: two new helpers in `patcher.py`:

- `read_modify_originals(root, patches) -> dict[str, str]` captures
  BEFORE-write content of every modify target (pure read).
- `validate_top_level_preservation(root, touched, originals) ->
  tuple[str, set[str]] | None` AST-parses both versions, returns
  `(path, missing_names)` on the FIRST missing top-level def.

Scope: top-level `FunctionDef` / `AsyncFunctionDef` / `ClassDef`
only. Assignments and class methods are deliberately out of scope
(too noisy across legitimate edits). Wired into BOTH the worker's
retry loop AND the refiner's apply path with `critic_ast_diff.fail`
trace events carrying `phase` = `"worker"` or `"refiner"`.

**Evidence**: rung-3 v22 — T001-S03 attempts 1 and 2 both emitted
`tests/test_db.py` missing the `db` fixture + 3 sibling tests.
Retry exhausted, subtask cleanly failed, test file stayed intact.

### G8 — Runtime test regression gate

**Catches**: body-level semantic regressions that every static guard
misses by design. The worker changes a function body, the signature
stays intact (so G7 silent), the file compiles (so BuildAnalyzer
silent), the syntax is valid (so G2 silent) — but the runtime
behaviour breaks a test. Rung-3 v20/v21 dropped
`conn.row_factory = sqlite3.Row` from `get_conn`; caller's
`dict(row)` blew up with `TypeError`. Rung-3 v19 injected a stray
`>` into an SQL string literal; `sqlite3.OperationalError` at DDL
execution.

**How**: `analyzers/test_runner.py:detect_failing_tests` is a
standalone function that reuses the per-language runner + parsers
and returns `set[str] | None` of currently-failing test
identifiers. Worker instance holds a lazy baseline captured before
the first subtask reaches the gate. After each subtask's
apply+syntax+AST+build all pass, re-run tests and compute
`current - baseline`:

- Non-empty set → `critic_test_regression.fail` event + revert +
  retry-with-feedback listing the broken tests.
- Empty set → baseline advances forward (legitimate fixes move
  tests OUT of baseline without triggering false positives).

Returns `None` when toolchain missing / timeout / no recognised
stack → gate silently skipped for the run. Opt-out via
`GITOMA_TEST_REGRESSION_GATE=off`.

**Evidence**: rung-3 v22 (both G7 and G8 live) — T001-S02 attempt 1
broke `test_find_known_user` (row_factory drop). G8 fired →
feedback injected → worker preserved row_factory on attempt 2 →
`critic_build_retry.success`. Same model, same prompt; runtime
feedback forced the fix. End result: **4/4 tests passing,
engineered rather than lucky**.

### G9 — Deterministic post-plan filter against Occam failure history

**Catches**: subtasks whose `file_hints` overlap with paths that have
failed repeatedly in prior runs, blocking them at plan time before the
worker even tries. Rung-3 v24 showed that soft prompt injection alone
(the PRIOR RUNS CONTEXT block) is too gentle — 4B planner read the
log, rephrased the subtask title, kept identical `file_hints`.

**How**: after `planner.plan()` returns, fetch a wide agent-log slice
(`since=7d limit=200`), build a `{path: fail_count}` counter via
`count_failed_hints`, call
`filter_plan_by_failure_history(plan, counter, threshold=2)` which
mutates the plan in place — dropping subtasks whose max-over-hints
count ≥ threshold, and dropping tasks that lose all their subtasks.
Emits `plan.occam_filter` with the summary `{filtered_subtasks,
tasks_dropped, kept_subtasks, total_subtasks, threshold}`. Threshold
via `GITOMA_OCCAM_FILTER_THRESHOLD`, default 2 (clamped ≥ 1).

Two-window design: the planner prompt uses the narrow 24h/20 slice
(fresh for display, <= 15 bullets rendered); G9 uses the wide
7d/200 slice (failure patterns from yesterday are still diagnostic).
Caught live v26b: narrow 24h/20 missed older CI-workflow fails that
had fallen out of the recent-20 slice due to successes pushing them
out.

**Evidence**: rung-3 v27 — `plan.occam_filter` dropped T001-S02
(`tests/test_db.py`, count=3) and T004-S01
(`.github/workflows/ci.yml`, count=5) at plan time. Worker never
tried either. 10 kept / 12 total. Only 1 worker.subtask.failed
(T002-S03 transient JSON-emit). Cleanest rung-3 run of the day.

### G6 — Refiner-phase syntax check

**Catches**: refiner's apply path silently corrupting working code.
The refiner runs after the panel + devil and emits patches against
devil findings; its output went straight to `git commit` without
any syntax validation.

**How**: `validate_post_write_syntax` extended to cover `.py` via
builtin `compile()`. `run.py`'s refiner block invokes the helper
after `apply_patches`. On failure: emit `critic_syntax_check.fail`
with `phase="refiner"`, hard-reset the worktree to v0, emit
`critic_refiner.reverted` with rationale `syntax_check_failed:
<path>: <error>`, and skip meta-eval entirely (a broken patch can
never be a refinement, no judge needed).

**Evidence**: rung-3 v16 — refiner changed a Python triple-quote
opener to an empty-string-plus-bare-bracket sequence in
`src/db.py`, breaking pytest collection. The corruption shipped
because refiner bypasses worker.py's `_apply_with_build_retry`.
v17 with G6 in place: same identical bug intercepted —
`critic_syntax_check.fail phase=refiner` fired on src/db.py line
17, reverted to v0, src/db.py final state = correct worker SQLi
fix preserved.

## Q&A self-consistency phase (orthogonal to the stack)

Not a guard against worker slop — a separate post-meta gate that asks
the patch three structured questions and gates revisions on a
BuildAnalyzer + test pass. Touched today only for visibility:

- **Crash → PR body annotation** (commit `a5f0ce3`). Until now, a
  Q&A phase that crashed mid-flight produced an empty PR-body
  section that looked identical to a successful Q&A pass.
  Reviewer had no flag. The crash branch now emits a
  `## ⚠️ Q&A self-consistency phase CRASHED` block with the first
  line of the crash reason and "Treat this PR as ungated"
  language.
- See [Q&A workflow](./qa-workflow.md) (TBD) for the full phase.

## In-process JSON repair (orthogonal)

`_attempt_json_repair` runs ONCE between the first `json.loads`
failure and the retry-with-correction-turn path. Two string-aware
passes:

1. `_strip_trailing_commas` — drop `,` immediately preceding `}`/`]`,
   string-aware so content like `"hello,}"` survives.
2. `_escape_bare_quotes` — escape unescaped quotes inside string
   values. The opener is the first `"` after structural punctuation;
   the closer is a quote followed by `,`/`:`/`}`/`]`/EOF. Quotes
   between opener and closer that don't qualify as closers get
   backslash-escaped. Also escapes raw newlines inside strings.

Saves ~30s of round-trip latency per recovered call on a 4B-class
model. Idempotent on already-valid JSON.

## Bench progression — rung-3 over the day

| Run | Worker | patch | tests | Notes |
|-----|--------|-------|-------|-------|
| v3 | gemma | ✅ | ❌ | 1-tuple bug `(name)` instead of `(name,)` |
| v11 | qwen8b | ✅ | ❌ | Correct fix + `pyproject.toml` collateral (G1 not yet shipped) |
| v12 | qwen8b | ❌ | ❌ | Silent JSON-emit failures (G3 not yet shipped) |
| v13 | qwen8b | ✅ | ❌ | Parameterised but psycopg2 over-scope (G4 not yet shipped) |
| v14 | qwen8b | ✅ | ❌ | sqlite3 kept BUT helpers deleted (G5 not yet shipped) |
| **v15** | qwen8b | ✅ | **✅** | **4/4 GREEN — first fully passing rung-3 PR (partly lucky)** |
| v16 | qwen8b | ✅ | ❌ | Refiner corrupted `"""` → `""` (G6 not yet shipped) |
| v17 | qwen8b | ✅ | ❌ | Refiner gap CLOSED, but T002 ate test fixtures |
| v18 | qwen8b | ✅ | ❌ | Same test-file rule-4 violation as v17 (2/2 — systematic) |
| v19 | qwen8b | ✅ | ❌ | Helpers preserved, but `>` in SQL string → runtime OperationalError |
| v20 | qwen8b | ✅ | 3/4 | `row_factory` dropped from `get_conn` body |
| v21 | qwen8b | ✅ | 3/4 | Same row_factory loss — stochastic repeat (G7 silent — no AST violation) |
| **v22** | qwen8b | ✅ | **✅** | **4/4 GREEN — ENGINEERED (G7 + G8-worker both fired live + recovered via retry)** |
| v23 | qwen8b | ✅ | ❌ | Mac first run with Occam; CRITIC_PANEL_DEVIL=false (no .env) → devil/refiner/Q&A silent; 14 observations POSTed to Occam |
| v24 | qwen8b | ✅ | ❌ | Occam read+write live (planner injected 15 prior entries); refiner injected `>` into init_schema SQL string — G8 gap on refiner path |
| **v25** | qwen8b | ✅ | **✅** | **4/4 GREEN — G8-on-refiner caught the v24 regression live (`phase=refiner` fired, reverted to v0)** |
| **v26b** | qwen8b | ✅ | **✅** | **4/4 GREEN — G9 partial fire (narrow window missed CI pattern) + G8-on-refiner catch** |
| **v27** | qwen8b | ✅ | **✅** | **4/4 GREEN — G9 full coverage (7d/200 window) dropped 2/12 subtasks at plan time + G8-on-refiner. Only 1 worker fail (transient). Cleanest run yet.** |

## Open problems (as of 2026-04-23 PM end-of-day)

### O1 — Helper deletion in test files — ✅ CLOSED by G7

Worker drops `db` fixture + sibling tests when emitting
`tests/test_db.py`. G7 AST-diff fires on the first attempt and
forces a retry; if the worker repeats the same deletion, retry
exhausts and the subtask cleanly fails with the test file
untouched. Validated live rung-3 v22.

### O2 — qwen3-8b TOML authoring ceiling

qwen3-8b emits invalid TOML on first attempt with high consistency
(observed in v12, v13, v14, v17 — same `Invalid value` errors at
`line 8 col 88` or `line 22 col 10`). The post-write syntax check
catches it; the file stays clean. v17 saw the first ever
`build_retry.success` after a TOML failure (worker fixed duplicate
`[build-system]` on attempt 2), so feedback CAN work — but it's
sporadic.

### O3 — Worker silent JSON-emit failures

qwen3-8b + `/no_think` occasionally fails to produce parseable JSON
even with the in-process repair. Visible as `worker.subtask.failed`
with error `Could not obtain valid JSON from LLM after 3 attempts`.
Dominant residual failure family on rung-3 today.

### O4 — Body-level semantic regression — ✅ CLOSED by G8 (worker) + G8-on-refiner

Worker preserves function signatures but rewrites bodies in ways
that break callers (e.g. drops `row_factory = sqlite3.Row`; injects
stray characters into string literals). G8 runtime test gate
catches this at the only layer where it's visible: running tests.

G8 was originally wired only to the WORKER apply path. Rung-3 v24
(first Mac run with Occam feedback loop) exposed the gap: the
REFINER phase has its own apply path without G8. Refiner injected
a stray `>` into `init_schema`'s SQL string — valid Python, valid
AST, but sqlite3 errors at DDL execution → 4/4 tests broken.
Fixed in commit `c2e3af6`: refiner apply captures a test baseline
before its patches, re-runs tests after syntax+AST both pass,
and resets to v0 + skips meta-eval when regressions are detected.

Validated live rung-3 v25 — `critic_test_regression.fail
phase=refiner sample=tests/test_db.py::test_find_known_user
total_count=4` fired, refiner reverted, worker's correct fix
shipped. 4/4 tests green.

## Feedback loop integration (Occam Observer)

Starting commit `49c1d57` gitoma speaks to a separate Go gateway
(Occam Observer) that aggregates observations across runs. Two
directions:

- **WRITE — `POST /observation`** after every subtask in
  `on_subtask_done` / `on_subtask_error`. Payload:
  `{run_id (=branch), agent:"gitoma", subtask_id, model,
  outcome (success|fail|skipped), touched_files, failure_modes,
  confidence}`. `failure_modes` is a closed set of 11 labels
  (`json_emit`, `ast_diff`, `test_regression`, `syntax_invalid`,
  `denylist`, `manifest_block`, `patcher_reject`,
  `build_retry_exhausted`, `git_refused`, `json_parse_bad`,
  `unknown`) mapped from error strings by
  `map_error_to_failure_modes`.

- **READ — `GET /repo/agent-log?since=24h&limit=20`** right before
  `planner.plan()`. Results render into a `== PRIOR RUNS CONTEXT ==`
  block in the planner user prompt, grouped by outcome (FAILED
  first since that's the actionable "don't re-propose" signal).

Validated end-to-end rung-3 v23 → v25: 31 observations landed
across 3 runs, 4 distinct failure_modes surfaced live, planner
console announced `Occam: injected N prior-runs entries into
planner context` on each run after the first.

Observed limitation (v24): soft prompt injection ("AVOID these
patterns") is too gentle for a 4B-class planner. It reshapes the
subtask title but keeps identical `file_hints`, so the worker hits
the same slop. Deterministic post-plan filter (reject subtasks
whose file_hints overlap with recent-failed file_hints) is the
likely next guard.

Feature off when `OCCAM_URL` env var is unset. Client
fail-open on every network / schema / gateway error — gitoma
pipeline runs unchanged without Occam.

## Composability model

The guards are layered, not optional. Every patch flows through:

```
worker prompt (G4+G5)
  → LLM call (JSON repair)
    → patcher (G1)
      → syntax check (G2+G6)
        → BuildAnalyzer
          → critic phases
```

Each layer catches a specific shape of failure. A single layer is
necessary but not sufficient — v15's first-ever fully-green PR
required all five layers shipped at the time. A regression in any
layer (e.g. a new model that produces a new failure shape) doesn't
require redesigning others; we just add a new guard alongside the
existing ones.

## Revision history

| Date | Run | Guards added |
|------|-----|--------------|
| 2026-04-23 AM | v12 launch | G1 (manifest hard-block always-on) |
| 2026-04-23 AM | v13 launch | G2 (syntax check), G3 (`worker.subtask.failed`) |
| 2026-04-23 PM | v14 launch | G4 (SCOPE BOUNDARIES) |
| 2026-04-23 PM | v15 launch | G5 (rule 4 WRONG/RIGHT) — **first 4/4 green** |
| 2026-04-23 PM | v17 launch | G6 (refiner syntax check, `.py` coverage) |
| 2026-04-23 PM | (orthogonal) | Q&A crash → PR annotation, JSON repair |
| 2026-04-23 PM | v22 launch | G7 (AST-diff top-level preservation) + G8 (runtime test regression gate) — **first 4/4 green ENGINEERED**: both guards fired live, retry recovered the row_factory regression |
| 2026-04-23 PM | v23 launch | Occam Observer P1 integration (commit `49c1d57`) — `POST /observation` after every subtask + `GET /repo/agent-log` pre-planner. Feature off when `OCCAM_URL` unset. |
| 2026-04-23 PM | v25 launch | G8 extended to refiner apply path (commit `c2e3af6`) — `critic_test_regression.fail phase=refiner` → v0 reset. Caught the v16/v24 `>`-in-SQL-string pattern that G6/G7 miss by design. **Third 4/4 green ENGINEERED run.** |
| 2026-04-23 PM | v27 launch | G9 deterministic post-plan filter (commits `e2e9a04` + `a17ebc3` + `d7ee293`) — drops subtasks with recently-failing `file_hints` at plan time. Wider 7d/200 window than planner prompt (24h/20). **Cleanest rung-3 run of the day** — 1 worker fail vs usual 3-5. |
