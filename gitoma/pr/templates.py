"""PR body templates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gitoma.analyzers.base import MetricReport
from gitoma.planner.task import TaskPlan

if TYPE_CHECKING:
    from gitoma.critic.types import QAResult


STATUS_EMOJI = {"pass": "✅", "warn": "⚠️", "fail": "❌"}


def build_qa_section(qa_result: "QAResult | None") -> str:
    """Render a Q&A annotation block for the PR body, or an empty string.

    The block is only emitted when there is a SIGNAL the operator should
    see before merging:

      * Defender admitted a ``gap`` AND no revised patch landed → ⚠ block
        listing every gap with its rationale (the most important case;
        caught live on rung-3 v6 — pipeline honest, PR ships, operator
        needs to know).
      * Defender admitted a gap AND a revised patch landed (``revised_applied``)
        → informational note that the gate-validated revision is on the
        branch.
      * All-handled (no gaps) → empty (nothing actionable to say).
      * Q&A skipped/never ran → empty (don't pollute the body with
        meta-noise on runs that didn't enable the phase).
      * Q&A CRASHED mid-phase → ⚠ block flagging the operator. Silent
        absence = "all clear" misreads "the gate broke" as "the gate
        passed". Rung-3 v13/v14 occasionally hit this; without a
        signal in the body, a reviewer would merge thinking the
        Q&A self-consistency had succeeded.
    """
    if qa_result is None:
        return ""

    if qa_result.crashed:
        reason = (qa_result.crash_reason or "no reason captured").strip()
        # Trim multi-line tracebacks — the PR body should give a clear
        # one-line signal; the full trace lives in the jsonl.
        reason_line = reason.splitlines()[0][:300]
        return (
            "\n---\n\n"
            "## ⚠️ Q&A self-consistency phase CRASHED\n\n"
            "The post-meta Q&A gate raised an exception before producing "
            "answers. **Treat this PR as ungated** — the patch was reviewed "
            "by the panel + devil but did NOT pass the additional Q&A "
            "self-consistency check. Inspect the run's trace JSONL for "
            "the full traceback before merging.\n\n"
            f"_Crash reason:_ `{reason_line}`\n"
        )

    if not qa_result.ran:
        return ""

    gap_answers = [
        a for a in qa_result.answers
        if isinstance(a, dict) and a.get("verdict") == "gap"
    ]
    if not gap_answers:
        return ""

    bullets = "\n".join(
        f"- **{a.get('id', '?')}** — {a.get('rationale', '(no rationale)')[:300]}"
        for a in gap_answers
    )

    if qa_result.revised_applied:
        # Gap was real AND closed by a gated revision. Reassuring note.
        return (
            "\n---\n\n"
            "## ✅ Q&A revised patch landed\n\n"
            "The post-meta Q&A phase identified gaps and the Defender's "
            "revised patch passed the BuildAnalyzer + test gate. Original "
            "gaps for context:\n\n"
            f"{bullets}\n"
        )

    # Honest unfixed-gap signal — most important case.
    revert_note = (
        f"\n\n_A revised patch was attempted but reverted by the gate: "
        f"`{qa_result.revert_reason[:200]}`._"
        if qa_result.revert_reason else ""
    )
    return (
        "\n---\n\n"
        "## ⚠️ Q&A gap (unfixed)\n\n"
        "The post-meta Q&A self-consistency phase identified one or more "
        "gaps in this PR that the Defender could not close from here. "
        "**Review carefully before merging.**\n\n"
        f"{bullets}{revert_note}\n"
    )


def build_pr_body(
    report: MetricReport,
    plan: TaskPlan,
    branch: str,
    qa_result: "QAResult | None" = None,
) -> str:
    """Generate a rich Markdown PR description."""

    # Metric table
    metric_rows = "\n".join(
        f"| {STATUS_EMOJI.get(m.status, '❓')} **{m.display_name}** "
        f"| `{m.score:.0%}` | {m.details} |"
        for m in report.metrics
    )

    # Task list
    task_items = ""
    for task in plan.tasks:
        task_items += f"\n### {task.id}: {task.title}\n"
        task_items += f"> {task.description}\n\n"
        for sub in task.subtasks:
            icon = "✅" if sub.status == "completed" else "🔧"
            sha_ref = f" `{sub.commit_sha[:7]}`" if sub.commit_sha else ""
            task_items += f"- {icon} **{sub.id}** — {sub.title}{sha_ref}\n"
        task_items += "\n"

    # Stats
    completed = plan.completed_tasks
    total = plan.total_tasks
    st_completed = sum(s.status == "completed" for t in plan.tasks for s in t.subtasks)
    st_total = plan.total_subtasks
    overall_before = f"{report.overall_score:.0%}"

    qa_block = build_qa_section(qa_result)

    # Model attribution: collapses gracefully from 3-way → 2-way →
    # 1-way as roles share models. PR readers see exactly which
    # models played which role, no fluff when the topology is simple.
    # ``getattr`` fallbacks defend against TaskPlan instances that
    # predate the worker_model/review_model fields — e.g. resumed
    # state files written before the schema bump 2026-05-01.
    _planner = plan.llm_model
    _worker = getattr(plan, "worker_model", "") or _planner
    _reviewer = getattr(plan, "review_model", "") or _planner
    _ensemble = list(getattr(plan, "review_models", []) or [])
    _min_agree = int(getattr(plan, "review_min_agree", 0) or 0)
    if _ensemble and len(_ensemble) >= 2 and _min_agree >= 2:
        # Reviewer ENSEMBLE path (2026-05-02). Show planner + worker
        # collapse if same, then enumerate ensemble members + agreement
        # floor. ``review_model`` (singular) is ignored on this branch.
        _members = ", ".join(f"`{m}`" for m in _ensemble)
        _ensemble_label = (
            f"Reviewed by ensemble {_min_agree}/{len(_ensemble)}: {_members}"
        )
        _ensemble_footer = (
            f"reviewer ensemble {_min_agree}/{len(_ensemble)} "
            f"({', '.join(f'`{m}`' for m in _ensemble)})"
        )
        if _worker != _planner:
            _model_label = (
                f"Planned by `{_planner}` · Coded by `{_worker}` · {_ensemble_label}"
            )
            _footer_model = (
                f"`{_planner}` (planner) + `{_worker}` (coder) + {_ensemble_footer}"
            )
        else:
            _model_label = f"Planned/coded by `{_planner}` · {_ensemble_label}"
            _footer_model = f"`{_planner}` (planner+coder) + {_ensemble_footer}"
    else:
        _distinct = {_planner, _worker, _reviewer}
        if len(_distinct) == 3:
            _model_label = (
                f"Planned by `{_planner}` · Coded by `{_worker}` "
                f"· Reviewed by `{_reviewer}`"
            )
            _footer_model = (
                f"`{_planner}` (planner) + `{_worker}` (coder) "
                f"+ `{_reviewer}` (reviewer)"
            )
        elif _worker != _planner:
            _model_label = f"planner=`{_planner}` · worker=`{_worker}`"
            _footer_model = f"`{_planner}` (planner) + `{_worker}` (worker)"
        elif _reviewer != _planner:
            _model_label = f"planner=`{_planner}` · reviewer=`{_reviewer}`"
            _footer_model = f"`{_planner}` (planner) + `{_reviewer}` (reviewer)"
        else:
            _model_label = f"`{_planner}`"
            _footer_model = f"`{_planner}`"

    return f"""## 🤖 Gitoma Automated Improvement PR

> Generated by **[Gitoma](https://github.com/fabgpt-coder)** — AI-powered repository improvement agent.
> Branch: `{branch}` | Model: {_model_label}

---

## 📊 Repo Health Before → After

| Status | Metric | Score | Details |
|--------|--------|-------|---------|
{metric_rows}

**Overall score before:** `{overall_before}`

---

## 📋 Tasks Completed ({completed}/{total} tasks · {st_completed}/{st_total} subtasks)

{task_items}
{qa_block}

---

## ✅ Review Checklist

- [ ] CI passes on this branch
- [ ] No unintended side effects from generated files
- [ ] Commit messages follow Conventional Commits
- [ ] Merge when ready — no further action needed from the bot

---

<sub>🤖 This PR was automatically generated by [Gitoma](https://github.com/fabgpt-coder) using LM Studio · {_footer_model} local inference.<br>
Please review carefully before merging. The agent worked hard on this! 💜</sub>
"""


def build_pr_title(repo_name: str, plan: TaskPlan) -> str:
    n_tasks = plan.total_tasks
    n_fixes = sum(
        1 for t in plan.tasks if t.metric in ("ci", "security", "tests")
    )
    if n_fixes:
        return f"🤖 [Gitoma] Improve {repo_name}: {n_tasks} improvements ({n_fixes} critical fixes)"
    return f"🤖 [Gitoma] Improve {repo_name}: {n_tasks} quality improvements"
