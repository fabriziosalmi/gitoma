"""FastAPI application initialization.

The top-level app wires together:

* the **Bearer-protected** ``/api/v1/*`` router from :mod:`gitoma.api.routers`
* the **public, read-only** ``/`` + ``/ws/state`` cockpit router from
  :mod:`gitoma.api.web`
* a lifespan hook that reaps running CLI subprocess on shutdown so a
  ``Ctrl-C`` on the server doesn't leave orphan ``gitoma`` processes.

Hardening additions (industrial-grade pass):

* **Constant-time** Bearer compare via :func:`secrets.compare_digest`.
* **401 vs 403**: missing header → 401 (RFC 7235 "needs auth"), wrong
  token → 403 (auth present, insufficient).
* **Cached config**: reading ``~/.gitoma/config.toml`` + ``.env`` on every
  request is wasteful. We memoise the Bearer token after startup and only
  reload when the underlying file's mtime changes.
* **Global exception handler** so a surprise ``ZeroDivisionError`` doesn't
  leak a traceback to the client — clients get an opaque ``error_id``
  they can correlate with the server log.
* **Middlewares**: GZip (the cockpit state snapshots are up to ~100 KB of
  JSON), TrustedHost (reject Host-header forgery by default), and an
  optional CORS stack driven by a single env var so operators can open
  the API to a specific frontend without editing code.
* **Structured startup banner** for sanity while debugging.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gitoma import __version__
from gitoma.api.routers import cancel_all_jobs, router
from gitoma.api.web import web_router
from gitoma.core.config import CONFIG_FILE, ENV_FILE, load_config

logger = logging.getLogger(__name__)


# ── Streaming-aware gzip wrapper ────────────────────────────────────────────
#
# ``GZipMiddleware`` happily compresses ``text/event-stream`` responses,
# which destroys SSE: the compressor buffers bytes until its threshold
# fires, so heartbeat comments and individual log lines never reach the
# browser in real time. Reverse proxies see no traffic for >30 s and drop
# the connection. We bypass the gzip layer for the SSE prefix and let
# everything else through unchanged.

# Path prefixes that must never go through gzip — kept narrow so we don't
# accidentally exempt ordinary JSON endpoints.
_GZIP_BYPASS_PREFIXES: tuple[str, ...] = (
    "/api/v1/stream/",   # SSE: per-line streaming with heartbeats
    "/ws/",              # WebSocket upgrade — already excluded by ASGI type, defensive
)


class _ConditionalGZip:
    """ASGI wrapper that skips ``GZipMiddleware`` for streaming endpoints.

    Built once at import; the inner gzip middleware is allocated lazily so
    we never run a request through both paths. The decision is made on the
    raw scope so we don't have to peek at response headers (which would
    require buffering — exactly what SSE can't tolerate).
    """

    def __init__(self, app, **gzip_kwargs):
        self._app = app
        self._gzip = GZipMiddleware(app, **gzip_kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "") or ""
            if path.startswith(_GZIP_BYPASS_PREFIXES):
                await self._app(scope, receive, send)
                return
        await self._gzip(scope, receive, send)


# ── Config cache ─────────────────────────────────────────────────────────────
# ``load_config()`` walks the dotenv file + TOML on every call. Doing that
# per authenticated request is pure disk I/O for a value that almost
# never changes. We cache the API token and refresh whenever any of the
# inputs that produce it move:
#
#   * ``config.toml`` mtime (TOML source)
#   * ``.env`` mtime (dotenv source)
#   * ``os.environ.get("GITOMA_API_TOKEN")`` snapshot — env wins over
#     both files in load_config(), so an operator rotating the token via
#     ``export GITOMA_API_TOKEN=…`` MUST be picked up. The previous cache
#     keyed only on file mtimes silently masked env-driven rotations.

_cached_token: str = ""
# Cache key triple: (toml_mtime, env_mtime, env_token_snapshot). We
# include the env snapshot literally — it's the rotation channel that
# doesn't bump any file's mtime, so it has to live in the key.
_cached_key: tuple[float, float, str] = (-1.0, -1.0, "\x00")


def _config_mtimes() -> tuple[float, float]:
    """Return (config.toml mtime, .env mtime); missing files ⇒ 0.0."""

    def _m(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    return (_m(CONFIG_FILE), _m(ENV_FILE))


def _reset_token_cache() -> None:
    """Force the next ``verify_token`` call to re-read the config.

    Used by tests that monkey-patch :func:`gitoma.core.config.load_config`
    — without the reset, an earlier test's token stays cached and the
    patched function never gets called.
    """
    global _cached_token, _cached_key
    _cached_token = ""
    _cached_key = (-1.0, -1.0, "\x00")


def _current_api_token() -> str:
    """Return the configured Bearer token, cached until any input moves.

    The cache key is (toml_mtime, env_mtime, GITOMA_API_TOKEN_in_env). The
    env snapshot is essential: ``load_config()`` lets the env var override
    file values, so an operator running ``export GITOMA_API_TOKEN=newone``
    expects that to take effect on the very next request — not after a
    config-file edit. The previous cache (mtime-only) made env rotations
    silently invisible, which is exactly the kind of failure mode that
    masks a credential leak in production.
    """
    global _cached_token, _cached_key
    # Sentinel "\x00" distinguishes "env unset" from "env set to empty
    # string" — both are meaningful and resolve to different load_config()
    # paths via dotenv's defaults.
    env_snapshot = os.environ.get("GITOMA_API_TOKEN", "\x00")
    key = (*_config_mtimes(), env_snapshot)
    if key != _cached_key:
        _cached_token = load_config().api_auth_token
        _cached_key = key
    return _cached_token


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """App-wide lifespan.

    Startup: warm the config cache and emit a single structured banner so
    ops can confirm which version is live and whether auth is wired.
    Shutdown: reap every running CLI subprocess so no orphan ``gitoma``
    processes are left behind if the operator Ctrl-C's ``uvicorn``.
    """
    _ = _current_api_token()  # warm cache
    has_token = bool(_cached_token)
    logger.info(
        "gitoma_api_ready",
        extra={
            "version": __version__,
            "auth": "bearer" if has_token else "UNCONFIGURED",
            "config_file": str(CONFIG_FILE),
        },
    )
    if not has_token:
        logger.warning(
            "gitoma_api_no_token_configured — every /api/v1/* request will 503 "
            "until GITOMA_API_TOKEN is set (server-side)"
        )

    try:
        yield
    finally:
        await cancel_all_jobs()


# ── Auth ─────────────────────────────────────────────────────────────────────

# ``auto_error=False`` so we control the 401 vs 403 distinction ourselves —
# RFC 7235 is clear: absent credentials ⇒ 401 WWW-Authenticate, wrong
# credentials ⇒ 403. FastAPI's default merges them into 403 which breaks
# automatic client retry logic.
auth_scheme = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(auth_scheme),
) -> None:
    """Validate the Bearer token against ``GITOMA_API_TOKEN`` in cached config."""
    expected = _current_api_token()
    if not expected:
        # Fail-closed: without a server-side token, auth cannot be validated.
        # 503 (not 500) so clients can distinguish "server misconfigured"
        # from "unexpected bug" and surface a clear remediation message.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITOMA_API_TOKEN is not configured on the server.",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Constant-time compare: `!=` leaks the token byte-by-byte via response
    # timing. Irrelevant on localhost, critical on LAN / VPN deploys.
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication token.",
        )


# ── App + middleware stack ───────────────────────────────────────────────────


app = FastAPI(
    title="Gitoma API",
    description=(
        "Autonomous GitHub Agent REST API. Dispatches local LLM agents that "
        "analyse, plan, commit, and open pull requests on your behalf. All "
        "`/api/v1/*` endpoints require a Bearer token; the `/` cockpit and "
        "`/ws/state` WebSocket are public on the assumption the server runs "
        "on localhost or a trusted VPN."
    ),
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "system", "description": "Liveness + config checks"},
        {"name": "jobs", "description": "Async CLI dispatch + status + SSE"},
        {"name": "state", "description": "Persisted agent state per repo"},
    ],
)

# GZip any response bigger than 1 KB. Cockpit state snapshots are mostly
# JSON text and compress ~5× — worth it on flaky links or LAN tunnels.
# Wrapped so /api/v1/stream/* (SSE) and /ws/* (WebSocket) bypass the
# gzip layer — both rely on un-buffered, real-time delivery.
app.add_middleware(_ConditionalGZip, minimum_size=1024)

# TrustedHost denies Host-header forgery. Default: loopback only +
# ``testserver`` (FastAPI TestClient's default). The previous default also
# included ``*.local``, which on a LAN with mDNS made the host reachable
# under any ``<name>.local`` — combined with the WS state stream, anyone
# on the same broadcast domain could sniff run state. Operators who
# legitimately need ``.local`` access can opt in via env.
_allowed_hosts = os.getenv(
    "GITOMA_ALLOWED_HOSTS",
    "localhost,127.0.0.1,0.0.0.0,testserver",
).split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

# CORS off by default (we assume localhost). Set GITOMA_CORS_ORIGINS to a
# comma-separated list of origins to open the API to a browser app.
#
# The wildcard ``*`` combined with ``allow_credentials=True`` is silently
# rejected by every browser per the CORS spec (forbidden combination). The
# server would still emit the Access-Control-* headers and the request
# would fail in the browser dev console with no server-side trace — a
# classic opaque-misconfig footgun. We refuse to install the middleware
# in that shape and emit a loud startup warning so the operator gets a
# clear remediation path instead of "my cockpit doesn't work".
_cors_origins_raw = os.getenv("GITOMA_CORS_ORIGINS", "").strip()
if _cors_origins_raw:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    if "*" in _cors_origins:
        # The combination is unusable. Loud refusal beats silent breakage.
        logger.error(
            "gitoma_cors_wildcard_with_credentials_rejected",
            extra={
                "configured": _cors_origins,
                "remediation": (
                    "GITOMA_CORS_ORIGINS=* is incompatible with the "
                    "Bearer auth model (browsers refuse credentials with "
                    "wildcard origin). Set explicit origins like "
                    "https://cockpit.example.com instead."
                ),
            },
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )


# ── Exception handlers ───────────────────────────────────────────────────────


import time as _time  # placed at use-site to keep import section tidy


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a request id + emit a structured access log per request.

    The request id correlates a client-side error with the server log line
    that produced it. Clients can supply ``X-Request-ID`` and we preserve
    it — useful for end-to-end tracing from the cockpit.

    The access log records method/path/status/duration/client — the
    minimum kit for incident response. We deliberately log only the
    ``Authorization`` header's *presence*, never its value, so a debug
    log dump can't leak the bearer token. Health-probe spam from k8s/LB
    is demoted to DEBUG so production INFO logs stay readable.
    """
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    started = _time.perf_counter()
    status_code: int = 500  # default if call_next throws before responding
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["x-request-id"] = rid
        return response
    finally:
        duration_ms = (_time.perf_counter() - started) * 1000.0
        # Demote noisy probes so prod logs stay readable.
        is_probe = request.url.path in ("/api/v1/health", "/")
        log = logger.debug if is_probe else logger.info
        client = request.client
        log(
            "http_access",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "duration_ms": round(duration_ms, 1),
                "client_ip": client.host if client else None,
                # Authorization presence only — never the value.
                "auth_present": "authorization" in request.headers,
            },
        )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Turn any uncaught exception into an opaque 500.

    The client learns *something broke* and a correlation id; the real
    stack trace goes to the server log keyed by the same id. Under no
    circumstances do we echo ``str(exc)`` back — that's routinely how
    path disclosure / stack-trace leaks happen.
    """
    rid = getattr(request.state, "request_id", "unknown")
    logger.exception("unhandled_exception", extra={"request_id": rid})
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
            "error_id": rid,
        },
        headers={"x-request-id": rid},
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Uniform 422 envelope that does not leak internal locations.

    The default FastAPI handler includes ``loc``, ``input`` and the
    request body verbatim. We keep ``loc`` + ``msg`` but drop ``input``
    so a validator that rejects e.g. a malformed token never echoes the
    token back.
    """
    rid = getattr(request.state, "request_id", "unknown")
    errors = [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "error_id": rid},
        headers={"x-request-id": rid},
    )


# ── Routers ──────────────────────────────────────────────────────────────────

# /api/v1/* — Bearer-protected REST
app.include_router(router, dependencies=[Depends(verify_token)])

# /, /ws/state — public read-only web cockpit (intended for localhost / VPN)
app.include_router(web_router)
