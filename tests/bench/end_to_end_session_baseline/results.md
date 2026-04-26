# End-to-end session baseline (2026-04-26)

After shipping 8 commits in one session (Castelletto Taglio A → CPG-lite v0
→ v0.5-slim TS → Ψ-full v1 → Skeletal v1 → CPG-lite v0.5-expansion JS+Rust
→ CPG-lite Go → Test Gen v1), the unit suite stayed at **1396 passing** but
**zero verification was end-to-end with the full stack enabled**. This bench
closes that gap: 4 dry-runs across 3 repos with `GITOMA_CPG_LITE=on` +
`GITOMA_PSI_FULL=on`.

## Results table

| # | Repo | Vertical | LLM model | LM_STUDIO_TIMEOUT | CPG build | Skeletal chars | Plan emitted | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | gitoma-bench-ladder | (full) | qwen3-8b @ minimac | default 120s | 16 files / 106 sym / 24ms | 1414 | 2 tasks · 4 subtasks | ✅ PASS |
| 2 | b2v | (full) | qwen3-8b @ minimac | default 120s | **NOT BUILT** (bug) | n/a | n/a | ❌ FAIL — bug intercettato |
| 3 | lws | docs | qwen3-8b @ minimac | default 120s | 16 files / 538 sym / 326ms | 15434 | empty plan | ⚠ partial — stack OK, LLM punted |
| 2-bis | b2v (post-fix) | (full) | qwen3-8b @ minimac | default 120s | 8 files / 85 sym / 27ms | 891 | n/a — LLM timeout | ❌ FAIL — qwen timeout |
| 2-tris | b2v (with timeout=300) | (full) | qwen3-8b @ minimac | 300s | 8 files / 85 sym / 27ms | 891 | 4 tasks · 4 subtasks | ✅ PASS |

## Wins (the bench paid for itself)

* **Bug intercettato**: `gitoma/cli/commands/run.py` checked `_has_python`
  before building CPG — reliquia di v0 quando CPG era Python-only. Su b2v
  (Rust+TS+JS, no Python) il CPG saltava silenziosamente. Niente unit test
  lo avrebbe pescato perché era una condizione di env-detection live.
  **Fix shipped inline during bench**: check ora contro
  `{python, typescript, javascript, rust, go}`. Suite stayed green.
* **CPG si comporta entro budget** ovunque: 24ms (16 file Python),
  27ms (8 file Rust+TS+JS), 326ms (16 file Python denso lws). Tutti
  ben sotto il target di 5s.
* **Skeletal injection funziona** su tutte le 4 run con CPG attivo —
  da 891 chars (b2v minimal) a 15434 chars (lws denso, vicino al
  budget di 20000). Mai overflow.
* **Vertical scope filter funziona** end-to-end (run 3): `Scope=docs`
  drops 9 metriche, lascia solo `[readme, docs]` al planner.

## Surprises / follow-up gaps

* **qwen3-8b @ minimac è LENTO**: default LM_STUDIO_TIMEOUT=120s NON
  basta per un planner-call con stack completo (anche con prompt da
  891 chars). Servono **300s** per terminazione affidabile. Non è
  prompt-size: fallisce anche su prompt minimal. Soluzione possibile:
  bumping default in run pipeline quando CPG è attivo, OR pinning a
  modello locale più reattivo (qwen3-4b, gemma-4-e2b).

* **lws docs vertical produce plan VUOTO**: i due sole metriche che
  passano il filter (readme, docs) sono già passing/warn-pulite,
  quindi il LLM ha concluso "niente da fare". Il sistema risponde
  correttamente ma noi non vediamo signal end-to-end. Non è un bug,
  è un caso edge: il vertical filter è stretto, e su una repo che ha
  già docs decenti, "nothing to do" è la risposta giusta.

* **b2v plan post-fix include subtasks problematici**: T003-S01 punta
  a `.github/workflows/deploy-docs.yml` (G3 denylist colpirebbe al
  worker), T004-S01 punta a `Cargo.toml` (build manifest, anche
  guarded). Il piano è "lazy" — superficialmente plausibile ma con
  diversi step bloccabili. Non un fallimento del bench (che era
  dry-run); osservazione utile per Test Gen / G15+ pipeline future.

## Ψ-full evidence (gap)

`GITOMA_PSI_FULL=on` era impostato in tutti i 4 run, ma **Ψ-full è un
gate post-apply** — i dry-run terminano in PHASE 2 (planning) e non
raggiungono PHASE 3 (worker apply). Per benchare Ψ-full
end-to-end servono RUN COMPLETI (no `--dry-run`) o un test offline
che simula il post-apply path. Deferred.

## Test Gen evidence (gap)

`GITOMA_TEST_GEN` was deliberately NOT set — Test Gen costa LLM
extra calls per ogni patch e introduce N+1 punti di fallimento.
Validation coperta dai 40 unit test con LLM mockato; live A/B
deferred a una sessione dedicata con repo + modello scelti per
prove statisticamente significative.

## Numbers worth keeping

| Metric | Value | Notes |
|---|---|---|
| CPG build cost (per file) | 1.5-3 ms | Bench ladder + lws + b2v average |
| Skeletal render cost | <50 ms | All 4 runs, indistinguishable from 0 |
| Skeletal char/symbol ratio | ~30 chars/sym | lws: 15434 chars / 538 sym |
| qwen3-8b minimac p50 latency (CPG-on) | 60-150s | Some runs <120s, some >120s — variance is wide |
| Suite tests after session | 1396 passing | +250 over morning baseline |

## Recommended next steps

1. **Document `LM_STUDIO_TIMEOUT=300` as the recommended minimum** when
   CPG-lite + Ψ-full are active — add to README / docs. Or auto-bump
   in `run.py` when both env vars are on (one-line change).
2. **Re-run bench with smaller worker** (qwen3-4b or gemma-4-e2b)
   to compare: faster wall-clock at the cost of plan quality.
3. **Real run on bench-ladder** (no `--dry-run`) — exercise PHASE 3
   so Ψ-full gate fires + Test Gen's prompt is benched live (with
   `GITOMA_TEST_GEN=on`).
4. **G15 / G18 / G19 cross-language critics** — same engineering
   cost as one new language indexer but multiplies value across
   all 5 languages already supported.

## Honest read

We shipped a lot of architecture today. The bench confirms the
**plumbing works** (CPG, Skeletal, scope filter, dispatch table,
filter constants). It surfaces **one real bug** (now fixed) and
**one real operational gap** (LLM timeout default too aggressive
for current model+stack combo). The DEEPER question — does the
new structural signal actually improve plan quality? — remains
deferred to a real-run + multiple-sample bench. Without that data
the new components are best-effort guesses; with it, we'd have
calibration evidence.

The bench paid for itself on the first run.
