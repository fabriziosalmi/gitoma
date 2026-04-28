"""Load a hand-curated TaskPlan from a JSON file, bypassing the LLM planner.

When `--plan-from-file path/to/tasks.json` is passed to `gitoma run`,
PHASE 2 (the LLM planning call) is skipped and the plan is loaded
directly from disk. The rest of the pipeline (worker, critic stack,
PR creation, self-review) runs unchanged.

Why this exists
---------------
The 5-way generation bench on 2026-04-28 proved that gitoma's LLM
planner is metric-driven and effectively blind to README intent,
spec files, and failing-test imports. For workloads where the
operator already knows exactly which tasks they want
(reproducible benches, regression tests for individual critics,
deterministic verticals, demos), routing through the LLM planner
adds noise without value. This loader is the operator's escape
hatch: write the plan once, run it deterministically, every time.

File format
-----------
The JSON must conform to ``TaskPlan.from_dict`` — same shape that
``TaskPlan.to_dict`` would produce, with at minimum::

    {
      "tasks": [
        {
          "id": "T001",
          "title": "Short task title",
          "priority": 1,
          "metric": "Code Quality",
          "description": "What this task achieves",
          "subtasks": [
            {
              "id": "T001-S01",
              "title": "Short subtask title",
              "description": "What the worker should do",
              "file_hints": ["src/foo.py"],
              "action": "modify"
            }
          ]
        }
      ]
    }

Fields not listed (status, commit_sha, error, created_at,
overall_score_before, llm_model) get sensible defaults.

Validation is intentionally minimal: file readable, JSON valid,
TaskPlan.from_dict accepts it, at least one task with at least
one subtask. Beyond that the worker / critic stack are the
authority on what's valid (mismatched file_hints, unknown
actions, etc. surface as worker-time errors with full context).
"""

from __future__ import annotations

import json
from pathlib import Path

from gitoma.planner.task import TaskPlan


class PlanFileError(ValueError):
    """Raised when --plan-from-file cannot be parsed into a usable TaskPlan."""


def load_plan_from_file(path: str | Path) -> TaskPlan:
    """Load and validate a TaskPlan JSON file.

    Raises ``PlanFileError`` with an actionable message on any
    failure mode (file missing, invalid JSON, schema mismatch,
    empty plan).
    """
    p = Path(path)
    if not p.exists():
        raise PlanFileError(f"plan file not found: {p}")
    if not p.is_file():
        raise PlanFileError(f"plan path is not a regular file: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanFileError(f"could not read {p}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanFileError(
            f"{p}:{exc.lineno}:{exc.colno}: invalid JSON — {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise PlanFileError(
            f"{p}: top-level JSON must be an object (got {type(data).__name__})"
        )
    if "tasks" not in data:
        raise PlanFileError(f"{p}: top-level object missing required 'tasks' key")
    if not isinstance(data["tasks"], list):
        raise PlanFileError(f"{p}: 'tasks' must be a list")
    if len(data["tasks"]) == 0:
        raise PlanFileError(
            f"{p}: 'tasks' is empty — plan must contain at least one task"
        )
    try:
        plan = TaskPlan.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        raise PlanFileError(
            f"{p}: TaskPlan schema mismatch — {type(exc).__name__}: {exc}"
        ) from exc
    if plan.total_subtasks == 0:
        raise PlanFileError(
            f"{p}: plan has {len(plan.tasks)} task(s) but zero subtasks total — "
            "every task must include at least one subtask"
        )
    # Stamp the source so downstream tracing can distinguish curated
    # plans from LLM-generated ones at a glance.
    plan.llm_model = f"plan-from-file:{p.name}"
    return plan
