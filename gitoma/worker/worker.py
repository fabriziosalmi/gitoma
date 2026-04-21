"""WorkerAgent — iterates TaskPlan, calls LLM for patches, commits each subtask."""

from __future__ import annotations

from typing import Callable

from gitoma.core.config import Config
from gitoma.core.repo import GitRepo
from gitoma.core.state import AgentState, save_state
from gitoma.core.trace import current as current_trace
from gitoma.critic import CriticPanel, PanelResult
from gitoma.planner.llm_client import LLMClient
from gitoma.planner.prompts import worker_system_prompt, worker_user_prompt
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.worker.committer import Committer
from gitoma.worker.patcher import apply_patches

# Cap on how many critic panel runs we keep in AgentState before dropping
# the oldest. State.json must stay manageable on long runs (60+ subtasks);
# the trace JSONL keeps the full history regardless.
_MAX_PANEL_LOG_ENTRIES = 200


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
        # Critic panel is created lazily — only when mode != "off" — so a
        # fresh-clone test that never touches the panel doesn't pay for an
        # unused object. None until first subtask in a non-off mode.
        self._critic_panel: CriticPanel | None = None

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

        # ── Critic panel (M7, walking-skeleton iteration 1) ────────────────
        # Runs AFTER patches hit the filesystem but BEFORE the commit. In
        # advisory mode it only logs findings; the commit proceeds unchanged.
        # Wrapped in try/except defensively — a critic crash must not kill
        # the worker (the patch is good, the meta-observation is the cherry).
        if self._config.critic_panel.mode != "off":
            try:
                self._run_critic_panel(subtask, touched)
            except Exception as exc:  # noqa: BLE001 — see comment above
                current_trace().exception(
                    "critic_panel.crashed",
                    subtask_id=subtask.id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        # Commit
        sha = self._committer.commit_patches(touched, commit_msg)
        return sha

    def _run_critic_panel(self, subtask: SubTask, touched: list[str]) -> None:
        """Build the diff for the touched files, run the panel, log the result.

        Pure side-effect method: emits trace events, mutates
        ``self._state.critic_panel_runs`` and ``critic_panel_findings_log``,
        does NOT alter the patch or the commit decision. Refinement /
        blocking behaviour lands in iteration 2/3.
        """
        if self._critic_panel is None:
            self._critic_panel = CriticPanel(self._config.critic_panel, self._llm)

        # Diff of files staged on disk vs HEAD — what the committer is about
        # to commit. Scoped to ``touched`` so we never feed the panel
        # unrelated noise (other in-flight worktree edits, IDE-droppings).
        try:
            diff_text = self._git.repo.git.diff("HEAD", "--", *touched)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception(
                "critic_panel.diff_failed",
                subtask_id=subtask.id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        # Lightweight repo context — list of touched paths only for now.
        # Iteration 2 may add `git ls-files` of the touched dirs so personas
        # can spot duplicates / orphans (lesson from PR#10 F002 audit fix).
        repo_context = "Touched files:\n" + "\n".join(f"  - {p}" for p in touched)

        with current_trace().span(
            "critic_panel.review",
            subtask_id=subtask.id,
            mode=self._config.critic_panel.mode,
            personas=self._config.critic_panel.personas,
        ) as fields:
            result: PanelResult = self._critic_panel.review(
                subtask_id=subtask.id,
                diff_text=diff_text,
                repo_files_summary=repo_context,
            )
            fields["verdict"] = result.verdict
            fields["findings_count"] = len(result.findings)
            fields["has_blocker"] = result.has_blocker()
            if result.tokens_extra is not None:
                fields["prompt_tokens"] = result.tokens_extra[0]
                fields["completion_tokens"] = result.tokens_extra[1]

        # Per-finding trace events — easier to grep ``gitoma logs`` for a
        # specific category than to walk the aggregated span.
        for f in result.findings:
            current_trace().emit(
                "critic_panel.finding",
                subtask_id=subtask.id,
                persona=f.persona,
                severity=f.severity,
                category=f.category,
                summary=f.summary,
                file=f.file,
            )

        # Persist into AgentState so the cockpit can render it without
        # parsing trace JSONL. Cap the log so a 60-subtask run doesn't
        # bloat state.json beyond reason.
        self._state.critic_panel_runs += 1
        log = self._state.critic_panel_findings_log
        log.append(result.to_dict())
        if len(log) > _MAX_PANEL_LOG_ENTRIES:
            del log[: len(log) - _MAX_PANEL_LOG_ENTRIES]
        save_state(self._state)

    def _persist_plan(self, plan: TaskPlan) -> None:
        """Update state with current plan and save to disk."""
        self._state.task_plan = plan.to_dict()
        save_state(self._state)
