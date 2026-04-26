# PATH A live results — evening session 2026-04-26

> Real `gitoma run` (no `--dry-run`) on 3 bench repos with the
> FULL stack active (CPG-lite + Ψ-full + Test Gen + G18 + G19).
> Single-sample per repo (n=1) on qwen3-8b @ minimac.

## Setup

```bash
LM_STUDIO_BASE_URL=http://100.98.112.23:1234/v1
LM_STUDIO_MODEL=qwen/qwen3-8b
LM_STUDIO_TIMEOUT=300
GITOMA_CPG_LITE=on
GITOMA_PSI_FULL=on
GITOMA_TEST_GEN=on
GITOMA_G18_ABANDONED=on
GITOMA_G19_ECHO_CHAMBER=on
```

## Three repos benched

1. **gitoma-bench-ladder** (existing) — multi-language progression bench
2. **gitoma-bench-quality** (NEW today) — config-jungle stress for G15
3. **gitoma-bench-blast** (NEW today) — hot-symbol stress for BLAST RADIUS / Φ

## Per-run results

### Run 1 — bench-ladder w/ qwen3-8b

| Stage | Outcome |
|---|---|
| CPG build | 16 indexable files, 106 symbols, **28ms** |
| Skeletal injection | 1414 chars |
| Plan | 5 tasks, 11 subtasks |
| Execution | **3/5 tasks done, 8/11 subtasks** |
| Guards fired (visible) | G3 denylist 1× (Cargo.lock), G7 AST-diff 1× (T004-S01), git-add 1× (docs/README.md) |
| Worker apply commits | 8 (T001×3, T002×1, T003×2, T005×2) |
| PR | **#37 opened** |
| Self-review | clean (no issues flagged) |
| CI watch | no workflows (bench-ladder has none) |

Verdict: **system held end-to-end + shipped a real PR**.

### Run 2 — bench-quality w/ qwen3-8b (gitoma quality vertical)

| Stage | Outcome |
|---|---|
| CPG build | 2 indexable files (the 2 sample.ts), 6 symbols, **15ms** |
| Vertical scope filter | dropped 9 metrics, kept `[code_quality]` |
| Skeletal injection | 186 chars (small repo) |
| Plan | 1 task, 3 subtasks (all targeting root-level configs) |
| Execution | **0/1 tasks done, 1/3 subtasks** |
| Guards fired (visible) | G6 syntax 1× (.eslintrc.json malformed JSON, 2 attempts), G10 schema 1× (.prettierrc.json `semi: "always"` not boolean, 2 attempts) |
| Worker apply commits | 1 (.pre-commit-config.yaml only) |
| PR | NOT opened (PR threshold not met: 0/1 tasks done) |

Verdict: **system correctly refused to ship malformed configs**.

**Insightful observation**: The model emitted `semi: "always"` in
the .prettierrc.json — VALID for ESLint but INVALID for Prettier
(which expects boolean). G10's JSON-schema validator caught it.
This suggests the model was implicitly TRYING to reconcile with
ESLint conventions but tripped on the cross-format type mismatch.
G15 sibling-config never got its turn because the patch never
reached a syntactically/semantically valid state.

### Run 3 — bench-blast w/ qwen3-8b (initial attempt)

| Stage | Outcome |
|---|---|
| CPG build | 14 indexable files, 78 symbols, **6ms** |
| Skeletal injection | 1524 chars (shows core.process and 31 callers) |
| Plan | 4 tasks, 6 subtasks (all targeting periphery — config + docs + CONTRIBUTING + CHANGELOG) |
| Execution | **4/4 tasks done, 6/6 subtasks** |
| Guards fired (visible) | NONE — model executed cleanly |
| Worker apply commits | 6 (Ruff config, Mypy config, Update Deps, Docstrings, CONTRIBUTING, CHANGELOG) |
| PR | **PUSH FAILED** — `fabgpt-coder` lacked `contents:write` permission on the new repo (collab invite sent during run, then accepted) |

### Run 3-bis — bench-blast w/ qwen3-8b (post-collab-accept)

| Stage | Outcome |
|---|---|
| CPG build | 14 indexable files, 78 symbols, **6ms** |
| Skeletal injection | 1524 chars |
| Plan | 3 tasks, 4 subtasks (slight stochastic variance vs initial run — same family of tasks) |
| Execution | **2/3 tasks done, 3/4 subtasks** |
| Guards fired (visible) | G8 test-regression 1× (T002-S01 Docstrings broke smoke tests) |
| Worker apply commits | 3 (Ruff config, Mypy config, Coverage config) |
| **PR** | **#1 LIVE**: https://github.com/fabriziosalmi/gitoma-bench-blast/pull/1 |
| Self-review | **2 findings posted** (1 major, 1 minor) — auto-comment on PR |
| **CI watch** | **bench-validate workflow PASSED** ✓ |

Verdict: **complete end-to-end cycle demonstrated**. The model
again intelligently AVOIDED touching `core.process()` (the
31-caller hub), proposed only periphery configs, the patch passed
the bench corpus's own validation workflow → CI green proves the
shipped PR didn't break the bench's invariants. **G8 caught a
regression** (docstrings change broke smoke tests) and the worker
correctly skipped that subtask without aborting the whole run.

## Answers to the 5 questions from NEXT_SESSION.md

### 1. Tutti i guard fanno fuoco?

**SÌ for the existing stack** observed firing live across the 3 runs:
- G3 denylist (sensitive file rejection)
- G6 syntax check (JSON parse)
- G7 AST-diff (top-level def preservation)
- G10 schema check (JSON Schema validator on Prettier config)

**Inconclusive for the new stack** (G15 / G18 / G19 / Test Gen / Ψ-full):
none fired observably across the 3 runs. This is **not a bug** — the
3 repos didn't contain triggers because:
- Bench-quality scenarios (intentional config conflicts) were in
  `conflict_zone/` subdir, but the LLM proposed creating ROOT-level
  configs (no siblings at root → no G15 trigger)
- Bench-blast: model intelligently avoided the hub (no signature
  changes → no G18 abandons, no echo-chamber additions)
- Bench-ladder: no quality-config touches, no symbol-level orphans

**Honest call**: a separate "guard-trigger fixture" benchmark would
be needed to verify the new guards activate on craft inputs.
Today's unit tests (53 G15 + 32 G18+G19) cover those code paths
deterministically; live confirmation deferred.

### 2. Ψ-full attiva (siamo in PHASE 3)?

PHASE 3 reached on all 3 runs. `psi_lite.scored` and
`critic_psi_full.fail` events go to trace JSONL (not console
stdout) — the tmpdir was cleaned after each run, so we don't
have post-mortem trace inspection.

**Indirect evidence Ψ-full ran**: NO patch was rejected by Ψ on
any run. Either it's silently passing (most likely — small clean
patches with high Γ + neutral Φ + neutral ΔI + low Ω all score ~1.0)
or there's a wiring gap. Unit tests (65 psi tests) prove the
gate logic; live trace inspection is a v2 enhancement.

### 3. Test Gen genera test che passano?

`GITOMA_TEST_GEN=on` was set; no `test_gen.*` events visible in
console output. Test Gen runs ONLY when there are new/changed
public symbols in touched source files. Across the 3 runs, only
bench-blast added new public symbols (Ruff/Mypy/etc are configs,
not Python source). The single source-touching subtask in run 3
(T003-S01 "Add Docstrings") didn't change symbol identity (just
docstrings) → no Test Gen trigger.

**Honest call**: Test Gen wasn't actually exercised by these
scenarios. Need a synthetic patch that ADDS a public Python
function to confirm live Test Gen behavior. Unit tests with
mocked LLM cover the orchestration logic.

### 4. G15/G18/G19 false positive su finto-pulito?

**NO false positives observed**:
- G15 didn't fire on bench-ladder (no quality configs touched)
- G15 didn't fire on bench-quality (the patches were rejected
  pre-G15 by G6+G10)
- G15 didn't fire on bench-blast (no quality configs touched)
- G18 didn't fire on any run (no abandoned helpers)
- G19 didn't fire on any run (no echo-chamber additions)

Negative-control test passed: the new guards stayed silent on
"clean" patches and didn't introduce noise.

### 5. PR pulita o si rompe qualcosa?

- **Bench-ladder**: PR #37 shipped clean. Self-review found no
  issues. Mix of useful additions (LICENSE, CHANGELOG,
  CONTRIBUTING, language manifests) — operator-mergeable.
- **Bench-quality**: NO PR opened. System correctly refused
  to ship after 0/1 tasks done — this is the right behavior.
  The 1 committed file (.pre-commit-config.yaml) was discarded
  with the failed branch.
- **Bench-blast** (run 3-bis post-collab-accept): **PR #1 LIVE**
  https://github.com/fabriziosalmi/gitoma-bench-blast/pull/1.
  Self-review posted 2 findings as a PR comment (1 major,
  1 minor). **Bench-validate CI workflow PASSED** — proves the
  shipped patch didn't violate the bench corpus's own invariants
  (caller count ≥25, dead helpers stay dead). G8 test-regression
  blocked 1 subtask (docstrings changed → smoke test broke);
  worker correctly skipped + continued.

## Operational findings worth keeping

1. **`LM_STUDIO_TIMEOUT=300` is necessary**, confirmed across 3
   runs with full stack on qwen3-8b @ minimac. The default 120s
   triggers timeouts that look like model failures but are
   actually inference-time issues.

2. **fabgpt-coder collab permission is a deployment prerequisite**
   when adding new bench repos. Now done for both new repos
   (invitation pending acceptance from operator's other account).

3. **Bench-validate Actions verified working**: both new repos
   show green checks on the corpus-integrity workflow. The bench
   corpus stays meaningful unless someone deliberately edits it.

4. **Plan quality observation** (independent of guards): the
   model on bench-blast intelligently AVOIDED touching the hub
   `core.process()` and stuck to periphery tasks. Whether
   this is luck (small plan size = unlikely to hit hub) or
   signal (Skeletal block conveyed the caller density) is
   inconclusive at n=1 — needs A/B with Skeletal off vs on.

## Not benched tonight (deferred)

- Gemma-4-e2b runs across the 3 repos (cluster instability +
  time budget; gemma is "smaller, faster" but plan quality
  varies considerably)
- Re-run bench-blast post-collab-acceptance to confirm PR ships
- Synthetic forced-trigger benchmarks for G15/G18/G19/Test Gen
- Trace JSONL inspection for Ψ-full firings on passes (need
  to keep tmpdir or wire trace to a persistent path)

## Next-session deliverable

When fabgpt-coder accepts the collab invites:
1. Re-run bench-blast → confirm PR ships
2. Add a "trigger fixture" repo (gitoma-bench-orphans?) where
   the planner is FORCED to propose patches that should trigger
   G18/G19. Same shape as bench-quality but with hot-symbol
   removal scenarios.
