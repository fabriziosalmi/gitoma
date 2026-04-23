"""Tests for G9 — ``filter_plan_by_failure_history``. The
deterministic post-plan filter that drops subtasks whose file_hints
have failed ≥ threshold times in the recent Occam agent-log window."""

from __future__ import annotations

import pytest

from gitoma.context.occam_client import count_failed_hints
from gitoma.planner.occam_filter import (
    DEFAULT_THRESHOLD,
    filter_plan_by_failure_history,
    resolve_threshold,
)
from gitoma.planner.task import SubTask, Task, TaskPlan


# ── count_failed_hints ──────────────────────────────────────────────────


def test_count_failed_hints_empty_log() -> None:
    assert count_failed_hints([]) == {}


def test_count_failed_hints_only_counts_fails() -> None:
    entries = [
        {"outcome": "success", "touched_files": ["src/a.py"]},
        {"outcome": "fail", "touched_files": ["src/b.py"]},
        {"outcome": "skipped", "touched_files": ["src/c.py"]},
    ]
    assert count_failed_hints(entries) == {"src/b.py": 1}


def test_count_failed_hints_accumulates_across_entries() -> None:
    """Same path failing in multiple subtasks → count increases."""
    entries = [
        {"outcome": "fail", "touched_files": ["tests/test_db.py"]},
        {"outcome": "fail", "touched_files": ["tests/test_db.py", "src/db.py"]},
        {"outcome": "fail", "touched_files": ["tests/test_db.py"]},
    ]
    assert count_failed_hints(entries) == {
        "tests/test_db.py": 3,
        "src/db.py": 1,
    }


def test_count_failed_hints_ignores_empty_paths() -> None:
    entries = [
        {"outcome": "fail", "touched_files": ["", None, "src/x.py"]},
    ]
    assert count_failed_hints(entries) == {"src/x.py": 1}


def test_count_failed_hints_missing_touched_files() -> None:
    """Entries without a ``touched_files`` key shouldn't crash."""
    entries = [
        {"outcome": "fail"},
        {"outcome": "fail", "touched_files": None},
    ]
    assert count_failed_hints(entries) == {}


# ── resolve_threshold ───────────────────────────────────────────────────


def test_resolve_threshold_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_OCCAM_FILTER_THRESHOLD", raising=False)
    assert resolve_threshold() == DEFAULT_THRESHOLD


def test_resolve_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_OCCAM_FILTER_THRESHOLD", "5")
    assert resolve_threshold() == 5


def test_resolve_threshold_clamps_below_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold of 0 or negative would filter every subtask with any
    hint history — useless. Fall back to the default."""
    monkeypatch.setenv("GITOMA_OCCAM_FILTER_THRESHOLD", "0")
    assert resolve_threshold() == DEFAULT_THRESHOLD
    monkeypatch.setenv("GITOMA_OCCAM_FILTER_THRESHOLD", "-1")
    assert resolve_threshold() == DEFAULT_THRESHOLD


def test_resolve_threshold_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_OCCAM_FILTER_THRESHOLD", "nope")
    assert resolve_threshold() == DEFAULT_THRESHOLD


# ── filter_plan_by_failure_history ──────────────────────────────────────


def _plan(*task_specs) -> TaskPlan:
    """Build a TaskPlan from shorthand: each spec is
    ``(task_id, [(sub_id, [hints...]), ...])``."""
    tasks = []
    for t_id, subs in task_specs:
        tasks.append(Task(
            id=t_id,
            title=f"Task {t_id}",
            description="",
            priority=1,
            metric="build",
            subtasks=[
                SubTask(id=s_id, title=f"Sub {s_id}", description="",
                        file_hints=hints, action="modify")
                for s_id, hints in subs
            ],
        ))
    return TaskPlan(tasks=tasks)


def test_filter_drops_subtask_at_threshold() -> None:
    """Hint failed exactly ``threshold`` times → drop."""
    plan = _plan(
        ("T001", [("T001-S01", ["src/db.py"]),
                  ("T001-S02", ["tests/test_db.py"])]),
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"tests/test_db.py": 2},
        threshold=2,
    )
    assert len(summary["filtered_subtasks"]) == 1
    assert summary["filtered_subtasks"][0]["subtask_id"] == "T001-S02"
    assert summary["filtered_subtasks"][0]["max_fail_count"] == 2
    assert summary["kept_subtasks"] == 1
    # Plan mutated in place
    assert len(plan.tasks[0].subtasks) == 1
    assert plan.tasks[0].subtasks[0].id == "T001-S01"


def test_filter_no_op_when_below_threshold() -> None:
    plan = _plan(
        ("T001", [("T001-S01", ["src/db.py"])]),
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"src/db.py": 1},
        threshold=2,
    )
    assert summary["filtered_subtasks"] == []
    assert summary["kept_subtasks"] == 1
    assert len(plan.tasks[0].subtasks) == 1


def test_filter_drops_task_when_all_subtasks_gone() -> None:
    """If all subtasks of a task are filtered, the task itself is
    dropped from ``plan.tasks`` and reported in ``tasks_dropped``."""
    plan = _plan(
        ("T001", [("T001-S01", ["hot.py"])]),  # will be dropped
        ("T002", [("T002-S01", ["cold.py"])]),  # stays
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"hot.py": 3},
        threshold=2,
    )
    assert len(summary["filtered_subtasks"]) == 1
    assert len(summary["tasks_dropped"]) == 1
    assert summary["tasks_dropped"][0]["task_id"] == "T001"
    # plan.tasks now has only T002
    assert [t.id for t in plan.tasks] == ["T002"]


def test_filter_empty_counter_is_no_op() -> None:
    """First run on a repo → no fail history → filter touches nothing."""
    plan = _plan(
        ("T001", [("T001-S01", ["src/x.py"]),
                  ("T001-S02", ["src/y.py"])]),
    )
    summary = filter_plan_by_failure_history(
        plan, failed_hints_count={}, threshold=2,
    )
    assert summary["filtered_subtasks"] == []
    assert summary["kept_subtasks"] == 2
    assert len(plan.tasks[0].subtasks) == 2


def test_filter_max_across_hints() -> None:
    """A subtask with multiple hints is filtered based on the MAX
    count across its hints, not sum. Any single hint at/above the
    threshold poisons the whole subtask."""
    plan = _plan(
        ("T001", [("T001-S01", ["safe.py", "hot.py", "also-safe.py"])]),
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"hot.py": 5, "safe.py": 1},
        threshold=3,
    )
    assert len(summary["filtered_subtasks"]) == 1
    assert summary["filtered_subtasks"][0]["max_fail_count"] == 5


def test_filter_subtask_without_hints_is_kept() -> None:
    """Subtasks with empty file_hints can't be matched → always kept
    regardless of the counter. (Rare but possible when the planner
    emits ``file_hints: []``.)"""
    plan = _plan(
        ("T001", [("T001-S01", [])]),
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"anything.py": 99},
        threshold=2,
    )
    assert summary["filtered_subtasks"] == []
    assert summary["kept_subtasks"] == 1


def test_filter_respects_custom_threshold() -> None:
    plan = _plan(
        ("T001", [("T001-S01", ["hot.py"])]),
    )
    summary_high = filter_plan_by_failure_history(
        plan, failed_hints_count={"hot.py": 2}, threshold=5,
    )
    assert len(summary_high["filtered_subtasks"]) == 0

    plan2 = _plan(
        ("T001", [("T001-S01", ["hot.py"])]),
    )
    summary_low = filter_plan_by_failure_history(
        plan2, failed_hints_count={"hot.py": 2}, threshold=1,
    )
    assert len(summary_low["filtered_subtasks"]) == 1


def test_filter_summary_includes_total_counts() -> None:
    plan = _plan(
        ("T001", [("T001-S01", ["a.py"]),
                  ("T001-S02", ["b.py"])]),
        ("T002", [("T002-S01", ["c.py"])]),
    )
    summary = filter_plan_by_failure_history(
        plan,
        failed_hints_count={"b.py": 3},
        threshold=2,
    )
    assert summary["total_subtasks"] == 3
    assert summary["kept_subtasks"] == 2
    assert summary["threshold"] == 2


# ── End-to-end: real v24-shape scenario ─────────────────────────────────


def test_v24_rung3_scenario_closed() -> None:
    """Simulate the exact rung-3 v23 → v24 reality: the v23 agent-log
    had T001-S02 failing on tests/test_db.py with ast_diff, and 2
    denylist fails on .github/workflows/ci.yml. v24's planner
    re-proposed the same shapes. The filter should drop them.

    Threshold=2 means:
      * A hint failed 2× within the window → dropped
      * A hint failed 1× → kept (could be transient)
    """
    agent_log = [
        # v23's observation replays (from the actual live run)
        {"outcome": "fail", "touched_files": ["tests/test_db.py"],
         "failure_modes": ["ast_diff"]},
        {"outcome": "fail", "touched_files": [".github/workflows/ci.yml"],
         "failure_modes": ["denylist"]},
        {"outcome": "fail", "touched_files": [".github/workflows/ci.yml"],
         "failure_modes": ["denylist"]},
        {"outcome": "fail", "touched_files": ["pyproject.toml", ".coveragerc"],
         "failure_modes": ["syntax_invalid"]},
        {"outcome": "success", "touched_files": ["src/db.py"]},
        {"outcome": "success", "touched_files": ["README.md"]},
    ]
    counter = count_failed_hints(agent_log)
    assert counter[".github/workflows/ci.yml"] == 2
    assert counter["tests/test_db.py"] == 1  # one fail, below threshold

    # v24's planner proposes similar subtasks
    plan = _plan(
        ("T001", [("T001-S01", ["src/db.py"]),          # safe, keep
                  ("T001-S02", ["tests/test_db.py"])]),  # 1 fail, keep
        ("T003", [("T003-S01", [".github/workflows/ci.yml"]),  # 2 fail, DROP
                  ("T003-S02", [".github/workflows/ci.yml"])]), # 2 fail, DROP
    )
    summary = filter_plan_by_failure_history(plan, counter, threshold=2)

    # T003 fully dropped, T001 intact
    assert len(summary["filtered_subtasks"]) == 2
    assert {s["subtask_id"] for s in summary["filtered_subtasks"]} == {
        "T003-S01", "T003-S02",
    }
    assert len(summary["tasks_dropped"]) == 1
    assert summary["tasks_dropped"][0]["task_id"] == "T003"
    assert [t.id for t in plan.tasks] == ["T001"]
    assert len(plan.tasks[0].subtasks) == 2  # both kept
