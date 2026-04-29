"""PlannerAgent — converts MetricReport → TaskPlan via LLM."""

from __future__ import annotations

from typing import Any

from gitoma.analyzers.base import MetricReport
from gitoma.context import RepoBrief
from gitoma.planner.llm_client import LLMClient
from gitoma.planner.prompts import planner_system_prompt, planner_user_prompt
from gitoma.planner.task import SubTask, Task, TaskPlan


class PlannerAgent:
    """Calls the LLM to generate a structured TaskPlan from a MetricReport."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def plan(
        self,
        report: MetricReport,
        file_tree: list[str],
        repo_brief: RepoBrief | None = None,
        prior_runs_context: str | None = None,
        repo_fingerprint_context: str | None = None,
        vertical_addendum: str | None = None,
        skeleton_context: str | None = None,
        scaffold_context: str | None = None,
        semgrep_context: str | None = None,
        trivy_context: str | None = None,
    ) -> TaskPlan:
        """
        Generate a TaskPlan from a MetricReport.

        Args:
            report: the full metric analysis report
            file_tree: list of relative file paths in the repo
            repo_brief: optional deterministic repo-wide brief
                (title, stack, build/test commands, CI tools, …) — when
                provided, it is injected at the top of the planner prompt
                so every LLM call has shared ground truth about the project
            vertical_addendum: optional one-paragraph narrowing rule
                from the active Vertical record (e.g. "VERTICAL=docs
                ACTIVE. You may only emit subtasks whose file_hints are
                documentation files."). Injected right before the JSON
                schema instruction so it acts as the highest-recency
                constraint the LLM sees.

        Returns:
            TaskPlan with prioritized tasks and subtasks
        """
        messages = [
            {"role": "system", "content": planner_system_prompt()},
            {
                "role": "user",
                "content": planner_user_prompt(
                    report, file_tree, report.languages,
                    repo_brief=repo_brief,
                    prior_runs_context=prior_runs_context,
                    repo_fingerprint_context=repo_fingerprint_context,
                    vertical_addendum=vertical_addendum,
                    skeleton_context=skeleton_context,
                    scaffold_context=scaffold_context,
                    semgrep_context=semgrep_context,
                    trivy_context=trivy_context,
                ),
            },
        ]

        raw = self._llm.chat_json(messages)
        task_plan = self._parse_plan(raw)
        task_plan.overall_score_before = report.overall_score
        task_plan.llm_model = self._llm.model
        return task_plan

    def _parse_plan(self, raw: dict[str, Any]) -> TaskPlan:
        """Parse the LLM JSON response into a TaskPlan."""
        tasks: list[Task] = []
        for i, t_raw in enumerate(raw.get("tasks", [])[:8]):
            subtasks: list[SubTask] = []
            for j, s_raw in enumerate(t_raw.get("subtasks", [])[:4]):
                st = SubTask(
                    id=s_raw.get("id", f"T{i+1:03d}-S{j+1:02d}"),
                    title=s_raw.get("title", "Untitled subtask"),
                    description=s_raw.get("description", ""),
                    file_hints=s_raw.get("file_hints", []),
                    action=s_raw.get("action", "modify"),
                )
                subtasks.append(st)

            task = Task(
                id=t_raw.get("id", f"T{i+1:03d}"),
                title=t_raw.get("title", "Untitled task"),
                priority=int(t_raw.get("priority", i + 1)),
                metric=t_raw.get("metric", ""),
                description=t_raw.get("description", ""),
                subtasks=subtasks,
            )
            tasks.append(task)

        # Sort by priority
        tasks.sort(key=lambda t: t.priority)
        return TaskPlan(tasks=tasks)
