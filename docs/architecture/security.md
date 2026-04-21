# Security + threat model

Gitoma's threat model is driven by three facts:

1. **It runs on a developer's machine or an internal VPN**, not on the public internet.
2. **The LLM it asks for patches is potentially prompt-injected** through the repo content it's asked to operate on.
3. **It holds a GitHub token with write scope**, plus (when `gitoma serve` is running) a Bearer token that unlocks every dispatch endpoint.

Every layer below exists to keep those three facts from combining into a compromise.

## Assets worth protecting

| Asset | Sensitivity | Where it lives |
|---|---|---|
| GitHub PAT | **High** — write access to your repos. | `~/.gitoma/config.toml` / env / `~/.gitoma/.env` |
| API Bearer token | **High** — unlocks every dispatch endpoint. | `~/.gitoma/runtime_token` (`0600`) or env |
| Source code of target repos | **Medium** — your private code. | Temp clone under `/tmp/gitoma_*` |
| Run state + trace | **Low** — metadata, no secrets. | `~/.gitoma/state/`, `~/.gitoma/logs/` |

## What Gitoma does *not* do

- **No telemetry, no phone-home.** There is no central backend. Nothing is sent outside your machine except to the GitHub API (for read/write on your repos) and to the local LLM endpoint (which you run).
- **No third-party SDKs with outbound calls at import.** Dependencies are pinned and reviewed.
- **No code generation at runtime.** Gitoma never `eval()`s, `exec()`s, or imports dynamic modules from untrusted input.

## Worker / patcher hardening

The worker is the privileged part of Gitoma — it writes to your working tree.

**Containment** — every LLM-proposed path is resolved with `Path.resolve()` and checked with `Path.is_relative_to(repo_root)`. The older `str.startswith` check (which accepts `/tmp/foo` as a prefix of `/tmp/foo-evil`) is gone.

**Denylist** — the following paths are **refused** regardless of containment:

- `.git/` at any depth
- `.github/workflows/`, `.github/actions/` — a malicious LLM can't spawn a workflow that exfiltrates secrets
- `.env`, `.envrc`, `.netrc`, `.pypirc`, `.env.*` — credential files
- `.gitmodules`, `.gitattributes`

**Size cap** — 2 MiB per file. A runaway LLM (or an attacker) can't fill the disk through a single write.

**Symlink TOCTOU** — writes go through `os.open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW)` where supported. A symlink planted between `resolve()` and the write is refused.

## API surface hardening

`gitoma serve` is hardened end-to-end.

**Auth**:

- Bearer on every `/api/v1/*` endpoint.
- `secrets.compare_digest` for constant-time token compare.
- RFC 7235 401 vs 403 distinction (missing header vs wrong token).
- Server without a configured token → fail-closed 503, not runtime 500.

**Request validation**:

- Pydantic `field_validator`s on `repo_url` and `branch` reject embedded credentials, wrong schemes, CLI-flag-looking branches at the edge (422). A malformed value never reaches `typer` as argv.
- Custom `RequestValidationError` handler drops the default `input` echo — so a credentialed URL submitted mistakenly never bounces back to the caller.

**Subprocess isolation**:

- Spawned with `preexec_fn=os.setsid` on POSIX — the CLI runs in its own session.
- Cancel / timeout paths use `os.killpg(pgid, SIGTERM)` then SIGKILL after 5 s, so `git`, `ssh`, or any other child dies too.
- `env=` is scrubbed of `GITOMA_API_TOKEN` before spawn — the CLI never inherits the server's own Bearer token.

**Log hygiene**:

- URL credentials (`https://user:pass@…`, `ssh://user:pass@…`) are redacted in every line published to the ring buffer.
- Per-line cap of 4 KiB with a visible `…(truncated)` marker.
- SSE stream uses drop-oldest back-pressure — a slow client can't stall the producer.

**Error surface**:

- Every exception not wrapped by a specific handler routes through the global handler and returns `{"detail": "Internal server error.", "error_id": "<hex>"}`. The stack trace goes to the server log keyed by the same `error_id`.
- Job statuses are restricted to a fixed enum (`queued|running|completed|cancelled|timed_out|failed`). The old `"failed: <str(exc)>"` format — which leaked paths via the repr — is gone.

**Transport middleware**:

- `TrustedHostMiddleware` — default allowlist is localhost + loopback + `testserver`. Override with `GITOMA_ALLOWED_HOSTS`.
- `CORSMiddleware` — off by default. Set `GITOMA_CORS_ORIGINS` to enable for a specific frontend.
- `GZipMiddleware` — compresses the ~100 KB cockpit state snapshots.

**Cockpit (public) response headers**:

- `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; …` — `unsafe-inline` is accepted only because the cockpit is a single self-contained HTML with inline script + style; the CSP still blocks external origins and eval.
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`

**WebSocket origin check**:

- `/ws/state` inspects the `Origin` header on handshake and refuses browser origins outside `GITOMA_WS_ALLOWED_ORIGINS` (default: localhost). WebSockets skip CORS preflight; this is the only layer that stops a drive-by page from subscribing. Non-browser clients (no Origin header) are allowed through, because the state they'd see carries no credentials.

## MCP hardening

**Logging discipline** — the server forces `logging.basicConfig(stream=sys.stderr, force=True)` at module load. MCP over stdio reserves stdout for the protocol; any `print()` leak to stdout would desync the framer. See [API → MCP](../api/mcp#stdio-protocol-hygiene).

**Input size caps** on every write tool — 2 MiB for file content, 300 chars for PR title, 65 536 chars for PR body, 20 labels max.

**Repo allow-list** — `GITOMA_MCP_REPO_ALLOWLIST` pins the server to a fixed set regardless of the token's scope.

**Rate-limit backoff** — every write tool wrapped in tenacity-like retry with exponential backoff + jitter on GitHub's abuse detection signal.

**Error envelope sanitisation** — classified `code` ("forbidden", "rate_limited", "invalid_input", …) + short message. `str(exc)` is never echoed back.

**Idempotent `open_pr`** — retries on a flaky client don't produce duplicate PRs.

## CI / build posture

- No network access at test time. Everything runs in-process with mocks or `TestClient`.
- Ruff + mypy strict on every push — zero warnings policy.
- `pytest` as a gate. No test is `xfail` or `skip`ed without an explicit reason.

## What's *not* in the threat model

- **Malicious GitHub org membership.** If the token you configure has write access to an org you don't control, Gitoma has the same access. Use a fine-grained PAT pinned to specific repos.
- **A compromised local machine.** Gitoma trusts the environment it runs in. Defence against a local attacker with filesystem access is out of scope.
- **A compromised LLM endpoint.** If you point `LM_STUDIO_BASE_URL` at a malicious server, the server can return hostile patches. The patcher denylist + containment + size caps limit the damage, but the final line of defence is your code review before you merge.

## Reporting a vulnerability

Open a private security advisory on the GitHub repo. Do not disclose issues publicly until we've had a chance to patch.
