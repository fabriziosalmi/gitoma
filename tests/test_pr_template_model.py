"""PR template model-name interpolation tests.

Caught live 2026-05-01 by user inspection: the footer hardcoded
"gemma local inference" even when running qwen3-8b/qwen3.5-9b.
The metadata header already used `{plan.llm_model}` correctly;
the footer was a forgotten string. This module pins the
interpolation so a future model swap (or adding a coder/reasoning
model to the zoo) doesn't silently misattribute the run.
"""

from __future__ import annotations

from gitoma.pr.templates import build_pr_body
from gitoma.planner.task import TaskPlan
from gitoma.analyzers.base import MetricReport


def _make_plan(
    model: str, worker_model: str = "", review_model: str = "",
) -> TaskPlan:
    return TaskPlan(
        tasks=[],
        llm_model=model,
        worker_model=worker_model,
        review_model=review_model,
    )


def _make_report() -> MetricReport:
    return MetricReport(
        repo_url="https://github.com/x/y",
        owner="x",
        name="y",
        languages=["Python"],
        default_branch="main",
        metrics=[],
        analyzed_at="2026-05-01T00:00:00Z",
    )


def test_footer_interpolates_planner_model() -> None:
    plan = _make_plan("qwen/qwen3-8b")
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "qwen/qwen3-8b" in body
    # Specifically the footer line, not just the metadata header
    assert "using LM Studio · `qwen/qwen3-8b` local inference" in body


def test_footer_no_hardcoded_gemma() -> None:
    """Regression: footer must not name a fixed model family."""
    plan = _make_plan("qwen/qwen3.5-9b")
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "gemma local inference" not in body
    assert "qwen/qwen3.5-9b" in body


def test_footer_handles_plan_from_file_marker() -> None:
    """`--plan-from-file` runs stamp llm_model='plan-from-file:<filename>'.
    The footer must surface that string verbatim — operators reading
    the PR can see the run was deterministic, not LLM-planned."""
    plan = _make_plan("plan-from-file:tasks.json")
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "`plan-from-file:tasks.json`" in body


def test_metadata_header_and_footer_agree() -> None:
    """Both occurrences of llm_model in the body should match."""
    plan = _make_plan("custom/model-v2")
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    # Header at line ~138: "Model: `{plan.llm_model}`"
    assert "Model: `custom/model-v2`" in body
    # Footer at line ~168: "using LM Studio · `{plan.llm_model}` local inference"
    assert "LM Studio · `custom/model-v2` local inference" in body


# ── Split-topology attribution (2026-05-01) ─────────────────────────────────

def test_split_topology_shows_both_models() -> None:
    """When worker_model differs from llm_model, header + footer show
    both planner and worker so PR readers see the actual topology."""
    plan = _make_plan("qwen/qwen3-8b", worker_model="qwen/qwen3.5-9b")
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    # Header lists both with role labels
    assert "planner=`qwen/qwen3-8b`" in body
    assert "worker=`qwen/qwen3.5-9b`" in body
    # Footer same
    assert "`qwen/qwen3-8b` (planner) + `qwen/qwen3.5-9b` (worker)" in body


def test_same_model_falls_back_to_single_name() -> None:
    """When worker_model is empty (default — single-endpoint setup) OR
    matches the planner, the body uses the single-name format. No
    'planner=' / 'worker=' noise on simple runs."""
    # empty worker_model
    plan_empty = _make_plan("qwen/qwen3-8b", worker_model="")
    body = build_pr_body(_make_report(), plan_empty, branch="x", qa_result=None)
    assert "Model: `qwen/qwen3-8b`" in body
    assert "planner=" not in body
    assert "worker=" not in body
    # worker_model == llm_model (someone set it explicitly to the same value)
    plan_same = _make_plan("qwen/qwen3-8b", worker_model="qwen/qwen3-8b")
    body_same = build_pr_body(_make_report(), plan_same, branch="x", qa_result=None)
    assert "Model: `qwen/qwen3-8b`" in body_same
    assert "planner=" not in body_same


def test_taskplan_dict_roundtrip_preserves_worker_model() -> None:
    """from_dict / to_dict must round-trip the new field — state save +
    resume relies on it."""
    plan = TaskPlan(
        tasks=[],
        llm_model="qwen/qwen3-8b",
        worker_model="qwen/qwen3.5-9b",
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert restored.llm_model == "qwen/qwen3-8b"
    assert restored.worker_model == "qwen/qwen3.5-9b"


# ── 3-way attribution (2026-05-01) ──────────────────────────────────────────

def test_three_way_attribution_when_all_distinct() -> None:
    """planner ≠ worker ≠ reviewer → header + footer show all three
    with explicit role labels."""
    plan = _make_plan(
        "qwen/qwen3-8b",
        worker_model="qwen/qwen3.5-9b",
        review_model="google/gemma-4-e2b",
    )
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "Planned by `qwen/qwen3-8b`" in body
    assert "Coded by `qwen/qwen3.5-9b`" in body
    assert "Reviewed by `google/gemma-4-e2b`" in body
    assert "(reviewer)" in body  # footer carries role tags


def test_planner_equals_reviewer_collapses_to_2way() -> None:
    """When reviewer falls back to planner (default), only planner +
    worker are shown — no redundant "Reviewed by `same-as-planner`"."""
    plan = _make_plan(
        "qwen/qwen3-8b",
        worker_model="qwen/qwen3.5-9b",
        review_model="",  # falls back to planner client
    )
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "planner=`qwen/qwen3-8b`" in body
    assert "worker=`qwen/qwen3.5-9b`" in body
    assert "Reviewed by" not in body  # no 3-way label
    assert "Planned by" not in body


def test_planner_equals_worker_with_distinct_reviewer() -> None:
    """Worker shares planner endpoint, but a third reviewer model is
    set — header shows planner + reviewer."""
    plan = _make_plan(
        "qwen/qwen3-8b",
        worker_model="",  # same as planner
        review_model="google/gemma-4-e2b",
    )
    body = build_pr_body(_make_report(), plan, branch="x", qa_result=None)
    assert "planner=`qwen/qwen3-8b`" in body
    assert "reviewer=`google/gemma-4-e2b`" in body
    assert "worker=" not in body


def test_taskplan_roundtrip_preserves_review_model() -> None:
    plan = TaskPlan(
        tasks=[],
        llm_model="A",
        worker_model="B",
        review_model="C",
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert restored.llm_model == "A"
    assert restored.worker_model == "B"
    assert restored.review_model == "C"
