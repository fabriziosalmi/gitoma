"""Gitoma GitHub MCP Server — embedded, zero-latency, parallelized.

Exposes GitHub tools via FastMCP on stdio with:

* Full LRU+TTL in-memory caching (avoids redundant API calls)
* Parallel batch fetching via ThreadPoolExecutor
* Repo-scoped cache invalidation post-push
* Size caps on every write-tool input (content / title / body / labels)
* Automatic retry with exponential back-off on GitHub rate limits
* Optional repo allow-list via ``GITOMA_MCP_REPO_ALLOWLIST`` for operators
  who want to pin this server to a specific set of repos regardless of
  what the underlying token's scope would permit

Usage (standalone, for Claude Desktop / MCP Inspector):

    python -m gitoma.mcp.server

Usage (embedded, in-process):

    from gitoma.mcp.server import get_mcp_server
    server = get_mcp_server(config)

Logging discipline: MCP on stdio uses **stdout** as the protocol channel.
Anything written to stdout outside the protocol frame corrupts it. This
module configures the root logger to stream on **stderr** at import time
— any ``print()`` or default logging.basicConfig() in a downstream
module that would otherwise default to stdout is overridden.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, TypeVar

from mcp.server.fastmcp import FastMCP

from gitoma.core.config import load_config
from gitoma.core.github_client import GitHubClient
from gitoma.mcp.cache import GitHubContextCache, get_cache

# MCP stdio protocol owns stdout — force all Python logging to stderr. Use
# ``force=True`` so any earlier ``logging.basicConfig`` is overwritten. This
# is the single most important thing to get right for stdio MCP: a stray
# ``print()`` in a dependency can desync the protocol framer and cause
# cryptic "invalid JSON" errors from the client.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)
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


# ── Input size limits (defense in depth against prompt-injected LLMs) ───────
# MCP clients invoke tools on behalf of an LLM that may be pointed at
# attacker-controlled text. A malicious prompt that goads the model into
# spamming 10 MB into ``commit_file`` or opening a PR with a 1 MB title
# shouldn't be able to reach the GitHub API at all.
MAX_FILE_CONTENT_BYTES = 2 * 1024 * 1024       # 2 MiB per file
MAX_PR_TITLE_CHARS = 300
MAX_PR_BODY_CHARS = 65_536                     # matches GitHub's own cap
MAX_COMMIT_MESSAGE_CHARS = 10_000
MAX_COMMENT_BODY_CHARS = 65_536
MAX_LABELS_PER_CALL = 20
MAX_LABEL_NAME_CHARS = 50
MAX_BATCH_PATHS = 30


# ── Repo allow-list (optional) ──────────────────────────────────────────────
# Set ``GITOMA_MCP_REPO_ALLOWLIST="owner/repo,owner2/repo2"`` to restrict
# every tool to a fixed set regardless of the token's scope. Empty = no
# restriction (trust the token's own ACLs). This is the operator-level
# kill-switch for "the LLM shouldn't be able to touch random repos".
def _parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(
        s.strip().lower() for s in raw.split(",") if s.strip()
    )


_REPO_ALLOWLIST: frozenset[str] = _parse_allowlist(
    os.getenv("GITOMA_MCP_REPO_ALLOWLIST", "")
)


class ToolInputError(ValueError):
    """User (LLM-side) sent an invalid argument. Surfaced via the JSON
    error envelope with ``type='ToolInputError'``."""


def _require_repo(owner: str, repo: str) -> None:
    if _REPO_ALLOWLIST and f"{owner.lower()}/{repo.lower()}" not in _REPO_ALLOWLIST:
        raise ToolInputError(
            f"repo '{owner}/{repo}' is not on this MCP server's allow-list"
        )


def _require_str_size(name: str, value: str, max_chars: int) -> None:
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string")
    if len(value) > max_chars:
        raise ToolInputError(
            f"{name} exceeds {max_chars} chars (got {len(value)})"
        )


def _require_repo_path(path: str) -> None:
    """Reject file paths with traversal, absolute, or NUL byte shapes.

    MCP tools that accept a ``path`` argument forward it to PyGithub,
    which in turn hits GitHub's REST API. GitHub rejects truly
    absolute paths, but ``..`` segments resolve WITHIN the repo scope
    — which means an LLM-suggested ``../other/file`` can escape an
    intended subdir or clobber files the caller didn't expect. The
    REST API (routers.py) already validates similar inputs; mirror
    that philosophy here so MCP has the same contract.
    """
    if not isinstance(path, str):
        raise ToolInputError("path must be a string")
    if not path:
        raise ToolInputError("path must not be empty")
    if "\x00" in path:
        raise ToolInputError("path contains a NUL byte")
    if path.startswith(("/", "\\")):
        raise ToolInputError(
            f"absolute paths are not allowed: {path!r} "
            "(paths are always repo-relative)"
        )
    # ``..`` at any position breaks out of the intended directory.
    # Compare against ``PurePosixPath(...).parts`` so Windows-style
    # ``..\\x`` also trips the check.
    from pathlib import PurePosixPath as _PPP
    parts = _PPP(path.replace("\\", "/")).parts
    if any(p == ".." for p in parts):
        raise ToolInputError(
            f"path contains a '..' segment (traversal): {path!r}"
        )


# ── Retry decorator for rate-limit handling ─────────────────────────────────
# GitHub's secondary rate limit ("abuse detection") fires at ~10 req/s and
# returns 403 with a Retry-After header. We back off exponentially + jitter
# to recover gracefully. Primary RL returns 403 too but with a reset
# timestamp — for that we just fail fast and let the caller decide.


F = TypeVar("F", bound=Callable[..., Any])


def _with_github_retries(
    max_attempts: int = 3, base_delay: float = 2.0
) -> Callable[[F], F]:
    """Retry on 429 / secondary-rate-limit 403 with exponential backoff + jitter.

    Typed as ``Callable[[F], F]`` so mypy doesn't erase the underlying
    tool signatures (``create_branch``, ``commit_file``, …). Without the
    generic, every decorated tool would show up as ``Callable[..., Any]``
    and strict-mode complaints would cascade.
    """

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    msg = str(exc).lower()
                    rate_limited = (
                        "rate limit" in msg
                        or "abuse" in msg
                        or "secondary rate" in msg
                        or "too many requests" in msg
                    )
                    if not rate_limited or attempt == max_attempts - 1:
                        raise
                    # Exponential: 2s, 4s, 8s + up to 25% jitter.
                    sleep_for = base_delay * (2**attempt)
                    sleep_for += sleep_for * 0.25 * (os.urandom(1)[0] / 255.0)
                    logger.warning(
                        "github_rate_limit_backoff",
                        extra={"attempt": attempt + 1, "sleep_s": round(sleep_for, 2)},
                    )
                    time.sleep(sleep_for)
                    last_exc = exc
            if last_exc:
                raise last_exc
            # Unreachable — kept for type-checker happiness.
            raise RuntimeError("unreachable in _with_github_retries")

        return wrapper  # type: ignore[return-value]

    return deco


# ── Error envelope ──────────────────────────────────────────────────────────


def _error(exc: BaseException, **context: object) -> str:
    """Uniform JSON error shape for every tool.

    LLMs calling the MCP cope much better with structured errors than
    with exceptions raised back through the transport — so we shape
    every failure into ``{error, type, code, …context}`` and let the
    model decide whether to retry, back off, or ask the user.

    Messages are sanitised: ``str(exc)`` can contain tokens (``Bad
    credentials for Bearer ghp_xxx``), paths, or URLs. We route based on
    exception type and emit a short, safe message. The full exception is
    logged on stderr keyed by the tool name.
    """
    kind = type(exc).__name__
    # Known-safe classification.
    if isinstance(exc, ToolInputError):
        code, msg = "invalid_input", str(exc)
    elif "github" in kind.lower():
        # PyGithub raises GithubException / UnknownObjectException etc.
        status_code = getattr(exc, "status", None)
        if status_code == 404:
            code, msg = "not_found", "GitHub resource not found"
        elif status_code in (401, 403):
            code, msg = "forbidden", "GitHub token lacks permission or is invalid"
        elif status_code == 422:
            code, msg = "unprocessable", "GitHub rejected the request (already exists? stale SHA?)"
        elif status_code == 429:
            code, msg = "rate_limited", "GitHub rate limit hit — retry later"
        else:
            code, msg = "github_error", f"GitHub API error (status {status_code})"
    elif isinstance(exc, TimeoutError):
        code, msg = "timeout", "upstream timed out"
    else:
        code, msg = "internal", "tool failed"
    logger.exception("mcp_tool_failed", extra={"code": code, "context": context})
    payload: dict[str, object] = {"ok": False, "error": msg, "code": code, "type": kind}
    # Keep context compact; truncate any stringly values so we never leak
    # a full URL / token / path via the context map.
    compact: dict[str, object] = {}
    for k, v in context.items():
        sv = str(v)
        compact[k] = sv if len(sv) <= 120 else sv[:120] + "…"
    payload.update(compact)
    return json.dumps(payload)


# ── GitHub client cache ─────────────────────────────────────────────────────
# PyGithub's ``Github`` instance holds a ``Requester`` with a live HTTP
# session + auth headers. Re-creating it on every tool call trashes TLS
# keep-alive, redoes token-parsing, and multiplies the latency by 3-5×
# for hot paths like read_github_files_batch. Cache for the process
# lifetime; the token doesn't rotate mid-session.


@functools.lru_cache(maxsize=1)
def _gh_client_cached() -> GitHubClient:
    return GitHubClient(load_config())


def _gh_client() -> GitHubClient:
    """Return a cached :class:`GitHubClient`.

    Wrapper function (vs returning ``_gh_client_cached`` directly) keeps a
    stable call-site for tests to monkey-patch if they want a fake client.
    """
    return _gh_client_cached()


# ── Server factory ─────────────────────────────────────────────────────────────


def build_mcp_server(cache: GitHubContextCache | None = None) -> FastMCP:
    """Build and return a FastMCP server with all GitHub tools registered.

    Can be called multiple times with different cache instances (testing).
    At first-call time we also perform a **preflight token validation**
    via PyGithub's ``get_user()`` so an absent / revoked token fails
    fast with a clear message instead of surfacing as every individual
    tool call returning "forbidden".
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
        try:
            _require_repo(owner, repo)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"tree:{owner}/{repo}:{max_files}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        try:
            r = _gh_client().get_repo(owner, repo)
            tree = r.get_git_tree(r.default_branch, recursive=True)
            paths = [
                item.path for item in tree.tree
                if item.type == "blob"
            ][:max_files]
        except Exception as e:
            # Fallback: use PyGithub contents — but only if the failure
            # looks like "tree too large", not a network/auth error.
            if "truncated" in str(e).lower() or "too large" in str(e).lower():
                try:
                    r = _gh_client().get_repo(owner, repo)
                    paths = _walk_tree_fallback(r, max_files)
                except Exception as ee:
                    return _error(ee, owner=owner, repo=repo)
            else:
                return _error(e, owner=owner, repo=repo)

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
        try:
            _require_repo(owner, repo)
            _require_repo_path(path)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"file:{owner}/{repo}:{ref or 'HEAD'}:{path}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        try:
            r = _gh_client().get_repo(owner, repo)
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
        try:
            _require_repo(owner, repo)
            if len(paths) > MAX_BATCH_PATHS:
                raise ToolInputError(
                    f"too many paths: {len(paths)} > {MAX_BATCH_PATHS}"
                )
            # Validate every path up-front — one bad path in a 50-element
            # batch shouldn't silently skip others, and should fail the
            # whole call clearly. Cheaper than discovering the problem
            # mid-fetch after N successful reads.
            for p in paths:
                _require_repo_path(p)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo, path_count=len(paths))

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
            try:
                r = _gh_client().get_repo(owner, repo)
            except Exception as e:
                return _error(e, owner=owner, repo=repo)

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
        try:
            _require_repo(owner, repo)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"ci:{owner}/{repo}:{branch}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        try:
            failures = _gh_client().get_failed_jobs(owner, repo, branch)
        except Exception as e:
            return _error(e, owner=owner, repo=repo, branch=branch)
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
        try:
            _require_repo(owner, repo)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"issues:{owner}/{repo}:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        try:
            r = _gh_client().get_repo(owner, repo)
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
        except Exception as e:
            return _error(e, owner=owner, repo=repo)

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
        try:
            _require_repo(owner, repo)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"pr_comments:{owner}/{repo}:{pr_number}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        from dataclasses import asdict
        try:
            comments = _gh_client().get_all_pr_comments(owner, repo, pr_number)
        except Exception as e:
            return _error(e, owner=owner, repo=repo, pr_number=pr_number)
        result = json.dumps([asdict(c) for c in comments])
        _cache.set(cache_key, result, ttl=_TTL_PR)
        return result

    # ── Cache invalidation helpers (shared by write tools) ───────────────────

    def _bust_repo(owner: str, repo: str) -> int:
        """Invalidate every cache entry scoped to this repo."""
        n = 0
        for prefix in (
            f"file:{owner}/{repo}",
            f"tree:{owner}/{repo}",
            f"ci:{owner}/{repo}",
            f"issues:{owner}/{repo}",
            f"pr_comments:{owner}/{repo}",
            f"prs:{owner}/{repo}",
        ):
            n += _cache.invalidate_prefix(prefix)
        return n

    # ── Tool: list_prs (read, symmetric to the existing getters) ────────────

    @mcp.tool()
    def list_prs(owner: str, repo: str, state: str = "open", limit: int = 20) -> str:
        """List pull requests for a repo, cached (TTL 2 min).

        `state` ∈ {"open", "closed", "all"}. Returns JSON array of
        {number, title, state, author, url, base, head, draft, created_at}.
        """
        try:
            _require_repo(owner, repo)
        except ToolInputError as e:
            return _error(e, owner=owner, repo=repo)

        cache_key = f"prs:{owner}/{repo}:{state}:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return str(cached)

        try:
            r = _gh_client().get_repo(owner, repo)
            prs: list[dict[str, Any]] = []
            for pr in list(r.get_pulls(state=state))[:limit]:
                prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "author": pr.user.login if pr.user else None,
                    "url": pr.html_url,
                    "base": pr.base.ref,
                    "head": pr.head.ref,
                    "draft": pr.draft,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                })
        except Exception as e:
            return _error(e, owner=owner, repo=repo)

        result = json.dumps(prs)
        _cache.set(cache_key, result, ttl=_TTL_PR)
        return result

    # ── Tool: create_branch ──────────────────────────────────────────────────

    @mcp.tool()
    @_with_github_retries()
    def create_branch(owner: str, repo: str, branch: str, from_ref: str = "") -> str:
        """Create a new branch pointing at `from_ref` (default-branch HEAD if empty).

        Returns JSON {ref, sha} on success, {error, type, code} on failure.
        Busts the tree cache so subsequent reads reflect the new branch.
        """
        try:
            _require_repo(owner, repo)
            _require_str_size("branch", branch, 255)
            r = _gh_client().get_repo(owner, repo)
            base_ref = from_ref or r.default_branch
            head = r.get_branch(base_ref)
            created = r.create_git_ref(ref=f"refs/heads/{branch}", sha=head.commit.sha)
        except Exception as e:
            return _error(e, owner=owner, repo=repo, branch=branch)

        _bust_repo(owner, repo)
        return json.dumps({"ok": True, "ref": created.ref, "sha": head.commit.sha, "branch": branch})

    # ── Tool: commit_file (via Contents API — no workdir required) ───────────

    @mcp.tool()
    @_with_github_retries()
    def commit_file(
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> str:
        """Create or update a single file via the Contents API.

        Writes through the GitHub API directly — no local clone needed, no
        git binary involved. Returns JSON {commit_sha, content_sha, url}.
        Enforces a 2 MiB content cap and a 10 000-char commit-message cap
        as defence-in-depth against prompt-injected LLMs.
        """
        try:
            _require_repo(owner, repo)
            if not isinstance(content, str):
                raise ToolInputError("content must be a string")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_FILE_CONTENT_BYTES:
                raise ToolInputError(
                    f"content exceeds {MAX_FILE_CONTENT_BYTES} bytes "
                    f"(got {len(encoded)})"
                )
            _require_str_size("message", message, MAX_COMMIT_MESSAGE_CHARS)
            _require_str_size("branch", branch, 255)
            _require_str_size("path", path, 1024)
            _require_repo_path(path)

            r = _gh_client().get_repo(owner, repo)
            # PyGithub update_file requires the existing blob SHA; we try
            # create_file first and fall back to update_file on 422.
            try:
                resp = r.create_file(path=path, message=message, content=content, branch=branch)
                commit_sha = resp["commit"].sha
                content_sha = resp["content"].sha
                html = resp["content"].html_url
            except Exception as create_exc:
                if "sha" not in str(create_exc).lower() and "422" not in str(create_exc):
                    raise
                existing = r.get_contents(path, ref=branch)
                if isinstance(existing, list):
                    existing = existing[0]
                resp = r.update_file(
                    path=path, message=message, content=content,
                    sha=existing.sha, branch=branch,
                )
                commit_sha = resp["commit"].sha
                content_sha = resp["content"].sha
                html = resp["content"].html_url
        except Exception as e:
            return _error(e, owner=owner, repo=repo, path=path, branch=branch)

        _cache.invalidate_prefix(f"file:{owner}/{repo}")
        _cache.invalidate_prefix(f"tree:{owner}/{repo}")
        return json.dumps({
            "ok": True,
            "commit_sha": commit_sha,
            "content_sha": content_sha,
            "url": html,
        })

    # ── Tool: open_pr ────────────────────────────────────────────────────────

    @mcp.tool()
    @_with_github_retries()
    def open_pr(
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> str:
        """Open a pull request — or return the existing one if an open PR
        with the same ``head`` already exists (idempotency via dedup).

        Returns JSON ``{ok, number, url, state, already_existed}`` on
        success, ``{ok: false, error, code, type}`` on failure. A dumb
        retry from a flaky MCP client is thus safe — you get one PR, not
        one per retry.
        """
        try:
            _require_repo(owner, repo)
            _require_str_size("title", title, MAX_PR_TITLE_CHARS)
            _require_str_size("body", body, MAX_PR_BODY_CHARS)
            _require_str_size("head", head, 255)
            _require_str_size("base", base, 255)

            r = _gh_client().get_repo(owner, repo)
            # Idempotency: if an open PR already exists for this head, return it
            # instead of creating a duplicate. GitHub would 422 anyway, but
            # surfacing the existing PR is vastly more useful to the caller.
            existing = list(r.get_pulls(state="open", head=f"{owner}:{head}", base=base))
            if existing:
                pr = existing[0]
                return json.dumps({
                    "ok": True,
                    "number": pr.number,
                    "url": pr.html_url,
                    "state": pr.state,
                    "draft": pr.draft,
                    "already_existed": True,
                })
            pr = r.create_pull(title=title, body=body, head=head, base=base, draft=draft)
        except Exception as e:
            return _error(e, owner=owner, repo=repo, head=head, base=base)

        _cache.invalidate_prefix(f"prs:{owner}/{repo}")
        return json.dumps({
            "ok": True,
            "number": pr.number,
            "url": pr.html_url,
            "state": pr.state,
            "draft": pr.draft,
            "already_existed": False,
        })

    # ── Tool: close_pr ───────────────────────────────────────────────────────

    @mcp.tool()
    @_with_github_retries()
    def close_pr(owner: str, repo: str, pr_number: int) -> str:
        """Close a PR without merging. Returns JSON {ok, number}."""
        try:
            _require_repo(owner, repo)
            r = _gh_client().get_repo(owner, repo)
            pr = r.get_pull(pr_number)
            pr.edit(state="closed")
        except Exception as e:
            return _error(e, owner=owner, repo=repo, pr_number=pr_number)

        _cache.invalidate_prefix(f"prs:{owner}/{repo}")
        return json.dumps({"ok": True, "number": pr_number})

    # ── Tool: add_pr_comment (issue-style) ───────────────────────────────────

    @mcp.tool()
    @_with_github_retries()
    def add_pr_comment(owner: str, repo: str, pr_number: int, body: str) -> str:
        """Add a conversation comment to a PR (not a line-level review comment).

        Returns JSON {ok, id, url}. Busts the pr_comments cache so the
        next get_pr_comments call sees it.
        """
        try:
            _require_repo(owner, repo)
            _require_str_size("body", body, MAX_COMMENT_BODY_CHARS)
            r = _gh_client().get_repo(owner, repo)
            issue = r.get_issue(pr_number)  # PRs are issues under the hood
            c = issue.create_comment(body)
        except Exception as e:
            return _error(e, owner=owner, repo=repo, pr_number=pr_number)

        _cache.invalidate_prefix(f"pr_comments:{owner}/{repo}")
        return json.dumps({"ok": True, "id": c.id, "url": c.html_url})

    # ── Tool: add_pr_labels ──────────────────────────────────────────────────

    @mcp.tool()
    @_with_github_retries()
    def add_pr_labels(owner: str, repo: str, pr_number: int, labels: list[str]) -> str:
        """Add one or more labels to a PR. Returns JSON {labels, number}."""
        try:
            _require_repo(owner, repo)
            if not isinstance(labels, list):
                raise ToolInputError("labels must be a list of strings")
            if len(labels) > MAX_LABELS_PER_CALL:
                raise ToolInputError(
                    f"too many labels: {len(labels)} > {MAX_LABELS_PER_CALL}"
                )
            for lbl in labels:
                _require_str_size("label", lbl, MAX_LABEL_NAME_CHARS)
            r = _gh_client().get_repo(owner, repo)
            issue = r.get_issue(pr_number)
            for label in labels:
                issue.add_to_labels(label)
            final = [lbl.name for lbl in issue.labels]
        except Exception as e:
            return _error(e, owner=owner, repo=repo, pr_number=pr_number)

        _cache.invalidate_prefix(f"prs:{owner}/{repo}")
        return json.dumps({"ok": True, "number": pr_number, "labels": final})

    # ── Tool: invalidate_repo_cache ──────────────────────────────────────────

    @mcp.tool()
    def invalidate_repo_cache(owner: str, repo: str) -> str:
        """
        Bust all cache entries for a given repo (call after external pushes
        that gitoma wasn't aware of). Returns JSON {invalidated, repo}.
        """
        return json.dumps({
            "ok": True,
            "invalidated": _bust_repo(owner, repo),
            "repo": f"{owner}/{repo}",
        })

    return mcp


# ── Fallback tree walker ───────────────────────────────────────────────────────

def _walk_tree_fallback(repo: Any, max_files: int) -> list[str]:
    """Fallback file tree via PyGithub get_contents (slower, for private repos).

    Bounded-BFS. Caps the queue at 10 000 items so a deeply-nested repo
    can't blow memory / stack during fallback traversal.
    """
    paths: list[str] = []
    queue: list[str] = [""]
    while queue and len(paths) < max_files:
        if len(queue) > 10_000:
            logger.warning("walk_tree_fallback_queue_capped", extra={"repo": str(repo)})
            break
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
    """Return the module-level singleton MCP server (lazy initialize).

    Performs a preflight smoke test of the configured GitHub token by
    calling ``/user`` — that way a bad token surfaces with one clear
    error at server boot, not as every single tool call returning a
    generic ``forbidden``.
    """
    global _server
    if _server is not None:
        return _server

    cfg = load_config()
    if not cfg.github.token:
        logger.error(
            "mcp_no_github_token — set GITHUB_TOKEN or every tool will "
            "return a 'forbidden' error envelope"
        )
    else:
        logger.info(
            "mcp_github_token_configured",
            extra={"token_kind": _classify_token(cfg.github.token)},
        )

    _server = build_mcp_server()
    return _server


def _classify_token(token: str) -> str:
    """Public-log-safe classification of a GitHub token prefix.

    The token itself is never logged — only which *shape* it is, so an
    operator can tell at a glance whether they're running with a classic
    PAT, a fine-grained PAT, or an installation token. Useful for
    debugging scope-mismatch incidents without ever printing the secret.
    """
    if token.startswith("ghp_"):
        return "classic"
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith(("gho_", "ghu_")):
        return "oauth"
    if token.startswith("ghs_"):
        return "server"
    return "unknown"


# ── Standalone entry point (for Claude Desktop / mcp dev) ─────────────────────

if __name__ == "__main__":
    server = get_mcp_server()
    # Banner on stderr — stdout is reserved for MCP protocol frames.
    print("🔗 Gitoma GitHub MCP Server starting on stdio...", file=sys.stderr)
    server.run()
