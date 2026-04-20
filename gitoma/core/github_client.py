"""GitHub API wrapper using PyGithub."""

from __future__ import annotations

from dataclasses import dataclass

from github import Github, GithubException, Auth
from github.PullRequest import PullRequest
from github.Repository import Repository

from gitoma.core.config import Config


@dataclass
class PRInfo:
    number: int
    url: str
    title: str
    state: str
    branch: str


@dataclass
class ReviewComment:
    id: int
    body: str
    path: str | None
    line: int | None
    author: str
    created_at: str
    url: str


class GitHubClient:
    """Thin wrapper around PyGithub for Gitoma's operations."""

    def __init__(self, config: Config) -> None:
        self._config = config
        auth = Auth.Token(config.github.token)
        self._gh = Github(auth=auth)

    def get_repo(self, owner: str, name: str) -> Repository:
        return self._gh.get_repo(f"{owner}/{name}")

    def create_user_repo(self, name: str, description: str = "", private: bool = True) -> Repository:
        """Create a new repository under the authenticated user's account."""
        user = self._gh.get_user()
        return user.create_repo(name=name, description=description, private=private, auto_init=True)

    def delete_user_repo(self, owner: str, name: str) -> None:
        """Delete a repository. Requires delete_repo scope on the token."""
        try:
            repo = self.get_repo(owner, name)
            repo.delete()
        except GithubException as e:
            if e.status == 404:
                return  # already gone
            raise

    # ── Repo info ───────────────────────────────────────────────────────────

    def repo_info(self, owner: str, name: str) -> dict:
        """Return basic metadata dict for a repo."""
        r = self.get_repo(owner, name)
        return {
            "full_name": r.full_name,
            "description": r.description or "",
            "default_branch": r.default_branch,
            "language": r.language or "Unknown",
            "stars": r.stargazers_count,
            "forks": r.forks_count,
            "open_issues": r.open_issues_count,
            "topics": r.get_topics(),
            "private": r.private,
            "url": r.html_url,
        }

    # ── Branches ───────────────────────────────────────────────────────────

    def list_branches(self, owner: str, name: str) -> list[str]:
        r = self.get_repo(owner, name)
        return [b.name for b in r.get_branches()]

    def gitoma_branches(self, owner: str, name: str) -> list[str]:
        """Return only gitoma/* branches."""
        return [b for b in self.list_branches(owner, name) if b.startswith("gitoma/")]

    def branch_exists(self, owner: str, name: str, branch: str) -> bool:
        return branch in self.list_branches(owner, name)

    # ── Pull Requests ──────────────────────────────────────────────────────

    def create_pr(
        self,
        owner: str,
        name: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PRInfo:
        r = self.get_repo(owner, name)
        pr = r.create_pull(title=title, body=body, head=head, base=base, draft=draft)
        return PRInfo(number=pr.number, url=pr.html_url, title=pr.title, state=pr.state, branch=head)

    def get_open_pr_for_branch(self, owner: str, name: str, branch: str) -> PRInfo | None:
        r = self.get_repo(owner, name)
        prs = list(r.get_pulls(state="open", head=f"{owner}:{branch}"))
        if not prs:
            return None
        pr = prs[0]
        return PRInfo(number=pr.number, url=pr.html_url, title=pr.title, state=pr.state, branch=branch)

    def get_pr(self, owner: str, name: str, pr_number: int) -> PullRequest:
        return self.get_repo(owner, name).get_pull(pr_number)

    # ── Reviews & Comments ─────────────────────────────────────────────────

    def get_pr_review_comments(self, owner: str, name: str, pr_number: int) -> list[ReviewComment]:
        """Return all inline review comments on a PR."""
        pr = self.get_pr(owner, name, pr_number)
        comments: list[ReviewComment] = []
        for c in pr.get_review_comments():
            comments.append(
                ReviewComment(
                    id=c.id,
                    body=c.body,
                    path=c.path,
                    line=c.original_line,
                    author=c.user.login,
                    created_at=c.created_at.isoformat(),
                    url=c.html_url,
                )
            )
        return comments

    def get_pr_issue_comments(self, owner: str, name: str, pr_number: int) -> list[ReviewComment]:
        """Return all general (issue) comments on a PR."""
        pr = self.get_pr(owner, name, pr_number)
        comments: list[ReviewComment] = []
        for c in pr.get_issue_comments():
            comments.append(
                ReviewComment(
                    id=c.id,
                    body=c.body,
                    path=None,
                    line=None,
                    author=c.user.login,
                    created_at=c.created_at.isoformat(),
                    url=c.html_url,
                )
            )
        return comments

    def get_all_pr_comments(self, owner: str, name: str, pr_number: int) -> list[ReviewComment]:
        return self.get_pr_review_comments(owner, name, pr_number) + self.get_pr_issue_comments(
            owner, name, pr_number
        )

    def get_pr_reviews(self, owner: str, name: str, pr_number: int) -> list[dict]:
        """Return PR review summaries."""
        pr = self.get_pr(owner, name, pr_number)
        return [
            {
                "id": r.id,
                "user": r.user.login,
                "state": r.state,
                "body": r.body,
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else "",
            }
            for r in pr.get_reviews()
        ]

    # ── CI/CD Checks ───────────────────────────────────────────────────────

    def get_failed_jobs(self, owner: str, name: str, branch: str) -> list[dict]:
        """Return a list of failed GitHub Action jobs for a given branch."""
        r = self.get_repo(owner, name)
        failed = []
        for run in r.get_workflow_runs(branch=branch):
            if run.status == "completed" and run.conclusion == "failure":
                for job in run.jobs():
                    if job.conclusion == "failure":
                        failed.append({
                            "run_id": run.id,
                            "job_id": job.id,
                            "name": job.name,
                            "url": job.html_url
                        })
        return failed

    def get_job_log(self, owner: str, name: str, job_id: int) -> str:
        """Fetch the raw text log of a specific job, returning the last 5000 chars."""
        import requests
        url = f"https://api.github.com/repos/{owner}/{name}/actions/jobs/{job_id}/logs"
        resp = requests.get(
            url, 
            headers={"Authorization": f"Bearer {self._config.github.token}", "Accept": "application/vnd.github.v3+json"},
            allow_redirects=True
        )
        if resp.status_code == 200:
            return resp.text[-5000:]
        return f"Could not fetch logs (HTTP {resp.status_code}). Please check permissions."

    # ── Labels ─────────────────────────────────────────────────────────────

    def add_pr_labels(self, owner: str, name: str, pr_number: int, labels: list[str]) -> None:
        r = self.get_repo(owner, name)
        pr = r.get_pull(pr_number)
        # Ensure labels exist
        existing = {lbl.name for lbl in r.get_labels()}
        colors = {"gitoma": "C084FC", "ai-improved": "67E8F9", "automated": "F472B6"}
        for label in labels:
            if label not in existing:
                r.create_label(name=label, color=colors.get(label, "6B7280"))
        pr.add_to_labels(*labels)
