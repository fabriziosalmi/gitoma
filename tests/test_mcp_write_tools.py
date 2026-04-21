"""Unit tests for the MCP write toolbox.

Each tool is exercised against a mock PyGithub Repo object: we're not
testing GitHub — we're testing the contract between the tool surface
and the MCP's cache/error model (structured {error, type, ...context}
shape, cache invalidation after writes, symmetric read cache on
list_prs).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from gitoma.mcp.cache import GitHubContextCache
from gitoma.mcp.server import build_mcp_server


@pytest.fixture
def mcp_with_isolated_cache(monkeypatch):
    """Spin up a fresh MCP server with its own cache so tests don't leak."""
    cache = GitHubContextCache(max_entries=32, default_ttl=60.0)
    server = build_mcp_server(cache=cache)
    # FastMCP stashes registered tool callables on its registry — pluck them
    # out so tests can call them directly without round-tripping the transport.
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    return cache, tools


def _call(tools, name, **kwargs):
    """Invoke a registered MCP tool synchronously."""
    tool = tools[name]
    # The @mcp.tool() decorator wraps the original function in a metadata
    # record; the callable lives at `.fn` in FastMCP ≥1.x.
    fn = getattr(tool, "fn", None) or tool
    return fn(**kwargs)


def _mock_github_client(mocker, repo_stub):
    """Patch `_gh_client()` inside the server module to yield a canned repo."""
    client_stub = MagicMock()
    client_stub.get_repo.return_value = repo_stub
    mocker.patch("gitoma.mcp.server.load_config", return_value=MagicMock())
    mocker.patch("gitoma.mcp.server.GitHubClient", return_value=client_stub)
    return client_stub


# ── Error envelope shape ─────────────────────────────────────────────────────


def test_every_write_tool_returns_structured_error_envelope(mcp_with_isolated_cache, mocker):
    """When PyGithub raises, the tool must not propagate — it returns JSON
    with {error, type, ...context}. LLMs can parse and retry intelligently."""
    _, tools = mcp_with_isolated_cache
    repo = MagicMock()
    repo.get_branch.side_effect = RuntimeError("boom")
    _mock_github_client(mocker, repo)

    raw = _call(tools, "create_branch", owner="o", repo="r", branch="feat")
    payload = json.loads(raw)
    assert payload["error"] == "boom"
    assert payload["type"] == "RuntimeError"
    assert payload["branch"] == "feat"


# ── list_prs: read path + cache symmetry ─────────────────────────────────────


def test_list_prs_caches_and_serves_from_cache(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache

    pr = MagicMock()
    pr.number = 7
    pr.title = "Fix thing"
    pr.state = "open"
    pr.user.login = "fabgpt-coder"
    pr.html_url = "https://github.com/o/r/pull/7"
    pr.base.ref = "main"
    pr.head.ref = "gitoma/improve-x"
    pr.draft = False
    pr.created_at.isoformat.return_value = "2026-04-21T00:00:00+00:00"

    repo = MagicMock()
    repo.get_pulls.return_value = [pr]
    _mock_github_client(mocker, repo)

    r1 = _call(tools, "list_prs", owner="o", repo="r")
    r2 = _call(tools, "list_prs", owner="o", repo="r")
    assert r1 == r2
    assert json.loads(r1)[0]["number"] == 7
    # Second call is a cache hit — get_pulls only invoked once.
    assert repo.get_pulls.call_count == 1


# ── create_branch: calls PyGithub + busts tree cache ────────────────────────


def test_create_branch_invokes_github_and_invalidates_tree_cache(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    # Pre-populate tree cache so we can assert it gets busted.
    cache.set("tree:o/r:300", "[\"old\"]", ttl=60.0)

    head = MagicMock()
    head.commit.sha = "abc123def"
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value = head
    created_ref = MagicMock()
    created_ref.ref = "refs/heads/feat"
    repo.create_git_ref.return_value = created_ref
    _mock_github_client(mocker, repo)

    out = json.loads(_call(tools, "create_branch", owner="o", repo="r", branch="feat"))
    assert out == {"ref": "refs/heads/feat", "sha": "abc123def", "branch": "feat"}
    repo.create_git_ref.assert_called_once_with(ref="refs/heads/feat", sha="abc123def")
    # Tree cache was invalidated.
    assert cache.get("tree:o/r:300") is None


def test_create_branch_defaults_to_default_branch_when_from_ref_empty(mcp_with_isolated_cache, mocker):
    _, tools = mcp_with_isolated_cache
    repo = MagicMock()
    repo.default_branch = "develop"
    repo.get_branch.return_value.commit.sha = "x"
    repo.create_git_ref.return_value.ref = "refs/heads/b"
    _mock_github_client(mocker, repo)

    _call(tools, "create_branch", owner="o", repo="r", branch="b")
    repo.get_branch.assert_called_once_with("develop")


# ── commit_file: busts file + tree caches ───────────────────────────────────


def test_commit_file_creates_new_file_and_busts_caches(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    cache.set("file:o/r:HEAD:x.py", "stale", ttl=60.0)
    cache.set("tree:o/r:300", "stale", ttl=60.0)

    repo = MagicMock()
    commit_obj = MagicMock()
    commit_obj.sha = "commit_sha_xyz"
    content_obj = MagicMock()
    content_obj.sha = "content_sha_abc"
    content_obj.html_url = "https://github.com/o/r/blob/main/x.py"
    repo.create_file.return_value = {"commit": commit_obj, "content": content_obj}
    _mock_github_client(mocker, repo)

    out = json.loads(_call(
        tools, "commit_file",
        owner="o", repo="r", path="x.py",
        content="print('hi')", message="initial x.py", branch="feat",
    ))
    assert out["commit_sha"] == "commit_sha_xyz"
    assert cache.get("file:o/r:HEAD:x.py") is None
    assert cache.get("tree:o/r:300") is None


def test_commit_file_falls_back_to_update_when_file_already_exists(mcp_with_isolated_cache, mocker):
    _, tools = mcp_with_isolated_cache

    repo = MagicMock()
    # First call (create) raises with an SHA hint → triggers update path.
    repo.create_file.side_effect = Exception("422 sha required")
    existing = MagicMock()
    existing.sha = "existing_sha"
    repo.get_contents.return_value = existing
    commit_obj = MagicMock()
    commit_obj.sha = "new_commit"
    content_obj = MagicMock()
    content_obj.sha = "new_content"
    content_obj.html_url = "u"
    repo.update_file.return_value = {"commit": commit_obj, "content": content_obj}
    _mock_github_client(mocker, repo)

    out = json.loads(_call(
        tools, "commit_file",
        owner="o", repo="r", path="x.py",
        content="new", message="update", branch="feat",
    ))
    assert out["commit_sha"] == "new_commit"
    repo.update_file.assert_called_once()


# ── open_pr / close_pr: bust the prs: cache ─────────────────────────────────


def test_open_pr_returns_number_and_busts_prs_cache(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    cache.set("prs:o/r:open:20", "stale", ttl=60.0)

    repo = MagicMock()
    pr = MagicMock()
    pr.number = 42
    pr.html_url = "https://github.com/o/r/pull/42"
    pr.state = "open"
    pr.draft = False
    repo.create_pull.return_value = pr
    _mock_github_client(mocker, repo)

    out = json.loads(_call(
        tools, "open_pr",
        owner="o", repo="r", title="t", body="b", head="feat", base="main",
    ))
    assert out["number"] == 42
    assert out["url"].endswith("/42")
    assert cache.get("prs:o/r:open:20") is None


def test_close_pr_edits_state_and_busts_prs_cache(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    cache.set("prs:o/r:open:20", "stale", ttl=60.0)

    repo = MagicMock()
    pr = MagicMock()
    repo.get_pull.return_value = pr
    _mock_github_client(mocker, repo)

    out = json.loads(_call(tools, "close_pr", owner="o", repo="r", pr_number=42))
    assert out["ok"] is True
    pr.edit.assert_called_once_with(state="closed")
    assert cache.get("prs:o/r:open:20") is None


# ── comments / labels ───────────────────────────────────────────────────────


def test_add_pr_comment_busts_comments_cache(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    cache.set("pr_comments:o/r:42", "stale", ttl=60.0)

    comment = MagicMock()
    comment.id = 123
    comment.html_url = "https://github.com/o/r/pull/42#issuecomment-123"
    issue = MagicMock()
    issue.create_comment.return_value = comment
    repo = MagicMock()
    repo.get_issue.return_value = issue
    _mock_github_client(mocker, repo)

    out = json.loads(_call(tools, "add_pr_comment", owner="o", repo="r", pr_number=42, body="hi"))
    assert out["id"] == 123
    assert cache.get("pr_comments:o/r:42") is None


def test_add_pr_labels_collects_final_label_set(mcp_with_isolated_cache, mocker):
    _, tools = mcp_with_isolated_cache

    bug = MagicMock()
    bug.name = "bug"
    good = MagicMock()
    good.name = "good-first-issue"
    issue = MagicMock()
    issue.labels = [bug, good]
    repo = MagicMock()
    repo.get_issue.return_value = issue
    _mock_github_client(mocker, repo)

    out = json.loads(_call(
        tools, "add_pr_labels",
        owner="o", repo="r", pr_number=42, labels=["bug", "good-first-issue"],
    ))
    assert out["labels"] == ["bug", "good-first-issue"]
    assert issue.add_to_labels.call_count == 2


# ── invalidate_repo_cache: new prefixes are covered ─────────────────────────


def test_invalidate_repo_cache_clears_every_prefix(mcp_with_isolated_cache, mocker):
    cache, tools = mcp_with_isolated_cache
    for key in [
        "file:o/r:HEAD:x", "tree:o/r:300", "ci:o/r:main",
        "issues:o/r:20", "pr_comments:o/r:1", "prs:o/r:open:20",
    ]:
        cache.set(key, "x", ttl=60.0)

    out = json.loads(_call(tools, "invalidate_repo_cache", owner="o", repo="r"))
    assert out["invalidated"] == 6
    # Confirm every one is gone.
    for key in [
        "file:o/r:HEAD:x", "tree:o/r:300", "ci:o/r:main",
        "issues:o/r:20", "pr_comments:o/r:1", "prs:o/r:open:20",
    ]:
        assert cache.get(key) is None
