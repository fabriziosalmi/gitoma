"""Structured tracing for gitoma runs.

Every non-trivial flow (phases, analyzers, LLM calls, git ops, GitHub API
calls, worker subtask boundaries, PR actions) emits a JSON line into a
per-run log file at ``~/.gitoma/logs/<owner>__<name>/<timestamp>.jsonl``.

Format is append-only JSON Lines so the file is cheap to tail, grep, pipe
through ``jq``, or render in the cockpit. Each record has a stable shape::

    {
      "ts":       "2026-04-21T05:12:34.123+00:00",
      "slug":     "fabriziosalmi__b2v",
      "phase":    "WORKING",        // best-effort context (may be "")
      "level":    "info",           // debug | info | warn | error
      "event":    "git.commit",     // dotted namespace for filtering
      "data":     { … }              // event-specific payload
    }

For timing, use ``Trace.span(name, **data)`` as a context manager — it
emits a ``*.start`` on enter and a ``*.end`` on exit with ``duration_ms``.
Exceptions inside a span are auto-logged as ``<name>.error`` with the
full traceback before re-raising.

Retention: we keep only the latest ``MAX_RUNS_PER_SLUG`` (default 20)
log files per slug so a busy machine doesn't fill the disk.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

_LOG_ROOT = Path.home() / ".gitoma" / "logs"
MAX_RUNS_PER_SLUG = 20

_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir_for(slug: str) -> Path:
    d = _LOG_ROOT / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_old(slug: str) -> None:
    """Keep only the most recent MAX_RUNS_PER_SLUG files per slug."""
    d = _runs_dir_for(slug)
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[MAX_RUNS_PER_SLUG:]:
        try:
            stale.unlink()
        except OSError:
            pass


class Trace:
    """Thread-safe JSONL writer bound to a single run."""

    def __init__(self, slug: str, path: Path) -> None:
        self.slug = slug
        self.path = path
        self._lock = threading.Lock()
        self._phase: str = ""

    # ── Public API ─────────────────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        """Update the context phase tag used by every subsequent emit."""
        self._phase = phase

    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        **data: Any,
    ) -> None:
        """Append one structured record."""
        record = {
            "ts": _now_iso(),
            "slug": self.slug,
            "phase": self._phase,
            "level": level,
            "event": event,
            "data": _sanitize(data),
        }
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as exc:
                _logger.debug("trace write failed for %s: %s", self.path, exc)

    @contextmanager
    def span(self, name: str, **data: Any) -> Generator[dict[str, Any], None, None]:
        """Emit ``name.start`` and ``name.end`` (with ``duration_ms``).

        Yields a mutable dict the caller can populate before the span
        closes — useful for attaching return-value-derived fields
        (commit sha, token count, file path, …).
        """
        self.emit(f"{name}.start", **data)
        t0 = time.monotonic()
        fields: dict[str, Any] = {}
        try:
            yield fields
        except BaseException as exc:
            self.emit(
                f"{name}.error",
                level="error",
                duration_ms=round((time.monotonic() - t0) * 1000, 2),
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:500],
                traceback=traceback.format_exc(limit=10),
                **fields,
            )
            raise
        self.emit(
            f"{name}.end",
            duration_ms=round((time.monotonic() - t0) * 1000, 2),
            **fields,
        )

    def exception(self, event: str, exc: BaseException, **data: Any) -> None:
        """Log an exception that was caught + handled (won't be re-raised)."""
        self.emit(
            event,
            level="error",
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:500],
            traceback=traceback.format_exc(limit=10),
            **data,
        )


# ── No-op trace for call sites that still need a Trace-shaped object ────────


class _NullTrace(Trace):
    """Drops every event on the floor — returned when tracing is disabled."""

    def __init__(self) -> None:  # noqa: D401
        self.slug = ""
        self.path = Path("/dev/null")
        self._lock = threading.Lock()
        self._phase = ""

    def emit(self, event: str, *, level: str = "info", **data: Any) -> None:
        return None


NULL_TRACE: Trace = _NullTrace()


# ── Ambient current-trace accessor ──────────────────────────────────────────
# Instrumentation scattered across modules (git ops, github client, LLM
# client, worker, PR agent) should call ``current()`` to get whatever
# Trace the outer CLI command opened, without every caller having to
# thread a Trace parameter through every function signature. Defaults to
# the no-op trace so code works fine when running outside an active run.

_CURRENT: Trace = NULL_TRACE


def current() -> Trace:
    """Return the Trace for the currently-active run, or the no-op trace."""
    return _CURRENT


# ── Entrypoints ─────────────────────────────────────────────────────────────


@contextmanager
def open_trace(slug: str, *, label: str = "run") -> Generator[Trace, None, None]:
    """Open a fresh trace file for this run + yield a Trace handle.

    The file is named ``<YYYYMMDD-HHMMSS>-<label>.jsonl`` so multiple
    concurrent invocations don't overwrite each other and different CLI
    subcommands (run / review / fix-ci) are easy to tell apart when
    tailing. Also binds ``current()`` to this trace for the duration.
    """
    global _CURRENT
    # Microseconds in the filename so concurrent or tight-loop traces don't
    # collide on the same second and silently overwrite each other.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = _runs_dir_for(slug) / f"{stamp}-{label}.jsonl"
    tr = Trace(slug, path)
    tr.emit("trace.open", label=label, path=str(path))
    previous = _CURRENT
    _CURRENT = tr
    try:
        yield tr
    finally:
        tr.emit("trace.close")
        _CURRENT = previous
        _prune_old(slug)


def latest_log_path(slug: str) -> Path | None:
    """Return the most-recently-modified jsonl for this slug, or None."""
    d = _LOG_ROOT / slug
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


# ── Helpers ────────────────────────────────────────────────────────────────


_SENSITIVE_KEYS = {"token", "authorization", "password", "secret", "api_key"}


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask fields whose name looks like a credential.

    Traces land on disk and may be shared for debugging — so we refuse
    to write `{"token": "ghp_..."}`.
    """
    out: dict[str, Any] = {}
    for k, v in payload.items():
        lk = k.lower()
        if any(s in lk for s in _SENSITIVE_KEYS) and isinstance(v, str) and v:
            out[k] = f"***{v[-4:]}" if len(v) > 4 else "***"
        elif isinstance(v, dict):
            out[k] = _sanitize(v)
        else:
            out[k] = v
    return out
