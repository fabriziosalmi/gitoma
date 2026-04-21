"""Agent state machine — persists phase + progress to ~/.gitoma/state/."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".gitoma" / "state"

# Serialize ``save_state`` writes within a single process. Both the CLI
# main thread and the heartbeat daemon mutate the same ``AgentState``
# object and call ``save_state`` from different threads. ``os.replace``
# already gives us a torn-write-free file at the FS layer, but two
# concurrent writers can still snapshot the dataclass at slightly
# different points in a multi-field mutation, leaving a brief on-disk
# window where one writer's update isn't visible. A short lock around
# the dataclass→JSON serialization removes that window — held only for
# microseconds, contended at most a few times per minute.
_SAVE_STATE_LOCK = threading.Lock()


class AgentPhase(str, Enum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    WORKING = "WORKING"
    PR_OPEN = "PR_OPEN"
    REVIEWING = "REVIEWING"
    DONE = "DONE"


@dataclass
class AgentState:
    repo_url: str
    owner: str
    name: str
    branch: str
    phase: str = AgentPhase.IDLE
    started_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    metric_report: dict[str, Any] | None = None
    task_plan: dict[str, Any] | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    current_task_id: str | None = None
    current_subtask_id: str | None = None
    # Human-readable description of what the agent is doing RIGHT NOW.
    # Fills the gaps between coarse-grained `phase` transitions (e.g. the
    # silent period between "last subtask committed" and "PR opened"), so
    # the cockpit always has a sentence to show.
    current_operation: str = ""
    errors: list[str] = field(default_factory=list)

    # Liveness fields — let observers (cockpit, `gitoma doctor --runs`)
    # distinguish a run that's actively progressing from one whose CLI
    # died without a chance to persist a terminal state.
    # `pid` is the OS PID of the CLI process owning this run.
    # `last_heartbeat` is refreshed by a daemon thread every ~30 s, so a
    # stale timestamp + dead PID means the process crashed / was killed.
    pid: int | None = None
    last_heartbeat: str = ""
    # `exit_clean` is set to True by the CLI's heartbeat context on normal
    # completion (including successful typer.Exit(0)). Without it, a run
    # that finished at PR_OPEN — perfectly valid, the user will continue
    # with `gitoma review` later — would be flagged as orphaned the
    # moment the CLI exited, because phase PR_OPEN is non-terminal and
    # the heartbeat stops. With it, orphan detection can differentiate
    # "CLI died unexpectedly" from "CLI finished its scope cleanly".
    exit_clean: bool = False

    # Critic panel observability (M7). Populated only when
    # ``Config.critic_panel.mode != "off"``. Each entry in
    # ``critic_panel_findings_log`` is one panel run for one subtask:
    #   {
    #     "subtask_id": "T001-S02",
    #     "ts": "2026-04-22T12:34:56Z",
    #     "personas_called": ["dev"],
    #     "findings": [{"persona": "dev", "severity": "...", "category":"...", "summary":"..."}],
    #     "verdict": "advisory_logged" | "refined_accepted" | "refined_rejected" | "no_op",
    #     "tokens_extra": {"prompt": int, "completion": int},
    #   }
    # Kept in state so the cockpit can render a "critic activity" panel
    # without having to re-parse trace files. Capped to last 200 entries
    # per run so a long run doesn't grow state.json unboundedly.
    critic_panel_runs: int = 0
    critic_panel_findings_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentState":
        # Tolerate older state files that are missing fields we've added
        # later (pid, last_heartbeat, current_operation…) and ignore
        # unknown keys so a forward-compatible rollback doesn't crash.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    def advance(self, phase: AgentPhase) -> None:
        self.phase = phase
        self.updated_at = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(owner: str, name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{owner}__{name}.json"


def save_state(state: AgentState) -> None:
    """Persist state to disk atomically.

    The CLI (main thread) and the heartbeat daemon both call this, so a
    naive `write_text` can truncate the file mid-write and leave an
    observer with invalid JSON. We write to a sibling temp file and
    `os.replace` it over the real path — atomic on every POSIX filesystem
    (and on NTFS via MoveFileEx).

    A module-level ``threading.Lock`` serializes concurrent callers in
    the same process so the snapshot taken by ``state.to_dict()`` is
    always coherent with the JSON dump that immediately follows. Without
    it, a heartbeat tick firing between two related main-thread mutations
    could persist a ``state`` with one mutation visible and the other
    not — never inconsistent JSON, but a brief on-disk window where the
    cockpit shows an intermediate state. Held for microseconds; never
    blocks on I/O paths external to ``save_state``.
    """
    with _SAVE_STATE_LOCK:
        state.updated_at = _now()
        path = _state_path(state.owner, state.name)
        data = json.dumps(state.to_dict(), indent=2)
        # tempfile in the same dir so os.replace stays on-device.
        fd, tmp = tempfile.mkstemp(
            prefix=f".{state.slug}.", suffix=".json.tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:
            # Best-effort cleanup of the temp file; re-raise so callers see it.
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise


def load_state(owner: str, name: str) -> AgentState | None:
    """Load state from disk; returns None if not found."""
    path = _state_path(owner, name)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return AgentState.from_dict(data)


def delete_state(owner: str, name: str) -> None:
    """Remove state file (after DONE or reset)."""
    path = _state_path(owner, name)
    if path.exists():
        path.unlink()


def list_all_states() -> list[AgentState]:
    """Return all active agent states."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    states: list[AgentState] = []
    for p in STATE_DIR.glob("*.json"):
        try:
            states.append(AgentState.from_dict(json.loads(p.read_text())))
        except Exception:
            pass
    return states


# ── Concurrent-run lock ─────────────────────────────────────────────────────
#
# Prior implementation used ``O_EXCL`` + a stale-PID takeover branch that
# read the PID, checked liveness, and blind-rewrote the file. Two processes
# hitting the same stale lock could both pass the check and both write their
# PID — ending up convinced they each owned the lock.
#
# The kernel-held advisory lock below (``fcntl.flock``) makes that
# impossible: only one process can hold ``LOCK_EX`` at a time, and the
# kernel releases it automatically when the holding fd is closed (including
# when the process dies by any signal, OOM, or power loss). No "stale file"
# concept to race over. The PID written to the file is for UX only — it
# tells the blocked user *which PID is holding it* — not for correctness.

try:
    import fcntl  # POSIX only
    _HAS_FLOCK = True
except ImportError:  # pragma: no cover — Windows fallback
    _HAS_FLOCK = False

# Keep the lock fds alive for the lifetime of the process. Closing the fd
# releases the flock; we need it held until release_run_lock() runs.
_HELD_LOCKS: dict[tuple[str, str], int] = {}


def _lock_path(owner: str, name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{owner}__{name}.lock"


def _pid_alive(pid: int) -> bool:
    """True iff `pid` is a currently-running process on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def acquire_run_lock(owner: str, name: str) -> tuple[bool, int | None]:
    """Try to grab the lock for (owner, name).

    Returns ``(True, None)`` on success. On failure, returns
    ``(False, existing_pid)`` so callers can tell the user who's holding
    it. Kernel-held advisory lock: no stale-takeover race — when a holding
    process dies, the kernel drops the lock.
    """
    path = _lock_path(owner, name)
    key = (owner, name)
    my_pid = os.getpid()

    # Re-entrant: the same process asking again already owns it.
    if key in _HELD_LOCKS:
        return True, None

    if not _HAS_FLOCK:
        # Windows fallback — best-effort, not race-free. Callers should
        # serialize at a higher level on Windows if they need strict mutex.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = _read_pid(path)
            if existing and _pid_alive(existing) and existing != my_pid:
                return False, existing
            # Stale — unlink then retry once. If racing, loser fails.
            try:
                path.unlink()
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except (OSError, FileExistsError):
                return False, existing
        os.write(fd, str(my_pid).encode())
        _HELD_LOCKS[key] = fd
        return True, None

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Someone else holds it. Read their PID for UX (best-effort; the
        # holder writes PID *after* taking the lock so we may see an empty
        # or stale value — caller just uses None in that case).
        existing = _read_pid(path)
        os.close(fd)
        return False, existing
    except OSError:
        os.close(fd)
        return False, None

    # We hold the lock. Publish our PID for the UX of the next would-be
    # acquirer. Truncate first so we don't leave a stale tail from the
    # previous holder's PID when PIDs differ in digit count.
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(my_pid).encode())
    except OSError:
        # Couldn't write PID — lock is still ours per the kernel, but UX
        # will degrade. Not a correctness issue, so proceed.
        pass

    _HELD_LOCKS[key] = fd
    return True, None


def release_run_lock(owner: str, name: str) -> None:
    """Release the lock if we own it. Never raises.

    Closing the fd implicitly drops the flock. We also unlink the file so
    ``gitoma doctor --runs`` doesn't show a stale-looking lockfile after a
    clean exit; if the unlink races another acquirer, it'll just recreate.
    """
    key = (owner, name)
    fd = _HELD_LOCKS.pop(key, None)
    if fd is None:
        return
    if _HAS_FLOCK:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass
    path = _lock_path(owner, name)
    try:
        path.unlink()
    except OSError:
        pass
