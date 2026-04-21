"""Base analyzer ABC and MetricResult / MetricReport dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ScoreStatus = Literal["pass", "warn", "fail"]

SCORE_THRESHOLDS = {"pass": 0.75, "warn": 0.4}  # ≥0.75=pass, ≥0.4=warn, else fail


def score_to_status(score: float) -> ScoreStatus:
    if score >= SCORE_THRESHOLDS["pass"]:
        return "pass"
    elif score >= SCORE_THRESHOLDS["warn"]:
        return "warn"
    return "fail"


def score_to_bar(score: float, width: int = 8) -> str:
    """ASCII progress bar for score (0-1)."""
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


@dataclass
class MetricResult:
    name: str                          # internal id e.g. "ci"
    display_name: str                  # human label e.g. "CI/CD Pipeline"
    score: float                       # 0.0 – 1.0
    status: ScoreStatus
    details: str                       # one-line summary
    suggestions: list[str] = field(default_factory=list)
    weight: float = 1.0                # importance weight for overall score

    @classmethod
    def from_score(
        cls,
        name: str,
        display_name: str,
        score: float,
        details: str,
        suggestions: list[str] | None = None,
        weight: float = 1.0,
    ) -> "MetricResult":
        return cls(
            name=name,
            display_name=display_name,
            score=round(score, 3),
            status=score_to_status(score),
            details=details,
            suggestions=suggestions or [],
            weight=weight,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "score": self.score,
            "status": self.status,
            "details": self.details,
            "suggestions": self.suggestions,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetricResult":
        """Inverse of ``to_dict`` — used by ``--resume`` to rehydrate a
        prior run's metric report from disk so the planner / PR agent
        don't need to re-run the analyzers.

        Tolerates missing fields (older state files predate ``weight``)
        by falling back to the dataclass defaults; ignores unknown keys
        for forward-compat.
        """
        return cls(
            name=d.get("name", ""),
            display_name=d.get("display_name", ""),
            score=float(d.get("score", 0.0)),
            status=d.get("status", "fail"),
            details=d.get("details", ""),
            suggestions=list(d.get("suggestions", [])),
            weight=float(d.get("weight", 1.0)),
        )


@dataclass
class MetricReport:
    repo_url: str
    owner: str
    name: str
    languages: list[str]
    default_branch: str
    metrics: list[MetricResult]
    analyzed_at: str

    @property
    def overall_score(self) -> float:
        if not self.metrics:
            return 0.0
        total_weight = sum(m.weight for m in self.metrics)
        weighted = sum(m.score * m.weight for m in self.metrics)
        return round(weighted / total_weight, 3)

    @property
    def failing(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == "fail"]

    @property
    def warning(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == "warn"]

    @property
    def passing(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == "pass"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_url": self.repo_url,
            "owner": self.owner,
            "name": self.name,
            "languages": self.languages,
            "default_branch": self.default_branch,
            "overall_score": self.overall_score,
            "analyzed_at": self.analyzed_at,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetricReport":
        """Inverse of ``to_dict`` — see :meth:`MetricResult.from_dict`."""
        return cls(
            repo_url=d.get("repo_url", ""),
            owner=d.get("owner", ""),
            name=d.get("name", ""),
            languages=list(d.get("languages", [])),
            default_branch=d.get("default_branch", "main"),
            metrics=[MetricResult.from_dict(m) for m in d.get("metrics", [])],
            analyzed_at=d.get("analyzed_at", ""),
        )


class BaseAnalyzer(ABC):
    """All analyzers extend this ABC."""

    name: str = ""
    display_name: str = ""
    weight: float = 1.0

    def __init__(self, root: Path, languages: list[str]) -> None:
        self.root = root
        self.languages = languages

    @abstractmethod
    def analyze(self) -> MetricResult:
        """Run analysis and return a MetricResult."""
        ...

    # ── Helpers ─────────────────────────────────────────────────────────────

    def file_exists(self, *paths: str) -> str | None:
        """Return the first existing path, or None."""
        for p in paths:
            if (self.root / p).exists():
                return p
        return None

    def glob_count(self, pattern: str) -> int:
        return len(list(self.root.glob(pattern)))

    def read(self, path: str) -> str | None:
        p = self.root / path
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
        return None

    def all_files(self, *exts: str) -> list[Path]:
        """Return all files matching given extensions (skip .git)."""
        results: list[Path] = []
        for path in self.root.rglob("*"):
            if ".git" in path.parts:
                continue
            if path.is_file() and (not exts or path.suffix.lower() in exts):
                results.append(path)
        return results
