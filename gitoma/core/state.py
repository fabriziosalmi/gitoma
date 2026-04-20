"""Agent state machine — persists phase + progress to ~/.gitoma/state/."""

from __future__ import annotations

import json
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

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentState":
        return cls(**d)

    def advance(self, phase: AgentPhase) -> None:
        self.phase = phase
        self.updated_at = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(owner: str, name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{owner}__{name}.json"


def save_state(state: AgentState) -> None:
    """Persist state to disk."""
    state.updated_at = _now()
    path = _state_path(state.owner, state.name)
    path.write_text(json.dumps(state.to_dict(), indent=2))


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
