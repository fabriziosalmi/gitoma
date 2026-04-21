"""Tests for the heartbeat + orphan-detection plumbing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from gitoma.api import web as web_module
from gitoma.core import state as state_module
from gitoma.core.state import AgentState


# ── State persistence ────────────────────────────────────────────────────────


def test_pid_and_heartbeat_roundtrip():
    s = AgentState(repo_url="u", owner="o", name="r", branch="b")
    s.pid = 4242
    s.last_heartbeat = "2026-04-21T10:00:00+00:00"
    restored = AgentState.from_dict(s.to_dict())
    assert restored.pid == 4242
    assert restored.last_heartbeat == "2026-04-21T10:00:00+00:00"


def test_from_dict_tolerates_legacy_state_files():
    """State files written before we added heartbeat fields must still load."""
    legacy = {
        "repo_url": "u", "owner": "o", "name": "r", "branch": "b",
        "phase": "WORKING",
        "started_at": "2026-04-20T10:00:00+00:00",
        "updated_at": "2026-04-20T10:01:00+00:00",
        # No pid, no last_heartbeat, no current_operation
    }
    s = AgentState.from_dict(legacy)
    assert s.pid is None
    assert s.last_heartbeat == ""


def test_from_dict_ignores_unknown_keys():
    """Forward-compat: newer versions may add fields this client doesn't know."""
    future = {
        "repo_url": "u", "owner": "o", "name": "r", "branch": "b",
        "phase": "IDLE",
        "mystery_field_from_the_future": "???",
    }
    s = AgentState.from_dict(future)
    assert s.owner == "o"


def test_save_state_is_atomic(tmp_path, monkeypatch):
    """A concurrent reader should never see a half-written state file."""
    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path)
    s = AgentState(repo_url="u", owner="o", name="r", branch="b")
    state_module.save_state(s)

    path = tmp_path / "o__r.json"
    assert path.exists()
    # Must be valid JSON (i.e., fully written)
    data = json.loads(path.read_text())
    assert data["owner"] == "o"


# ── Process liveness helper ──────────────────────────────────────────────────


def test_pid_alive_returns_false_for_none_and_zero():
    assert web_module._pid_alive(None) is False
    assert web_module._pid_alive(0) is False
    assert web_module._pid_alive(-1) is False


def test_pid_alive_detects_own_process():
    import os as _os
    assert web_module._pid_alive(_os.getpid()) is True


def test_pid_alive_returns_false_for_bogus_pid():
    # PIDs above 2^22 aren't issued by any sane kernel
    assert web_module._pid_alive(999_999_999) is False


# ── Orphan classification ────────────────────────────────────────────────────


def _iso(dt):
    return dt.isoformat()


def test_enrich_marks_stale_non_terminal_run_as_orphaned():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    snapshot = {
        "phase": "WORKING",
        "pid": 999_999_999,           # dead
        "last_heartbeat": _iso(old),  # ancient
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True
    assert enriched["is_alive"] is False
    assert enriched["heartbeat_age_s"] is not None


def test_enrich_does_not_mark_completed_run_as_orphaned():
    snapshot = {
        "phase": "DONE",
        "pid": 999_999_999,
        "last_heartbeat": _iso(datetime.now(timezone.utc) - timedelta(days=7)),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is False


def test_enrich_with_fresh_heartbeat_is_alive_not_orphaned():
    import os as _os
    snapshot = {
        "phase": "WORKING",
        "pid": _os.getpid(),  # we're alive
        "last_heartbeat": _iso(datetime.now(timezone.utc)),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_alive"] is True
    assert enriched["is_orphaned"] is False
    assert enriched["heartbeat_age_s"] < 5


def test_enrich_no_pid_no_heartbeat_is_orphaned_if_non_terminal():
    """A state written before we added heartbeat (pid=None, heartbeat="")
    in a non-terminal phase must be flagged as orphaned — that's exactly
    the 'run stuck for 5h' scenario we're fixing."""
    snapshot = {"phase": "WORKING"}
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True


def test_enrich_idle_phase_without_heartbeat_is_orphaned():
    """Even IDLE counts as non-terminal — if a run was kicked off but never
    advanced and its process is dead, it's still an orphan."""
    snapshot = {"phase": "IDLE", "pid": 999_999_999, "last_heartbeat": ""}
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True


def test_enrich_pr_open_with_exit_clean_is_not_orphaned():
    """A `gitoma run` that reaches PR_OPEN exits cleanly by design — the
    user will continue manually with `gitoma review`. The heartbeat thread
    dies with the process. Without the `exit_clean` flag, the cockpit
    would falsely flag this as orphaned.

    Regression guard for the screenshot scenario: PR #5 opened, phase
    PR_OPEN, heartbeat 32s stale, pid dead → must NOT be orphan.
    """
    snapshot = {
        "phase": "PR_OPEN",
        "pid": 999_999_999,
        "last_heartbeat": "2026-04-21T04:00:00+00:00",  # very stale
        "exit_clean": True,
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is False


def test_enrich_exit_clean_false_still_flags_real_orphans():
    """Defense in depth: a crashed run that somehow has exit_clean absent
    or False must still be flagged."""
    snapshot = {
        "phase": "WORKING",
        "pid": 999_999_999,
        "last_heartbeat": "2026-04-21T04:00:00+00:00",
        "exit_clean": False,
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True
