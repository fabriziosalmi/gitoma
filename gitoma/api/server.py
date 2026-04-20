"""FastAPI application initialization."""

from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from gitoma import __version__
from gitoma.core.config import load_config
from gitoma.api.routers import router

# Setup Authentication Schema
auth_scheme = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Security(auth_scheme)) -> None:
    """Validates the static Bearer token against GITOMA_API_TOKEN."""
    config = load_config()
    expected_token = config.api_auth_token
    
    if not expected_token:
        # If no token is configured in .env, we reject all authenticated requests to be safe
        raise HTTPException(
            status_code=500, 
            detail="GITOMA_API_TOKEN is not configured on the server."
        )
        
    if credentials.credentials != expected_token:
        raise HTTPException(
            status_code=403, 
            detail="Invalid authentication token."
        )


app = FastAPI(
    title="Gitoma API",
    description="Autonomous GitHub Agent API. Triggers local LLM agents to repair and review repositories.",
    version=__version__,
    dependencies=[Depends(verify_token)] # Protect all routes globally
)

app.include_router(router)
