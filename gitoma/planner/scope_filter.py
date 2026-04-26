"""Vertical-mode scope filter — narrows plan + audit to a vertical.

Castelletto Taglio A turned each vertical into a single declarative
record under :mod:`gitoma.verticals`. This module's job shrinks to:

  * **`active_scope()`** — read the env var and normalise.
  * **`filter_metrics_by_vertical(report, vertical)`** — drop metrics
    not in the vertical's allow-list.
  * **`filter_plan_by_vertical(plan, vertical)`** — drop subtasks
    that touch any path outside the vertical's file allow-list.

The legacy ``DOC_*`` constants and ``filter_*_to_doc_scope`` /
``is_doc_path`` names are preserved as thin shims that delegate to
the docs vertical. Existing call-sites and tests keep working;
new code should consume the registry.

Both filters are deterministic, sub-1ms, no LLM. The audit filter
runs BEFORE :func:`gitoma.planner.planner.Planner.plan` so the LLM
only sees in-scope metrics. The plan filter runs AFTER planning and
after Layer-A/B/G9, as the last gate before worker apply.

Activated by ``GITOMA_SCOPE=docs`` (or the value of
:attr:`gitoma.verticals._base.Vertical.name` for any registered
vertical) — set by the corresponding CLI command (``gitoma docs``
etc.) or directly in the shell.
"""

from __future__ import annotations

import os
from typing import Any

from gitoma.analyzers.base import MetricReport
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.verticals import VERTICALS, Vertical, get_vertical
from gitoma.verticals.docs import DOCS_VERTICAL

__all__ = [
    # Registry-aware API (preferred)
    "active_scope",
    "active_vertical",
    "filter_metrics_by_vertical",
    "filter_plan_by_vertical",
    # Legacy API (kept as shims so existing callers / tests don't churn)
    "DOC_FILE_EXTENSIONS",
    "DOC_PATH_PREFIXES",
    "DOC_ROOT_NAMES",
    "DOC_METRIC_NAMES",
    "is_doc_path",
    "filter_metrics_to_doc_scope",
    "filter_plan_to_doc_scope",
]


# ── Registry-aware API ─────────────────────────────────────────────


def active_scope() -> str | None:
    """Return the active vertical name from ``GITOMA_SCOPE`` env, or
    ``None`` for the default full-pass mode. Trimmed + lowercased so
    ``  Docs  `` and ``DOCS`` both resolve. Pass-through for unknown
    names (lookup happens via :func:`active_vertical`)."""
    raw = (os.environ.get("GITOMA_SCOPE") or "").strip().lower()
    return raw or None


def active_vertical() -> Vertical | None:
    """Return the active :class:`Vertical` instance, or ``None`` for
    full-pass mode / unknown name. Combines :func:`active_scope` +
    registry lookup so callers don't repeat the two-step.
    """
    return get_vertical(active_scope())


def filter_metrics_by_vertical(
    report: MetricReport, vertical: Vertical,
) -> dict[str, Any] | None:
    """Mutate ``report.metrics`` to keep only those whose ``name`` is
    in ``vertical.metric_allow_list``. Returns a summary dict on
    filter (for trace), or ``None`` when the report was already
    in-scope / empty.

    Rationale: if Build Integrity is failing, that's NOT the docs
    vertical's job to fix. Same for Test Results, Security, etc.
    Filtering BEFORE the planner runs means the planner prompt
    doesn't even mention those failures — no hallucination
    possible.
    """
    before = [m.name for m in report.metrics]
    kept = [m for m in report.metrics if m.name in vertical.metric_allow_list]
    if len(kept) == len(report.metrics):
        return None
    dropped = [n for n in before if n not in {m.name for m in kept}]
    report.metrics = kept
    return {
        "metrics_kept": [m.name for m in kept],
        "metrics_dropped": dropped,
        "scope": vertical.name,
    }


def filter_plan_by_vertical(
    plan: TaskPlan, vertical: Vertical,
) -> dict[str, Any] | None:
    """Drop every subtask whose ``file_hints`` contain ANY path that
    falls outside ``vertical.file_allow_list``. Returns a summary
    dict on filter, or ``None`` on no-op.

    Stricter than Layer-B (which only drops README-ONLY subtasks):
    here a subtask hinting both ``README.md`` and ``src/main.py``
    gets dropped — under a narrowed vertical, source edits are
    out-of-scope by definition.

    Subtasks with EMPTY ``file_hints`` (no file specified) are
    KEPT — those are typically ``verify`` actions (e.g. "run
    pytest") that don't write anything; the worker will either
    no-op them or surface a soft error.
    """
    dropped: list[dict[str, Any]] = []
    for task in plan.tasks:
        keep: list[SubTask] = []
        for sub in task.subtasks:
            hints = sub.file_hints or []
            if hints and not all(
                vertical.is_path_in_scope(h) for h in hints
            ):
                dropped.append({
                    "task_id": task.id,
                    "subtask_id": sub.id,
                    "title": sub.title,
                    "file_hints": list(hints),
                })
                continue
            keep.append(sub)
        task.subtasks = keep
    if not dropped:
        return None
    before_task_count = len(plan.tasks)
    plan.tasks = [t for t in plan.tasks if t.subtasks]
    return {
        "scope": vertical.name,
        "dropped_subtasks": dropped,
        "drop_count": len(dropped),
        "tasks_removed": before_task_count - len(plan.tasks),
    }


# ── Legacy API (delegated to the docs vertical) ────────────────────
# Kept so callers / tests written before Castelletto Taglio A keep
# working without churn. New code should call the registry-aware
# functions above.


DOC_FILE_EXTENSIONS = DOCS_VERTICAL.file_allow_list.extensions
DOC_PATH_PREFIXES = DOCS_VERTICAL.file_allow_list.path_prefixes
DOC_ROOT_NAMES = DOCS_VERTICAL.file_allow_list.root_names
DOC_METRIC_NAMES = DOCS_VERTICAL.metric_allow_list


def is_doc_path(path: str) -> bool:
    """Legacy alias for ``DOCS_VERTICAL.is_path_in_scope`` — delegates
    to the registered docs vertical so the predicate stays in sync
    with the dataclass. Prefer the vertical-aware API in new code."""
    return DOCS_VERTICAL.is_path_in_scope(path)


def filter_metrics_to_doc_scope(report: MetricReport) -> dict[str, Any] | None:
    """Legacy alias for :func:`filter_metrics_by_vertical` against the
    docs vertical. Prefer ``filter_metrics_by_vertical(report, vert)``
    in new code so the choice of vertical is explicit."""
    return filter_metrics_by_vertical(report, DOCS_VERTICAL)


def filter_plan_to_doc_scope(plan: TaskPlan) -> dict[str, Any] | None:
    """Legacy alias for :func:`filter_plan_by_vertical` against the
    docs vertical. Prefer ``filter_plan_by_vertical(plan, vert)``
    in new code so the choice of vertical is explicit."""
    return filter_plan_by_vertical(plan, DOCS_VERTICAL)
