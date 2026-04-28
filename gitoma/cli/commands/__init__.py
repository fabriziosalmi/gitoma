"""gitoma CLI commands -- each module registers itself with `app` on import.

Per-vertical commands (``gitoma docs``, ``gitoma quality``, …) are
generated from the :data:`gitoma.verticals.VERTICALS` registry by
:func:`gitoma.cli.commands._vertical.register_all`. Adding a new
vertical requires NO edit to this file."""

from gitoma.cli.commands import (  # noqa: F401
    analyze,
    config_cmd,
    doctor,
    fix_ci,
    gitignore,
    list_cmd,
    logs,
    mcp,
    reset,
    review,
    run,
    sandbox,
    scaffold,
    serve,
    status,
)
from gitoma.cli.commands._vertical import register_all as _register_verticals

# Generate per-vertical commands from the registry. Done here (after
# `run` is imported, since vertical commands delegate to it) so the
# CLI is fully wired by the time `gitoma --help` enumerates commands.
_register_verticals()
