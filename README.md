<div align="center">

# Gitoma

**An autonomous agent that improves your GitHub repo.**

Analyzes. Plans. Commits. Opens a PR. Reviews its own work. Remembers what worked.

[**Documentation**](https://fabriziosalmi.github.io/gitoma/) · [Quickstart](https://fabriziosalmi.github.io/gitoma/guide/quickstart) · [CLI reference](https://fabriziosalmi.github.io/gitoma/guide/cli) · [REST API](https://fabriziosalmi.github.io/gitoma/api/rest)

</div>

---

```bash
pipx install gitoma
gitoma config set GITHUB_TOKEN=<your-token>
gitoma run https://github.com/owner/repo
```

That's it. A local LLM writes the plan, commits fix-by-fix, opens a pull request, then runs an adversarial self-review on the diff it just shipped.

```
ANALYZE  →  PLAN  →  EXECUTE  →  PR  →  SELF-REVIEW  →  REVIEW (you)
```

## Why Gitoma

- **Self-correcting critic stack — 22 guards (G1–G22, G17 reserved).** Catches specific classes of LLM patch slop — broken syntax, dropped functions, hallucinated frameworks/URLs/configs, README destruction, dead code, abandoned helpers, echo-chamber cliques, broken TOML, semgrep/trivy regressions — and feeds the worker a deterministic retry signal instead of shipping the bad patch. Plus two planner-time post-processors (Layer-A real-bug synthesis, Layer-B README banishment) and a scalar Ψ-lite quality gate. See the [critic stack](https://fabriziosalmi.github.io/gitoma/architecture/critic-stack) for the chronology.
- **Local-first.** The LLM runs on your machine (LM Studio, Ollama, or any OpenAI-compatible endpoint). Code, diffs, and secrets never leave your laptop.
- **Spider-web architecture — orchestrate, never reimplement.** Every external capability is its own deterministic tool with an MCP/HTTP/gRPC API; gitoma sits in the middle and consumes them via thin client wrappers. Currently 5 confirmed legs: [Occam Observer](https://github.com/fabriziosalmi/occam-observer) (per-host TSDB + repo fingerprint), [occam-gitignore](https://github.com/fabriziosalmi/occam-gitignore) (deterministic .gitignore), [Layer0](https://github.com/fabriziosalmi/layer0) (HNSW + Poincaré-ball cross-run memory), [occam-trees](https://github.com/fabriziosalmi/occam-trees) (1000 canonical project scaffolds), [semgrep](https://semgrep.dev) + [trivy](https://aquasec.com/products/trivy/) (static + supply-chain analysis).
- **Cross-run memory.** When [Layer0](https://github.com/fabriziosalmi/layer0) is reachable, every successful run ingests its plan + guards + outcome to a per-repo namespace; the next run on the same repo queries the top-K most relevant memories before invoking the LLM planner. The bot reads its own past + writes its own future.
- **From-zero scaffolding.** `gitoma scaffold <repo> --stack mern --level 4` materialises the canonical scaffold (100 stacks × 10 archetypes via occam-trees) into a PR, additive only. Pairs with `gitoma run` to close the from-zero generation gap.
- **Operator-curated planning.** `gitoma run <repo> --plan-from-file tasks.json` skips the LLM planner phase entirely and executes a hand-written TaskPlan — the escape hatch for cases where the planner doesn't have enough context.
- **Resumable.** State is persisted per repo. Kill the CLI mid-run; `--resume` picks up at the last committed subtask.
- **Observable.** A live cockpit at `http://localhost:8000` streams every phase. Structured JSONL trace per invocation. Optional auto-diary push to a remote git repo via PHASE 7.
- **Scriptable.** Everything the CLI does is also a REST endpoint (`POST /api/v1/run`, SSE at `/stream/{id}`). Bearer-protected, CSP-hardened.
- **Extensible via MCP.** A built-in Model Context Protocol server exposes GitHub context + write tools to Claude Desktop and any MCP client.

## Quickstart

Three commands to your first PR:

```bash
# 1. Install
pipx install gitoma

# 2. Point at your GitHub token (contents:write + pull-requests:write)
gitoma config set GITHUB_TOKEN=ghp_your_fine_grained_token

# 3. Run
gitoma run https://github.com/<owner>/<repo>
```

Open the cockpit while it runs:

```bash
gitoma serve &
open http://localhost:8000
```

Full install + prerequisites in the [**Getting Started guide**](https://fabriziosalmi.github.io/gitoma/guide/quickstart).

## What's under the hood

The pipeline runs as discrete `PHASE N` blocks; the optional ones silently skip when their substrate is unavailable so the core flow always works.

| Phase | What happens | Where |
|---|---|---|
| **1   Analyze** | Nine metric analyzers score the repo + `RepoBrief` extracts deterministic stack/build/test signals | `gitoma/analyzers/*`, `gitoma/context/*` |
| **1.5 Layer0 query** *(opt-in)* | Top-K bucketised cross-run memories injected as planner context | `gitoma/integrations/layer0.py` |
| **1.6 Semgrep scan** *(opt-in)* | Top-N high-severity in-code findings injected as planner context | `gitoma/integrations/semgrep_scan.py` |
| **1.7 Stack-shape** *(opt-in)* | Inferred (stack, level) → occam-trees canonical scaffold → missing-paths delta | `gitoma/planner/scaffold_shape.py` |
| **1.8 Trivy scan** *(opt-in)* | Top-N supply-chain findings (CVEs + secrets + IaC misconfigs) injected as planner context | `gitoma/integrations/trivy_scan.py` |
| **2   Plan** | Local LLM turns failing metrics + context blocks into an executable `TaskPlan` (or `--plan-from-file`) | `gitoma/planner/*` |
| **3-6 Execute + Critic stack** | Each subtask becomes a patch + commit; G1–G22 guards reject regressions with revert+retry | `gitoma/worker/*` |
| **PR** | Branch is pushed; PR is opened with a structured description | `gitoma/pr/*` |
| **Self-Review** | Adversarial critic reads the diff + posts findings on the PR | `gitoma/review/self_critic.py` |
| **7   Diary push** *(opt-in)* | Run summary auto-pushed as markdown entry to a remote log repo | `gitoma/cli/diary.py` |
| **8   Layer0 ingest** *(opt-in)* | Plan + guards + outcome written to per-repo namespace for future runs | `gitoma/cli/commands/run.py` |
| **Review** *(on demand)* | Copilot feedback is fetched; `--integrate` auto-fixes + pushes | `gitoma/review/*` |
| **Fix-CI** *(on demand)* | Reflexion dual-agent remediates broken GitHub Actions | `gitoma/review/reflexion.py` |

Read the full architecture, state machine, and threat model in the [**docs**](https://fabriziosalmi.github.io/gitoma/architecture/overview).

## Status

Gitoma passes **1949+ tests**, mypy strict, ruff clean, and a draconian security/UX audit across the CLI, the REST API, the MCP server, and the web cockpit. See the [security posture](https://fabriziosalmi.github.io/gitoma/architecture/security) page for the full threat model and [critic stack](https://fabriziosalmi.github.io/gitoma/architecture/critic-stack) for the engineering history of every guard.

**Current `main`**: post-v0.4 — ships PHASE 1.5/1.6/1.7/1.8 planner-context blocks, PHASE 7 auto-diary + repo-allowlist, PHASE 8 Layer0 ingest, the G16/G18/G19 orphan-symbol critic family, G20 (TOML/INI syntax), G21 (semgrep regression), G22 (trivy regression), `--plan-from-file` operator-curated plans, the `gitoma scaffold` vertical, the Ψ-lite scoring gate, and operator-side rotation scripts (`scripts/ops/`).

## License

MIT © Fabrizio Salmi
