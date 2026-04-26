---
layout: home

hero:
  name: Gitoma
  text: An autonomous agent that improves your GitHub repo.
  tagline: Analyzes. Plans. Commits. Opens a pull request. Reviews its own work. Local-first by design — your code, diffs, and secrets never leave your machine.
  image:
    src: /logo.svg
    alt: Gitoma
  actions:
    - theme: brand
      text: Get started
      link: /guide/quickstart
    - theme: alt
      text: View on GitHub
      link: https://github.com/fabriziosalmi/gitoma

features:
  - title: Self-correcting guard stack
    details: 14 composable guards (G1–G14) catch specific classes of LLM patch slop — broken syntax, dropped functions, hallucinated frameworks/URLs/configs, README destruction. Plus two planner-time post-processors (Layer-A real-bug task synthesis, Layer-B README banishment) and an opt-in scalar Ψ-lite quality gate. Every guard was added in response to a specific live bench failure.
  - title: Local-first LLM
    details: Plans and writes patches with a model running on your machine — LM Studio, Ollama, or any OpenAI-compatible endpoint. No SaaS, no egress, no surprises on the bill.
  - title: Grounded by Occam Observer
    details: Optional integration with the companion Occam Observer gateway. Per-run fingerprint snapshot (declared deps, frameworks, manifests) is injected into the planner prompt as ground truth and consumed by the worker-side content-grounding guards. Pre-empts hallucinations at plan time.
  - title: Full pipeline, resumable
    details: Analyze → Plan → Execute → PR → Self-Review. Every phase persists state; a crash or kill is one `--resume` away from picking up at the last committed subtask.
  - title: Live web cockpit
    details: A read-only dashboard streams every phase over WebSocket, with keyboard shortcuts, command palette, and SSE live logs — no build step, no external deps.
  - title: REST + MCP
    details: Everything the CLI does is a Bearer-protected REST endpoint. A built-in MCP server also exposes GitHub context + write tools to Claude Desktop and any MCP client.
  - title: Hardened by default
    details: Constant-time auth, process-group isolation, credential redaction, WCAG AA contrast, CSP headers, focus trap in dialogs, prefers-reduced-motion. Audited surface end-to-end.
  - title: Structured observability
    details: Each run writes a JSONL trace under `~/.gitoma/logs/`. `gitoma logs --follow` tails it live; the cockpit renders phases, heartbeat, and orphan detection without polling the agent.
---

<style>
.VPHome {
  background:
    radial-gradient(1200px 400px at 80% -10%, rgba(0, 113, 227, 0.06), transparent 60%),
    radial-gradient(900px 300px at 20% 120%, rgba(0, 113, 227, 0.04), transparent 50%);
}
.dark .VPHome {
  background:
    radial-gradient(1200px 400px at 80% -10%, rgba(10, 132, 255, 0.10), transparent 60%),
    radial-gradient(900px 300px at 20% 120%, rgba(10, 132, 255, 0.06), transparent 50%);
}
.VPHero .image {
  /* Quieter logo render — smaller, more Apple. */
  max-width: 180px; margin-left: auto;
}
.VPHero .image-bg { display: none !important; }
</style>

<div style="max-width: 880px; margin: 0 auto; padding: 0 24px 5rem;">

## From zero to a pull request in three commands

```bash
pipx install gitoma
gitoma config set GITHUB_TOKEN=ghp_your_fine_grained_token
gitoma run https://github.com/<owner>/<repo>
```

Open the cockpit while it runs:

```bash
gitoma serve &
open http://localhost:8000
```

## Who it's for

- **Engineers** who maintain a portfolio of repositories and want a consistent baseline — tests, CI, docs, dependencies — without writing the same PR fifty times.
- **Teams** that need a trusted agent on a VPN to apply low-risk improvements to internal tools, with every write on an ergonomic audit trail.
- **Researchers** exploring autonomous coding agents: every prompt, every LLM I/O, every git op is traceable, inspectable, and replayable offline.

## What it does *not* do

- It does not ship your code to a third-party service. The LLM is yours.
- It does not merge PRs on its own. You keep the decision; the agent does the boring part.
- It does not touch protected paths. `.git/`, `.github/workflows/`, `.env*` are denylisted at the patcher level — even if the LLM is prompt-injected through the repo content, those are off-limits.

</div>
