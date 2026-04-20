"""Sandbox module — handles scaffolding temporary flawed repositories for testing."""

from __future__ import annotations

from pathlib import Path

from gitoma.core.config import Config
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo


def setup_sandbox(config: Config) -> str:
    """
    Scaffold the 'gitoma-sandbox' repository on GitHub with flawed code.
    Returns the URL of the created repository.
    """
    gh = GitHubClient(config)
    owner = config.bot.github_user
    repo_name = "gitoma-sandbox"

    # 1. Clear any existing sandbox
    gh.delete_user_repo(owner, repo_name)

    # 2. Create fresh repo
    gh.create_user_repo(
        name=repo_name,
        description="Temporary repository for testing Gitoma end-to-end automation. Can be safely deleted.",
        private=True,
    )

    repo_url = f"https://github.com/{owner}/{repo_name}"

    # 3. Clone and poison with flaws
    git_repo = GitRepo(repo_url, config)
    with git_repo:
        _write_flawed_codebase(git_repo.root)
        
        # GitPython push
        git_repo.stage_all()
        git_repo.commit(
            "Initial commit: boilerplate with intentional flaws",
            author_name=config.bot.name,
            author_email=config.bot.email,
        )
        git_repo.push("main", force=True)

    return repo_url


def teardown_sandbox(config: Config) -> None:
    """Delete the 'gitoma-sandbox' repository from GitHub."""
    gh = GitHubClient(config)
    owner = config.bot.github_user
    repo_name = "gitoma-sandbox"
    gh.delete_user_repo(owner, repo_name)


def _write_flawed_codebase(root: Path) -> None:
    """Write some intentionally terrible code to trigger Gitoma analyzers."""
    
    # 1. Missing docs, missing types, basic flawed python
    app_py = root / "app.py"
    app_py.write_text(
        "def calculate(a,b):\n"
        "    return a+b\n\n"
        "def subtract(a,b):\n"
        "    return a-b\n\n"
        "API_KEY = 'sk-live-1234567890abcdef' # intentional leak\n"
        "print(calculate(10, 5))\n"
    )

    # 2. Terribly empty README
    readme = root / "README.md"
    readme.write_text("# gitoma-sandbox\n\njust an app.\n")

    # 3. Empty requirements without fixed versions
    reqs = root / "requirements.txt"
    reqs.write_text("requests\nflask\n")
