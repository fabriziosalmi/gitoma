"""gitoma CLI commands -- each module registers itself with `app` on import."""

from gitoma.cli.commands import (  # noqa: F401
    analyze,
    config_cmd,
    doctor,
    fix_ci,
    list_cmd,
    logs,
    mcp,
    reset,
    review,
    run,
    sandbox,
    serve,
    status,
)
