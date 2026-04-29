# Pipeline + state machine

## The graph

The core states are `IDLE → ANALYZING → PLANNING → WORKING → PR_OPEN → REVIEWING → DONE`.
PHASE blocks numbered with a decimal (1.5 / 1.6 / 1.7 / 1.8 / 7 / 8) are
**opt-in context substrates** that thread through the core states without
replacing them — each silently skips when its substrate is unavailable
so the core flow always works.

```
                  gitoma run <url>
                        │
                        ▼
                      IDLE
                        │ acquire_run_lock (fcntl.flock)
                        ▼
                   ANALYZING                          ← registry.run + RepoBrief
                        │
                        ├── PHASE 1.5 (opt-in)        ← Layer0.search_grouped → planner context
                        ├── PHASE 1.6 (opt-in)        ← semgrep scan → planner context + G21 baseline
                        ├── PHASE 1.7 (opt-in)        ← occam-trees stack-shape → planner context
                        ├── PHASE 1.8 (opt-in)        ← trivy scan → planner context + G22 baseline
                        ▼
                   PLANNING (PHASE 2)                 ← planner.plan(report, file_tree, …context)
                        │                               OR --plan-from-file (skip LLM)
                        │ confirm
                        ▼
                   WORKING (PHASE 3-6)                ← worker.execute(plan, callbacks)
                        │ for each subtask:
                        │   LLM → patches → apply → critic stack G1..G22 → commit
                        ▼
                   PR_OPEN                            ← push + pr_agent.open_pr
                        │
                        ├── (--no-self-review) skip →
                        ▼
                   [self-review comment posted]
                        │
                        ├── (--no-ci-watch) skip →
                        ▼
                   [watching GitHub Actions]
                        │
                        ├── CI success → continue
                        ├── CI failure → [auto fix-ci] → re-watch → continue
                        └── CI timeout → warn, continue
                        │
                        ├── PHASE 7 (opt-in)          ← diary.write_diary_entry → public/private log repo
                        ├── PHASE 8 (opt-in)          ← Layer0.ingest_one × N (plan + guards + outcome)
                        ▼
            [pause — user invokes `gitoma review` later]
                        │
                        ▼
                   REVIEWING                          ← watcher.fetch + integrator.integrate
                        │
                        ▼
                      DONE
```

## Optional PHASE blocks at a glance

| PHASE | Substrate | Trigger | Opt-out |
|---|---|---|---|
| 1.5 | Layer0 gRPC server | `LAYER0_GRPC_URL` set + reachable | unset env |
| 1.6 | semgrep CLI binary | `semgrep` on PATH | `GITOMA_PHASE16_OFF=1` |
| 1.7 | occam-trees HTTP server | `OCCAM_TREES_URL` set + reachable | `GITOMA_PHASE17_OFF=1` |
| 1.8 | trivy CLI binary | `trivy` on PATH | `GITOMA_PHASE18_OFF=1` |
| 7   | git push to remote diary repo | `GITOMA_DIARY_REPO` + `GITOMA_DIARY_TOKEN` set | unset either; `GITOMA_DIARY_REPO_ALLOWLIST` further filters |
| 8   | Layer0 gRPC server (write side) | same as 1.5 | unset env |

Critic-side regression gates that consume PHASE 1.6 / 1.8 baselines:

| Critic | Reads baseline from | Trigger |
|---|---|---|
| **G21** | PHASE 1.6 semgrep | `GITOMA_G21_SEMGREP=1` (default OFF) |
| **G22** | PHASE 1.8 trivy | `GITOMA_G22_TRIVY=1` (default OFF) |

## Each phase, precisely

### IDLE → ANALYZING

The CLI acquires a kernel-held `fcntl.flock` on `~/.gitoma/state/<slug>.lock`. A second `gitoma run` on the same slug finds the lock held and exits with a message pointing at the holder PID. The lock is released automatically when the process dies by any signal — the kernel owns ownership, not a file on disk.

The heartbeat daemon thread starts. It refreshes `state.last_heartbeat` and atomically rewrites the state file every 30 seconds. It also resets `state.exit_clean = False` on entry (so an orphaned restart doesn't inherit a stale clean flag from a previous PR_OPEN exit).

### ANALYZING

`AnalyzerRegistry` iterates every registered `Analyzer` subclass — README, CI, tests, security, code quality, deps, docs, license, structure. Each returns a `MetricResult` with a score (0..1), status (`pass` / `warn` / `fail`), and a short details string. The aggregate `MetricReport` is attached to the state.

No LLM calls yet. This phase is read-only; if `--dry-run` is the only thing you run after analyze, no branch is created.

### PLANNING

The planner feeds the failing + warning metrics to the LLM and asks for a structured `TaskPlan`. Prompt template lives in `planner/prompts.py`; the output is forced to JSON via the model's structured-output mode where available.

A `TaskPlan` is a list of `Task`s, each of which is a list of `SubTask`s. Each subtask is an atomic unit of work with a commit message and a set of file hints.

If the plan is empty, the phase short-circuits to DONE with a "repo already in great shape" message.

### WORKING

For each subtask:

1. Worker reads the hinted files (capped at 3) to limit context size.
2. Worker calls the LLM with `chat_json` — the response is a list of patches + a commit message.
3. The patcher applies each patch. Guards enforced at the patcher layer (even if the LLM is prompt-injected):
   - **Containment** — target path must be strictly inside the repo root (`Path.is_relative_to`).
   - **Denylist** — `.git/`, `.github/workflows/`, `.github/actions/`, `.env*`, `.netrc`, `.pypirc`, `.gitmodules` are off-limits.
   - **Size cap** — 2 MiB per file.
   - **O_NOFOLLOW** — symlinks planted between resolve() and write are refused.
4. The committer stages and commits the touched paths with the bot identity.
5. State is persisted after every subtask — `--resume` restarts at the first uncommitted one.

Per-subtask failures mark the subtask `failed`, mark the parent task `failed`, **but do not abort the whole plan**. Other tasks continue. Errors are accumulated into `state.errors` and surfaced in the cockpit's red banner.

### PR_OPEN

`PRAgent` pushes the feature branch via the authenticated remote URL and opens the PR through the GitHub API. The PR body is structured: the metric report, the task plan, the list of commits.

State advances to `PR_OPEN`. If the run stops here (default `gitoma run` behaviour when you haven't asked for review/integration), the heartbeat context flips `state.exit_clean = True` on the way out — distinguishing "CLI finished cleanly, PR waiting for you" from "CLI crashed at PR_OPEN".

### Self-review (Phase 5)

The adversarial critic reads the diff of the freshly-opened PR and posts a structured comment summarising findings by severity (blocker / major / minor / nit). **Never re-raises** — a failed self-review doesn't undo a successful PR open; it's just logged.

Opt out with `--no-self-review` on `gitoma run`.

### CI watch + auto-remediate (Phase 6)

After the self-review, the CLI polls the latest GitHub Actions workflow run on the feature branch every 30 seconds, up to 20 minutes total. Three outcomes:

- **`success`** — narrate + return. The run is as merge-ready as the agent can make it on its own.
- **`failure`** — invoke the existing `CIDiagnosticAgent` (the same one `gitoma fix-ci` drives) **once**. If the critic approves the proposed patch, it's pushed; GitHub triggers a fresh workflow run; the watcher re-polls. If the second run also fails, the phase ends with `failure` and the user is told to invoke `gitoma fix-ci` manually.
- **`timeout`** — the budget expired while CI was still pending. The phase ends with `timeout`; the PR is still open. The user decides how to proceed.

Never re-raises; any unhandled exception from the fix-ci agent is caught and logged. Transient probe failures are treated as `pending` so a single network blip doesn't abort the watch.

Every poll + decision is traced as a `ci.watch.*` event in the run's JSONL log; the cockpit's `current_operation` narrates live ("Watching CI — pending (2m 30s)").

Two opt-out flags:

- `--no-ci-watch` — skip Phase 6 entirely. Behaviour matches the pre-feature `gitoma run`.
- `--no-auto-fix-ci` — watch CI and surface pass/fail, but don't invoke the Reflexion agent on failure.

### REVIEWING (on-demand)

`gitoma review <url> --integrate` reopens the state, fetches every review comment on the PR (Copilot bots, human reviewers), and drives a ReviewIntegrator loop: for each comment, call the LLM, apply + commit fixes, push. Without `--integrate`, this phase is read-only.

### DONE

Terminal. State stays on disk for inspection; a fresh `gitoma run` on the same slug requires `--reset` (or `gitoma reset <url>`) to start over.

## Fix-CI Reflexion

`gitoma fix-ci <url> --branch <b>` is a separate entry point that doesn't move the main state machine. It runs the **dual-agent Reflexion pattern**:

```
Fixer → proposes patch → Critic → approve?
  ├─ yes → apply + commit + push
  └─ no → feedback → Fixer (up to MAX_RETRIES=3)
```

Both agents are LLMs. The Fixer has access to the CI logs; the Critic reads the Fixer's patch proposal and the same logs, and returns `{approved, feedback}`. Approved patches go through the same `apply_patches` + commit + push pipeline as the main worker. Rejected patches loop back with the Critic's feedback as additional context. After MAX_RETRIES the circuit breaker trips and the run flags human intervention.

The `CRITIC_MODEL` env var lets you run Fixer and Critic on **different** models — a smaller, faster fixer and a larger, pickier critic is a common and effective pairing.

## Timing boundaries

- **Per-job runtime cap** (REST): 1 hour. `asyncio.wait_for` on the subprocess. Exceeded → status becomes `timed_out`, whole process group gets SIGTERM then SIGKILL.
- **Subprocess cancel grace**: 5 seconds between SIGTERM and SIGKILL.
- **Heartbeat interval**: 30 seconds.
- **Orphan threshold**: 90 seconds without a heartbeat **or** dead owning PID, on a non-terminal, non-clean-exit state.
- **SSE heartbeat**: 15 seconds (comment frame every interval of silence).
- **Job record TTL**: 15 minutes after finish, 50-record hard cap.
