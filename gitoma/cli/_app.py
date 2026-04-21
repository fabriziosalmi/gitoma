"""The top-level Typer app.

Lives in its own module so command files can ``from gitoma.cli._app import app``
without triggering the import of ``gitoma.cli``'s __init__ (which imports the
command files themselves -- i.e. a cycle)."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="gitoma",
    help="AI-powered GitHub repository improvement agent",
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,  # we handle our own error presentation
)
