"""ReviewIntegrator — generates and commits fixes for Copilot review comments."""

from __future__ import annotations

from gitoma.core.config import Config
from gitoma.core.github_client import ReviewComment
from gitoma.core.repo import GitRepo
from gitoma.core.state import AgentState
from typing import Any, Callable
from gitoma.planner.llm_client import LLMClient
from gitoma.planner.prompts import (
    review_integrator_system_prompt,
    review_integrator_user_prompt,
)
from gitoma.worker.committer import Committer
from gitoma.worker.patcher import apply_patches


class ReviewIntegrator:
    """Addresses Copilot/reviewer comments by generating patches and committing fixes."""

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

    def integrate(
        self,
        comments: list[ReviewComment],
        on_comment_start: "Callable[[ReviewComment], None] | None" = None,
        on_comment_done: "Callable[[ReviewComment, str | None], None] | None" = None,
        on_comment_error: "Callable[[ReviewComment, str], None] | None" = None,
    ) -> list[dict[str, Any]]:
        """
        Generate and commit a fix for each review comment.

        Returns list of results: [{comment_id, sha, error}]
        """
        results: list[dict[str, Any]] = []

        for comment in comments:
            if on_comment_start:
                on_comment_start(comment)

            try:
                sha = self._fix_comment(comment)
                results.append({"comment_id": comment.id, "sha": sha, "error": None})
                if on_comment_done:
                    on_comment_done(comment, sha)
            except Exception as e:
                error = str(e)[:200]
                results.append({"comment_id": comment.id, "sha": None, "error": error})
                if on_comment_error:
                    on_comment_error(comment, error)

        return results

    def _fix_comment(self, comment: ReviewComment) -> str | None:
        """Generate a patch for one review comment and commit it."""
        # Read file content if comment is on a specific file
        file_content: str | None = None
        if comment.path:
            file_content = self._git.read_file(comment.path)

        messages = [
            {"role": "system", "content": review_integrator_system_prompt()},
            {
                "role": "user",
                "content": review_integrator_user_prompt(
                    comment_body=comment.body,
                    file_path=comment.path,
                    file_content=file_content,
                    line=comment.line,
                ),
            },
        ]

        raw = self._llm.chat_json(messages)
        patches = raw.get("patches", [])
        commit_msg = raw.get(
            "commit_message",
            f"fix: address review comment #{comment.id} [gitoma]",
        )

        if not patches:
            raise ValueError("LLM returned no patches for review comment")

        if "[gitoma]" not in commit_msg:
            commit_msg += " [gitoma]"

        touched = apply_patches(self._git.root, patches)
        if not touched:
            raise ValueError("Review fix patches produced no file changes")

        sha = self._committer.commit_patches(touched, commit_msg)
        return sha
