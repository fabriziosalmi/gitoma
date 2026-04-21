"""Gitoma CLI -- the ``gitoma`` entry point.

Previously a single 1887-line file; now a small package that wires commands
defined in ``gitoma.cli.commands.*`` into a shared Typer ``app``. Importing
``gitoma.cli`` is enough to register every command.
"""

from __future__ import annotations

# The urllib3 v2 warning on macOS/LibreSSL is noisy and not actionable for
# end users -- silence it before any import that could trigger it.
import warnings
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

from gitoma.cli._app import app
from gitoma.cli import commands  # noqa: F401 -- side-effect: registers commands

# Re-exports for callers that used to import these from ``gitoma.cli`` when
# everything was in one file. Tests in tests/test_cli_errors.py rely on this.
from gitoma.cli._helpers import (  # noqa: E402, F401 -- public re-exports
    _abort,
    _check_config,
    _check_github,
    _check_lmstudio,
    _clone_repo,
    _heartbeat,
    _ok,
    _phase,
    _safe_cleanup,
    _warn,
)

__all__ = ["app"]


if __name__ == "__main__":
    app()
