"""Tests for the --plan-from-file loader.

Covers: file IO errors, JSON syntax errors, schema mismatches, edge
cases (empty plan, plan with task but no subtasks), and a happy
path with a fully-specified plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitoma.planner.plan_loader import PlanFileError, load_plan_from_file
from gitoma.planner.task import TaskPlan


def _write(tmp_path: Path, name: str, body: str | dict) -> Path:
    p = tmp_path / name
    if isinstance(body, dict):
        p.write_text(json.dumps(body))
    else:
        p.write_text(body)
    return p


def _minimal_plan_dict() -> dict:
    return {
        "tasks": [
            {
                "id": "T001",
                "title": "Refactor core",
                "priority": 1,
                "metric": "Code Quality",
                "description": "Consolidate duplicated wrappers",
                "subtasks": [
                    {
                        "id": "T001-S01",
                        "title": "Inline wrappers",
                        "description": "Inline process_a/b/c/d into call sites",
                        "file_hints": ["core_helpers.py"],
                        "action": "modify",
                    }
                ],
            }
        ]
    }


# ── File IO + JSON parse ──────────────────────────────────────────


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PlanFileError, match="not found"):
        load_plan_from_file(tmp_path / "nonexistent.json")


def test_directory_path_raises(tmp_path: Path) -> None:
    d = tmp_path / "a-dir"
    d.mkdir()
    with pytest.raises(PlanFileError, match="not a regular file"):
        load_plan_from_file(d)


def test_invalid_json_raises_with_location(tmp_path: Path) -> None:
    p = _write(tmp_path, "bad.json", '{"tasks": [unterminated')
    with pytest.raises(PlanFileError, match="invalid JSON"):
        load_plan_from_file(p)


def test_top_level_array_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "arr.json", "[]")
    with pytest.raises(PlanFileError, match="must be an object"):
        load_plan_from_file(p)


def test_missing_tasks_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "no-tasks.json", {"created_at": "2026-01-01"})
    with pytest.raises(PlanFileError, match="missing required 'tasks' key"):
        load_plan_from_file(p)


def test_tasks_not_a_list_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "bad-tasks.json", {"tasks": "nope"})
    with pytest.raises(PlanFileError, match="'tasks' must be a list"):
        load_plan_from_file(p)


def test_empty_tasks_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "empty.json", {"tasks": []})
    with pytest.raises(PlanFileError, match="must contain at least one task"):
        load_plan_from_file(p)


def test_task_with_no_subtasks_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "no-subs.json", {
        "tasks": [{
            "id": "T001",
            "title": "x",
            "priority": 1,
            "metric": "Code Quality",
            "description": "y",
            "subtasks": [],
        }]
    })
    with pytest.raises(PlanFileError, match="zero subtasks"):
        load_plan_from_file(p)


def test_schema_mismatch_rejected(tmp_path: Path) -> None:
    """Missing required field on Task should bubble up with a clear msg."""
    p = _write(tmp_path, "broken.json", {
        "tasks": [{"id": "T001"}]   # missing title/priority/metric/description
    })
    with pytest.raises(PlanFileError, match="schema mismatch"):
        load_plan_from_file(p)


# ── Happy path ────────────────────────────────────────────────────


def test_minimal_valid_plan_loads(tmp_path: Path) -> None:
    p = _write(tmp_path, "ok.json", _minimal_plan_dict())
    plan = load_plan_from_file(p)
    assert isinstance(plan, TaskPlan)
    assert plan.total_tasks == 1
    assert plan.total_subtasks == 1
    assert plan.tasks[0].id == "T001"
    assert plan.tasks[0].subtasks[0].file_hints == ["core_helpers.py"]
    assert plan.tasks[0].subtasks[0].action == "modify"


def test_loader_stamps_source_in_llm_model(tmp_path: Path) -> None:
    """Provenance: downstream tracing must be able to tell curated
    plans apart from LLM-generated ones."""
    p = _write(tmp_path, "curated.json", _minimal_plan_dict())
    plan = load_plan_from_file(p)
    assert plan.llm_model == "plan-from-file:curated.json"


def test_roundtrip_to_dict_then_load(tmp_path: Path) -> None:
    """A plan emitted via TaskPlan.to_dict and reloaded via the
    loader must be equivalent to the original."""
    p = _write(tmp_path, "rt.json", _minimal_plan_dict())
    plan_a = load_plan_from_file(p)
    p2 = _write(tmp_path, "rt2.json", plan_a.to_dict())
    plan_b = load_plan_from_file(p2)
    assert plan_a.total_tasks == plan_b.total_tasks
    assert plan_a.total_subtasks == plan_b.total_subtasks
    assert plan_a.tasks[0].id == plan_b.tasks[0].id


def test_multi_task_multi_subtask(tmp_path: Path) -> None:
    """3 tasks × 2 subtasks each — full shape exercises Task.from_dict
    + SubTask.from_dict iteration."""
    body = {
        "tasks": [
            {
                "id": f"T00{i}",
                "title": f"Task {i}",
                "priority": i,
                "metric": "Code Quality",
                "description": f"Do thing {i}",
                "subtasks": [
                    {
                        "id": f"T00{i}-S0{j}",
                        "title": f"sub {i}.{j}",
                        "description": f"Do sub {i}.{j}",
                        "file_hints": [f"file_{i}_{j}.py"],
                        "action": "create" if j == 1 else "modify",
                    }
                    for j in (1, 2)
                ],
            }
            for i in (1, 2, 3)
        ]
    }
    p = _write(tmp_path, "multi.json", body)
    plan = load_plan_from_file(p)
    assert plan.total_tasks == 3
    assert plan.total_subtasks == 6
    # Spot-check action variety
    actions = {s.action for t in plan.tasks for s in t.subtasks}
    assert actions == {"create", "modify"}


def test_loader_accepts_str_or_path(tmp_path: Path) -> None:
    p = _write(tmp_path, "str.json", _minimal_plan_dict())
    a = load_plan_from_file(str(p))
    b = load_plan_from_file(p)
    assert a.total_tasks == b.total_tasks
