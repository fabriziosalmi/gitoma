"""Vertical registry — single source of truth for narrowed pipelines.

Adding a new vertical = drop a module under :mod:`gitoma.verticals`,
import the constant here, add it to :data:`VERTICALS`. The CLI
factory (:mod:`gitoma.cli.commands._vertical`), the audit-side scope
filter, and the plan-side scope filter all read from this dict. No
``run.py`` edits required.

Lookup pattern:

    from gitoma.verticals import VERTICALS, get_vertical
    vert = get_vertical("docs")  # → DOCS_VERTICAL or None
"""

from __future__ import annotations

from gitoma.verticals._base import Vertical, VerticalFileScope
from gitoma.verticals.docs import DOCS_VERTICAL
from gitoma.verticals.quality import QUALITY_VERTICAL

__all__ = ["Vertical", "VerticalFileScope", "VERTICALS", "get_vertical"]


VERTICALS: dict[str, Vertical] = {
    DOCS_VERTICAL.name: DOCS_VERTICAL,
    QUALITY_VERTICAL.name: QUALITY_VERTICAL,
}


def get_vertical(name: str | None) -> Vertical | None:
    """Look up a vertical by name. Returns ``None`` for unknown names
    or for ``None`` (the default full-pass mode signal). Always
    case-insensitive on the name to match
    :func:`gitoma.planner.scope_filter.active_scope` normalisation."""
    if not name:
        return None
    return VERTICALS.get(name.strip().lower())
