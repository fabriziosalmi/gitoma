<div align="center">

# Gitoma

**An autonomous agent that improves your GitHub repo.**

Analyzes. Plans. Commits. Opens a PR. Reviews its own work.

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

- **Local-first.** The LLM runs on your machine (LM Studio, Ollama, or any OpenAI-compatible endpoint). Code, diffs, and secrets never leave your laptop.
- **Resumable.** State is persisted per repo. Kill the CLI mid-run; `--resume` picks up at the last committed subtask.
- **Observable.** A live cockpit at `http://localhost:8000` streams every phase. Structured JSONL trace per invocation.
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

| Phase | What happens | Where |
|---|---|---|
| **Analyze** | Nine metric analyzers score the repo | `gitoma/analyzers/*` |
| **Plan** | Local LLM turns failing metrics into an executable `TaskPlan` | `gitoma/planner/*` |
| **Execute** | Each subtask becomes a patch + commit on a feature branch | `gitoma/worker/*` |
| **PR** | Branch is pushed; PR is opened with a structured description | `gitoma/pr/*` |
| **Self-Review** | Adversarial critic reads the diff + posts findings on the PR | `gitoma/review/self_critic.py` |
| **Review** *(on demand)* | Copilot feedback is fetched; `--integrate` auto-fixes + pushes | `gitoma/review/*` |
| **Fix-CI** *(on demand)* | Reflexion dual-agent remediates broken GitHub Actions | `gitoma/review/reflexion.py` |

Read the full architecture, state machine, and threat model in the [**docs**](https://fabriziosalmi.github.io/gitoma/architecture/overview).

## Status

Gitoma passes 250+ tests, mypy strict, ruff clean, and a draconian security/UX audit across the CLI, the REST API, the MCP server, and the web cockpit. See the [security posture](https://fabriziosalmi.github.io/gitoma/architecture/security) page for the full threat model.

## License

MIT © Fabrizio Salmi
