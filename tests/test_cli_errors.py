"""Tests for the error-persistence contract of `_abort` + `_phase`.

These guard the UX fix: when a pipeline phase explodes, the cockpit must
see a non-silent failure (state.errors populated, current_operation set
to FAILED) instead of the previous behaviour where the state file was
frozen at the last successful checkpoint.
"""

from __future__ import annotations

import pytest
import typer

from gitoma.cli import _abort, _phase
from gitoma.core.state import AgentPhase, AgentState


def _fresh_state() -> AgentState:
    return AgentState(repo_url="u", owner="o", name="r", branch="b", phase=AgentPhase.WORKING)


def test_abort_without_state_still_exits(mocker):
    mocker.patch("gitoma.cli._helpers.save_state")
    with pytest.raises(typer.Exit) as exc:
        _abort("boom")
    assert exc.value.exit_code == 1


def test_abort_persists_error_to_state(mocker):
    save = mocker.patch("gitoma.cli._helpers.save_state")
    state = _fresh_state()

    with pytest.raises(typer.Exit):
        _abort("Git push failed: permission denied", hint="Fix token scope.", state=state)

    assert any("Git push failed" in e for e in state.errors)
    assert "FAILED" in state.current_operation
    save.assert_called_once_with(state)


def test_abort_does_not_lose_prior_errors(mocker):
    mocker.patch("gitoma.cli._helpers.save_state")
    state = _fresh_state()
    state.errors.append("already present")

    with pytest.raises(typer.Exit):
        _abort("new failure", state=state)

    assert state.errors[0] == "already present"
    assert any("new failure" in e for e in state.errors[1:])


def test_phase_persists_unhandled_exception(mocker):
    save = mocker.patch("gitoma.cli._helpers.save_state")
    mocker.patch("gitoma.cli._helpers._safe_cleanup")
    state = _fresh_state()

    with pytest.raises(typer.Exit):
        with _phase("PHASE 4 — PULL REQUEST", state=state):
            raise RuntimeError("boom")

    assert any("PHASE 4" in e and "boom" in e for e in state.errors)
    assert "FAILED in PHASE 4" in state.current_operation
    save.assert_called_once_with(state)


def test_phase_does_not_swallow_typer_exit(mocker):
    """`_abort` raises typer.Exit inside a phase — the phase must let it
    propagate verbatim (without wrapping or double-logging)."""
    mocker.patch("gitoma.cli._helpers.save_state")
    mocker.patch("gitoma.cli._helpers._safe_cleanup")
    state = _fresh_state()

    with pytest.raises(typer.Exit) as exc:
        with _phase("PHASE X", state=state):
            _abort("from inside", state=state)

    assert exc.value.exit_code == 1


def test_phase_persists_errors_even_without_state():
    """When no state is passed, the phase must still exit cleanly (legacy
    callers that don't pass state shouldn't crash)."""
    with pytest.raises(typer.Exit):
        with _phase("PHASE LEGACY"):
            raise ValueError("oops")
