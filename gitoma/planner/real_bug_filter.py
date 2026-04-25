"""Layer-A + Layer-B deterministic plan post-processors.

Two LLM-free transformations applied to the plan AFTER ``planner.plan()``
returns and BEFORE ``rewrite_plan_in_place`` (Layer-2 test→source) runs:

* **Layer-A — `synthesize_real_bug_task`**: when the audit's
  ``test_results`` metric is failing AND the existing LLM-emitted plan
  has no task touching the source-under-test, synthesize a priority-1
  ``T000`` task and prepend it. Closes the recurring "planner ignores
  the actual broken file and emits 12 generic-project subtasks
  instead" pattern documented from the rung-0 bench (memory:
  ``project_backlog_planner_focus_real_bug``).

* **Layer-B — `banish_readme_only_subtasks`**: drop subtasks whose
  ONLY file_hint is a README file, unless Documentation metric is
  explicitly failing AND its details cite README. Rationale (user
  principle 2026-04-25): README updates are a CONSEQUENCE of code
  changes, not a primary planning goal. In practice on the user's
  repos legitimate doc improvements go into ``docs/``, not README.
  The b2v PR #24/#26/#27 README destruction pattern (3 of 4 shipped
  PRs) is dominantly the result of the planner inventing
  "Update README with Documentation Links"-style subtasks that the
  worker then mishandles. Dropping these at plan time prevents the
  worker from ever seeing them — cheaper than catching the
  destruction post-hoc with G13/G14.

Both are deterministic — no LLM, no network. Same return shape:
``dict | None`` where None = no-op (logged but no event needed).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gitoma.analyzers.base import MetricReport
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.planner.test_to_source import infer_source_files_from_tests

__all__ = [
    "synthesize_real_bug_task",
    "banish_readme_only_subtasks",
    "_extract_failing_tests",
    "README_BASENAMES",
]


# Bullet format from the test_runner analyzer: ``  • path/to/test.py``
# or ``  • path/to/test.py::test_function`` (pytest). Strip the
# ``::name`` suffix so we get the file path.
_FAILING_TEST_BULLET_RE = re.compile(r"^\s*•\s+(.+?)\s*$", re.M)


# Files we treat as "the README" for banishment purposes. Variants
# like ``readme.md`` or unsuffixed ``README`` are included so a
# planner that hallucinates the casing doesn't slip through.
README_BASENAMES: frozenset[str] = frozenset({
    "README.md", "README.rst", "README.MD", "README", "readme.md",
    "Readme.md", "ReadMe.md",
})


def _extract_failing_tests(metric_details: str) -> list[str]:
    """Pull failing test file paths from the ``test_results`` metric
    ``details`` field (bullets format ``  • path``). Strips
    ``::test_name`` pytest suffix if present. De-duplicates while
    preserving first-seen order. Returns empty when no bullets
    present (covers the "parser couldn't extract" details branch
    and the no-fail status case)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _FAILING_TEST_BULLET_RE.finditer(metric_details):
        path = m.group(1).strip()
        if "::" in path:
            path = path.split("::", 1)[0]
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def synthesize_real_bug_task(
    plan: TaskPlan,
    report: MetricReport,
    repo_root: Path,
) -> dict[str, Any] | None:
    """Layer-A: synthesize a priority-1 ``T000`` task targeting
    failing-test source files when the planner's own output didn't.

    Mutates ``plan`` in place when synthesis fires. Returns a summary
    dict for trace logging, or ``None`` for no-op (no failing tests,
    no extractable paths, no inferable source, OR plan already covers
    at least one source file).

    Conditions for synthesis (ALL must hold):
      * ``test_results`` metric exists with ``status == "fail"``
      * Failing test paths extractable from ``details`` bullets
      * At least one source file inferable from those test paths
        (uses ``infer_source_files_from_tests`` from test_to_source)
      * No existing plan task has any subtask file_hint matching ANY
        of the inferred source files (otherwise the planner already
        did the right thing — don't duplicate)
    """
    test_metric = next(
        (m for m in report.metrics if m.name == "test_results"),
        None,
    )
    if test_metric is None or test_metric.status != "fail":
        return None

    failing = _extract_failing_tests(test_metric.details or "")
    if not failing:
        return None

    sources = infer_source_files_from_tests(failing, repo_root)
    if not sources:
        return None

    source_set = set(sources)
    for task in plan.tasks:
        for sub in task.subtasks:
            for hint in (sub.file_hints or []):
                if hint in source_set:
                    return None  # planner already covered it

    # Synthesize T000 task with one subtask per source file (capped at 4).
    new_subtasks = [
        SubTask(
            id=f"T000-S{i + 1:02d}",
            title=f"Fix code in {src}",
            description=(
                f"Failing test(s) point at this source file: "
                f"{', '.join(failing[:3])}"
                + (f" (+{len(failing) - 3} more)" if len(failing) > 3 else "")
                + ". Read the test expectations and fix the source so they pass."
            ),
            file_hints=[src],
            action="modify",
        )
        for i, src in enumerate(sources[:4])
    ]
    new_task = Task(
        id="T000",
        title=f"Fix {len(failing)} failing test(s)",
        priority=1,
        metric="test_results",
        description=(
            "Synthesized by real-bug pre-filter — the audit reported failing "
            f"tests but the LLM-emitted plan didn't target the source files. "
            f"Failing tests: {', '.join(failing[:3])}"
            + (f" (+{len(failing) - 3} more)" if len(failing) > 3 else "")
        ),
        subtasks=new_subtasks,
    )

    plan.tasks.insert(0, new_task)
    return {
        "synthesized_task_id": "T000",
        "failing_test_count": len(failing),
        "failing_test_sample": failing[:3],
        "source_files": sources[:4],
        "subtasks_created": len(new_subtasks),
    }


def banish_readme_only_subtasks(
    plan: TaskPlan,
    report: MetricReport,
) -> dict[str, Any] | None:
    """Layer-B: drop every subtask whose ONLY file_hint is a README
    file, UNLESS Documentation metric explicitly fails AND its
    details cite README. Mutates ``plan`` in place.

    Tasks that lose all their subtasks via this filter are also
    removed from ``plan.tasks``. Returns a summary dict on banish,
    None on no-op.

    Multi-file_hint subtasks (e.g. README + src/main.rs) are NOT
    dropped — only single-file_hint README-only ones, since those
    are the pure "stub README update" pattern that recurred in
    b2v PRs.
    """
    doc_metric = next(
        (m for m in report.metrics if m.name in ("documentation", "docs")),
        None,
    )
    if (
        doc_metric is not None
        and doc_metric.status == "fail"
        and "readme" in (doc_metric.details or "").lower()
    ):
        return None  # Documentation explicitly cited README — let it through.

    dropped: list[dict[str, Any]] = []
    for task in plan.tasks:
        keep: list[SubTask] = []
        for sub in task.subtasks:
            hints = sub.file_hints or []
            if hints and all(
                Path(h).name in README_BASENAMES for h in hints
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

    # Remove tasks that ended up with no subtasks left.
    before_task_count = len(plan.tasks)
    plan.tasks = [t for t in plan.tasks if t.subtasks]
    tasks_removed = before_task_count - len(plan.tasks)

    return {
        "dropped_subtasks": dropped,
        "drop_count": len(dropped),
        "tasks_removed": tasks_removed,
    }
