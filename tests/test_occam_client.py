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
    format_fingerprint_for_prompt,
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


# ── GET /repo/fingerprint ───────────────────────────────────────────────


def test_get_repo_fingerprint_returns_dict() -> None:
    """Happy path — Occam returns the structured snapshot, client passes
    it through unchanged so callers can inspect every field."""
    sample = {
        "target": "/tmp/repo",
        "commit_sha": "abc123",
        "computed_at": "2026-04-23T22:00:00Z",
        "languages": [{"name": "Rust", "files": 47}],
        "stack": ["rust/cargo"],
        "declared_deps": {"rust": ["clap", "serde"], "npm": [], "python": [], "go": []},
        "declared_frameworks": ["clap", "serde"],
        "entrypoints": ["src/main.rs"],
        "manifest_files": ["Cargo.toml"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repo/fingerprint"
        assert request.url.params.get("target") == "/tmp/repo"
        return httpx.Response(200, json=sample)

    c = _make_client(handler)
    fp = c.get_repo_fingerprint("/tmp/repo")
    assert fp is not None
    assert fp["commit_sha"] == "abc123"
    assert fp["declared_frameworks"] == ["clap", "serde"]
    assert fp["manifest_files"] == ["Cargo.toml"]


def test_get_repo_fingerprint_returns_none_on_400() -> None:
    """Non-2xx → None. Treated as fail-open: caller proceeds without
    fingerprint context, no exception bubbles up."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid target"})

    c = _make_client(handler)
    assert c.get_repo_fingerprint("/tmp/x") is None


def test_get_repo_fingerprint_returns_none_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dead")

    c = _make_client(handler)
    assert c.get_repo_fingerprint("/tmp/x") is None


def test_get_repo_fingerprint_returns_none_when_disabled() -> None:
    c = OccamClient(None)
    assert c.get_repo_fingerprint("/tmp/x") is None


def test_get_repo_fingerprint_returns_none_when_body_not_dict() -> None:
    """Defensive: server returns a JSON list / scalar by mistake — client
    treats it as malformed and returns None instead of leaking the wrong
    shape upstream."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    c = _make_client(handler)
    assert c.get_repo_fingerprint("/tmp/x") is None


# ── format_fingerprint_for_prompt ───────────────────────────────────────


def test_format_fingerprint_empty_returns_empty_string() -> None:
    assert format_fingerprint_for_prompt(None) == ""
    assert format_fingerprint_for_prompt({}) == ""


def test_format_fingerprint_renders_each_section() -> None:
    """Sample with every field populated — every section we promised
    in the docstring shows up in the output."""
    fp = {
        "languages": [
            {"name": "Rust", "files": 47},
            {"name": "Markdown", "files": 9},
        ],
        "stack": ["rust/cargo", "github-actions"],
        "declared_frameworks": ["clap"],
        "declared_deps": {
            "rust": ["clap", "serde", "tokio"],
            "npm": [],
            "python": [],
            "go": [],
        },
        "entrypoints": ["src/main.rs"],
        "manifest_files": ["Cargo.toml"],
    }
    out = format_fingerprint_for_prompt(fp)
    assert "languages: Rust(47)" in out
    assert "Markdown(9)" in out
    assert "stack: rust/cargo, github-actions" in out
    assert "declared_frameworks: clap" in out
    assert "declared_deps.rust: clap, serde, tokio" in out
    assert "entrypoints: src/main.rs" in out
    assert "manifest_files: Cargo.toml" in out


def test_format_fingerprint_renders_none_for_empty_frameworks() -> None:
    """``declared_frameworks: (none)`` is intentional — the empty list
    is a hard constraint we WANT the planner to see, not skip."""
    fp = {
        "languages": [{"name": "Rust", "files": 47}],
        "stack": ["rust/cargo"],
        "declared_frameworks": [],
        "declared_deps": {"rust": ["serde"]},
        "manifest_files": ["Cargo.toml"],
    }
    out = format_fingerprint_for_prompt(fp)
    assert "declared_frameworks: (none)" in out


def test_format_fingerprint_truncates_huge_dep_list() -> None:
    """A package.json with 50 deps shouldn't blow up the planner prompt.
    Cap is 12 + ``(+N more)`` suffix for total visibility."""
    npm_deps = [f"pkg{i}" for i in range(20)]
    fp = {
        "declared_frameworks": [],
        "declared_deps": {"npm": npm_deps},
        "manifest_files": ["package.json"],
    }
    out = format_fingerprint_for_prompt(fp)
    assert "pkg0" in out and "pkg11" in out      # first 12 shown
    assert "pkg12" not in out                    # rest hidden
    assert "(+8 more)" in out


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
