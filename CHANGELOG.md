# Changelog

All notable changes to gitoma are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning is
[SemVer](https://semver.org/).

## [0.4.0] — 2026-04-25

The "planner-time discipline" release. Three new guards (G12, G13, G14)
extend the critic stack to cover npm-config grounding + doc preservation
+ URL/path reachability — the recurring hallucination shapes that
survived v0.3.0 in real-world b2v PRs #21/#24/#26/#27. The headline
addition is a pair of LLM-free planner-time deterministic
post-processors (Layer-A + Layer-B) that close the planner-side root
cause: "Update README"-style subtasks are now banished BEFORE the
worker ever sees them, and missing real-bug T000 tasks are synthesized
when the planner ignored failing tests.

Replay matrix on b2v across qwen3-8b / gemma-4-e4b / gemma-4-e2b
confirmed 3 of 3 PRs ship clean (vs 0 of 3 morning baseline) with
README untouched and zero hallucinated link targets.

### Added

- **Plan-time deterministic post-processors (Layer-A + Layer-B)**
  (`gitoma/planner/real_bug_filter.py`): two LLM-free
  transformations applied after `planner.plan()` returns and
  before worker apply.
  - **Layer-A `synthesize_real_bug_task`**: when `test_results`
    metric is failing AND the planner's plan doesn't touch the
    source-under-test (mapped from failing test paths via
    existing `infer_source_files_from_tests`), prepend a
    synthesized priority-1 `T000` task that does. Closes the
    rung-0 pattern where the planner emits 12 generic-project
    subtasks instead of fixing the actual broken file.
  - **Layer-B `banish_readme_only_subtasks`**: drop every
    subtask whose file_hints list contains ONLY README variants
    (`README.md`/`README.rst`/`README`/`Readme.md`), unless
    Documentation metric is failing AND its details explicitly
    cite README. Catches the b2v PR #24/#26/#27 root cause: 3
    of 4 shipped PRs across all model sizes contained
    "Update README"-style subtasks the worker then mishandled.
    Multi-file_hint subtasks (README + code file) are kept.
  - Plus planner-prompt **HARD RULE — README IS A CONSEQUENCE,
    NOT A GOAL** in `prompts.py`. Closes the principle in code:
    README updates derive from code changes, they're not a
    primary planning target.
  - New trace events: `plan.real_bug_synthesized`,
    `plan.readme_banished`. Both deterministic, sub-10ms.

- **G14 — URL/path grounding against fabricated link targets**
  (`gitoma/worker/url_grounding.py`): closing piece of the content-
  grounding trilogy (G11 frameworks, G12 npm refs, G13 code-block
  preservation). On MODIFY of doc files (`.md`/`.mdx`/`.rst`/
  `.txt`), validates added external URLs via two-tier DNS → HEAD
  check (catches `*.github.io` subdomains that wildcard-DNS-
  resolve but return HTTP 404, the b2v PR #24 case) AND added
  Markdown link targets via filesystem existence check (catches
  invented `docs/guide/code/*` paths, the b2v PR #27 case).
  Carry-over URLs/paths exempt — only validates what the worker
  added. Fail-open on transient errors; opt-out via
  `GITOMA_URL_GROUNDING_OFFLINE=true` for sandboxed CI envs.
  Wired both worker and refiner apply paths. New trace event
  `critic_url_grounding.fail`.

- **G13 — Doc-preservation against fenced code-block destruction**
  (`gitoma/worker/doc_preservation.py`): two deterministic checks
  on MODIFY operations against `.md`/`.mdx`/`.rst`/`.txt` files —
  (a) code-block char-count preservation (flag when new < 30% of
  original AND original ≥ 50 chars), (b) literal `\n` corruption
  detection (flag when 2+ literal `\n` text on same line inside a
  fenced block, the JSON-double-escape signature). Catches the
  recurring b2v PRs #24/#26/#27 README destruction that all model
  sizes (gemma-2B/4B, qwen3-8B) produced and that self-review only
  caught 1 of 4 times. Pure-string, no LLM, no parsing libraries.
  Wired both worker and refiner apply paths. New trace event
  `critic_doc_preservation.fail`.

- **G12 — Config-grounding for JS/TS configs**
  (`gitoma/worker/config_grounding.py`): JS/TS config files
  (47-basename closed set: prettier, eslint, tailwind, vite,
  webpack, jest, playwright, next, nuxt, astro, svelte, postcss,
  babel) get their package references checked against
  `fingerprint['declared_deps']['npm']`. Three extractors cover
  `require()`, `import-from`, and `plugins/presets: [...]`
  arrays. Normaliser handles `@scope/pkg/sub` and `lodash/fp` →
  base package; 84-entry Node-builtin set + relative paths are
  skipped. Catches the b2v PR #21 second-half: generated
  `prettier.config.js` referenced `prettier-plugin-tailwindcss`
  while `package.json` only declared `vitepress`. G11 caught the
  doc hallucination; G12 catches the symmetric config one.
  Wired both worker and refiner apply paths. Live-validated
  against the b2v fingerprint via `/repo/fingerprint`. First
  live fire on b2v PR #28 replay (qwen3-8b, 2026-04-25 PM):
  caught package-ref mismatch in `prettier.config.js`.

- **Markdown fence-strip + opt-in chat_template_kwargs**
  (`gitoma/planner/llm_client.py`): `_strip_markdown_fences`
  added as the first pass of `_attempt_json_repair` — strips a
  single ` ```{lang}? ... ``` ` wrapper before running existing
  trailing-comma + bare-quote repair. Coder/instruct fine-tunes
  (gemma-4-e4b, qwen2.5-coder-14b, llama3.1-8b-leetcoder)
  routinely wrap JSON in fences despite "no markdown" system
  prompts; without this, `json.loads` raised and the worker
  burned 3 retry attempts. Idempotent on already-clean strings.
  Plus opt-in `LM_STUDIO_DISABLE_THINKING_TEMPLATE_KWARG` env
  var that sends `extra_body={"chat_template_kwargs":
  {"enable_thinking": false}}` on the OpenAI call — the Jinja-
  template kill-switch for GLM/Gemma/some-Qwen variants and
  vLLM/Together backends. Gated separately from the existing
  `LM_STUDIO_DISABLE_THINKING` (`/no_think` suffix) because LM
  Studio's OpenAI-compat shim chokes on the unknown `chat_
  template_kwargs` field for medium prompts.

### Trace events

- `plan.real_bug_synthesized` — Layer-A synthesized a T000 task
- `plan.readme_banished` — Layer-B dropped README-only subtask(s)
- `critic_doc_preservation.fail` — G13 fired (carries `phase` =
  `worker` or `refiner`)
- `critic_url_grounding.fail` — G14 fired (carries `phase`)
- `critic_config_grounding.fail` — G12 fired (carries `phase`)

### Environment knobs (new)

- `GITOMA_URL_GROUNDING_OFFLINE=true` — skip G14 DNS/HEAD
  checks (sandboxed CI envs)
- `LM_STUDIO_DISABLE_THINKING_TEMPLATE_KWARG=true` — opt-in
  `chat_template_kwargs` injection for backends that accept it

### Tests

- 950 passing (was 822 at v0.3.0). +128 new tests across:
  - `test_config_grounding.py` (37) — G12
  - `test_llm_json_repair.py` (+9 fence-strip cases)
  - `test_doc_preservation.py` (20) — G13
  - `test_url_grounding.py` (42) — G14
  - `test_real_bug_filter.py` (20) — Layer A+B

### Replay validation (b2v, 3 models, post-ship)

| Model | Morning baseline | Tonight (post-ship) |
|-------|------------------|---------------------|
| qwen3-8b | PR #27 closed (README destroyed, `\n` literal) | PR #28 clean — Layer-B + G12 + G14 fired live |
| gemma-4-e4b | PR #26 closed (bash blocks deleted) | PR #29 clean — Layer-B + G14 |
| gemma-4-e2b | PR #25 borderline | PR #30 perfect (4/4 subtasks, G9 dropped 6) |

3 of 3 PRs shipped with README untouched (vs 0 of 3 in morning
baseline). G14 + G12 had their first live fires ever. Empirical
validation for the architectural pivot.

## [0.3.0] — 2026-04-23

The "ground truth" release. Two new guards (G10, G11) extend the
critic stack to cover the failure modes that survived v0.2.0 in
real-world b2v PRs: valid-but-wrong-shape configs (G10) and
hallucinated documentation content (G11). G11 is the
gitoma-side half of a new two-sided integration with Occam
Observer, which gains a dedicated `/repo/fingerprint` endpoint
serving as ground truth for both the planner prompt and the
worker apply loop.

### Added

- **G10 — Semantic config schema validator**
  (`gitoma/worker/schema_validator.py`): bundled ~860KB of
  schemastore.org schemas (ESLint, Prettier, package.json, tsconfig,
  github-workflow, dependabot, Cargo). On every apply, files matching
  `PATH_MATCHERS` are validated against their bundled schema via
  `jsonschema`. Custom YAML loader excludes `on/off/yes/no` from the
  bool resolver so GitHub Actions `on:` keys stay strings. Wired both
  worker and refiner apply paths. Catches the b2v PR #19 case:
  `.eslintrc.json` with `parser` as object instead of string —
  parses as valid JSON (G2 silent), but ESLint refuses it.
- **G11 — Content-grounding via Occam `/repo/fingerprint`**
  (`gitoma/worker/content_grounding.py` + new endpoint in Occam):
  doc files (`.md/.mdx/.rst/.txt`) are checked against the verified
  "what is this repo" snapshot. A doc that mentions a framework
  absent from `declared_frameworks` and `declared_deps` (across all
  languages) triggers revert+retry. 42-pattern map covers React,
  Vue, Angular, Django, FastAPI, Clap, Cobra, etc. Two-sided
  integration: the same fingerprint also feeds the planner prompt
  as `== REPO FINGERPRINT (GROUND TRUTH — verified by Occam) ==`,
  pre-empting hallucinated tasks at plan time. Wired both worker
  and refiner apply paths. Catches the b2v PR #21 case: a generated
  `architecture.md` claiming React+Redux+WebSocket frontend in a
  pure-Rust CLI repo — every prior guard silent because the file
  parses, isn't a config, isn't Python, doesn't break build/tests.
- **Occam Observer — `GET /repo/fingerprint` endpoint** (Occam-side,
  ~400 LoC in `api/coordination.go`): returns a stable, time-
  invariant snapshot (`commit_sha`, `languages`, `stack`,
  `declared_deps` per-language, `declared_frameworks`, `entrypoints`,
  `manifest_files`). Parses `Cargo.toml`, `package.json`,
  `pyproject.toml` (PEP 621 + Poetry shapes), `go.mod`. The TOML
  parser handles the two regression cases that bit during dev:
  `mcp[cli]>=1.0` (extras `]` inside dep value) and a comment line
  containing `[brackets]` mid-array — both fixed via
  `stripTomlComment` + bracket-depth tracking.

### Trace events

- `critic_schema_check.fail` — G10 fired (carries `phase` =
  `worker` or `refiner`)
- `critic_content_grounding.fail` — G11 fired (carries `phase`)

### Tests

- 822 passing (was 783 at v0.2.0 release). +39 new tests:
  `test_content_grounding.py` (30) + extensions to
  `test_occam_client.py` (+9 for `get_repo_fingerprint` and
  `format_fingerprint_for_prompt`).
- Occam coordination test suite: +5 tests in `test_coordination.sh`
  exercising `/repo/fingerprint`, including the
  `mcp[cli]` + comment-with-brackets regression cases.

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
