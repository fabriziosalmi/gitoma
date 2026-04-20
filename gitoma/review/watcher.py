"""CopilotWatcher — polls a PR for Copilot/reviewer comments."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from gitoma.core.github_client import GitHubClient, ReviewComment


@dataclass
class ReviewStatus:
    pr_number: int
    pr_url: str
    review_comments: list[ReviewComment]
    issue_comments: list[ReviewComment]
    reviews: list[dict]

    @property
    def all_comments(self) -> list[ReviewComment]:
        return self.review_comments + self.issue_comments

    @property
    def copilot_comments(self) -> list[ReviewComment]:
        """Filter comments from GitHub Copilot."""
        return [
            c for c in self.all_comments
            if "copilot" in c.author.lower() or "github-advanced-security" in c.author.lower()
        ]

    @property
    def total_comments(self) -> int:
        return len(self.all_comments)


class CopilotWatcher:
    """Fetches and monitors PR review state."""

    def __init__(self, gh_client: GitHubClient, owner: str, repo_name: str) -> None:
        self._gh = gh_client
        self._owner = owner
        self._repo = repo_name

    def fetch(self, pr_number: int, pr_url: str) -> ReviewStatus:
        """Fetch current PR review status."""
        review_comments = self._gh.get_pr_review_comments(self._owner, self._repo, pr_number)
        issue_comments = self._gh.get_pr_issue_comments(self._owner, self._repo, pr_number)
        reviews = self._gh.get_pr_reviews(self._owner, self._repo, pr_number)

        return ReviewStatus(
            pr_number=pr_number,
            pr_url=pr_url,
            review_comments=review_comments,
            issue_comments=issue_comments,
            reviews=reviews,
        )

    def wait_for_review(
        self,
        pr_number: int,
        pr_url: str,
        poll_interval: int = 30,
        max_wait: int = 600,
        on_update: "Callable[[ReviewStatus], None] | None" = None,
    ) -> ReviewStatus:
        """
        Poll until at least one review comment arrives or max_wait is reached.

        Args:
            poll_interval: seconds between polls
            max_wait: total seconds to wait
            on_update: callback invoked on each poll

        Returns:
            Final ReviewStatus
        """
        elapsed = 0
        last_count = 0

        while elapsed < max_wait:
            status = self.fetch(pr_number, pr_url)
            if on_update:
                on_update(status)

            if status.total_comments > last_count:
                last_count = status.total_comments

            if status.total_comments > 0:
                return status

            time.sleep(poll_interval)
            elapsed += poll_interval

        return self.fetch(pr_number, pr_url)
