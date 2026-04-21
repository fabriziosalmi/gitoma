# Authentication

Every `/api/v1/*` endpoint requires a Bearer token. The public cockpit (`/`) and the state WebSocket (`/ws/state`) do not — they are designed for localhost/VPN use and only expose derived state.

## The Bearer token

Clients authenticate by sending:

```
Authorization: Bearer <token>
```

The server compares the presented token against `GITOMA_API_TOKEN` using `secrets.compare_digest` — **constant-time**, immune to the byte-by-byte timing side-channels that a naïve `!=` would expose on any non-localhost deploy.

## Where the token comes from

There are two ways to pin it:

### Auto-generated on first start (default)

If no token is configured, `gitoma serve` generates one with `secrets.token_urlsafe(32)` on first start and persists it to `~/.gitoma/runtime_token` (mode `0600`, verified after write — if the filesystem can't honour POSIX permissions the token is deleted and the server aborts).

The token is printed once in the startup banner:

```
◉  New API token generated
   ────────────────────────
   <the-token>

   Persisted to ~/.gitoma/runtime_token (mode 0600).
   Paste into the cockpit Settings dialog when prompted.
   Delete that file and restart to rotate.
```

Subsequent starts reuse the persisted token until the file is deleted.

### Explicit

Set a token you control:

```bash
gitoma config set GITOMA_API_TOKEN=my-long-secret
# or
export GITOMA_API_TOKEN=my-long-secret
gitoma serve
```

Shell env beats the TOML — see [Configuration](/guide/configuration) for precedence.

## Status code semantics

Gitoma follows RFC 7235 strictly:

| Condition | Status | Extra |
|---|---|---|
| `Authorization` header missing or not `Bearer <…>` | **401** | `WWW-Authenticate: Bearer` header. Message: *"Missing or malformed Authorization header."* |
| Token does not match the server's configured value | **403** | *"Invalid authentication token."* |
| Server has no `GITOMA_API_TOKEN` set | **503** | *"GITOMA_API_TOKEN is not configured on the server."* — distinguishable from a runtime bug. |

The older FastAPI default (403 on missing header) was merged with 403 on wrong token, which makes automatic-retry clients misbehave. Gitoma opts out of the default behaviour by using `HTTPBearer(auto_error=False)` and returning the right status itself.

## Config cache

Reading `~/.gitoma/config.toml` + `.env` on every authenticated request is wasteful. The server caches the token after the first read and invalidates automatically when the mtime of `config.toml` or `.env` advances — so rotating via `gitoma config set` takes effect on the next request, without a server restart.

## Rotating the token

- **Auto-generated token**: delete `~/.gitoma/runtime_token` and restart `gitoma serve`. A fresh token is generated and printed.
- **Explicit token**: `gitoma config set GITOMA_API_TOKEN=<new>` or update your shell env. The cache picks up the new value on the next request.

## Cockpit storage

The web cockpit stores the token in **`sessionStorage`** scoped to the browser tab. Closing the tab clears it. A one-shot migration reads any token left in `localStorage` by a prior Gitoma version and moves it to `sessionStorage` on first page load, then removes the old key.

There is a **Clear** button in the Settings dialog for explicit logout.

## Threat model highlights

- **Not meant for public internet exposure.** The assumption is localhost or a trusted VPN. If you front Gitoma with a reverse proxy on a public hostname, combine Bearer auth with at least an IP allowlist and set `GITOMA_ALLOWED_HOSTS` / `GITOMA_CORS_ORIGINS` tightly.
- **No session cookies.** Every request carries the Bearer explicitly; there is no CSRF surface.
- **Env scrub on subprocess spawn.** The CLI dispatched by the API inherits a scrubbed environment — the server's `GITOMA_API_TOKEN` is stripped before spawn so it never lands in `/proc/<pid>/environ` or a CLI trace file.
- **Credential redaction.** Anything published to the SSE ring buffer has basic-auth credentials (`https://user:pass@…`) redacted proactively — defence-in-depth against LLM stack traces that print authenticated clone URLs.

Full details in [Architecture → Security](/architecture/security).
