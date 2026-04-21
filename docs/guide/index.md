# Introduction

Gitoma is a command-line agent that improves a GitHub repository end to end. It reads the repo, decides what to do, commits fixes task by task on a feature branch, opens a pull request with a structured description, and runs an adversarial self-review on its own diff.

The loop looks like this:

```
ANALYZE  →  PLAN  →  EXECUTE  →  PR  →  SELF-REVIEW  →  REVIEW (you)
```

Every phase is observable, resumable, and persists its state on disk. The LLM runs locally — on LM Studio, Ollama, or any OpenAI-compatible endpoint — so source code and diffs never leave the machine you invoke Gitoma from.

## Why another coding agent

Most coding agents sit between your editor and a SaaS model. Gitoma is the opposite:

- **The agent is yours, the model is yours, the state is yours.** There is no backend service. `pipx install gitoma` is enough to run it; `gitoma serve` is enough to expose it to a cockpit or a cron job.
- **The blast radius is tight.** The worker only writes patches inside the repo under clone, with a hardened denylist for `.git/`, GitHub Actions workflows, and `.env*` files. The MCP write tools have per-repo allow-lists and size caps.
- **Crashes are boring.** If the CLI dies mid-run — SIGKILL, OOM, terminal closed — the state file survives, the orphan detector flags it in the cockpit, and `--resume` restarts at the last committed subtask.

## Where to go next

- [**Prerequisites**](./prerequisites) — what you need installed before your first run.
- [**Install**](./install) — pipx, pip, or source.
- [**Quickstart**](./quickstart) — three commands to your first pull request.
- [**CLI reference**](./cli) — every command and every flag.
- [**Web cockpit**](./cockpit) — the live dashboard and keyboard shortcuts.
- [**Configuration**](./configuration) — env vars, `config.toml`, precedence rules.
- [**Observability**](./observability) — trace files, `gitoma logs`, orphan detection.

If you are looking for the REST API or the MCP server, jump to [API reference](/api/rest).
