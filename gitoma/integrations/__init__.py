"""External tool integrations gitoma calls into.

Distinct from ``gitoma/context/`` (which gathers data INTO gitoma's
prompts). Integrations here are tools gitoma DELEGATES TO — typically
deterministic generators that gitoma wraps as PR-producing
verticals. The first one is :mod:`occam_gitignore` which ships
deterministic ``.gitignore`` content; future integrations follow the
same pattern (semgrep, reuse-tool, license-checker, etc.).

Each integration module is OPTIONAL at runtime — missing the
underlying dependency yields a clean "feature unavailable" signal
to the CLI command, never a crash inside the worker pipeline.
"""
