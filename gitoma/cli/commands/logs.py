"""gitoma logs command."""

from __future__ import annotations

import json as _json
import time as _time
from pathlib import Path
from typing import Annotated, Optional

import typer

from gitoma.cli._app import app
from gitoma.cli._helpers import _abort
from gitoma.core.repo import parse_repo_url
from gitoma.ui.console import console


# ─────────────────────────────────────────────────────────────────────────────
# gitoma logs
# ─────────────────────────────────────────────────────────────────────────────


@app.command(name="logs")
def logs_cmd(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new events as they arrive")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Print raw JSONL instead of the pretty summary")] = False,
    filter_event: Annotated[Optional[str], typer.Option("--filter", help="Only show events whose 'event' field starts with this prefix (e.g. 'run.', 'phase.')")] = None,
) -> None:
    """
    📜 Tail the structured trace for a repo's latest run.

    Every ``gitoma run`` / ``review`` / ``fix-ci`` writes one JSONL file
    per invocation under ``~/.gitoma/logs/<slug>/``. This command finds
    the most recent one and prints it — live with ``--follow``, grepped
    with ``--filter=phase.``, or verbatim with ``--raw``.
    """
    from gitoma.core.trace import latest_log_path

    try:
        owner, name = parse_repo_url(repo_url)
    except ValueError as exc:
        _abort(f"Invalid repo URL: {exc}")

    slug = f"{owner}__{name}"
    path = latest_log_path(slug)
    if path is None:
        _abort(
            f"No trace logs for {slug}.",
            hint="The trace is written on first `gitoma run`. Start one and re-try.",
        )

    console.print(f"[muted]Tailing {path}[/muted]\n")

    _stream_log_file(path, follow=follow, raw=raw, filter_prefix=filter_event)


def _stream_log_file(
    path: Path,
    *,
    follow: bool,
    raw: bool,
    filter_prefix: Optional[str],
) -> None:
    """Print every record, optionally streaming new ones as they're appended."""
    with path.open("r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                if not follow:
                    return
                _time.sleep(0.25)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if raw:
                console.print(stripped)
                continue
            try:
                rec = _json.loads(stripped)
            except _json.JSONDecodeError:
                console.print(f"[dim]{stripped}[/dim]")
                continue
            event = rec.get("event", "")
            if filter_prefix and not event.startswith(filter_prefix):
                continue
            ts = rec.get("ts", "")[11:19]  # HH:MM:SS
            level = rec.get("level", "info")
            phase = rec.get("phase", "")
            data = rec.get("data", {})
            level_style = {"warn": "warning", "error": "danger", "debug": "dim"}.get(level, "info")
            phase_chip = f"[muted]{phase}[/muted] " if phase else ""
            detail = " ".join(f"{k}={v}" for k, v in data.items() if v not in ("", None))
            console.print(
                f"[dim]{ts}[/dim] {phase_chip}[{level_style}]{event}[/{level_style}] "
                f"[muted]{detail}[/muted]"
            )
