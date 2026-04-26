"""Vertical-mode declarative config — Castelletto Taglio A.

A *vertical* is a narrowed pipeline mode: instead of the full-pass
`gitoma run` looking at every metric and proposing edits anywhere,
a vertical restricts both the audit (which metrics survive into the
planner prompt) and the plan (which file paths a subtask may touch).

Before this refactor, the docs vertical lived as ad-hoc
constants in :mod:`gitoma.planner.scope_filter` plus three
``GITOMA_SCOPE`` env-var checks scattered across
:mod:`gitoma.cli.commands.run`. Adding a second vertical meant
duplicating both halves. This module turns each vertical into a
single declarative record so adding a vertical = one file and the
CLI command + filters wire themselves up from the registry.

Pure data + a single predicate. No I/O, no env reads, no LLM. The
env reader (:func:`gitoma.planner.scope_filter.active_scope`) is the
ONLY runtime input — the registry is consulted for everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["VerticalFileScope", "Vertical"]


@dataclass(frozen=True)
class VerticalFileScope:
    """Allow-list describing which file paths belong to a vertical.

    A path is in scope when ANY of these match:

    * its lower-cased suffix appears in :attr:`extensions`, OR
    * it lives under one of the :attr:`path_prefixes` (forward-slash
      normalised; matches both at root and nested), OR
    * its basename appears in :attr:`root_names` (case-insensitive on
      typical project meta files like ``README.md``).

    Empty tuples / frozensets disable that match channel — a vertical
    that only cares about a fixed set of root files would leave
    ``extensions`` and ``path_prefixes`` empty.
    """

    extensions: frozenset[str] = frozenset()
    path_prefixes: tuple[str, ...] = ()
    root_names: frozenset[str] = frozenset()

    def is_in_scope(self, path: str) -> bool:
        """Return ``True`` when ``path`` is covered by this scope.

        Conservative: when uncertain (empty path, nothing matches),
        returns ``False`` so the calling filter drops the subtask —
        the safe direction for a narrowing filter.
        """
        if not path:
            return False
        p = Path(path)
        norm = path.replace("\\", "/")
        if self.extensions and p.suffix.lower() in self.extensions:
            return True
        if self.root_names:
            # Match either the literal name or its uppercased form so
            # README / Readme / ReadMe all hit the same allow-list
            # without forcing callers to enumerate every casing.
            if p.name in self.root_names or p.name.upper() in self.root_names:
                return True
        for prefix in self.path_prefixes:
            if norm.startswith(prefix) or f"/{prefix}" in f"/{norm}":
                return True
        return False


@dataclass(frozen=True)
class Vertical:
    """Declarative spec for a single vertical mode.

    A vertical bundles three shape parameters:

    * :attr:`file_allow_list` — which paths a subtask may touch (gates
      the post-Layer-B scope filter on the plan).
    * :attr:`metric_allow_list` — which metric names survive into the
      planner prompt (gates the post-audit scope filter on the report).
    * :attr:`prompt_addendum` — a short paragraph appended to the
      planner system prompt so the LLM knows the active narrowing
      and won't propose out-of-scope subtasks in the first place.

    Plus two flags controlling pipeline wiring:

    * :attr:`no_auto_fix_ci` — verticals that don't touch CI (docs,
      quality, tests-improvements) skip the auto-fix-CI phase by
      default. The CLI factory passes this to ``run_full_pipeline``.
    * :attr:`guards_disabled` — guard names (``"G3"``, ``"G14"``, …)
      to deactivate for this vertical. Empty by default; rare —
      most verticals want the full guard stack.
    """

    name: str
    summary: str
    file_allow_list: VerticalFileScope
    metric_allow_list: frozenset[str]
    prompt_addendum: str = ""
    no_auto_fix_ci: bool = True
    guards_disabled: frozenset[str] = field(default_factory=frozenset)

    def is_path_in_scope(self, path: str) -> bool:
        """Convenience proxy to ``file_allow_list.is_in_scope``."""
        return self.file_allow_list.is_in_scope(path)
