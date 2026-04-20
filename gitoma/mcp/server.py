"""Gitoma GitHub MCP Server — embedded, zero-latency, parallelized.

Exposes 6 GitHub tools via FastMCP with:
- Full LRU+TTL in-memory caching (avoids redundant API calls)
- Parallel batch fetching via ThreadPoolExecutor
- Repo-scoped cache invalidation post-push

Usage (standalone, for Claude Desktop / MCP Inspector):
    python -m gitoma.mcp.server

Usage (embedded, in-process):
    from gitoma.mcp.server import get_mcp_server
    server = get_mcp_server(config)
    result = server.call_tool("read_github_file", {"owner": ..., "repo": ..., "path": ...})
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from mcp.server.fastmcp import FastMCP

from gitoma.mcp.cache import GitHubContextCache, get_cache

logger = logging.getLogger(__name__)

# ── Global thread pool for parallel GitHub API calls ─────────────────────────
# max_workers=8: tuned for typical GitHub API concurrency limits (secondary rate limit)
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gitoma-mcp")

# Cache TTLs by data type
_TTL_FILE_CONTENT = 300.0   # 5 min — files change rarely during a run
_TTL_TREE = 180.0           # 3 min — tree might change after commits
_TTL_CI = 60.0              # 1 min — CI status is more volatile
_TTL_ISSUES = 600.0         # 10 min — slow-moving
_TTL_PR = 120.0             # 2 min — PR comments can arrive anytime
_TTL_SEARCH = 300.0         # 5 min


# ── Server factory ─────────────────────────────────────────────────────────────

def build_mcp_server(cache: GitHubContextCache | None = None) -> FastMCP:
    """
    Build and return a FastMCP server with all GitHub tools registered.
    Can be called multiple times with different cache instances (testing).
    """
    _cache = cache or get_cache()
    mcp = FastMCP("Gitoma GitHub MCP", dependencies=["PyGithub", "requests"])

    # ── Tool: list_repo_tree ──────────────────────────────────────────────────

    @mcp.tool()
    def list_repo_tree(owner: str, repo: str, max_files: int = 300) -> str:
        """
        List all files in a GitHub repository as a JSON array, cached (TTL 3min).
        Returns only file paths (no directories). Truncated to max_files.
        """
        cache_key = f"tree:{owner}/{repo}:{max_files}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from gitoma.core.config import load_config
        from gitoma.core.github_client import GitHubClient
        cfg = load_config()
        gh = GitHubClient(cfg)
        r = gh.get_repo(owner, repo)

        try:
            tree = r.get_git_tree(r.default_branch, recursive=True)
            paths = [
                item.path for item in tree.tree
                if item.type == "blob"
            ][:max_files]
        except Exception:
            # Fallback: use PyGithub contents
            paths = _walk_tree_fallback(r, max_files)

        result = json.dumps(paths)
        _cache.set(cache_key, result, ttl=_TTL_TREE)
        return result

    # ── Tool: read_github_file ─────────────────────────────────────────────────

    @mcp.tool()
    def read_github_file(owner: str, repo: str, path: str, ref: str = "") -> str:
        """
        Read a single file from GitHub, cached (TTL 5min).
        Returns file content as UTF-8 string. Returns empty string if not found.
        """
        cache_key = f"file:{owner}/{repo}:{ref or 'HEAD'}:{path}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from gitoma.core.config import load_config
        from gitoma.core.github_client import GitHubClient
        cfg = load_config()
        gh = GitHubClient(cfg)
        r = gh.get_repo(owner, repo)

        try:
            kwargs: dict[str, Any] = {}
            if ref:
                kwargs["ref"] = ref
            content_file = r.get_contents(path, **kwargs)
            if isinstance(content_file, list):
                content_file = content_file[0]
            result = content_file.decoded_content.decode("utf-8", errors="replace")
        except Exception as e:
            logger.debug("read_github_file(%s/%s, %s): %s", owner, repo, path, e)
            result = ""

        _cache.set(cache_key, result, ttl=_TTL_FILE_CONTENT)
        return result

    # ── Tool: read_github_files_batch ──────────────────────────────────────────

    @mcp.tool()
    def read_github_files_batch(owner: str, repo: str, paths: list[str]) -> str:
        """
        Read multiple files from GitHub IN PARALLEL (ThreadPoolExecutor), all cached.
        Returns JSON dict {path: content}. Missing files map to empty string.
        Dramatically faster than sequential reads for 3+ files.
        """
        results: dict[str, str] = {}
        to_fetch: list[str] = []

        # Check cache first (zero I/O for hits)
        for path in paths:
            cache_key = f"file:{owner}/{repo}:HEAD:{path}"
            hit = _cache.get(cache_key)
            if hit is not None:
                results[path] = hit
            else:
                to_fetch.append(path)

        if to_fetch:
            from gitoma.core.config import load_config
            from gitoma.core.github_client import GitHubClient
            cfg = load_config()
            gh = GitHubClient(cfg)
            r = gh.get_repo(owner, repo)

            def _fetch_one(path: str) -> tuple[str, str]:
                try:
                    cf = r.get_contents(path)
                    if isinstance(cf, list):
                        cf = cf[0]
                    content = cf.decoded_content.decode("utf-8", errors="replace")
                except Exception:
                    content = ""
                cache_key = f"file:{owner}/{repo}:HEAD:{path}"
                _cache.set(cache_key, content, ttl=_TTL_FILE_CONTENT)
                return path, content

            # Submit all fetches in parallel
            futures = {_EXECUTOR.submit(_fetch_one, p): p for p in to_fetch}
            for future in as_completed(futures):
                path, content = future.result()
                results[path] = content

        return json.dumps(results)

    # ── Tool: get_ci_failures ─────────────────────────────────────────────────

    @mcp.tool()
    def get_ci_failures(owner: str, repo: str, branch: str) -> str:
        """
        Return failed GitHub Actions jobs for a branch as JSON, cached (TTL 1min).
        Each entry: {run_id, job_id, name, url, conclusion}.
        """
        cache_key = f"ci:{owner}/{repo}:{branch}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from gitoma.core.config import load_config
        from gitoma.core.github_client import GitHubClient
        cfg = load_config()
        gh = GitHubClient(cfg)
        failures = gh.get_failed_jobs(owner, repo, branch)
        result = json.dumps(failures)
        _cache.set(cache_key, result, ttl=_TTL_CI)
        return result

    # ── Tool: get_open_issues ─────────────────────────────────────────────────

    @mcp.tool()
    def get_open_issues(owner: str, repo: str, limit: int = 20) -> str:
        """
        Return open GitHub issues as JSON, cached (TTL 10min).
        Each entry: {number, title, body, labels, created_at, url}.
        """
        cache_key = f"issues:{owner}/{repo}:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from gitoma.core.config import load_config
        from gitoma.core.github_client import GitHubClient
        cfg = load_config()
        gh = GitHubClient(cfg)
        r = gh.get_repo(owner, repo)

        issues: list[dict[str, Any]] = []
        open_issues = r.get_issues(state="open")
        for issue in list(open_issues)[:limit]:
            issues.append({
                "number": issue.number,
                "title": issue.title,
                "body": (issue.body or "")[:500],
                "labels": [lbl.name for lbl in issue.labels],
                "created_at": issue.created_at.isoformat(),
                "url": issue.html_url,
            })

        result = json.dumps(issues)
        _cache.set(cache_key, result, ttl=_TTL_ISSUES)
        return result

    # ── Tool: get_pr_comments ─────────────────────────────────────────────────

    @mcp.tool()
    def get_pr_comments(owner: str, repo: str, pr_number: int) -> str:
        """
        Return all review + issue comments on a PR as JSON, cached (TTL 2min).
        Each entry: {id, author, body, path, line, url}.
        """
        cache_key = f"pr_comments:{owner}/{repo}:{pr_number}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from gitoma.core.config import load_config
        from gitoma.core.github_client import GitHubClient
        from dataclasses import asdict
        cfg = load_config()
        gh = GitHubClient(cfg)
        comments = gh.get_all_pr_comments(owner, repo, pr_number)
        result = json.dumps([asdict(c) for c in comments])
        _cache.set(cache_key, result, ttl=_TTL_PR)
        return result

    # ── Tool: invalidate_repo_cache ───────────────────────────────────────────

    @mcp.tool()
    def invalidate_repo_cache(owner: str, repo: str) -> str:
        """
        Bust all cache entries for a given repo (call after push/commit).
        Returns JSON with count of invalidated entries.
        """
        prefix = f"file:{owner}/{repo}"
        n1 = _cache.invalidate_prefix(prefix)
        prefix2 = f"tree:{owner}/{repo}"
        n2 = _cache.invalidate_prefix(prefix2)
        prefix3 = f"ci:{owner}/{repo}"
        n3 = _cache.invalidate_prefix(prefix3)
        total = n1 + n2 + n3
        return json.dumps({"invalidated": total, "repo": f"{owner}/{repo}"})

    return mcp


# ── Fallback tree walker ───────────────────────────────────────────────────────

def _walk_tree_fallback(repo: Any, max_files: int) -> list[str]:
    """Fallback file tree via PyGithub get_contents (slower, for private repos)."""
    paths: list[str] = []
    queue = [""]
    while queue and len(paths) < max_files:
        current = queue.pop()
        try:
            contents = repo.get_contents(current or "/")
            if not isinstance(contents, list):
                contents = [contents]
            for item in contents:
                if item.type == "dir":
                    queue.append(item.path)
                else:
                    paths.append(item.path)
        except Exception:
            continue
    return paths[:max_files]


# ── Singleton server instance ─────────────────────────────────────────────────

_server: FastMCP | None = None


def get_mcp_server() -> FastMCP:
    """Return the module-level singleton MCP server (lazy initialize)."""
    global _server
    if _server is None:
        _server = build_mcp_server()
    return _server


# ── Standalone entry point (for Claude Desktop / mcp dev) ─────────────────────

if __name__ == "__main__":
    import sys
    server = get_mcp_server()
    print("🔗 Gitoma GitHub MCP Server starting on stdio...", file=sys.stderr)
    server.run()
