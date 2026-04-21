"""Shared Rich console with Gitoma's custom dark theme.

Industrial-grade pass:

* **Compact banner** by default — the 8-line ASCII art used to eat a
  whole screen on every sub-command. Set ``GITOMA_BANNER=full`` to
  get the old box-art back, or ``GITOMA_BANNER=off`` to skip it.
* **Emoji guard** — detection of terminals that can't render emoji
  (legacy Windows cmd, minimal TTYs). Glyphs downgrade to ASCII via
  :func:`glyph`. Set ``GITOMA_NO_EMOJI=1`` to force-disable.
* **Plain / JSON mode** — :func:`is_plain()` returns True when output
  is piped, ``NO_COLOR`` is set, or the caller sets
  ``GITOMA_PLAIN=1``. Panels/CLI helpers consult it to downshift to
  machine-friendly output.
* Rich already honours ``NO_COLOR`` (env convention) — we just add
  the explicit opt-outs on top.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.theme import Theme

GITOMA_THEME = Theme(
    {
        # Base palette
        "primary": "bold #C084FC",       # violet-400
        "secondary": "bold #67E8F9",     # cyan-300
        "accent": "bold #F472B6",        # pink-400
        "muted": "#9CA3AF",              # gray-400 (bumped from 500 for AA)
        "success": "bold #4ADE80",       # green-400
        "warning": "bold #FBBF24",       # amber-400
        "danger": "bold #F87171",        # red-400
        "info": "#818CF8",               # indigo-400
        # Semantic aliases
        "metric.pass": "bold #4ADE80",
        "metric.warn": "bold #FBBF24",
        "metric.fail": "bold #F87171",
        "metric.score": "#A5B4FC",
        "phase": "bold #C084FC",
        "commit": "#67E8F9",
        "pr": "bold #F472B6",
        "task.done": "bold #4ADE80",
        "task.current": "bold #FBBF24",
        "task.pending": "#9CA3AF",
        "task.failed": "bold #F87171",
        "heading": "bold #E2E8F0",
        "code": "#F1F5F9",
        "url": "underline #67E8F9",
        "dim": "#6B7280",
    }
)

console = Console(theme=GITOMA_THEME, highlight=True)


# ─────────────────────────────────────────────────────────────────────────────
# Runtime capability detection
# ─────────────────────────────────────────────────────────────────────────────


def _env_truthy(name: str) -> bool:
    """Return True when ``name`` is set to a non-empty, non-zero value."""
    v = os.environ.get(name, "").strip().lower()
    return v not in ("", "0", "false", "no")


def is_plain() -> bool:
    """True when the caller asked for a machine-friendly console.

    Triggers:
    * ``NO_COLOR`` env (convention, respected by many tools).
    * ``GITOMA_PLAIN=1`` / ``--plain`` wire-through.
    * stdout is not a TTY (i.e. piped into another command or a file).
    """
    if _env_truthy("NO_COLOR") or _env_truthy("GITOMA_PLAIN"):
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return False


def _emoji_supported() -> bool:
    """Conservative emoji-capable check.

    The canonical ways to get mojibake:
    * Legacy Windows cmd.exe (cp437 / cp850) — Rich exposes
      ``console.options.legacy_windows`` at render time, but the cheap
      module-level check here is to see whether stdout claims UTF-8.
    * TTY with a non-emoji font (Linux console on a server) — we can't
      detect that, so we rely on the ``GITOMA_NO_EMOJI`` escape hatch.
    """
    if _env_truthy("GITOMA_NO_EMOJI"):
        return False
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if not enc or enc == "ascii":
        return False
    if "utf" not in enc:
        return False
    return True


# Module-constant: resolved once at import so every call-site sees the
# same answer and tests that flip env vars can just re-import.
EMOJI_OK = _emoji_supported()


def glyph(emoji: str, ascii_fallback: str) -> str:
    """Return ``emoji`` if the terminal can render it, else ``ascii_fallback``.

    Usage: ``console.print(f"{glyph('🎉', '>>')} PR opened")``. Keep the
    fallback short — it lives on hot paths where a verbose ``[OK]`` can
    crowd the layout.
    """
    return emoji if EMOJI_OK else ascii_fallback


# ─────────────────────────────────────────────────────────────────────────────
# Banners
# ─────────────────────────────────────────────────────────────────────────────

# Full ASCII-art banner — opt-in via ``GITOMA_BANNER=full``. Left intact so
# users who liked it can bring it back.
BANNER_FULL = r"""
[primary]
   _____ _ _
  / ____(_) |
 | |  __ _| |_ ___  _ __ ___   __ _
 | | |_ | | __/ _ \| '_ ` _ \ / _` |
 | |__| | | || (_) | | | | | | (_| |
  \_____|_|\__\___/|_| |_| |_|\__,_|
[/primary]"""

# Compact banner — default. One-liner, plays nicely with piped output.
BANNER_COMPACT = "[primary]◉ gitoma[/primary]"

BANNER_SUBTITLE = "[muted]AI-powered GitHub repository improvement agent[/muted]"


def banner_mode() -> str:
    """Return the configured banner mode: 'full', 'compact', or 'off'."""
    mode = os.environ.get("GITOMA_BANNER", "").strip().lower()
    if mode in ("full", "compact", "off"):
        return mode
    # Default: compact on TTY, off when piping / NO_COLOR.
    return "off" if is_plain() else "compact"


# Legacy alias so existing callers that import ``BANNER`` keep working.
# It resolves to the compact form by default; downstream code that wants
# the old box-art can opt in via the env var + banner_mode() helper.
BANNER = BANNER_COMPACT
