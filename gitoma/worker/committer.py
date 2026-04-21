"""Committer — stage + commit changes with bot identity.

Industrial-grade pass:

* **Stage only the patcher's touched paths.** The previous implementation
  fell back to ``git add -A`` to "also stage untracked files that match
  touched paths". That call has no path filter — it stages **every**
  untracked file in the working tree, which on a real repo means build
  artifacts (Rust ``target/``, Python ``__pycache__``, JS ``node_modules``)
  and any local junk the developer left lying around end up in the
  agent's commits. Touched paths come from the patcher's safety-checked
  list; that's exactly what we want to commit, no more.
* **Surface staging failures.** A failed ``git add`` used to be silently
  swallowed (``except Exception: pass``) and the committer would then
  return ``None`` — indistinguishable from "nothing to commit". The
  worker would record the subtask as "completed (no changes)" when in
  reality git refused to stage. We now collect every failure into a
  ``CommitterError`` so the worker can mark the subtask failed instead
  of silently misreporting success.
"""

from __future__ import annotations

import logging

from gitoma.core.config import Config
from gitoma.core.repo import GitRepo

logger = logging.getLogger(__name__)


class CommitterError(Exception):
    """Raised when staging or committing fails in a way the caller must handle."""


class Committer:
    """Handles staging and committing on behalf of the bot identity."""

    def __init__(self, git_repo: GitRepo, config: Config) -> None:
        self._git = git_repo
        self._config = config

    def commit_patches(self, touched_paths: list[str], message: str) -> str | None:
        """Stage the given paths and commit with the bot identity.

        Args:
            touched_paths: relative paths the patcher wrote/deleted. Only
                these are staged — never ``git add -A``, which would scoop
                up unrelated untracked files (build artifacts, local junk).
            message: conventional commit message

        Returns:
            commit SHA on success, or ``None`` when there genuinely was
            nothing to commit (e.g. patcher wrote a file identical to the
            existing version, so git diff is empty after staging).

        Raises:
            CommitterError: when ``git add`` failed for any of the touched
                paths. Surfaces what was previously silent so the worker
                can mark the subtask failed instead of "completed".
        """
        if not touched_paths:
            return None

        # Stage exactly the patcher's touched paths — including deletions
        # (``git add`` of a removed path stages the deletion). No ``-A``,
        # so untracked files outside this list stay outside the commit.
        failures: list[tuple[str, str]] = []
        for path in touched_paths:
            try:
                self._git.repo.git.add(path)
            except Exception as e:
                # Real failure (permission, gitignore conflict, …). Don't
                # silently swallow — collect and surface.
                failures.append((path, f"{type(e).__name__}: {str(e)[:160]}"))
                logger.warning(
                    "committer_git_add_failed",
                    extra={"path": path, "error": str(e)[:300]},
                )

        if failures:
            summary = "; ".join(f"{p}: {err}" for p, err in failures[:5])
            raise CommitterError(
                f"git add failed for {len(failures)} of {len(touched_paths)} path(s): {summary}"
            )

        # Nothing actually staged (patcher wrote files identical to HEAD,
        # or staged a no-op delete) — legitimate "no changes" path.
        if not self._git.repo.index.diff("HEAD"):
            return None

        sha = self._git.commit(
            message=message,
            author_name=self._config.bot.name,
            author_email=self._config.bot.email,
        )
        return sha
