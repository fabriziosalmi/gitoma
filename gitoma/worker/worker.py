"""WorkerAgent — iterates TaskPlan, calls LLM for patches, commits each subtask."""

from __future__ import annotations

from typing import Callable

from gitoma.core.config import Config
from gitoma.core.repo import GitRepo
from gitoma.core.state import AgentState, save_state
from gitoma.planner.llm_client import LLMClient
from gitoma.planner.prompts import worker_system_prompt, worker_user_prompt
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.worker.committer import Committer
from gitoma.worker.patcher import apply_patches


class WorkerAgent:
    """Executes a TaskPlan by generating and committing file patches for each subtask."""

    def __init__(
        self,
        llm: LLMClient,
        git_repo: GitRepo,
        config: Config,
        state: AgentState,
    ) -> None:
        self._llm = llm
        self._git = git_repo
        self._config = config
        self._state = state
        self._committer = Committer(git_repo, config)

    def execute(
        self,
        plan: TaskPlan,
        on_task_start: Callable[[Task], None] | None = None,
        on_subtask_start: Callable[[Task, SubTask], None] | None = None,
        on_subtask_done: Callable[[Task, SubTask, str | None], None] | None = None,
        on_subtask_error: Callable[[Task, SubTask, str], None] | None = None,
    ) -> TaskPlan:
        """
        Execute all pending tasks in the plan.
        Updates plan in-place and persists state after each subtask.

        Returns the updated TaskPlan.
        """
        file_tree = self._git.file_tree(max_files=100)
        languages = self._git.detect_languages()

        for task in plan.tasks:
            if task.status == "completed":
                continue

            task.status = "in_progress"
            if on_task_start:
                on_task_start(task)
            self._persist_plan(plan)

            all_subtasks_ok = True
            for subtask in task.subtasks:
                if subtask.status == "completed":
                    continue

                subtask.status = "in_progress"
                if on_subtask_start:
                    on_subtask_start(task, subtask)
                self._persist_plan(plan)

                try:
                    sha = self._execute_subtask(subtask, file_tree, languages)
                    subtask.status = "completed"
                    subtask.commit_sha = sha or ""
                    # Refresh file tree after changes
                    file_tree = self._git.file_tree(max_files=100)
                    if on_subtask_done:
                        on_subtask_done(task, subtask, sha)
                except Exception as e:
                    error_msg = str(e)[:200]
                    subtask.status = "failed"
                    subtask.error = error_msg
                    all_subtasks_ok = False
                    if on_subtask_error:
                        on_subtask_error(task, subtask, error_msg)

                self._persist_plan(plan)

            task.status = "completed" if all_subtasks_ok else "failed"
            self._persist_plan(plan)

        return plan

    def _execute_subtask(
        self,
        subtask: SubTask,
        file_tree: list[str],
        languages: list[str],
    ) -> str | None:
        """Generate patches for one subtask, apply them, and commit."""
        # Read current content of hinted files
        current_files: dict[str, str] = {}
        for hint in subtask.file_hints[:3]:  # cap to 3 files to control context
            content = self._git.read_file(hint)
            if content:
                current_files[hint] = content

        messages = [
            {"role": "system", "content": worker_system_prompt()},
            {
                "role": "user",
                "content": worker_user_prompt(
                    subtask_title=subtask.title,
                    subtask_description=subtask.description,
                    file_hints=subtask.file_hints,
                    languages=languages,
                    repo_name=self._git.name,
                    current_files=current_files,
                    file_tree=file_tree,
                ),
            },
        ]

        raw = self._llm.chat_json(messages)
        patches = raw.get("patches", [])
        commit_msg = raw.get("commit_message", f"chore: {subtask.title} [gitoma]")

        if not patches:
            raise ValueError("LLM returned no patches for subtask")

        # Ensure commit message has [gitoma] tag
        if "[gitoma]" not in commit_msg:
            commit_msg += " [gitoma]"

        # Apply patches
        touched = apply_patches(self._git.root, patches)

        if not touched:
            raise ValueError("Patches produced no file changes")

        # Commit
        sha = self._committer.commit_patches(touched, commit_msg)
        return sha

    def _persist_plan(self, plan: TaskPlan) -> None:
        """Update state with current plan and save to disk."""
        self._state.task_plan = plan.to_dict()
        save_state(self._state)
