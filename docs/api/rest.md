# REST endpoints

`gitoma serve` publishes a FastAPI application on the host and port you pass. The API is versioned under `/api/v1/*` and protected by a Bearer token. OpenAPI + Swagger are available at `/docs`.

Unless noted, every endpoint returns JSON and uses the uniform response model defined in [`gitoma/api/routers.py`](https://github.com/fabriziosalmi/gitoma/blob/main/gitoma/api/routers.py).

## Conventions

- **Bearer auth** on every `/api/v1/*` endpoint. Missing header → **401** with `WWW-Authenticate: Bearer`. Wrong token → **403**. Server without `GITOMA_API_TOKEN` → **503** (explicit misconfiguration, not a runtime bug).
- **Async dispatch endpoints** (`/run`, `/review`, `/analyze`, `/fix-ci`) return **`202 Accepted`** with a `JobResponse`. The work continues in the background; progress is available via `/status/{job_id}` or the SSE stream.
- **Every response** includes a correlation id in the `x-request-id` header. Clients can supply their own `x-request-id` and the server will echo it back — useful for end-to-end tracing.
- **Errors are opaque.** 5xx responses carry `{"detail": "Internal server error.", "error_id": "<hex>"}`. The full stack trace lives in the server log keyed by the same `error_id`. No client ever sees `str(exc)`.

See [Authentication](./auth) for the auth flow and [Streaming](./streaming) for the SSE protocol.

## `GET /api/v1/health`

Liveness + configuration check.

**Response** — `HealthResponse`

```json
{
  "status": "ok",
  "lm_studio": { "level": "ok", "message": "…", "available_models": ["…"] },
  "github_token_set": true
}
```

## `POST /api/v1/run`

Dispatch a full autonomous run.

**Request body** — `RunRequest`

| Field | Type | Constraints |
|---|---|---|
| `repo_url` | string | Must match `^https://github\.com/<owner>/<repo>(?:\.git)?/?$`. No embedded credentials. |
| `branch` | string or null | Optional. Must match `^(?!-)[A-Za-z0-9._/-]{1,255}$`. |
| `dry_run` | boolean | Default `false`. |

**Response — 202** — `JobResponse`

```json
{ "job_id": "uuid", "status": "started", "message": "Autonomous run dispatched in background." }
```

**Error responses**

- **422** — Pydantic validation failed. The default `input` field is **not** echoed (so a credentialed URL never bounces back to the caller). `detail` is a structured list: `[{"loc": ["body", "repo_url"], "msg": "…", "type": "value_error"}]`.

## `POST /api/v1/analyze`

Dispatch a read-only analyze job. Body: `AnalyzeRequest` with just `repo_url`. Returns **202** + `JobResponse`.

## `POST /api/v1/review`

Dispatch a review job.

**Request body** — `ReviewRequest`

| Field | Type | Notes |
|---|---|---|
| `repo_url` | string | Validated as above. |
| `integrate` | boolean | When `true`, the worker drives an LLM loop that proposes and pushes fixes. |

Returns **202** + `JobResponse`.

## `POST /api/v1/fix-ci`

Dispatch the Reflexion agent.

**Request body** — `RunRequest` (same schema, but `branch` is **required** here).

- **400** if `branch` is missing.

Returns **202** + `JobResponse`.

## `GET /api/v1/status/{job_id}`

Poll the status of a background job.

**Response** — `JobStatusResponse`

```json
{
  "job_id":         "uuid",
  "label":          "run",
  "status":         "running",
  "created_at":     "2026-04-21T10:00:00+00:00",
  "finished_at":    null,
  "lines_buffered": 42,
  "error_id":       null
}
```

`status` is one of `queued`, `running`, `completed`, `cancelled`, `timed_out`, or `failed`. On non-OK exits, `error_id` is populated — correlate with the server log (and the `x-request-id` header on this response).

- **404** if the job id is unknown.

## `POST /api/v1/jobs/{job_id}/cancel`

Request cancellation of a running job. The server sends `SIGTERM` to the CLI subprocess **and the whole process group** (`os.killpg` on POSIX); after 5 seconds without exit it escalates to `SIGKILL`. The response is immediate; the transition to `cancelled` is visible via `/status` or the SSE stream.

**Response** — `CancelResponse`

```json
{ "job_id": "uuid", "status": "cancelling" }
```

- **404** if the job id is unknown.
- **409** if the job is already terminal.

## `GET /api/v1/jobs`

List every job the server knows about. Finished jobs are evicted by TTL (15 min) or when the in-memory cap (50) is exceeded; running jobs are never evicted.

```json
{
  "uuid-1": {
    "status": "running", "label": "run", "lines": 42,
    "created_at": "…", "finished_at": null, "error_id": null
  }
}
```

## `GET /api/v1/stream/{job_id}`

Server-Sent Events stream of the job's merged stdout/stderr. Full protocol in [Streaming](./streaming).

## `DELETE /api/v1/state/{owner}/{name}`

Delete the persisted state for a repo. Idempotent — returns `{"result": "deleted"}` if state existed, `{"result": "not_found"}` otherwise. Does **not** touch the remote branch or PR.

## Global middlewares

- **GZip** for responses ≥ 1 KB (cockpit state snapshots compress ~5×).
- **Trusted-host** — rejects Host-header forgery. Allowed hosts default to `localhost`, loopback, `*.local`, `testserver`. Override with `GITOMA_ALLOWED_HOSTS`.
- **CORS** — off by default. Set `GITOMA_CORS_ORIGINS=https://your.app` to enable.
- **Request-id** — `x-request-id` is attached to every response (request-supplied or server-minted).

## Security headers on `/` (cockpit)

The public cockpit response carries:

- `Content-Security-Policy` — `default-src 'self'; script-src 'self' 'unsafe-inline'; …` (inline-script allowed only because the cockpit ships as a single self-contained HTML).
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`

See [Architecture → Security](/architecture/security) for the full threat model.
