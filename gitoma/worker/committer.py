"""Committer — stage + commit changes with bot identity."""

from __future__ import annotations

from gitoma.core.config import Config
from gitoma.core.repo import GitRepo


class Committer:
    """Handles staging and committing on behalf of the bot identity."""

    def __init__(self, git_repo: GitRepo, config: Config) -> None:
        self._git = git_repo
        self._config = config

    def commit_patches(self, touched_paths: list[str], message: str) -> str | None:
        """
        Stage the given paths and commit with the bot identity.

        Args:
            touched_paths: list of relative paths that were modified
            message: conventional commit message

        Returns:
            commit SHA, or None if nothing staged
        """
        if not touched_paths:
            return None

        # Stage touched files
        for path in touched_paths:
            try:
                self._git.repo.git.add(path)
            except Exception:
                pass

        # Check if anything is actually staged
        if not self._git.repo.index.diff("HEAD") and not self._git.repo.untracked_files:
            return None

        # Also stage untracked files that match touched paths
        self._git.repo.git.add(A=True)

        sha = self._git.commit(
            message=message,
            author_name=self._config.bot.name,
            author_email=self._config.bot.email,
        )
        return sha
