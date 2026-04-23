"""Occam Observer MCP/HTTP client — feedback loop substrate.

Occam exposes a local HTTP gateway (default ``127.0.0.1:29999``) +
an MCP-over-stdio binary. The endpoints gitoma consumes for P1
(feedback-loop self-learning) are HTTP-only:

  * ``POST /observation`` — agent logs what it did after every
    subtask. Feeds the TSDB that ``/repo/agent-log`` queries.
  * ``GET /repo/agent-log`` — planner reads the history of
    prior runs + their failure modes before emitting a new plan.
  * ``GET /repo/context`` — (optional) enriches RepoBrief with
    hot_files, recent_churn, stack.

Design contract:

  * **Silent fail-open**. If ``OCCAM_URL`` is unset, or the gateway
    is unreachable, every call returns a benign default (None / empty
    list) and the gitoma pipeline proceeds unchanged. Running gitoma
    without Occam must always work.
  * **Short timeouts**. 2s per request. Occam is local; if it's
    taking longer than that either it's down or the network path
    broke — in both cases the LLM call we'd block is way more
    expensive than losing one observation.
  * **No retries**. A missed observation is a tiny data loss, not a
    correctness issue. Gitoma's trace JSONL is still authoritative.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

__all__ = [
    "OccamClient",
    "default_client",
    "FAILURE_MODES",
    "OUTCOMES",
    "count_failed_hints",
]


# Closed set of failure-mode labels so the agent-log is queryable
# with ``SELECT ... WHERE failure_modes @> '["ast_diff"]'`` shape
# rather than free-text grep. Extended only here + mirrored in
# ``map_error_to_failure_modes``.
FAILURE_MODES: frozenset[str] = frozenset({
    "json_emit",            # worker LLM couldn't produce valid JSON
    "ast_diff",             # G7 top-level def missing in modify patch
    "test_regression",      # G8 previously-passing test now failing
    "syntax_invalid",       # G2/G6 post-write parser rejected content
    "denylist",             # patcher blocked a sensitive path
    "manifest_block",       # unsanctioned manifest edit rejected
    "patcher_reject",       # generic patcher refusal (size, path, etc.)
    "build_retry_exhausted",  # BuildAnalyzer failure after max attempts
    "git_refused",          # git add / git commit rejected the change
    "json_parse_bad",       # raw response not JSON-parseable
    "unknown",              # fallback when we can't classify
})

# Closed set of outcome labels. ``skipped`` is for subtasks that
# produced no touched files (``no_op`` panel case) without crashing.
OUTCOMES: frozenset[str] = frozenset({"success", "fail", "skipped"})


def map_error_to_failure_modes(error_msg: str) -> list[str]:
    """Bucket a worker error string into one or more ``FAILURE_MODES``.
    Heuristic — the same error can imply multiple modes (e.g. a JSON
    emit that terminated mid-patches-array). Order preserved for
    human readability."""
    if not error_msg:
        return ["unknown"]
    msg = error_msg.lower()
    hits: list[str] = []
    if "could not obtain valid json" in msg or "json parse" in msg:
        hits.append("json_emit")
    if "ast-diff" in msg or "top-level" in msg and "missing" in msg:
        hits.append("ast_diff")
    if "test regression" in msg or "were passing before" in msg:
        hits.append("test_regression")
    if "syntax check failed" in msg or "tomldecodeerror" in msg or "jsondecodeerror" in msg:
        hits.append("syntax_invalid")
    if "refusing to touch sensitive path" in msg:
        hits.append("denylist")
    if "refusing to edit build manifest" in msg or "compile-fix mode" in msg:
        hits.append("manifest_block")
    if "build check failed" in msg:
        hits.append("build_retry_exhausted")
    if "git add failed" in msg or "gitcommanderror" in msg:
        hits.append("git_refused")
    return hits or ["unknown"]


class OccamClient:
    """Thin HTTP client around the Occam Observer gateway.

    Construct via ``default_client()`` to honour the env-var opt-in;
    direct construction is for tests that inject a specific URL /
    transport. An instance with ``base_url=None`` is a no-op client
    — every call returns an empty default.
    """

    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/") or None
        self._timeout = timeout
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return self._base_url is not None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url or "http://localhost",
            timeout=self._timeout,
            transport=self._transport,
        )

    def post_observation(self, payload: dict[str, Any]) -> int | None:
        """POST /observation. Returns the inserted id on success,
        ``None`` on any failure (unreachable, wrong schema, gateway
        error). Never raises."""
        if not self.enabled:
            return None
        try:
            with self._client() as c:
                r = c.post("/observation", json=payload)
                if r.status_code >= 400:
                    return None
                body = r.json()
                return int(body.get("id")) if isinstance(body, dict) else None
        except Exception:
            return None

    def get_agent_log(
        self, *, since: str = "24h", limit: int = 20,
    ) -> list[dict[str, Any]]:
        """GET /repo/agent-log. Returns an empty list on any
        failure — caller treats empty as "no prior context"."""
        if not self.enabled:
            return []
        try:
            with self._client() as c:
                r = c.get(
                    "/repo/agent-log",
                    params={"since": since, "limit": str(limit)},
                )
                if r.status_code >= 400:
                    return []
                body = r.json()
                return body if isinstance(body, list) else []
        except Exception:
            return []

    def get_repo_context(self, target: str) -> dict[str, Any] | None:
        """GET /repo/context. Returns ``None`` on failure."""
        if not self.enabled:
            return None
        try:
            with self._client() as c:
                r = c.get("/repo/context", params={"target": target})
                if r.status_code >= 400:
                    return None
                body = r.json()
                return body if isinstance(body, dict) else None
        except Exception:
            return None


def default_client() -> OccamClient:
    """Build a client from the ``OCCAM_URL`` env var. Returns a
    disabled (no-op) client when the var is unset or empty."""
    return OccamClient(os.environ.get("OCCAM_URL") or None)


def count_failed_hints(entries: list[dict[str, Any]]) -> dict[str, int]:
    """From an agent-log slice, count how many distinct fail
    observations each file_hint appears in.

    ``tests/test_db.py`` failing in 3 separate subtasks → result is
    ``{"tests/test_db.py": 3}``. Used by the post-plan filter (G9)
    to decide whether a freshly-emitted subtask hinting at a path
    should be dropped (count >= threshold) — the assumption is
    "this file has been tried and failed N times in the recent
    window; the (N+1)th attempt is unlikely to differ".

    Skips ``success`` and ``skipped`` outcomes. Only counts the
    ``touched_files`` array (which is the planner's ``file_hints``
    propagated through the worker's observation payload).
    """
    counter: dict[str, int] = {}
    for entry in entries:
        if entry.get("outcome") != "fail":
            continue
        for hint in entry.get("touched_files") or []:
            if hint:
                counter[hint] = counter.get(hint, 0) + 1
    return counter


def format_agent_log_for_prompt(entries: list[dict[str, Any]], max_bullets: int = 15) -> str:
    """Render an agent-log slice into the `== PRIOR RUNS CONTEXT ==`
    block injected into the planner prompt. Grouped by outcome,
    sorted by recency (assumes entries come newest-first from the
    API). Empty list → empty string (caller injects nothing)."""
    if not entries:
        return ""
    fails: list[str] = []
    successes: list[str] = []
    for e in entries[:max_bullets]:
        subtask = e.get("subtask_id", "?")
        touched = ", ".join((e.get("touched_files") or [])[:3]) or "—"
        if e.get("outcome") == "fail":
            modes = ", ".join(e.get("failure_modes") or ["unknown"])
            fails.append(f"  • {subtask} on [{touched}] — failed: {modes}")
        elif e.get("outcome") == "success":
            successes.append(f"  • {subtask} on [{touched}] — succeeded")
    lines: list[str] = []
    if fails:
        lines.append("Recent FAILED subtasks — AVOID re-proposing these patterns:")
        lines.extend(fails)
    if successes:
        if lines:
            lines.append("")
        lines.append("Recent SUCCESSFUL subtasks — safe area to build on:")
        lines.extend(successes)
    return "\n".join(lines)
