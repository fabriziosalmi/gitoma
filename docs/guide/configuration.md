# Configuration

Gitoma reads configuration from four sources and merges them at runtime. Precedence from **highest** to **lowest**:

1. **Shell environment** — e.g. `GITHUB_TOKEN=… gitoma run …`
2. **`~/.gitoma/.env`** — dotenv file in the user config dir
3. **`<cwd>/.env`** — dotenv file in the current working directory
4. **`~/.gitoma/config.toml`** — persistent config written by `gitoma config set`

A shell env wins over any dotenv; a dotenv wins over the TOML. If you ever wonder "why is my token not picking up?", the answer is almost always that a higher-priority source has one.

## Inspecting the effective config

```bash
gitoma config show
```

Every line shows the value and its source. Example output:

```
── GitHub ──────────────────────────────────────────
  token         ********abc1   ~/.gitoma/config.toml

── LM Studio ───────────────────────────────────────
  base_url      http://localhost:1234/v1   default
  model         qwen2.5-coder:14b          ~/.gitoma/.env
  temperature   0.3                        default

── Cockpit API ─────────────────────────────────────
  token         ********xyz9   $ENV
```

`gitoma config set` warns before writing if a higher-priority source already has the key:

```
⚠  Your new GITHUB_TOKEN will be overridden at load time by:
   → $ENV
   Remove that source first, or edit it directly. Writing to config.toml anyway.
```

## Keys

| Key | Default | Notes |
|---|---|---|
| `GITHUB_TOKEN` | — | **Required.** Fine-grained or classic PAT. |
| `GITOMA_API_TOKEN` | auto-generated on first `gitoma serve` | Bearer token for `/api/v1/*`. Persisted to `~/.gitoma/runtime_token` (mode `0600`) when auto-generated. |
| `BOT_NAME` | `FabGPT` | Used as git commit author. |
| `BOT_EMAIL` | `fabgpt.inbox@gmail.com` | Used as git commit email. |
| `BOT_GITHUB_USER` | `fabgpt-coder` | Used by `gitoma doctor --push` to validate token-user match. |
| `LM_STUDIO_BASE_URL` | `http://localhost:1234/v1` | OpenAI-compatible base URL. Any endpoint that speaks the protocol. |
| `LM_STUDIO_MODEL` | `gemma-4-e2b-it` | Default model name. |
| `CRITIC_MODEL` | (empty → falls back to `LM_STUDIO_MODEL`) | Optional — a separate model used by the CI Reflexion critic and the self-review agent. |
| `LM_STUDIO_API_KEY` | `lm-studio` | Dummy key for local endpoints; set properly if pointing at a hosted OpenAI-compatible gateway. |
| `LM_STUDIO_TEMPERATURE` | `0.3` | |
| `LM_STUDIO_MAX_TOKENS` | `4096` | |

## Operational env vars (no persisted config)

These are shell-only and affect runtime behaviour without being part of `config show`:

| Variable | Effect |
|---|---|
| `GITOMA_BANNER` | `full`, `compact`, or `off`. Controls the CLI banner. Default: `compact` on TTY, `off` when piped. |
| `GITOMA_NO_EMOJI` | Any truthy value forces emoji glyphs to ASCII fallbacks — useful on minimal TTYs. |
| `GITOMA_PLAIN` | Force machine-friendly output regardless of TTY detection. `NO_COLOR` has the same effect. |
| `GITOMA_ALLOWED_HOSTS` | Comma-separated list accepted by the API's `TrustedHostMiddleware`. Default covers `localhost`, loopback IPs, `*.local`, `testserver`. |
| `GITOMA_CORS_ORIGINS` | Comma-separated list of origins allowed by the API's CORS middleware. Off by default. |
| `GITOMA_WS_ALLOWED_ORIGINS` | Browser origins allowed to connect to `/ws/state`. Default: localhost. |
| `GITOMA_MCP_REPO_ALLOWLIST` | Comma-separated `owner/repo` list. When set, every MCP tool refuses repos outside the list, regardless of the token's own scope. |

## Where Gitoma writes

All state is user-local. Gitoma has **no** central server, no account, no telemetry.

| Path | What's there |
|---|---|
| `~/.gitoma/config.toml` | Persistent config (set via `gitoma config set`). |
| `~/.gitoma/.env` | Optional dotenv — takes precedence over the TOML. |
| `~/.gitoma/runtime_token` | Auto-generated API Bearer token. Mode `0600`. Delete + restart to rotate. |
| `~/.gitoma/state/<slug>.json` | Per-repo run state (phase, task plan, PR URL, heartbeat, exit flag). |
| `~/.gitoma/state/<slug>.lock` | Concurrent-run lock. Held by `fcntl.flock`; released automatically on process death. |
| `~/.gitoma/logs/<slug>/<ts>.jsonl` | Structured trace per invocation. Retained up to 20 most recent per repo. |

Delete any of these at any time — Gitoma regenerates them on the next run.
