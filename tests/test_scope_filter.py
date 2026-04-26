"""Tests for the docs vertical scope filter — `gitoma docs`'s
deterministic narrowing of metrics + plan to doc-only scope."""

from __future__ import annotations

import pytest

from gitoma.analyzers.base import MetricResult, MetricReport
from gitoma.planner.scope_filter import (
    DOC_METRIC_NAMES,
    active_scope,
    filter_metrics_to_doc_scope,
    filter_plan_to_doc_scope,
    is_doc_path,
)
from gitoma.planner.task import SubTask, Task, TaskPlan


def _mk_metric(name: str) -> MetricResult:
    return MetricResult.from_score(
        name=name, display_name=name, score=0.5, details="",
    )


def _mk_subtask(sid: str, file_hints: list[str]) -> SubTask:
    return SubTask(
        id=sid, title="x", description="", file_hints=file_hints, action="modify",
    )


def _mk_task(tid: str, subs: list[SubTask]) -> Task:
    return Task(id=tid, title=tid, priority=2, metric="x", description="", subtasks=subs)


def _mk_report(metrics: list[MetricResult]) -> MetricReport:
    return MetricReport(
        repo_url="x", owner="x", name="x", languages=[],
        default_branch="main", metrics=metrics, analyzed_at="t",
    )


# ── is_doc_path ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("README.md",                       True),
    ("README",                          True),
    ("README.rst",                      True),
    ("CHANGELOG.md",                    True),
    ("CONTRIBUTING.md",                 True),
    ("docs/index.md",                   True),
    ("docs/guide/quickstart.md",        True),
    ("doc/getting-started.rst",         True),
    ("documentation/architecture.adoc", True),
    ("website/blog/post.mdx",           True),
    ("notes.txt",                       True),
    # Source code — not docs
    ("src/main.py",                     False),
    ("src/main.rs",                     False),
    ("config.yaml",                     False),
    ("package.json",                    False),
    ("Cargo.toml",                      False),
    (".github/workflows/ci.yml",        False),
    # Edge: a Python module under docs/ would still match (path-prefix)
    ("docs/hooks.py",                   True),
    # Empty
    ("",                                False),
])
def test_is_doc_path(path: str, expected: bool) -> None:
    assert is_doc_path(path) is expected


# ── filter_metrics_to_doc_scope ──────────────────────────────────────


def test_filter_metrics_keeps_only_doc_relevant() -> None:
    report = _mk_report([
        _mk_metric("documentation"),
        _mk_metric("readme"),
        _mk_metric("build"),
        _mk_metric("test_results"),
        _mk_metric("security"),
    ])
    summary = filter_metrics_to_doc_scope(report)
    assert summary is not None
    assert summary["scope"] == "docs"
    kept_names = {m.name for m in report.metrics}
    assert "documentation" in kept_names
    assert "readme" in kept_names
    assert "build" not in kept_names
    assert "test_results" not in kept_names
    assert summary["metrics_dropped"] == ["build", "test_results", "security"]


def test_filter_metrics_noop_when_already_doc_only() -> None:
    report = _mk_report([_mk_metric("documentation"), _mk_metric("readme")])
    assert filter_metrics_to_doc_scope(report) is None
    assert len(report.metrics) == 2


def test_filter_metrics_noop_on_empty_report() -> None:
    report = _mk_report([])
    assert filter_metrics_to_doc_scope(report) is None


# ── filter_plan_to_doc_scope ─────────────────────────────────────────


def test_plan_filter_drops_source_only_subtask() -> None:
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["docs/intro.md"]),
            _mk_subtask("T001-S02", ["src/main.py"]),
        ]),
    ])
    summary = filter_plan_to_doc_scope(plan)
    assert summary is not None
    assert summary["drop_count"] == 1
    assert plan.tasks[0].subtasks[0].id == "T001-S01"
    assert summary["dropped_subtasks"][0]["subtask_id"] == "T001-S02"


def test_plan_filter_drops_mixed_doc_and_source() -> None:
    """A subtask hinting BOTH a doc and a source file is OUT — the
    docs vertical is strict; mixed-hint = source touch = drop."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["README.md", "src/main.py"]),
        ]),
    ])
    summary = filter_plan_to_doc_scope(plan)
    assert summary is not None
    assert summary["drop_count"] == 1
    assert plan.tasks == []


def test_plan_filter_keeps_empty_file_hints() -> None:
    """No file_hints = ``verify`` action; let it through. Worker
    will either no-op or soft-fail it."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", [])]),
    ])
    summary = filter_plan_to_doc_scope(plan)
    assert summary is None
    assert plan.tasks[0].subtasks[0].id == "T001-S01"


def test_plan_filter_removes_empty_tasks() -> None:
    """Task whose subtasks were all dropped → task removed."""
    plan = TaskPlan(tasks=[
        _mk_task("T001", [_mk_subtask("T001-S01", ["src/main.py"])]),
        _mk_task("T002", [_mk_subtask("T002-S01", ["docs/intro.md"])]),
    ])
    summary = filter_plan_to_doc_scope(plan)
    assert summary is not None
    assert summary["drop_count"] == 1
    assert summary["tasks_removed"] == 1
    assert [t.id for t in plan.tasks] == ["T002"]


def test_plan_filter_noop_on_all_doc_plan() -> None:
    plan = TaskPlan(tasks=[
        _mk_task("T001", [
            _mk_subtask("T001-S01", ["README.md"]),
            _mk_subtask("T001-S02", ["docs/guide.md"]),
        ]),
    ])
    assert filter_plan_to_doc_scope(plan) is None


# ── active_scope ─────────────────────────────────────────────────────


def test_active_scope_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_SCOPE", raising=False)
    assert active_scope() is None


def test_active_scope_returns_docs_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_SCOPE", "docs")
    assert active_scope() == "docs"


def test_active_scope_normalises_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_SCOPE", "  DOCS  ")
    assert active_scope() == "docs"


def test_active_scope_passes_through_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Future verticals can opt in via the same env var without
    touching this helper. We pass-through rather than enumerate."""
    monkeypatch.setenv("GITOMA_SCOPE", "tests")
    assert active_scope() == "tests"


# ── Constants sanity ────────────────────────────────────────────────


def test_doc_metric_names_includes_standard() -> None:
    for name in ("documentation", "readme"):
        assert name in DOC_METRIC_NAMES
