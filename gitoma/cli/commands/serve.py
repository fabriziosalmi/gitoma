"""gitoma serve command."""

from __future__ import annotations

from typing import Annotated, TYPE_CHECKING

import typer
from rich.panel import Panel

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _check_config,
)
from gitoma.ui.console import console
from gitoma.ui.panels import (
    print_banner,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command(name="serve")
def serve(
    port: Annotated[int, typer.Option(help="Port to run the REST API on")] = 8000,
    host: Annotated[str, typer.Option(help="Host to bind the server to")] = "0.0.0.0",
) -> None:
    """
    🌐  Launch the Gitoma FastAPI REST Server.
    """
    import os

    import uvicorn

    from gitoma.core.config import RUNTIME_TOKEN_FILE, ensure_runtime_api_token

    print_banner(__version__)
    _check_config(require_token=False)

    token, generated = ensure_runtime_api_token()
    # Publish to the process env so `load_config()` picks it up in every
    # request handler (verify_token calls it per request).
    os.environ["GITOMA_API_TOKEN"] = token

    masked = (token[:6] + "…" + token[-4:]) if len(token) > 12 else "***"
    if generated:
        console.print(
            Panel(
                f"[bold]{token}[/bold]\n\n"
                f"[muted]Persisted to {RUNTIME_TOKEN_FILE} (mode 0600).[/muted]\n"
                f"[muted]Paste into the cockpit Settings dialog when prompted.[/muted]\n"
                f"[muted]Delete that file and restart to rotate.[/muted]",
                title="[primary]◉ New API token generated[/primary]",
                border_style="info",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            f"[success]API secured[/success] "
            f"[muted](token {masked})[/muted]"
        )

    console.print(f"Starting server on [primary]http://{host}:{port}[/primary]")
    console.print(f"Cockpit:         [primary]http://{host}:{port}/[/primary]")
    console.print(f"Swagger docs:    [primary]http://{host}:{port}/docs[/primary]\n")

    uvicorn.run("gitoma.api.server:app", host=host, port=port, log_level="info")
