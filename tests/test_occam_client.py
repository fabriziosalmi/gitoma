"""Tests for ``OccamClient`` — the HTTP wrapper gitoma uses to feed
the Occam Observer agent-log + query prior-runs context.

Contract from the client's docstring:
  * Silent fail-open on network / schema / gateway errors.
  * No retries.
  * ``OCCAM_URL`` unset → every call returns the disabled default."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from gitoma.context.occam_client import (
    FAILURE_MODES,
    OUTCOMES,
    OccamClient,
    default_client,
    format_agent_log_for_prompt,
    map_error_to_failure_modes,
)


def _make_client(handler, base_url: str = "http://occam.test") -> OccamClient:
    """Build an OccamClient that routes every request through
    ``handler`` via httpx.MockTransport."""
    transport = httpx.MockTransport(handler)
    return OccamClient(base_url, timeout=0.5, transport=transport)


# ── Feature flag / disabled path ────────────────────────────────────────


def test_disabled_client_is_noop_on_observation() -> None:
    c = OccamClient(None)
    assert c.enabled is False
    assert c.post_observation({"any": "payload"}) is None


def test_disabled_client_returns_empty_agent_log() -> None:
    c = OccamClient(None)
    assert c.get_agent_log() == []


def test_disabled_client_returns_none_repo_context() -> None:
    c = OccamClient(None)
    assert c.get_repo_context("/tmp/x") is None


def test_default_client_honours_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCCAM_URL", raising=False)
    assert default_client().enabled is False

    monkeypatch.setenv("OCCAM_URL", "http://127.0.0.1:29999")
    c = default_client()
    assert c.enabled is True


def test_default_client_empty_env_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OCCAM_URL=""`` must be treated as unset, not "base URL is
    empty string" which would then try to hit whatever."""
    monkeypatch.setenv("OCCAM_URL", "")
    assert default_client().enabled is False


# ── POST /observation — happy path + failure paths ──────────────────────


def test_post_observation_returns_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 42, "ts": "2026-04-23T14:31:52Z"})

    c = _make_client(handler)
    obs_id = c.post_observation({
        "run_id": "run-42",
        "agent": "gitoma",
        "subtask_id": "T001-S01",
        "outcome": "success",
        "touched_files": ["src/db.py"],
        "failure_modes": [],
    })
    assert obs_id == 42
    assert captured["path"] == "/observation"
    assert captured["body"]["subtask_id"] == "T001-S01"


def test_post_observation_swallows_400() -> None:
    """A 4xx response is still "gateway responded, we just got it
    wrong" — the pipeline must not care."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad payload"})

    c = _make_client(handler)
    assert c.post_observation({"foo": "bar"}) is None


def test_post_observation_swallows_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("occam is down")

    c = _make_client(handler)
    assert c.post_observation({}) is None


def test_post_observation_swallows_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    c = _make_client(handler)
    assert c.post_observation({}) is None


def test_post_observation_handles_non_dict_response() -> None:
    """If occam returned a list or a string for /observation by
    mistake, we must not crash — we just return None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["weird"])

    c = _make_client(handler)
    assert c.post_observation({}) is None


# ── GET /repo/agent-log ─────────────────────────────────────────────────


def test_get_agent_log_returns_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"subtask_id": "T001-S01", "outcome": "success",
             "touched_files": ["src/db.py"], "failure_modes": []},
            {"subtask_id": "T002-S01", "outcome": "fail",
             "touched_files": ["pyproject.toml"],
             "failure_modes": ["syntax_invalid"]},
        ])

    c = _make_client(handler)
    log = c.get_agent_log(since="24h", limit=20)
    assert len(log) == 2
    assert log[0]["subtask_id"] == "T001-S01"


def test_get_agent_log_passes_query_params() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    c = _make_client(handler)
    c.get_agent_log(since="7d", limit=50)
    assert captured["params"]["since"] == "7d"
    assert captured["params"]["limit"] == "50"


def test_get_agent_log_returns_empty_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dead")

    c = _make_client(handler)
    assert c.get_agent_log() == []


def test_get_agent_log_returns_empty_on_non_list_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad"})

    c = _make_client(handler)
    assert c.get_agent_log() == []


# ── GET /repo/context ───────────────────────────────────────────────────


def test_get_repo_context_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "target": "/tmp/repo",
            "hot_files": [{"path": "src/db.py", "changes": 41}],
            "languages": [{"name": "Python", "files": 2, "bytes": 1212}],
            "stack": ["python/poetry"],
        })

    c = _make_client(handler)
    ctx = c.get_repo_context("/tmp/repo")
    assert ctx is not None
    assert ctx["stack"] == ["python/poetry"]


def test_get_repo_context_returns_none_on_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dead")

    c = _make_client(handler)
    assert c.get_repo_context("/tmp/x") is None


# ── map_error_to_failure_modes ──────────────────────────────────────────


def test_map_json_emit_failure() -> None:
    msg = "Could not obtain valid JSON from LLM after 3 attempt(s)."
    assert "json_emit" in map_error_to_failure_modes(msg)


def test_map_ast_diff_failure() -> None:
    msg = "AST-diff check failed after 2 attempt(s) on tests/test_db.py. Missing: db, test_a"
    assert "ast_diff" in map_error_to_failure_modes(msg)


def test_map_test_regression_failure() -> None:
    msg = "Test regression: the following tests were passing before your patch"
    assert "test_regression" in map_error_to_failure_modes(msg)


def test_map_syntax_invalid_failure() -> None:
    msg = "Syntax check failed on pyproject.toml: TOMLDecodeError: Invalid value"
    assert "syntax_invalid" in map_error_to_failure_modes(msg)


def test_map_denylist_failure() -> None:
    msg = "Refusing to touch sensitive path: .github/workflows/ci.yml"
    assert "denylist" in map_error_to_failure_modes(msg)


def test_map_manifest_block_failure() -> None:
    msg = (
        "Refusing to edit build manifest 'pyproject.toml' — "
        "no subtask file_hint explicitly targets it"
    )
    assert "manifest_block" in map_error_to_failure_modes(msg)


def test_map_unknown_fallback() -> None:
    assert map_error_to_failure_modes("something we've never seen") == ["unknown"]


def test_map_empty_error_returns_unknown() -> None:
    assert map_error_to_failure_modes("") == ["unknown"]


# ── format_agent_log_for_prompt ─────────────────────────────────────────


def test_format_empty_log_returns_empty_string() -> None:
    assert format_agent_log_for_prompt([]) == ""


def test_format_groups_by_outcome_fails_first() -> None:
    """Fails first because they're the actionable signal the planner
    needs to AVOID; successes are reassurance, secondary."""
    entries = [
        {"subtask_id": "T001", "outcome": "success",
         "touched_files": ["src/db.py"], "failure_modes": []},
        {"subtask_id": "T002", "outcome": "fail",
         "touched_files": ["pyproject.toml"],
         "failure_modes": ["syntax_invalid"]},
    ]
    out = format_agent_log_for_prompt(entries)
    fail_pos = out.find("FAILED")
    succ_pos = out.find("SUCCESSFUL")
    assert fail_pos != -1 and succ_pos != -1
    assert fail_pos < succ_pos


def test_format_includes_failure_modes_in_bullet() -> None:
    entries = [
        {"subtask_id": "T001-S02", "outcome": "fail",
         "touched_files": ["tests/test_db.py"],
         "failure_modes": ["ast_diff", "test_regression"]},
    ]
    out = format_agent_log_for_prompt(entries)
    assert "ast_diff" in out
    assert "test_regression" in out
    assert "T001-S02" in out


def test_format_caps_at_max_bullets() -> None:
    """Don't flood the planner prompt — cap to ``max_bullets``."""
    entries = [
        {"subtask_id": f"T{i:03d}", "outcome": "fail",
         "touched_files": [f"f{i}.py"], "failure_modes": ["json_emit"]}
        for i in range(50)
    ]
    out = format_agent_log_for_prompt(entries, max_bullets=5)
    # Exactly 5 "•" bullets (plus the header, which doesn't carry •).
    assert out.count("•") == 5


def test_format_skipped_outcome_ignored() -> None:
    """``skipped`` is neither failure nor success — neutral. Keep the
    prompt block focused on actionable fail/success signal."""
    entries = [
        {"subtask_id": "T001", "outcome": "skipped",
         "touched_files": [], "failure_modes": []},
    ]
    out = format_agent_log_for_prompt(entries)
    assert out == ""


# ── Enum closed-set sanity ──────────────────────────────────────────────


def test_failure_modes_set_is_non_empty() -> None:
    assert len(FAILURE_MODES) >= 10
    assert "unknown" in FAILURE_MODES


def test_outcomes_set_contents() -> None:
    assert OUTCOMES == {"success", "fail", "skipped"}
