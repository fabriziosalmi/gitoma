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


# ── Config cache ─────────────────────────────────────────────────────────────
# ``load_config()`` walks the dotenv file + TOML on every call. Doing that
# per authenticated request is pure disk I/O for a value that almost
# never changes. We cache the API token and refresh only when the backing
# file's mtime moves forward — or when tests call ``_reset_token_cache()``.

_cached_token: str = ""
_cached_mtime: tuple[float, float] = (-1.0, -1.0)


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
    global _cached_token, _cached_mtime
    _cached_token = ""
    _cached_mtime = (-1.0, -1.0)


def _current_api_token() -> str:
    """Return the configured Bearer token, cached until config files change.

    If neither ``config.toml`` nor ``.env`` exists on disk (mtime == 0), we
    never cache — the token has to come from the shell env, which can
    change without a file mtime, and tests rely on that re-read semantic.
    """
    global _cached_token, _cached_mtime
    mt = _config_mtimes()
    # mtime of 0.0 means "no file" — don't cache in that case, since the
    # token lives in os.environ and could be monkey-patched by tests.
    if mt == (0.0, 0.0):
        return load_config().api_auth_token
    if mt != _cached_mtime:
        _cached_token = load_config().api_auth_token
        _cached_mtime = mt
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
app.add_middleware(GZipMiddleware, minimum_size=1024)

# TrustedHost denies Host-header forgery. Default allows localhost and the
# loopback IPs plus ``testserver`` (FastAPI TestClient's default Host); ops
# opening the API to a LAN override via env.
_allowed_hosts = os.getenv(
    "GITOMA_ALLOWED_HOSTS",
    "localhost,127.0.0.1,0.0.0.0,*.local,testserver",
).split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

# CORS off by default (we assume localhost). Set GITOMA_CORS_ORIGINS to a
# comma-separated list of origins to open the API to a browser app.
_cors_origins_raw = os.getenv("GITOMA_CORS_ORIGINS", "").strip()
if _cors_origins_raw:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins_raw.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


# ── Exception handlers ───────────────────────────────────────────────────────


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a request id to every response + log line.

    Cheap to compute, invaluable for correlating a client-side error with
    the server log line that produced it. The id is set on
    ``request.state.request_id`` so downstream handlers can include it
    without recomputing. Clients can also supply ``X-Request-ID`` and we
    preserve it — useful for end-to-end tracing from the cockpit.
    """
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response


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
