"""Tests for the Phase-6 CI-watch helper + auto fix-ci integration.

The helper is a coarse state machine: poll → classify → remediate →
re-poll. We assert each branch:

* CI passes immediately → returns "success", no fix-ci call.
* CI fails + auto_fix off → returns "failure", no CIDiagnosticAgent.
* CI fails + auto_fix on → CIDiagnosticAgent invoked, then re-poll
  either succeeds or gives up.
* CI never completes within the budget → returns "timeout", no fix-ci.
* No workflow runs at all → returns "no_runs" (non-blocking).
* Transient network error during probe → treated as pending + retries.

The real ``time.sleep`` is neutralised so the tests run in milliseconds.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gitoma.cli._helpers import _watch_ci_and_maybe_fix
from gitoma.core.config import BotConfig, Config, GitHubConfig, LMStudioConfig
from gitoma.core.state import AgentState


@pytest.fixture(autouse=True)
def _fake_sleep(monkeypatch):
    """Neutralise the helper's ``time.sleep`` so tests don't actually wait."""
    monkeypatch.setattr("gitoma.cli._helpers.time.sleep", lambda _: None)


@pytest.fixture(autouse=True)
def _fake_save_state(monkeypatch):
    """State saves hit the real filesystem by default; stub so tests stay hermetic."""
    monkeypatch.setattr("gitoma.cli._helpers.save_state", lambda _: None)


@pytest.fixture
def cfg():
    return Config(
        github=GitHubConfig(token="x"),
        bot=BotConfig(),
        lmstudio=LMStudioConfig(),
    )


@pytest.fixture
def state():
    return AgentState(repo_url="u", owner="o", name="r", branch="b")


# ── Success path ─────────────────────────────────────────────────────────────


def test_watch_returns_success_when_first_poll_sees_ci_pass(cfg, state, mocker):
    gh = MagicMock()
    gh.get_latest_ci_status.return_value = {
        "state": "success", "run_id": 1, "run_url": "https://…/1",
        "conclusion": "success", "workflow": "tests", "updated_at": None,
    }
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=5.0,
    )
    assert outcome == "success"
    # No fix-ci attempt since the first poll already succeeded.
    gh.get_latest_ci_status.assert_called_once()


def test_watch_polls_until_ci_reaches_success(cfg, state, mocker):
    """``pending`` → ``pending`` → ``success`` should loop and return success."""
    gh = MagicMock()
    gh.get_latest_ci_status.side_effect = [
        {"state": "pending", "run_id": 9, "run_url": None, "conclusion": None, "workflow": "w", "updated_at": None},
        {"state": "pending", "run_id": 9, "run_url": None, "conclusion": None, "workflow": "w", "updated_at": None},
        {"state": "success", "run_id": 9, "run_url": "url", "conclusion": "success", "workflow": "w", "updated_at": None},
    ]
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=10.0,
    )
    assert outcome == "success"
    assert gh.get_latest_ci_status.call_count == 3


# ── Failure paths ────────────────────────────────────────────────────────────


def test_watch_does_not_invoke_fix_ci_when_auto_fix_disabled(cfg, state, mocker):
    gh = MagicMock()
    gh.get_latest_ci_status.return_value = {
        "state": "failure", "run_id": 5, "run_url": "url",
        "conclusion": "failure", "workflow": "ci", "updated_at": None,
    }
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)
    agent_class = mocker.patch("gitoma.review.reflexion.CIDiagnosticAgent")

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        auto_fix=False, poll_interval_s=0.001, timeout_s=5.0,
    )
    assert outcome == "failure"
    agent_class.assert_not_called()


def test_watch_invokes_fix_ci_once_on_failure(cfg, state, mocker):
    """Auto-fix on + CI fails → CIDiagnosticAgent called with (repo_url, branch)."""
    gh = MagicMock()
    # First two polls fail (initial CI), third poll sees success after fix.
    gh.get_latest_ci_status.side_effect = [
        {"state": "failure", "run_id": 1, "run_url": "url", "conclusion": "failure", "workflow": "w", "updated_at": None},
        {"state": "success", "run_id": 2, "run_url": "url2", "conclusion": "success", "workflow": "w", "updated_at": None},
    ]
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)

    agent = MagicMock()
    agent_class = mocker.patch("gitoma.review.reflexion.CIDiagnosticAgent", return_value=agent)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=10.0,
    )
    assert outcome == "success"
    agent_class.assert_called_once_with(cfg)
    agent.analyze_and_fix.assert_called_once_with("https://github.com/o/r", "br")
    # Two polls total: the failing one + the post-fix success one.
    assert gh.get_latest_ci_status.call_count == 2


def test_watch_returns_failure_when_fix_ci_also_fails(cfg, state, mocker):
    """If post-remediation CI still fails, outcome is ``failure`` and we
    do NOT loop indefinitely (max_fix_attempts is exhausted)."""
    gh = MagicMock()
    gh.get_latest_ci_status.side_effect = [
        {"state": "failure", "run_id": 1, "run_url": None, "conclusion": "failure", "workflow": "w", "updated_at": None},
        {"state": "failure", "run_id": 2, "run_url": None, "conclusion": "failure", "workflow": "w", "updated_at": None},
    ]
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)
    mocker.patch("gitoma.review.reflexion.CIDiagnosticAgent")

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=10.0,
    )
    assert outcome == "failure"
    # Exactly the initial budget's worth of attempts — not an infinite loop.
    assert gh.get_latest_ci_status.call_count == 2


def test_watch_survives_fix_ci_exception(cfg, state, mocker):
    """A crashing fix-ci attempt must not propagate; we surface ``failure``."""
    gh = MagicMock()
    gh.get_latest_ci_status.return_value = {
        "state": "failure", "run_id": 1, "run_url": None,
        "conclusion": "failure", "workflow": "w", "updated_at": None,
    }
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)
    agent = MagicMock()
    agent.analyze_and_fix.side_effect = RuntimeError("LLM exploded")
    mocker.patch("gitoma.review.reflexion.CIDiagnosticAgent", return_value=agent)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=10.0,
    )
    assert outcome == "failure"


# ── Timeout + no_runs ────────────────────────────────────────────────────────


def test_watch_returns_timeout_when_budget_expires(cfg, state, mocker):
    """If ``time.monotonic`` advances past the deadline while CI stays
    pending, the helper returns ``"timeout"`` and does NOT call fix-ci."""
    gh = MagicMock()
    gh.get_latest_ci_status.return_value = {
        "state": "pending", "run_id": 1, "run_url": None,
        "conclusion": None, "workflow": "w", "updated_at": None,
    }
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)
    agent_class = mocker.patch("gitoma.review.reflexion.CIDiagnosticAgent")

    # Monotonic sequence: first call = 0 (start), subsequent calls leap
    # straight past the 1s budget so the while condition fails at iter 1.
    t = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    with patch("gitoma.cli._helpers.time.monotonic", side_effect=lambda: next(t)):
        outcome = _watch_ci_and_maybe_fix(
            cfg, "o", "r", "br", "https://github.com/o/r", state,
            poll_interval_s=0.001, timeout_s=1.0,
        )
    assert outcome == "timeout"
    agent_class.assert_not_called()


def test_watch_returns_no_runs_when_branch_has_no_workflows(cfg, state, mocker):
    gh = MagicMock()
    gh.get_latest_ci_status.return_value = {
        "state": "no_runs", "run_id": None, "run_url": None,
        "conclusion": None, "workflow": None, "updated_at": None,
    }
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=5.0,
    )
    assert outcome == "no_runs"


# ── Transient API errors → keep polling ─────────────────────────────────────


def test_watch_treats_probe_error_as_pending_and_keeps_trying(cfg, state, mocker):
    """A transient network error on one probe must NOT abort the watch —
    return pending so the outer loop polls again."""
    gh = MagicMock()
    gh.get_latest_ci_status.side_effect = [
        RuntimeError("connection refused"),
        {"state": "success", "run_id": 1, "run_url": "url",
         "conclusion": "success", "workflow": "w", "updated_at": None},
    ]
    mocker.patch("gitoma.cli._helpers.GitHubClient", return_value=gh)

    outcome = _watch_ci_and_maybe_fix(
        cfg, "o", "r", "br", "https://github.com/o/r", state,
        poll_interval_s=0.001, timeout_s=10.0,
    )
    assert outcome == "success"
    # The transient error costs one call; the recovery costs one more.
    assert gh.get_latest_ci_status.call_count == 2


# ── GitHubClient.get_latest_ci_status ────────────────────────────────────────


def test_get_latest_ci_status_maps_github_statuses_correctly(mocker):
    """The aggregate status bucket handles every real combination."""
    from gitoma.core.github_client import GitHubClient

    def _build_run(status, conclusion, name="workflow", run_id=1):
        run = MagicMock()
        run.status = status
        run.conclusion = conclusion
        run.name = name
        run.id = run_id
        run.html_url = f"https://example/{run_id}"
        run.updated_at.isoformat.return_value = "2026-04-21T00:00:00+00:00"
        return run

    cases = [
        (("completed", "success"),      "success"),
        (("completed", "failure"),      "failure"),
        (("completed", "cancelled"),    "failure"),  # non-success → failure bucket
        (("completed", "timed_out"),    "failure"),
        (("completed", "neutral"),      "failure"),
        (("queued",    None),           "pending"),
        (("in_progress", None),         "pending"),
        (("waiting",   None),           "pending"),
    ]
    for (status, concl), expected in cases:
        repo = MagicMock()
        repo.get_workflow_runs.return_value = [_build_run(status, concl)]
        client = GitHubClient.__new__(GitHubClient)
        client._gh = MagicMock()
        client._gh.get_repo.return_value = repo
        client._config = MagicMock()
        out = client.get_latest_ci_status("o", "r", "br")
        assert out["state"] == expected, (
            f"{status}/{concl} should bucket to {expected}, got {out['state']}"
        )


def test_get_latest_ci_status_returns_no_runs_on_empty():
    from gitoma.core.github_client import GitHubClient

    repo = MagicMock()
    repo.get_workflow_runs.return_value = []
    client = GitHubClient.__new__(GitHubClient)
    client._gh = MagicMock()
    client._gh.get_repo.return_value = repo
    client._config = MagicMock()
    out = client.get_latest_ci_status("o", "r", "br")
    assert out["state"] == "no_runs"
    assert out["run_id"] is None
