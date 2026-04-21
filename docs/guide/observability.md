# Observability

Gitoma is designed to be inspectable. Every non-trivial event during a run — phase transition, analyzer start, LLM request, git commit, PR action — is emitted as a structured JSON line. The same events drive the cockpit and the `gitoma logs` command.

## The trace file

Every invocation of `gitoma run`, `gitoma review`, or `gitoma fix-ci` opens a fresh trace file:

```
~/.gitoma/logs/<owner>__<repo>/<iso-timestamp>.jsonl
```

Each line is a self-contained JSON record:

```json
{
  "ts":    "2026-04-21T05:12:34.123+00:00",
  "slug":  "octocat__hello-world",
  "phase": "WORKING",
  "level": "info",
  "event": "git.commit",
  "data":  { "sha": "a1b2c3d", "subtask_id": "s0_0", "files": ["README.md"] }
}
```

The schema is stable:

| Field | Type | Meaning |
|---|---|---|
| `ts` | ISO 8601 UTC | Wall-clock timestamp of the event. |
| `slug` | `<owner>__<repo>` | Identifier for the run. |
| `phase` | string | Best-effort phase context. May be empty for early events. |
| `level` | `debug` / `info` / `warn` / `error` | Severity. |
| `event` | dotted namespace | Stable event name (`run.begin`, `phase.start`, `llm.call`, `git.commit`, `pr.open`, `run.exit_clean`, `run.crashed`, …). |
| `data` | object | Event-specific payload. Fields are additive across versions. |

Files are **append-only**, so standard tooling works: `tail -f`, `jq`, `grep`, or shipping them to a log aggregator.

## `gitoma logs`

Tail the most recent trace for a repo:

```bash
gitoma logs https://github.com/owner/repo
```

Stream new events as they arrive:

```bash
gitoma logs https://github.com/owner/repo --follow
```

Filter to a namespace:

```bash
gitoma logs https://github.com/owner/repo --filter phase.
gitoma logs https://github.com/owner/repo --filter llm.
```

Raw JSONL (for piping into `jq`):

```bash
gitoma logs https://github.com/owner/repo --raw | jq 'select(.event == "git.commit")'
```

## Retention

Gitoma keeps the **20 most recent traces per slug** and prunes older ones on the next run. If you need longer retention, copy the directory elsewhere or adjust `MAX_RUNS_PER_SLUG` in `gitoma/core/trace.py`.

## Heartbeat + orphan detection

While a run is active, a daemon thread in the CLI refreshes `state.last_heartbeat` every 30 seconds and writes the state file atomically. The cockpit — and `gitoma doctor --runs` — classify a run as **orphaned** when:

- the phase is non-terminal (`IDLE`, `ANALYZING`, `PLANNING`, `WORKING`, `PR_OPEN`, `REVIEWING`),
- and **not** flagged `exit_clean=True`,
- and either the owning PID is dead **or** the heartbeat is older than 90 seconds.

`exit_clean` is set to `True` on a normal exit (including `typer.Exit(0)` — e.g. a successful run that pauses at `PR_OPEN` waiting for human review) and reset to `False` on every fresh entry into the heartbeat context. Together these two signals distinguish "CLI finished cleanly, waiting for you" from "CLI died unexpectedly".

### Seeing orphans

```bash
gitoma doctor --runs
```

```
Tracked runs
────────────────────────────────────────────────────────────
Repo                Phase       PID     Heartbeat   Verdict
octocat/a           DONE        —       never       done
octocat/b           PR_OPEN     —       12m ago     idle
acme/c              WORKING     1234    15m ago     ORPHANED

⚠  1 orphaned run(s) detected.
  → Reset with: gitoma reset https://github.com/acme/c
```

The cockpit shows the same signal as an orange banner with the dead PID and the heartbeat age.

## What is *not* logged

By design, Gitoma never persists:

- **The LLM's raw chat payload** (unless you enable the optional telemetry dump in `core/telemetry.py`).
- **The Bearer token**, either the API one or the GitHub one.
- **Authenticated clone URLs** — the log redactor strips `https://user:pass@` basic-auth before publishing any line to the ring buffer or the trace.

If you fork Gitoma and add an event that could carry a secret, route it through the same sanitiser (`gitoma.api.routers._sanitize_line` for the live log path, or an equivalent for the trace).

## Integrating with external tooling

The trace is pure JSONL on disk — a few one-liners cover the common cases:

```bash
# Count LLM calls per phase over the last run.
gitoma logs <repo-url> --raw \
  | jq -r 'select(.event == "llm.call") | "\(.phase) \(.data.model)"' \
  | sort | uniq -c

# Grafana / Loki / Splunk: tail the file.
tail -F ~/.gitoma/logs/*/*.jsonl | your-log-shipper

# Grep a specific run for errors.
grep '"level":"error"' ~/.gitoma/logs/octocat__repo/*.jsonl
```

Everything is local, everything is inspectable.
