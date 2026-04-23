"""G9 — deterministic post-plan filter against Occam's failure history.

Soft prompt injection (the ``== PRIOR RUNS CONTEXT ==`` block fed to
the planner) is too gentle for 4B-class models. Caught live on
rung-3 v24: planner saw the v23 fail log including
``T001-S02 on [tests/test_db.py] — failed: ast_diff``, rephrased the
subtask title from "Verify Test Coverage" to "Update Test for SQL
Injection Fix", but kept the same ``file_hints`` — and the worker
hit the same helper-deletion slop again.

This module is the deterministic cousin of that prompt injection: it
mutates the freshly-emitted ``TaskPlan`` to drop any subtask whose
``file_hints`` overlap with paths that have failed ≥ threshold times
in the recent agent-log window. The planner's intent isn't
overruled — the filter only fires on paths with an established
failure pattern.

Tunables: threshold (default 2 — two distinct fails counts as
"established"), env override ``GITOMA_OCCAM_FILTER_THRESHOLD``.
"""

from __future__ import annotations

import os
from typing import Any

from gitoma.planner.task import TaskPlan

__all__ = [
    "filter_plan_by_failure_history",
    "DEFAULT_THRESHOLD",
    "resolve_threshold",
]


DEFAULT_THRESHOLD = 2


def resolve_threshold() -> int:
    """Return the active threshold. Reads
    ``GITOMA_OCCAM_FILTER_THRESHOLD`` env var (clamped to >=1).
    Returns ``DEFAULT_THRESHOLD`` when unset / unparseable."""
    raw = os.environ.get("GITOMA_OCCAM_FILTER_THRESHOLD") or ""
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


def filter_plan_by_failure_history(
    plan: TaskPlan,
    failed_hints_count: dict[str, int],
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Mutate ``plan`` in place: drop subtasks whose ``file_hints``
    contain at least one path with ``failed_hints_count[path] >=
    threshold``. Tasks that lose ALL their subtasks are dropped from
    ``plan.tasks``.

    No-op when ``failed_hints_count`` is empty (first run on a repo)
    or when no subtask hints overlap.

    Returns a structured summary the caller renders to console + the
    trace event:

        {
          "filtered_subtasks": [
            {"subtask_id": "T001-S02", "title": "...",
             "file_hints": [...], "max_fail_count": 3},
            ...
          ],
          "tasks_dropped":  [{"task_id": "T003", "title": "..."}],
          "kept_subtasks":  18,
          "total_subtasks": 22,
          "threshold":      2,
        }
    """
    filtered_subtasks: list[dict[str, Any]] = []
    tasks_dropped: list[dict[str, Any]] = []
    kept_count = 0
    total_count = 0

    for task in plan.tasks:
        original_subtask_count = len(task.subtasks)
        kept_subtasks = []
        for sub in task.subtasks:
            total_count += 1
            hints = list(sub.file_hints or [])
            max_fail = max(
                (failed_hints_count.get(h, 0) for h in hints),
                default=0,
            )
            if max_fail >= threshold and hints:
                filtered_subtasks.append({
                    "subtask_id": sub.id,
                    "title": sub.title,
                    "file_hints": hints,
                    "max_fail_count": max_fail,
                })
            else:
                kept_subtasks.append(sub)
                kept_count += 1
        task.subtasks = kept_subtasks
        if not task.subtasks and original_subtask_count > 0:
            tasks_dropped.append({"task_id": task.id, "title": task.title})

    plan.tasks = [t for t in plan.tasks if t.subtasks]

    return {
        "filtered_subtasks": filtered_subtasks,
        "tasks_dropped": tasks_dropped,
        "kept_subtasks": kept_count,
        "total_subtasks": total_count,
        "threshold": threshold,
    }
