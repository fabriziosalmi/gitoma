"""Agent state machine — persists phase + progress to ~/.gitoma/state/."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".gitoma" / "state"


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
    """
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


def acquire_run_lock(owner: str, name: str) -> tuple[bool, int | None]:
    """Try to grab the lock for (owner, name).

    Returns ``(True, None)`` on success. On failure, returns
    ``(False, existing_pid)`` so callers can tell the user who's holding
    it. A stale lock (whose PID is no longer alive) is silently taken
    over — otherwise a crash would leave you locked out forever.
    """
    path = _lock_path(owner, name)
    my_pid = os.getpid()

    # O_EXCL → atomic create. If it exists, we race-check the stale case.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            existing_raw = path.read_text().strip()
            existing = int(existing_raw) if existing_raw else -1
        except (OSError, ValueError):
            existing = -1
        if existing > 0 and _pid_alive(existing) and existing != my_pid:
            return False, existing
        # Stale — take it over by rewriting.
        try:
            path.write_text(str(my_pid))
        except OSError:
            return False, existing if existing > 0 else None
        return True, None

    with os.fdopen(fd, "w") as f:
        f.write(str(my_pid))
    return True, None


def release_run_lock(owner: str, name: str) -> None:
    """Release the lock if we own it. Never raises."""
    path = _lock_path(owner, name)
    my_pid = os.getpid()
    try:
        existing = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    if existing == my_pid:
        try:
            path.unlink()
        except OSError:
            pass
