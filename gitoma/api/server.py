"""FastAPI application initialization."""

import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gitoma import __version__
from gitoma.api.routers import cancel_all_jobs, router
from gitoma.api.web import web_router
from gitoma.core.config import load_config


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """App-wide lifespan. On shutdown, reap any running CLI subprocesses so
    the server doesn't leave orphan `gitoma` processes behind."""
    yield
    await cancel_all_jobs()


# Setup Authentication Schema
auth_scheme = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(auth_scheme)) -> None:
    """Validates the static Bearer token against GITOMA_API_TOKEN."""
    config = load_config()
    expected_token = config.api_auth_token

    if not expected_token:
        # Fail-closed: without a server-side token, auth cannot be validated.
        # 503 (not 500) so clients can distinguish "server misconfigured" from
        # "unexpected bug" and surface a clear remediation message.
        raise HTTPException(
            status_code=503,
            detail="GITOMA_API_TOKEN is not configured on the server.",
        )

    # Constant-time compare: `!=` leaks the token byte-by-byte via response
    # timing. Irrelevant on localhost, critical on LAN / VPN deploys.
    if not secrets.compare_digest(credentials.credentials, expected_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid authentication token.",
        )


app = FastAPI(
    title="Gitoma API",
    description="Autonomous GitHub Agent API. Triggers local LLM agents to repair and review repositories.",
    version=__version__,
    lifespan=lifespan,
)

# /api/v1/* — Bearer-protected REST
app.include_router(router, dependencies=[Depends(verify_token)])

# /, /ws/state — public read-only web cockpit (intended for localhost / VPN)
app.include_router(web_router)
