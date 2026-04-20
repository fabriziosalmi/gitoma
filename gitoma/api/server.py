"""FastAPI application initialization."""

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gitoma import __version__
from gitoma.api.routers import router
from gitoma.api.web import web_router
from gitoma.core.config import load_config

# Setup Authentication Schema
auth_scheme = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(auth_scheme)) -> None:
    """Validates the static Bearer token against GITOMA_API_TOKEN."""
    config = load_config()
    expected_token = config.api_auth_token

    if not expected_token:
        # Fail-closed: without a server-side token, auth cannot be validated.
        raise HTTPException(
            status_code=500,
            detail="GITOMA_API_TOKEN is not configured on the server.",
        )

    if credentials.credentials != expected_token:
        raise HTTPException(
            status_code=403,
            detail="Invalid authentication token.",
        )


app = FastAPI(
    title="Gitoma API",
    description="Autonomous GitHub Agent API. Triggers local LLM agents to repair and review repositories.",
    version=__version__,
)

# /api/v1/* — Bearer-protected REST
app.include_router(router, dependencies=[Depends(verify_token)])

# /, /ws/state — public read-only web cockpit (intended for localhost / VPN)
app.include_router(web_router)
