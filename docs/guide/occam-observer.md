# Occam Observer integration

[Occam Observer](https://github.com/fabriziosalmi/occam-observer) is a separate, optional companion service. When `OCCAM_URL` points at a reachable Occam gateway, Gitoma teaches the planner about your repo's actual stack and accumulates a cross-run learning signal that suppresses recurring failure patterns. Without it, every Gitoma run is amnesiac — the planner re-derives ground truth from prompts and previous mistakes don't compound into avoided ones.

The integration is **fail-open by design**: if `OCCAM_URL` is unset, or the gateway is down, every Occam-touching call returns a benign default and the rest of the pipeline runs unchanged. You can adopt it incrementally and turn it off at any time without breaking a workflow.

## What Gitoma asks Occam for

Three reads + one write, per run:

| Direction | Endpoint | When | Consumed by |
|---|---|---|---|
| **read** | `GET /repo/fingerprint?target=<abs-path>` | Once per run, before `planner.plan()` | Planner prompt (`== REPO FINGERPRINT (GROUND TRUTH) ==` block) **and** the worker-side guards G11 / G12 / G14 + Ψ-lite Γ |
| **read** | `GET /repo/agent-log?since=24h&limit=20` | Once per run, before `planner.plan()` | Planner prompt (`== PRIOR RUNS CONTEXT ==` block) |
| **read** | `GET /repo/agent-log?since=7d&limit=200` | Once per run, after `planner.plan()` | G9 deterministic post-plan filter (drops subtasks whose file_hints have failed N+ times in the recent window) |
| **write** | `POST /observation` | After every subtask in `on_subtask_done` / `on_subtask_error` | Occam's TSDB → fed back into the next run's `/repo/agent-log` reads |

The `/repo/fingerprint` payload includes:

- `commit_sha`, `computed_at`
- `languages` (per-extension file count)
- `stack` (manifests + CI tools detected)
- `declared_deps` keyed by language (`rust`, `npm`, `python`, `go`)
- `declared_frameworks` (canonical names like `clap`, `react`, `fastapi`)
- `entrypoints` (e.g. `src/main.rs`, `manage.py`)
- `manifest_files` (which build files exist)

This is the deterministic "what is this repo" snapshot the planner uses to avoid suggesting React work in a Rust CLI, and the worker uses to flag a `prettier.config.js` that references a plugin not in `package.json`.

The `POST /observation` payload uses a closed-set vocabulary (see [`gitoma/context/occam_client.py`](https://github.com/fabriziosalmi/gitoma/blob/main/gitoma/context/occam_client.py)) — `outcome` is one of `success` / `fail` / `skipped`, `failure_modes` is a 11-label enum (`json_emit`, `ast_diff`, `test_regression`, `syntax_invalid`, `denylist`, `manifest_block`, `patcher_reject`, `build_retry_exhausted`, `git_refused`, `json_parse_bad`, `unknown`). Closed sets keep the agent-log queryable without free-text grep.

## Setup

### 1. Run Occam Observer

Follow the upstream README at <https://github.com/fabriziosalmi/occam-observer>. The minimal path is:

```bash
git clone https://github.com/fabriziosalmi/occam-observer
cd occam-observer
go build -o occam ./api
OCCAM_DB=/tmp/occam.db API_PORT=29030 \
  ENGINE_SCRIPT=$(pwd)/telemetry_observer.sh \
  ./occam
```

Verify it's listening:

```bash
curl -s http://127.0.0.1:29030/healthz
```

For a more permanent deployment, the upstream repo ships a `Dockerfile` and an `occam-mcp` binary for stdio MCP clients; only the HTTP gateway is required for Gitoma.

### 2. Point Gitoma at it

One environment variable in either your shell, `~/.gitoma/.env`, or `<cwd>/.env`:

```bash
export OCCAM_URL=http://127.0.0.1:29030
```

That's the entire integration surface. Gitoma doesn't need any other Occam-specific knob — once `OCCAM_URL` is set and the gateway is reachable, the planner prompt block, the worker-side grounding guards, and the post-subtask observation POSTs all activate automatically.

### 3. Verify the wire works

Run any Gitoma command. The planner phase prints two muted info lines when Occam is reachable:

```text
Occam: injected 13 prior-runs entries into planner context
Occam fingerprint: 2 manifest(s), 2 framework(s) — clap, serde
```

The structured trace under `~/.gitoma/logs/<slug>/<timestamp>-run.jsonl` carries the full picture:

| Event | Meaning |
|---|---|
| `plan.real_bug_synthesized` | Layer-A prepended a synthesized T000 task targeting failing-test source files |
| `plan.readme_banished` | Layer-B dropped a README-only subtask |
| `plan.occam_filter` | G9 dropped subtasks whose file_hints exceeded the recent-failure threshold |
| `critic_*.fail` (G11–G14) | A worker-side grounding guard fired with fingerprint data |

If none of these appear over a couple of runs, double-check that `OCCAM_URL` is exported in the shell that launches `gitoma run` (env-precedence is shell > `~/.gitoma/.env` > `<cwd>/.env` > config file).

## Tuning knobs

All optional, all default to safe values:

| Variable | Default | Effect |
|---|---|---|
| `OCCAM_URL` | unset (off) | Gateway base URL. Empty / unreachable → silent fail-open. |
| `GITOMA_OCCAM_FILTER_THRESHOLD` | `2` | G9 drops a subtask whose file_hints have appeared in N+ failing observations within the 7d/200 window. Lower = stricter (more drops). |
| `GITOMA_PSI_LITE` | unset (off) | Opt-in Ψ-lite scalar quality gate. When `on`, every patch gets a `psi_lite.scored` event in the trace; below-threshold patches block. |
| `GITOMA_PSI_LITE_THRESHOLD` | `0.5` | Reject when `Ψ < threshold`. Clamped 0.0–1.0. |
| `GITOMA_PSI_ALPHA` | `1.0` | Weight on Γ (grounding score) inside `Ψ = α·Γ - λ·Ω`. |
| `GITOMA_PSI_LAMBDA` | `1.0` | Weight on Ω (slop penalty). Raise to punish slop more aggressively. |

## What you actually gain

Concretely, on the [b2v](https://github.com/fabriziosalmi/b2v) bench:

- **Before Occam**: 4 of 4 PRs had some form of README destruction (bash-block deletion, literal `\n` corruption, fabricated `b2v.github.io` URLs). Self-review caught the regression 1 of 4 times.
- **After Occam + the planner-time post-processors that consume it**: 3 replay PRs across qwen3-8b / gemma-4-e4b / gemma-4-e2b shipped with README untouched — Layer-B dropped the offending "Update README" subtasks at plan time, and the surviving worker output was checked against the fingerprint by G11 / G12 / G14.

Per-feature contribution:

| Gitoma feature | Without Occam | With Occam |
|---|---|---|
| Planner GROUND TRUTH block | absent | injected |
| G9 post-plan filter | no-op | drops failing-history subtasks |
| G11 doc framework grounding | no-op | rejects doc that mentions undeclared frameworks |
| G12 npm config grounding | no-op | rejects JS config referencing undeclared packages |
| G14 URL/path grounding | filesystem only | filesystem + DNS/HEAD on added URLs |
| Ψ-lite Γ component | always 1.0 (neutral) | scores patch grounding against fingerprint |
| Cross-run learning | absent | every observation feeds the next run's prior-runs context + G9 history |

## Security notes

- **What Occam sees**: paths, declared dep names, framework identifiers, branch + commit SHA, the closed-set failure-mode tags. **Not** the contents of your patches, prompts, or LLM responses. The integration was deliberately scoped to "what shape is this repo" + "what failed where" — the LLM I/O stays local.
- **Auth**: if your Occam deployment requires a Bearer token, set `OCCAM_URL=http://user:token@host:port` (HTTP basic) or extend `gitoma/context/occam_client.py`'s `OccamClient` to inject a header. Today the gateway is assumed local / on a tailnet.
- **No retries** on the client side — a missed observation is a tiny data loss, not a correctness issue. The local Gitoma trace JSONL is still authoritative for forensics.
- **Fail-open contract** is enforced by every method on `OccamClient`: HTTP errors, schema mismatches, connection failures all return a benign default without raising. Removing the env var or stopping the gateway is always safe; you'll just lose the cross-run learning signal until you reattach.

## Going further

- The companion repo's [coordination API](https://github.com/fabriziosalmi/occam-observer/blob/main/docs/guide/coordination-api.md) exposes additional endpoints (claims, blame, churn, file-level fingerprint, AST imports/exports/symbols). Gitoma consumes the four covered above; the rest are available for downstream tooling or future Gitoma features (CPG-lite is the natural next consumer).
- Trace events emitted by Gitoma are documented end-to-end in [Observability](./observability) and the per-event detail lives in [`gitoma/core/trace.py`](https://github.com/fabriziosalmi/gitoma/blob/main/gitoma/core/trace.py).
- The full chronology of every guard that consumes fingerprint data — what it catches, why it was added, what bench evidence motivated it — is in [Critic stack](../architecture/critic-stack).
