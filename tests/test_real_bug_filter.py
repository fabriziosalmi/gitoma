"""Tests for the deterministic plan post-processors:
``synthesize_real_bug_task`` (Layer-A) + ``banish_readme_only_subtasks``
(Layer-B). LLM-free, replays scenarios from b2v PR matrix and from
the original rung-0 evidence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitoma.analyzers.base import MetricResult, MetricReport
from gitoma.planner.real_bug_filter import (
    README_BASENAMES,
    _extract_failing_tests,
    banish_readme_only_subtasks,
    synthesize_real_bug_task,
)
from gitoma.planner.task import SubTask, Task, TaskPlan


def _mk_metric(name: str, status: str, details: str = "") -> MetricResult:
    """Build a MetricResult with a chosen status. ``from_score`` maps
    score → status; we cheat by picking a score in the right band."""
    score = {"pass": 0.9, "warn": 0.5, "fail": 0.1}[status]
    return MetricResult.from_score(
        name=name,
        display_name=name,
        score=score,
        details=details,
    )


def _mk_report(metrics: list[MetricResult]) -> MetricReport:
    return MetricReport(
        repo_url="x",
        owner="x",
        name="x",
        languages=[],
        default_branch="main",
        metrics=metrics,
        analyzed_at="2026-04-25T00:00:00Z",
    )


def _mk_subtask(sid: str, file_hints: list[str], title: str = "x") -> SubTask:
    return SubTask(
        id=sid,
        title=title,
        description="",
        file_hints=file_hints,
        action="modify",
    )


def _mk_task(tid: str, subtasks: list[SubTask], priority: int = 2) -> Task:
    return Task(
        id=tid,
        title=f"Task {tid}",
        priority=priority,
        metric="x",
        description="",
        subtasks=subtasks,
    )


# ── _extract_failing_tests ────────────────────────────────────────────


def test_extract_basic_bullets() -> None:
    details = "Tests failing.\n  • tests/test_db.py\n  • tests/test_api.py\n"
    assert _extract_failing_tests(details) == ["tests/test_db.py", "tests/test_api.py"]


def test_extract_strips_pytest_test_name() -> None:
    """``  • tests/test_db.py::test_get_user`` → ``tests/test_db.py``."""
    details = "  • tests/test_db.py::test_get_user\n  • tests/test_db.py::test_create\n"
    assert _extract_failing_tests(details) == ["tests/test_db.py"]


def test_extract_dedupes_preserving_order() -> None:
    details = "  • a/b.py\n  • c/d.py\n  • a/b.py\n"
    assert _extract_failing_tests(details) == ["a/b.py", "c/d.py"]


def test_extract_returns_empty_on_no_bullets() -> None:
    """Covers the 'parser couldn't extract' details branch — we
    don't have specific failing files, so we can't synthesize."""
    assert _extract_failing_tests("TESTS FAILED (parser couldn't extract).") == []


def test_extract_returns_empty_on_empty_input() -> None:
    assert _extract_failing_tests("") == []


# ── synthesize_real_bug_task ─────────────────────────────────────────


def test_synthesize_fires_when_failing_tests_uncovered(tmp_path: Path) -> None:
    """The headline scenario: test_results=fail, planner ignored the
    failing tests and emitted only generic tasks → synthesize T000."""
    # Set up a tests dir and source dir so test_to_source can map
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text(
        "from src.db import get_conn\n\ndef test_get(): assert get_conn()\n"
    )
    (tmp_path / "src" / "db.py").write_text("def get_conn(): return True\n")
    # Existing plan has T001 targeting unrelated docs
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["README.md"])]),
    ])
    report = _mk_report([
        _mk_metric("test_results", "fail",
                   "PYTHON TESTS FAILING (1).\n  • tests/test_db.py::test_get\n"),
    ])
    summary = synthesize_real_bug_task(plan, report, tmp_path)
    assert summary is not None
    assert summary["synthesized_task_id"] == "T000"
    # T000 is now first
    assert plan.tasks[0].id == "T000"
    assert plan.tasks[0].priority == 1
    # And targets src/db.py
    assert plan.tasks[0].subtasks[0].file_hints == ["src/db.py"]


def test_synthesize_noop_when_test_results_passing(tmp_path: Path) -> None:
    plan = TaskPlan(tasks=[_mk_task("T001", [_mk_subtask("T001-S01", ["x"])])])
    report = _mk_report([_mk_metric("test_results", "pass", "all good")])
    assert synthesize_real_bug_task(plan, report, tmp_path) is None


def test_synthesize_noop_when_no_test_results_metric(tmp_path: Path) -> None:
    """No test_results metric in the report at all (e.g. test runner
    skipped because no recognised stack)."""
    plan = TaskPlan(tasks=[_mk_task("T001", [_mk_subtask("T001-S01", ["x"])])])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    assert synthesize_real_bug_task(plan, report, tmp_path) is None


def test_synthesize_noop_when_failing_paths_unparseable(tmp_path: Path) -> None:
    plan = TaskPlan(tasks=[_mk_task("T001", [_mk_subtask("T001-S01", ["x"])])])
    report = _mk_report([
        _mk_metric("test_results", "fail",
                   "TESTS FAILED (parser couldn't extract specific failures)."),
    ])
    assert synthesize_real_bug_task(plan, report, tmp_path) is None


def test_synthesize_noop_when_plan_already_targets_source(tmp_path: Path) -> None:
    """If the planner already emitted a task with file_hints matching
    a mapped source file, we don't duplicate — let the planner's
    plan stand."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text(
        "from src.db import get_conn\ndef test_get(): pass\n"
    )
    (tmp_path / "src" / "db.py").write_text("def get_conn(): pass\n")
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["src/db.py"])]),
    ])
    report = _mk_report([
        _mk_metric("test_results", "fail",
                   "TESTS FAILING (1).\n  • tests/test_db.py\n"),
    ])
    assert synthesize_real_bug_task(plan, report, tmp_path) is None


def test_synthesize_caps_at_4_subtasks(tmp_path: Path) -> None:
    """Many failing tests → many source files → still cap at 4 subtasks
    to keep the synthesized task focused."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    for i in range(6):
        (tmp_path / "tests" / f"test_m{i}.py").write_text(
            f"from src.m{i} import f\ndef test_(): pass\n"
        )
        (tmp_path / "src" / f"m{i}.py").write_text("def f(): pass\n")
    plan = TaskPlan(tasks=[_mk_task("T001", [_mk_subtask("T001-S01", ["x"])])])
    bullets = "\n".join(f"  • tests/test_m{i}.py" for i in range(6))
    report = _mk_report([_mk_metric("test_results", "fail", bullets)])
    summary = synthesize_real_bug_task(plan, report, tmp_path)
    assert summary is not None
    assert summary["subtasks_created"] == 4
    assert len(plan.tasks[0].subtasks) == 4


# ── banish_readme_only_subtasks ──────────────────────────────────────


def test_banish_drops_readme_only_subtask() -> None:
    """The b2v PR #24/#26/#27 pattern: one subtask whose only
    file_hint is README.md. Drop it deterministically."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["src/main.rs"]),
            _mk_subtask("T001-S02", ["README.md"], title="Update README"),
        ]),
    ])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    summary = banish_readme_only_subtasks(plan, report)
    assert summary is not None
    assert summary["drop_count"] == 1
    assert len(plan.tasks[0].subtasks) == 1
    assert plan.tasks[0].subtasks[0].id == "T001-S01"


def test_banish_keeps_readme_when_doc_metric_explicitly_cites() -> None:
    """If documentation metric is failing AND the details text cites
    README, the user/analyzer is signaling a real README problem —
    don't banish."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["README.md"])]),
    ])
    report = _mk_report([
        _mk_metric("documentation", "fail",
                   "README missing key sections (Installation, Usage)"),
    ])
    assert banish_readme_only_subtasks(plan, report) is None
    assert plan.tasks[0].subtasks[0].file_hints == ["README.md"]


def test_banish_keeps_subtasks_with_readme_AND_other_files() -> None:
    """A subtask hinting BOTH README + a source file is NOT pure
    README-stub work — keep it. Only drop pure README-only subtasks."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["src/main.rs", "README.md"]),
        ]),
    ])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    assert banish_readme_only_subtasks(plan, report) is None
    assert plan.tasks[0].subtasks[0].file_hints == ["src/main.rs", "README.md"]


def test_banish_removes_task_left_empty() -> None:
    """A task whose ALL subtasks were README-only → after banish has
    no subtasks → remove the whole task from the plan."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["src/main.rs"])]),
        _mk_task("T002", [
            _mk_subtask("T002-S01", ["README.md"]),
            _mk_subtask("T002-S02", ["README.rst"]),
        ]),
    ])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    summary = banish_readme_only_subtasks(plan, report)
    assert summary is not None
    assert summary["drop_count"] == 2
    assert summary["tasks_removed"] == 1
    assert [t.id for t in plan.tasks] == ["T001"]


def test_banish_handles_readme_variants() -> None:
    """All registered README basename variants get banished."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask(f"T001-S{i:02d}", [name])
                          for i, name in enumerate(README_BASENAMES, 1)]),
    ])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    summary = banish_readme_only_subtasks(plan, report)
    assert summary is not None
    assert summary["drop_count"] == len(README_BASENAMES)
    assert plan.tasks == []


def test_banish_noop_when_no_readme_subtasks() -> None:
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["src/main.rs"])]),
    ])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    assert banish_readme_only_subtasks(plan, report) is None


def test_banish_noop_when_empty_plan() -> None:
    plan = TaskPlan(tasks=[])
    report = _mk_report([_mk_metric("documentation", "warn", "")])
    assert banish_readme_only_subtasks(plan, report) is None


def test_banish_noop_when_no_documentation_metric() -> None:
    """Plan with README subtask but no documentation metric in audit
    — banish still applies (no signal that README is genuinely
    needed)."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["README.md"])]),
    ])
    report = _mk_report([_mk_metric("test_results", "pass", "")])
    summary = banish_readme_only_subtasks(plan, report)
    assert summary is not None
    assert summary["drop_count"] == 1


# ── Integration: A + B compose ───────────────────────────────────────


def test_synthesize_then_banish_compose(tmp_path: Path) -> None:
    """Realistic flow: synthesize T000 (Layer A) AND drop a
    README-only subtask (Layer B). Both should fire on a single plan
    that has tests failing AND a hallucinated README task."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "from src.x import f\ndef test_(): pass\n"
    )
    (tmp_path / "src" / "x.py").write_text("def f(): pass\n")
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["docs/guide.md"]),
            _mk_subtask("T001-S02", ["README.md"], title="Update README"),
        ]),
    ])
    report = _mk_report([
        _mk_metric("test_results", "fail",
                   "TESTS FAILING (1).\n  • tests/test_x.py\n"),
        _mk_metric("documentation", "warn", "missing CONTRIBUTING.md"),
    ])
    sa = synthesize_real_bug_task(plan, report, tmp_path)
    sb = banish_readme_only_subtasks(plan, report)
    assert sa is not None
    assert sb is not None
    assert sb["drop_count"] == 1
    # T000 (synth) + T001 (with only docs/guide.md surviving)
    assert [t.id for t in plan.tasks] == ["T000", "T001"]
    assert plan.tasks[1].subtasks[0].id == "T001-S01"
