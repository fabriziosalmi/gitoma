"""Task and SubTask dataclasses with full serialization support."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

TaskStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]
SubTaskAction = Literal["create", "modify", "delete", "verify"]


@dataclass
class SubTask:
    id: str
    title: str
    description: str
    file_hints: list[str]
    action: SubTaskAction = "modify"
    status: TaskStatus = "pending"
    commit_sha: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubTask":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Task:
    id: str
    title: str
    priority: int                 # 1 = highest
    metric: str                   # which metric this task addresses
    description: str
    subtasks: list[SubTask] = field(default_factory=list)
    status: TaskStatus = "pending"

    @property
    def completed_subtasks(self) -> int:
        return sum(1 for s in self.subtasks if s.status == "completed")

    @property
    def total_subtasks(self) -> int:
        return len(self.subtasks)

    @property
    def progress(self) -> float:
        if not self.subtasks:
            return 0.0
        return self.completed_subtasks / self.total_subtasks

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["subtasks"] = [s.to_dict() for s in self.subtasks]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        subtasks = [SubTask.from_dict(s) for s in d.pop("subtasks", [])]
        return cls(subtasks=subtasks, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskPlan:
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    overall_score_before: float = 0.0
    llm_model: str = ""
    # Worker model, when split-topology is in use (LM_STUDIO_WORKER_MODEL
    # set). Empty when planner and worker share the same model — the
    # PR template + diary then show a single name. Surfaces tonight's
    # mm1+qwen3-8b planner + mm2+qwen3.5-9b worker setup so PRs
    # accurately attribute who-did-what.
    worker_model: str = ""
    # Reviewer model, when 3-way split-topology is in use
    # (LM_STUDIO_REVIEW_MODEL set). Same shape as worker_model.
    # Empty when reviewer falls back to the planner client.
    review_model: str = ""
    # Reviewer ENSEMBLE members + agreement floor (2026-05-02). When
    # populated (len >= 2), PHASE 5 fanned out across N reviewers and
    # the PR template renders the ensemble shape instead of the
    # single ``review_model``. Empty list means no ensemble (solo or
    # planner-fallback path). ``review_min_agree`` 0 means "n/a".
    review_models: list[str] = field(default_factory=list)
    review_min_agree: int = 0

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)

    @property
    def total_subtasks(self) -> int:
        return sum(t.total_subtasks for t in self.tasks)

    @property
    def completed_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.status == "completed")

    @property
    def pending_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status in ("pending", "in_progress")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "overall_score_before": self.overall_score_before,
            "llm_model": self.llm_model,
            "worker_model": self.worker_model,
            "review_model": self.review_model,
            "review_models": list(self.review_models),
            "review_min_agree": self.review_min_agree,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskPlan":
        tasks = [Task.from_dict(t) for t in d.pop("tasks", [])]
        return cls(tasks=tasks, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
