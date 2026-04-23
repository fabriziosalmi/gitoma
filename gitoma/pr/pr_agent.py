"""PRAgent — pushes the branch and creates the GitHub PR."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gitoma.analyzers.base import MetricReport
from gitoma.core.config import Config
from gitoma.core.github_client import GitHubClient, PRInfo
from gitoma.core.repo import GitRepo
from gitoma.core.state import AgentState, AgentPhase, save_state
from gitoma.planner.task import TaskPlan
from gitoma.pr.templates import build_pr_body, build_pr_title

if TYPE_CHECKING:
    from gitoma.critic.types import QAResult


class PRAgent:
    """Pushes the gitoma branch and opens a PR on GitHub."""

    def __init__(
        self,
        git_repo: GitRepo,
        gh_client: GitHubClient,
        config: Config,
        state: AgentState,
    ) -> None:
        self._git = git_repo
        self._gh = gh_client
        self._config = config
        self._state = state

    def push_and_open_pr(
        self,
        report: MetricReport,
        plan: TaskPlan,
        branch: str,
        base: str,
        qa_result: "QAResult | None" = None,
    ) -> PRInfo:
        """
        Push the agent branch to origin and create a PR.

        Args:
            report: the metric analysis that drove the plan
            plan: the completed task plan
            branch: the gitoma branch name (e.g. gitoma/improve-20260420)
            base: the default branch to merge into (e.g. main)
            qa_result: optional Q&A phase result. When the Defender
                admitted a gap, the PR body grows a ``⚠ Q&A gap``
                block so the operator sees the unfixed signal before
                merging. ``None`` when the Q&A phase was disabled or
                never reached — body unchanged in that case.

        Returns:
            PRInfo with PR number and URL
        """
        # Push branch
        self._git.push(branch)

        # Check if PR already exists
        existing = self._gh.get_open_pr_for_branch(
            self._git.owner, self._git.name, branch
        )
        if existing:
            return existing

        # Build PR content
        title = build_pr_title(self._git.name, plan)
        body = build_pr_body(report, plan, branch, qa_result=qa_result)

        # Create PR
        pr_info = self._gh.create_pr(
            self._git.owner,
            self._git.name,
            title=title,
            body=body,
            head=branch,
            base=base,
            draft=False,
        )

        # Add labels
        try:
            self._gh.add_pr_labels(
                self._git.owner,
                self._git.name,
                pr_info.number,
                ["gitoma", "ai-improved", "automated"],
            )
        except Exception:
            pass  # Labels are nice-to-have

        # Persist PR info to state
        self._state.pr_number = pr_info.number
        self._state.pr_url = pr_info.url
        self._state.advance(AgentPhase.PR_OPEN)
        save_state(self._state)

        return pr_info
