# Changelog

All notable changes to gitoma are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning is
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- **G15 sibling-config reconciliation guard**
  (`gitoma/worker/sibling_config.py`): worker-side check that
  rejects patches creating / modifying a JS/TS quality config
  (`.editorconfig`, `.prettierrc*`, `.eslintrc*`, `package.json`
  with embedded `prettier` / `eslintConfig`) when the new config
  contradicts sibling configs already present in the repo.
  **Closes the b2v PR #32 failure mode** that started this entire
  architectural arc — `gitoma quality` shipped a `.prettierrc`
  with `tabWidth=2 / semi=false / singleQuote=true` blind to
  the existing `.editorconfig` (`indent_size=4`) and `.eslintrc`
  (`semi: ["error", "always"]`, `quotes: ["error", "double"]`).
  Pure deterministic check (no LLM); reconciliation matrix
  covers 4 conflict shapes: `indent_size↔tabWidth`,
  `end_of_line↔endOfLine`, ESLint `semi` ↔ Prettier `semi`,
  ESLint `quotes` ↔ Prettier `singleQuote`. Comparators bail
  to "no conflict" when EITHER side is absent (operator omitted
  on purpose) or when ESLint rule shape isn't recognized — never
  false-positives. Wired between G14 URL grounding and Ψ-full
  in the worker apply pipeline; on conflict, reverts + retries
  with feedback listing every conflict so the LLM can fix all
  in one round. New trace event `critic_sibling_config.fail`
  with `conflict_count` + touched files. Out of scope for v1
  (deferred): Python config family (Ruff vs Black vs isort vs
  mypy), Go, conflict-severity weighting, suggesting fixes,
  tsconfig.json reconciliation.

- **Test Gen v1 — 5th critic, autogenerates tests for shipped
  patches** (`gitoma/critic/test_gen.py`,
  `gitoma/critic/test_gen_prompts.py`): closes the missing
  Multi-Agent Pipeline role from the horizon blueprint
  (Architect / Implementer / Verifier / **Test Gen**). After the
  worker applies a patch and after all gates pass (G1-G14, Ψ-full),
  but before the commit, this agent uses CPG-lite diff (BEFORE +
  AFTER content per touched source file) to find new /
  signature-changed public symbols, asks the LLM to generate ONE
  test file per language in the project's existing test framework
  (pytest / jest / cargo test / go test detected via marker file),
  applies the test files alongside the source patch, and re-runs
  G8's test-baseline check. If the new tests fail or break the
  baseline, ONLY the test additions are reverted — the source
  patch is preserved. Multi-language: Python / TypeScript /
  JavaScript / Rust / Go (matches CPG coverage). Test-file path
  follows per-language convention: `tests/test_x.py`,
  `x.test.ts` colocated, `tests/x_tests.rs`, `x_test.go`
  colocated. **Opt-in via `GITOMA_TEST_GEN=on`**; default off
  until benched against real LLM responses. Cap of 5 symbols
  per source file in the LLM prompt; cap of 3000 chars per
  source snippet — same limits as worker prompt. Defensive:
  any failure (LLM error, framework not detected, no symbols
  changed, malformed response) returns silently — Test Gen
  NEVER blocks a patch. New trace events:
  `test_gen.llm_call`, `test_gen.generated`, `test_gen.applied`,
  `test_gen.reverted`, `test_gen.failed`, `test_gen.apply_failed`.
  Also extracted `gitoma/cpg/diff.py` as the shared in-memory
  re-indexing helper (was a private helper in psi_delta_i;
  promoted so test_gen could share without crossing layer
  boundaries). Out of scope for v1: coverage measurement,
  mutation testing, property-based tests, fixture-heavy
  integration tests, style adoption from existing tests,
  multiple test files per patch per language.

- **CPG-lite Go indexer** (`gitoma/cpg/go_indexer.py`): adds `.go`
  to the indexed-suffix set, completing the mainstream-backend
  language coverage (Python + TS + JS + Rust + Go). Two Go-
  specific quirks: **visibility via capital-letter naming** (no
  `pub` keyword — name starting with ASCII uppercase = exported;
  the Python/TS underscore convention is NOT applied) and
  **methods declared via receiver functions outside the type
  block** (`func (r *Repo) Find()`) — same pattern as Rust
  impl_item, with `parent_id` chained best-effort to the
  receiver type's Symbol via same-file lookup. Generic receivers
  (`*Repo[T]`) have their generics stripped before lookup.
  `type_declaration` body's type_specs map to CLASS (struct) /
  INTERFACE (interface_type) / TYPE_ALIAS (everything else);
  parenthesized `type ( … )` blocks flatten to one symbol per
  spec. Same for `const ( … )` / `var ( … )`. Imports parse
  bare and aliased shapes (`import "fmt"`, `import log
  "github.com/sirupsen/logrus"`); bare imports bind the last
  path segment as the local name. Selector calls
  (`fmt.Println(...)`) record `Println` as the called name. `init()`
  / `main()` end up `is_public=False` (lowercase rule); the
  BLAST RADIUS still surfaces them when touched. Mixed-build
  test extends to 5 languages — `.py + .ts + .js + .rs + .go` in
  one `build_index()` call all coexist with correct language
  tags + cross-language renderable BLAST RADIUS / Skeletal.
  Out of scope: embedded fields as separate Symbols, go.mod /
  go.work resolution, build tags (`// +build`), generic
  type-parameter parsing.

- **CPG-lite v0.5-expansion — JavaScript + Rust indexers**
  (`gitoma/cpg/javascript_indexer.py`, `gitoma/cpg/rust_indexer.py`):
  drops in two more tree-sitter grammars on top of v0.5-slim TS,
  using the same Symbol/Reference data shape. **JavaScript**
  (.js / .mjs / .cjs) is structurally identical to TS minus type
  annotations + interfaces + type aliases. **Rust** (.rs) has its
  own grammar shape: `struct_item` + `enum_item` map to
  SymbolKind.CLASS, `trait_item` to INTERFACE, `impl_item` is
  NOT a Symbol but its function_items become METHOD with
  parent_id = the target type's Symbol (best-effort same-file
  lookup). `function_signature_item` (trait method declarations)
  also captured. Visibility from the `visibility_modifier`
  named child (presence of `pub` keyword) — the Python/TS
  leading-underscore heuristic is NOT applied to Rust. `use`
  declarations parsed into Import rows including the
  brace-list shape (`crate::types::{User, Repo as DataRepo}`)
  via a small textual parser. `INDEXED_SUFFIXES` now covers
  `.py`, `.ts`, `.tsx`, `.js`, `.mjs`, `.cjs`, `.rs`. Filter
  constants in `blast_radius.py` + `psi_phi.py` +
  `psi_delta_i.py` updated; ΔI re-indexes JS/Rust files
  via the new dispatch. b2v live bench: 8 files (vs 3 in
  v0.5-slim) / 85 symbols across rust+typescript+javascript /
  build 39ms — BLAST RADIUS for `src/decoder.rs` now shows
  cross-language callers from TS test files. Bench artifact at
  `tests/bench/cpg_lite_v05_expansion/index_demo_output.txt`.
  Out of scope (deferred): Go, Java, Kotlin, Swift, C/C++,
  C# (mechanical follow-ups, ~1 session each); Rust macros,
  enum variant enumeration, generic-bound parsing in impl;
  CommonJS `require()` import recording; tsconfig path
  resolution.

- **Skeletal Representation v1 — signature view in planner prompt**
  (`gitoma/cpg/skeletal.py`): compresses the CPG-lite index into a
  per-file signature view (`def process_request(req: dict) -> str`,
  `class RequestHandler:` with methods nested) and injects it into
  the planner prompt right after the FILE TREE section. Planner
  now sees what each file ACTUALLY DEFINES (with full Python /
  TypeScript signatures), not just that it exists — drops the
  hallucinated-symbol-reference failure mode at the source.
  Token-budgeted (`DEFAULT_MAX_CHARS = 20000` ≈ 5000 tokens) with
  alphabetical file ordering + truncation marker showing omitted
  count. Schema additions: `Symbol.signature: str` (back-compat
  default `""`) + matching SQLite `signature TEXT NOT NULL DEFAULT
  ''` column. Both indexers populate signatures: Python via
  `ast.unparse(node.args)` + return annotation; TypeScript via
  tree-sitter `parameters` + `return_type` named fields. Both
  capped at 200 chars per signature. CPG-lite build was MOVED
  from PHASE 3 to BEFORE PHASE 2 so the planner can consume the
  index for the skeleton (worker still uses it for BLAST RADIUS
  via the same instance — single build per run). Opt-out via
  `GITOMA_CPG_SKELETAL=off` (independent of `GITOMA_CPG_LITE`)
  or budget tuning via `GITOMA_CPG_SKELETAL_BUDGET=<chars>`.
  New trace events `cpg.skeletal_rendered` (with chars + budget),
  `cpg.skeletal_render_failed` (defensive). Auto-applicazione
  bench on gitoma's own source: 201 files / 4572 symbols indexed,
  skeleton renders in 13ms; at 20000-char budget 64 files fit
  + 117 omitted with marker (operator can bump budget). Bench
  artifact at `tests/bench/skeletal_v1/render_demo_output.txt`.
  Out of scope for v1: docstring inclusion, inheritance chain
  rendering, custom file ranking beyond alphabetical, type-level
  reasoning beyond the captured signature text.

- **Ψ-full v1 — Φ + ΔI on top of CPG-lite** (`gitoma/worker/psi_phi.py`,
  `gitoma/worker/psi_delta_i.py`): adds two new components to the
  scalar quality gate. Ψ = αΓ + βΦ + γΔI − λΩ. **Φ** is caller-impact-
  weighted safety: per-symbol score `1/(1+log(1+caller_count))` from
  CPG-lite, aggregated across touched files. **ΔI** is structural
  conservativeness: re-index file content before+after, score
  `1 - mean(Δsymbols, Δrefs)` — high = surgical patch, low = rewrite.
  When CPG isn't loaded OR no `.py/.ts/.tsx` is touched, Φ/ΔI
  default to 1.0 each (the structural components contribute a "free"
  0.8 floor). Calibration **frozen and committed**: α=1.0, β=0.5,
  γ=0.3, λ=1.0, threshold=1.0, phi_hard_min=0.20 — see
  `project_psi_full_calibration.md` (memory file) for the full
  worked-example walkthrough + the math behind the numbers
  (initial 0.15 hard-min draft was 290-caller territory due to
  inverse-math error; corrected to 0.20 ≈ 54-caller floor). New
  env vars `GITOMA_PSI_FULL`, `GITOMA_PSI_BETA`, `GITOMA_PSI_GAMMA`,
  `GITOMA_PSI_FULL_THRESHOLD`, `GITOMA_PSI_PHI_HARD_MIN`,
  `GITOMA_PSI_DELETE_ALLOWED`. `evaluate_psi_gate` is now a
  dispatcher: FULL takes precedence over LITE; back-compat with
  the existing 3-arg signature preserved. New trace events
  `critic_psi_full.fail` (with components dict) alongside
  `critic_psi_lite.fail`. Bench artifact at
  `tests/bench/psi_full_v1/replay_results.txt` (4 worked-example
  scenarios with verdicts confirmed). Out of scope for v1: sub-
  thresholds on Γ/ΔI/Ω, caching Φ across subtasks, language-
  specific weights.

- **CPG-lite v0.5-slim — TypeScript indexer** (`gitoma/cpg/typescript_indexer.py`):
  extends the v0 Python-only CPG to TypeScript via `tree-sitter` +
  `tree-sitter-typescript`. Same Symbol/Reference/Import row shapes
  as Python (additive `language` column on the storage table; v0
  rows default to `"python"`, TS rows carry `"typescript"`). Two
  new SymbolKind values (`INTERFACE`, `TYPE_ALIAS`) for TS
  declarations with no Python analogue. Coverage: function /
  class / method / interface / type-alias declarations, named +
  default + namespace imports, calls + member access + `new X(...)`
  constructor refs, `extends` + `implements` heritage clauses.
  Indexer dispatches `.ts` → `language_typescript()` grammar,
  `.tsx` → `language_tsx()` grammar so JSX files parse cleanly.
  Resolver gained TS path-based import handling: `./other` /
  `../utils/helper` resolve relative to the importing file's
  directory through the canonical Node module-resolution lite-set
  (`<base>.ts`, `<base>.tsx`, `<base>/index.ts`, `<base>/index.tsx`).
  Bare specifiers (`react`, `@/components/Button`) stay
  unresolved — needs `tsconfig.json paths` config which is v0.5
  expansion. `build_index()` now walks `.py` + `.ts` + `.tsx` in
  one pass with the right per-extension dispatch; `BLAST RADIUS`
  block in the worker prompt fires on TS file_hints exactly as it
  does on Python. Bench artifact at
  `tests/bench/cpg_lite_v05_slim/index_demo_output.txt` (b2v live
  build: 3 .ts files / 14 symbols / 50 refs / 17ms). Out of scope
  for slim (deferred to v0.5 / v0.5-expansion): plain JavaScript
  (`.js`/`.mjs`/`.cjs`), Rust, JSX-as-references, type-level
  references, TS namespaces, tsconfig path resolution.

- **CPG-lite v0 — Python symbol+reference index** (`gitoma/cpg/`):
  first concrete cut on the horizon CPG 2.0 path. Pure stdlib `ast`
  parser + in-memory SQLite, mono-language (Python only), no caching
  across runs. New public surface: `from gitoma.cpg import
  build_index, CPGIndex` exposing `get_symbol`, `find_references`,
  `callers_of`, `who_imports`, `call_graph_for`. Wired into the
  worker (`gitoma/worker/worker.py`) so when the index is loaded and
  a subtask touches a `.py` file, a deterministic
  `== BLAST RADIUS (CPG-lite) ==` block is injected into the worker
  user prompt right after the file contents — citing the cross-file
  callers of every public symbol the patch is about to modify, with
  a per-symbol cap of 5 callers (+ "+N more" marker for hot
  symbols). The block exists to close the failure mode demonstrated
  by b2v PR #32 (worker generated `.prettierrc` blind to existing
  `.editorconfig` / `.eslintrc`) at the structural level — the LLM
  now sees what its patch will impact before emitting it.
  **Opt-in via `GITOMA_CPG_LITE=on`**; default off until the v0.5
  multi-language extension lands. Trace events:
  `cpg.index_built` (always when on), `cpg.blast_radius_failed`
  (defensive). Auto-applicazione bench on gitoma's own source:
  187 files / 4243 symbols / 32051 refs indexed in ~1.4s; bench
  artifact in `tests/bench/cpg_lite_v0/index_demo_output.txt`.
  Known limitations (documented + intentional): star imports
  opaque, dynamic attribute access invisible, no CFG/PDG yet,
  single-language. Honest A/B vs LLM deferred to v0.1 (needs
  sample size + cross-run cache).

- **Castelletto Taglio A — vertical-as-config refactor**
  (`gitoma/verticals/`): turns vertical mode (`gitoma docs`,
  `gitoma quality`, …) from ad-hoc env-var checks scattered
  across the planner / CLI into a single declarative `Vertical`
  dataclass per concern. New `gitoma/verticals/__init__.py`
  registry is the single source of truth — adding a vertical now
  costs **1 file** with NO edits to `run.py`, `scope_filter.py`,
  `prompts.py`, or `cli/commands/_vertical.py`. The CLI factory
  (`gitoma/cli/commands/_vertical.py`) generates per-vertical
  Typer commands by iterating the registry; the audit-side and
  plan-side scope filters consume the registry; the planner
  prompt receives the vertical's `prompt_addendum` as a
  HARD-RULE block injected right before the JSON schema
  instruction (highest-recency narrowing constraint). Legacy
  `is_doc_path` / `filter_metrics_to_doc_scope` /
  `filter_plan_to_doc_scope` / `DOC_*` constants kept as thin
  shims for callers and tests written before the refactor.
- **`gitoma quality` — second vertical** (lint / format /
  type-check config files only): registered as the architectural
  acceptance test for Taglio A — `gitoma quality --help`
  appearing without any wiring edit proves the "1 file = 1
  vertical" property. Allows ~30 common config basenames
  (`.prettierrc*`, `.eslintrc*`, `biome.json`, `tsconfig.json`,
  `.ruff.toml`, `setup.cfg`, `mypy.ini`, `.pylintrc`,
  `.editorconfig`, `.pre-commit-config.yaml`, `.golangci.yml`,
  `rustfmt.toml`, `clippy.toml`, …); narrows the audit to the
  `code_quality` metric only. Excludes `pyproject.toml`
  intentionally — `[tool.*]` sub-section logic is deferred to a
  future Taglio.

- **Ψ-lite — universal fitness function (Γ + Ω components)**
  (`gitoma/worker/psi_score.py`): scalar quality gate between
  structural guards and LLM critics. `Ψ = α·Γ - λ·Ω` where Γ is
  the grounding fraction (mentioned framework/package refs that
  appear in fingerprint deps, reusing G11/G12 extraction logic)
  and Ω is the slop penalty (heuristic count of literal-`\n`
  in code blocks, triple-blank-line runs, trailing whitespace,
  source-file-wrapped-in-markdown-fence, near-empty source).
  Aggregated `min` across touched files (worst dominates).
  Defaults `α=1.0, λ=1.0, threshold=0.5`. **Opt-in via
  `GITOMA_PSI_LITE=on`**; threshold/weights tunable via
  `GITOMA_PSI_LITE_THRESHOLD`, `GITOMA_PSI_ALPHA`,
  `GITOMA_PSI_LAMBDA`. Calibrated empirically against the b2v
  PR matrix: PR #27 README (literal `\n` corruption) scores
  Ψ=0.40 (would block), clean PRs #28/#29/#30 score Ψ ≥ 0.935.
  New trace events: `psi_lite.scored` (always when enabled),
  `critic_psi_lite.fail` (on block). Wired into worker apply
  path post-G8; refiner not yet (telemetry only).

### Tests

- 976 passing (was 950 at v0.4.0). +26 in `test_psi_score.py`:
  Γ for grounded/hallucinated/mixed/source-neutral/no-fingerprint;
  Ω for clean/literal-`\n`/triple-blanks/source-wrapped/near-empty/
  clamped-at-1; full Ψ aggregation; env-driven gate
  on/off/threshold/weights/invalid-fallback.

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
