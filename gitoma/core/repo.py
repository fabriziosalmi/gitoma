"""Git repo abstraction — clone, branch, commit, push."""

from __future__ import annotations

from typing import Any

import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import git
from git import Repo

from gitoma.core.config import Config


def parse_repo_url(url: str) -> tuple[str, str]:
    """Extract (owner, name) from GitHub URL or owner/name shorthand."""
    url = url.rstrip("/").removesuffix(".git")
    if url.startswith("http"):
        parts = urlparse(url).path.strip("/").split("/")
        return parts[0], parts[1]
    if "/" in url:
        owner, name = url.split("/", 1)
        return owner, name
    raise ValueError(f"Cannot parse repo URL: {url}")


class GitRepo:
    """Wraps GitPython for all repo operations needed by the agent."""

    def __init__(self, url: str, config: Config) -> None:
        self.url = url
        self.config = config
        self.owner, self.name = parse_repo_url(url)
        self._tmpdir: str | None = None
        self._repo: Repo | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def clone(self) -> Path:
        """Clone the repo to a temp directory. Returns local root path."""
        self._tmpdir = tempfile.mkdtemp(prefix="gitoma_")
        auth_url = self._authed_url()
        self._repo = Repo.clone_from(auth_url, self._tmpdir)
        return Path(self._tmpdir)

    def cleanup(self) -> None:
        """Remove the cloned temp directory."""
        if self._tmpdir and os.path.exists(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self) -> "GitRepo":
        self.clone()
        return self

    def __exit__(self, *_: object) -> None:
        self.cleanup()

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        if self._tmpdir is None:
            raise RuntimeError("Repo not cloned yet. Call clone() first.")
        return Path(self._tmpdir)

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            raise RuntimeError("Repo not cloned yet. Call clone() first.")
        return self._repo

    # ── Branch management ──────────────────────────────────────────────────

    def create_branch(self, branch_name: str) -> None:
        """Create and checkout a new branch."""
        self.repo.git.checkout("-b", branch_name)

    def checkout_base(self, base: str) -> None:
        """Move the worktree onto ``base`` so a subsequent ``create_branch``
        branches off it.

        ``Repo.clone_from`` checks out the repo's default branch. When
        ``--base X`` targets a different branch, both the worktree AND the
        local ref must be re-pointed at X — otherwise the working branch is
        created off the default and the resulting PR has no common ancestor
        with X (GitHub answers 422). No-op when already on ``base``.
        """
        if self.current_branch() == base:
            return
        origin = self.repo.remotes.origin
        origin.fetch()
        remote_ref = f"origin/{base}"
        if not any(ref.name == remote_ref for ref in origin.refs):
            raise ValueError(
                f"Base branch '{base}' not found on origin. "
                f"Available: {sorted(r.name.removeprefix('origin/') for r in origin.refs if r.name != 'origin/HEAD')}"
            )
        self.repo.git.checkout("-B", base, remote_ref)

    def checkout_existing_branch(self, branch_name: str) -> bool:
        """Check out a branch that may already exist on the remote.

        Used by ``--resume``: when a prior run pushed partial commits to
        ``origin/<branch>``, the resumed run must continue on top of those
        commits instead of branching off the default base. We fetch first
        (in case the local clone is stale), then ``checkout -B`` which
        creates the local branch (or resets it) to the remote tip.

        Returns True when the remote branch existed and was checked out;
        False when no remote branch was found and the caller should fall
        back to ``create_branch``.
        """
        origin = self.repo.remotes.origin
        origin.fetch()
        remote_ref_name = f"origin/{branch_name}"
        if not any(ref.name == remote_ref_name for ref in origin.refs):
            return False
        # ``-B`` is "create or reset"; combined with a remote-tracking
        # source it gives us a fast-forward-equivalent local branch
        # whether or not a stale local copy already exists.
        self.repo.git.checkout("-B", branch_name, remote_ref_name)
        return True

    def current_branch(self) -> str:
        return self.repo.active_branch.name

    def branch_exists_remote(self, branch_name: str) -> bool:
        origin = self.repo.remotes.origin
        origin.fetch()
        return any(ref.name == f"origin/{branch_name}" for ref in origin.refs)

    def sha_reachable(self, sha: str) -> bool:
        """Return True iff ``sha`` is an ancestor of (or equal to) HEAD.

        Used by the ``--resume`` path to validate that a subtask marked
        ``status=completed`` with a ``commit_sha`` actually has its
        commit on the current branch. After a crash + resume where the
        prior run's tempdir was cleaned up before PHASE 4's push, the
        commit only ever existed locally in the old tempdir — on the
        fresh clone + ``checkout_existing_branch`` (which resets to
        ``origin/<branch>``) it's gone. Resume would trust the stale
        ``completed`` flag, the worker would skip the subtask, and the
        final branch would silently miss that work.

        ``git merge-base --is-ancestor <sha> HEAD`` exits 0 when the
        ancestor relationship holds, 1 when it doesn't, and other codes
        for other errors (invalid sha, dangling ref, …). GitPython
        raises ``GitCommandError`` on any non-zero exit, so a bare
        try/except gives us a clean boolean — and a non-existent sha
        is "not reachable", which is the right answer for the caller
        (re-run the subtask).
        """
        if not sha:
            return False
        try:
            self.repo.git.merge_base("--is-ancestor", sha, "HEAD")
            return True
        except git.exc.GitCommandError:
            return False

    # ── File operations ────────────────────────────────────────────────────

    def read_file(self, relative_path: str) -> str | None:
        """Read file content; returns None if not found."""
        path = self.root / relative_path
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
        return None

    def write_file(self, relative_path: str, content: str) -> None:
        """Write (or overwrite) a file inside the repo."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def delete_file(self, relative_path: str) -> bool:
        """Delete a file if it exists."""
        path = self.root / relative_path
        if path.exists():
            path.unlink()
            return True
        return False

    def file_tree(self, max_files: int = 200) -> list[str]:
        """Return list of relative file paths (respects .gitignore via git ls-files)."""
        try:
            result = self.repo.git.ls_files()
            files = [line for line in result.splitlines() if line]
            return files[:max_files]
        except Exception:
            return self._fallback_file_tree(max_files)

    def _fallback_file_tree(self, max_files: int) -> list[str]:
        files: list[str] = []
        for path in Path(self._tmpdir or "").rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                files.append(str(path.relative_to(self.root)))
                if len(files) >= max_files:
                    break
        return files

    def detect_languages(self) -> list[str]:
        """Detect primary languages from file extensions."""
        ext_map: dict[str, str] = {
            ".py": "Python",
            ".go": "Go",
            ".rs": "Rust",
            ".js": "JavaScript",
            ".ts": "TypeScript",
            ".jsx": "JavaScript",
            ".tsx": "TypeScript",
            ".rb": "Ruby",
            ".java": "Java",
            ".cpp": "C++",
            ".c": "C",
        }
        counts: dict[str, int] = {}
        for f in self.file_tree(500):
            ext = Path(f).suffix.lower()
            if ext in ext_map:
                lang = ext_map[ext]
                counts[lang] = counts.get(lang, 0) + 1
        return sorted(counts, key=lambda k: counts[k], reverse=True)

    # ── Git operations ──────────────────────────────────────────────────────

    def stage_all(self) -> None:
        """Stage all changes."""
        self.repo.git.add(A=True)

    def stage_file(self, relative_path: str) -> None:
        self.repo.index.add([relative_path])

    def commit(self, message: str, *, author_name: str, author_email: str) -> str:
        """Commit staged changes. Returns commit SHA."""
        actor = git.Actor(author_name, author_email)
        commit = self.repo.index.commit(
            message,
            author=actor,
            committer=actor,
        )
        return commit.hexsha

    def push(self, branch: str, *, force: bool = False) -> None:
        """Push branch to origin using authenticated URL."""
        origin = self.repo.remotes.origin
        # Ensure the remote uses the authenticated URL
        with origin.config_writer as cw:
            cw.set("url", self._authed_url())
        push_flags = ["--force"] if force else []
        self.repo.git.push("origin", branch, *push_flags)

    def has_staged_changes(self) -> bool:
        return bool(self.repo.index.diff("HEAD"))

    def has_uncommitted_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    def log(self, n: int = 10) -> list[dict[str, Any]]:
        """Return last N commits as dicts."""
        commits = []
        for c in list(self.repo.iter_commits())[:n]:
            commits.append(
                {
                    "sha": c.hexsha[:8],
                    "message": str(c.message).split("\n")[0],
                    "author": c.author.name,
                    "date": c.authored_datetime.isoformat(),
                }
            )
        return commits

    # ── Helpers ────────────────────────────────────────────────────────────

    def _authed_url(self) -> str:
        """Build authenticated HTTPS URL with GitHub token.

        Uses the `x-access-token` convention (same as the GitHub CLI): the
        token authenticates its owner, so embedding a bot username would
        only introduce a way for the two to disagree. Works for classic
        PATs, fine-grained PATs, GitHub App installation tokens, and OAuth
        tokens without special-casing.
        """
        token = self.config.github.token
        return f"https://x-access-token:{token}@github.com/{self.owner}/{self.name}.git"

    def github_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    def branch_url(self, branch: str) -> str:
        return f"https://github.com/{self.owner}/{self.name}/tree/{branch}"
