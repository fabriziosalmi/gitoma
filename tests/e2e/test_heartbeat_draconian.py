"""Draconian tests for the run-observability stack.

These exist to prove the heartbeat + orphan + lock + atomic-save plumbing
survives the nasty cases:

* many threads racing on save_state
* concurrent readers catching mid-write
* heartbeat thread keeping its cadence despite transient save_state errors
* orphan classification at the exact TTL boundary
* malformed state files in the middle of the directory
* concurrent-run lock collisions (fresh vs stale PID)
* SIGKILL of a real CLI subprocess → orphan detected end-to-end

If any of these regress, the cockpit starts lying about what's happening
on the machine — so the bar has to be strict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from gitoma.api import web as web_module
from gitoma.core import state as state_module
from gitoma.core.state import (
    AgentPhase,
    AgentState,
    acquire_run_lock,
    release_run_lock,
    save_state,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(web_module, "STATE_DIR", tmp_path)
    return tmp_path


# ── Atomic save + concurrent writers ────────────────────────────────────────


def test_save_state_under_10_threads_never_produces_invalid_json(state_dir):
    """10 writer threads × 200 iterations. After the storm the file must be
    valid JSON and contain exactly one of the values written.

    Failure mode being guarded: a naive write_text lets reader/other writer
    observe a truncated file → json.JSONDecodeError in snapshots.
    """
    stop = threading.Event()
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        local = AgentState(
            repo_url="u",
            owner="stress",
            name="writer",
            branch="b",
            current_operation=f"writer-{idx}",
        )
        for _ in range(200):
            if stop.is_set():
                return
            try:
                save_state(local)
            except Exception as exc:  # pragma: no cover — should never hit
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    stop.set()

    assert not errors, f"writer raised: {errors!r}"

    final = json.loads((state_dir / "stress__writer.json").read_text())
    assert final["owner"] == "stress"
    # Every writer's current_operation matches the pattern "writer-N"
    assert final["current_operation"].startswith("writer-")


def test_concurrent_reader_never_sees_invalid_json(state_dir):
    """Writer spams save_state, reader loops on json.loads. Atomic rename
    means the reader only ever sees a fully-written snapshot — never the
    half-written temp file."""
    state = AgentState(repo_url="u", owner="race", name="reader", branch="b")
    save_state(state)
    path = state_dir / "race__reader.json"

    stop = threading.Event()
    bad_reads: list[str] = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            state.current_operation = f"tick-{i}"
            save_state(state)
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                bad_reads.append(str(exc))
                return
            except FileNotFoundError:
                continue

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start()
    r.start()
    time.sleep(0.5)
    stop.set()
    w.join(timeout=2.0)
    r.join(timeout=2.0)

    assert not bad_reads, f"reader caught partial state: {bad_reads!r}"


# ── Heartbeat resilience ────────────────────────────────────────────────────


def test_heartbeat_context_keeps_ticking_despite_save_failures(state_dir, mocker):
    """If save_state throws intermittently (disk pressure, NFS hiccup), the
    daemon must keep trying on the next tick instead of dying silently."""
    # The helpers module is where save_state is actually looked up by
    # _heartbeat — patching gitoma.cli would be a no-op since the function
    # resolves the symbol in its own module's namespace.
    from gitoma.cli import _helpers as helpers_module

    state = AgentState(repo_url="u", owner="flaky", name="disk", branch="b")

    call_count = {"n": 0}
    original_save = state_module.save_state

    def flaky_save(s):
        call_count["n"] += 1
        if call_count["n"] % 2 == 0:
            raise OSError("simulated transient failure")
        original_save(s)

    mocker.patch.object(helpers_module, "save_state", side_effect=flaky_save)
    mocker.patch.object(helpers_module, "_HEARTBEAT_INTERVAL_S", 0.05)

    with helpers_module._heartbeat(state):
        time.sleep(0.35)  # ~7 ticks expected; ~half will fail

    assert call_count["n"] >= 5, f"heartbeat stopped after {call_count['n']} ticks"
    # The context exit shouldn't raise even though half the ticks errored.


# ── Orphan classification boundaries ────────────────────────────────────────


def _iso(dt):
    return dt.isoformat()


def test_orphan_boundary_just_under_grace_is_alive():
    """heartbeat 89s old, PID alive → NOT orphaned (grace=90s)."""
    fresh = datetime.now(timezone.utc) - timedelta(seconds=89)
    snapshot = {
        "phase": "WORKING",
        "pid": os.getpid(),
        "last_heartbeat": _iso(fresh),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is False
    assert enriched["is_alive"] is True


def test_orphan_boundary_just_over_grace_but_pid_alive_is_orphan():
    """heartbeat 91s old, PID alive → orphaned (stale heartbeat is
    sufficient even when the PID is technically live, because PIDs can
    recycle)."""
    stale = datetime.now(timezone.utc) - timedelta(seconds=91)
    snapshot = {
        "phase": "WORKING",
        "pid": os.getpid(),
        "last_heartbeat": _iso(stale),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True


def test_orphan_boundary_dead_pid_but_fresh_heartbeat_is_orphan():
    """Heartbeat fresh but PID dead → orphaned. (The heartbeat must be
    owned by a live process; a dead PID wins over a fresh timestamp.)"""
    snapshot = {
        "phase": "WORKING",
        "pid": 999_999_999,  # dead
        "last_heartbeat": _iso(datetime.now(timezone.utc)),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is True


def test_orphan_ignores_terminal_phase_even_when_everything_stale():
    """DONE is DONE; we don't care that the owner process exited."""
    snapshot = {
        "phase": "DONE",
        "pid": 999_999_999,
        "last_heartbeat": _iso(datetime.now(timezone.utc) - timedelta(hours=24)),
    }
    enriched = web_module._enrich_liveness(snapshot)
    assert enriched["is_orphaned"] is False


# ── Malformed state files ───────────────────────────────────────────────────


def test_snapshot_states_survives_malformed_neighbors(state_dir):
    """A corrupt state file must not hide its valid peers from the cockpit."""
    good = AgentState(repo_url="u", owner="good", name="a", branch="b", phase=AgentPhase.WORKING)
    save_state(good)

    (state_dir / "broken__one.json").write_text("{not valid json at all")
    (state_dir / "empty__one.json").write_text("")

    snaps = web_module._snapshot_states()
    owners = {s.get("owner") for s in snaps}
    assert "good" in owners
    # The broken siblings are simply skipped, not re-raised.
    assert len(snaps) == 1


def test_from_dict_ignores_unknown_keys_and_keeps_known_ones():
    raw = {
        "repo_url": "u", "owner": "o", "name": "r", "branch": "b",
        "phase": "WORKING",
        "this_field_does_not_exist": [1, 2, 3],
        "neither_does_this": {"foo": "bar"},
    }
    s = AgentState.from_dict(raw)
    assert s.phase == "WORKING"
    # Round-tripping doesn't smuggle the unknown fields back out.
    assert "this_field_does_not_exist" not in s.to_dict()


# ── Concurrent-run lock ─────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="advisory flock is POSIX")
def test_lock_rejects_second_acquire_from_different_process(state_dir):
    """A peer CLI running concurrently must be refused, with the holder's
    pid surfaced for UX. Since the lock is now a kernel-held flock, we
    can't simulate "another process" by writing bytes into the file — a
    foreign PID in the lockfile does NOT mean foreign ownership. So we
    spawn a real subprocess that acquires and sleeps, then try to acquire
    from this process while the peer is alive.
    """
    helper = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["HOME"] = sys.argv[2]
from gitoma.core import state as st
st.STATE_DIR = __import__("pathlib").Path(sys.argv[3])
ok, _ = st.acquire_run_lock("lockowner", "repo")
print("PID", os.getpid(), "OK" if ok else "FAIL", flush=True)
time.sleep(30)
"""
    repo_root = str(__import__("pathlib").Path(__file__).resolve().parents[2])
    proc = subprocess.Popen(
        [sys.executable, "-c", helper, repo_root, str(state_dir), str(state_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Wait until the peer has acquired.
        line = proc.stdout.readline().strip() if proc.stdout else ""
        assert "OK" in line, f"peer didn't acquire: {line!r}"
        peer_pid = int(line.split()[1])

        ok2, holder = acquire_run_lock("lockowner", "repo")
        assert ok2 is False
        assert holder == peer_pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="advisory flock is POSIX")
def test_lock_taken_over_when_previous_holder_died(state_dir):
    """When the prior holder dies (SIGKILL / crash / power loss), the
    kernel drops the flock. The next acquire must succeed — otherwise a
    crash would permanently lock out the repo."""
    helper = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from gitoma.core import state as st
st.STATE_DIR = __import__("pathlib").Path(sys.argv[2])
ok, _ = st.acquire_run_lock("ghost", "repo")
print("OK" if ok else "FAIL", flush=True)
time.sleep(300)
"""
    repo_root = str(__import__("pathlib").Path(__file__).resolve().parents[2])
    proc = subprocess.Popen(
        [sys.executable, "-c", helper, repo_root, str(state_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    assert "OK" in proc.stdout.readline().strip()

    # SIGKILL — no chance to release_run_lock. Kernel must drop the flock.
    proc.kill()
    proc.wait(timeout=5)

    ok, _ = acquire_run_lock("ghost", "repo")
    assert ok is True
    assert (state_dir / "ghost__repo.lock").read_text() == str(os.getpid())
    release_run_lock("ghost", "repo")


def test_lock_is_reentrant_within_same_process(state_dir):
    """Back-to-back acquires from the same process must both succeed —
    a legitimate caller that forgot to release once shouldn't deadlock
    itself forever on retry."""
    ok1, _ = acquire_run_lock("reentrant", "repo")
    ok2, _ = acquire_run_lock("reentrant", "repo")
    assert ok1 is True
    assert ok2 is True
    release_run_lock("reentrant", "repo")


def test_lock_ignores_foreign_pid_in_file(state_dir):
    """PID written to the lockfile is UX-only (so a blocked user knows
    who's holding the lock). It is NOT ownership data — writing a random
    PID into the file must not grant or block ownership."""
    # Pre-stamp a random live PID into the lockfile. Without a real flock
    # holder, we must still be able to acquire.
    (state_dir / "foreign__repo.lock").write_text(str(os.getppid()))
    ok, _ = acquire_run_lock("foreign", "repo")
    assert ok is True
    # And after acquiring, the file must reflect *our* PID.
    assert (state_dir / "foreign__repo.lock").read_text() == str(os.getpid())
    release_run_lock("foreign", "repo")


# ── End-to-end: real subprocess + SIGKILL → orphan detected ─────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics")
def test_subprocess_sigkill_produces_orphan_state(tmp_path, monkeypatch):
    """Spin up a tiny Python program that behaves like the CLI (writes a
    state file with pid + initial heartbeat, then sleeps forever). SIGKILL
    it and verify the enricher flags the state as orphaned."""
    state_file = tmp_path / "kill__test.json"

    # Program: write its own PID + heartbeat, then sleep forever.
    # Pass the state file path via argv so we avoid any literal-escaping issues.
    code = r"""
import datetime, json, os, sys, time
path = sys.argv[1]
payload = {
    "repo_url": "u",
    "owner": "kill",
    "name": "test",
    "branch": "b",
    "phase": "WORKING",
    "started_at": "2026-04-21T10:00:00+00:00",
    "updated_at": "2026-04-21T10:00:00+00:00",
    "pid": os.getpid(),
    "last_heartbeat": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open(path, "w") as f:
    json.dump(payload, f)
time.sleep(3600)
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(state_file)])
    try:
        # Wait for the state file to appear + contain the pid.
        for _ in range(30):
            if state_file.exists():
                data = json.loads(state_file.read_text())
                if data.get("pid"):
                    break
            time.sleep(0.1)
        else:
            pytest.fail("subprocess never wrote state")

        # Hard kill. atexit handlers DON'T run — this is the scenario we
        # designed the orphan detection for.
        proc.kill()
        proc.wait(timeout=5.0)

        # Point the enricher at the now-orphaned state.
        monkeypatch.setattr(web_module, "STATE_DIR", tmp_path)

        # The heartbeat is still "fresh" (written seconds ago) but the PID
        # is definitively dead after wait() returns. The dead-PID branch
        # must fire immediately — no waiting for the 90s grace window.
        enriched = web_module._enrich_liveness(json.loads(state_file.read_text()))
        assert enriched["is_alive"] is False
        assert enriched["is_orphaned"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
