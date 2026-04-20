"""Analyzer registry — runs all analyzers and produces MetricReport."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from gitoma.analyzers.base import MetricReport, MetricResult
from gitoma.analyzers.ci import CIAnalyzer
from gitoma.analyzers.code_quality import CodeQualityAnalyzer
from gitoma.analyzers.deps import DepsAnalyzer
from gitoma.analyzers.docs import DocsAnalyzer
from gitoma.analyzers.license import LicenseAnalyzer
from gitoma.analyzers.readme import ReadmeAnalyzer
from gitoma.analyzers.security import SecurityAnalyzer
from gitoma.analyzers.structure import StructureAnalyzer
from gitoma.analyzers.tests import TestsAnalyzer

ALL_ANALYZER_CLASSES = [
    ReadmeAnalyzer,
    CIAnalyzer,
    TestsAnalyzer,
    SecurityAnalyzer,
    CodeQualityAnalyzer,
    DepsAnalyzer,
    DocsAnalyzer,
    LicenseAnalyzer,
    StructureAnalyzer,
]


class AnalyzerRegistry:
    """Runs all registered analyzers against a cloned repo."""

    def __init__(
        self,
        root: Path,
        languages: list[str],
        repo_url: str,
        owner: str,
        name: str,
        default_branch: str,
    ) -> None:
        self.root = root
        self.languages = languages
        self.repo_url = repo_url
        self.owner = owner
        self.name = name
        self.default_branch = default_branch

    def run(
        self, on_progress: "Callable[[str, int, int], None] | None" = None
    ) -> MetricReport:
        """
        Run all analyzers.

        Args:
            on_progress: optional callback(analyzer_name, current_idx, total)
        """
        results: list[MetricResult] = []
        total = len(ALL_ANALYZER_CLASSES)

        for idx, AnalyzerCls in enumerate(ALL_ANALYZER_CLASSES):
            if on_progress:
                on_progress(AnalyzerCls.display_name, idx, total)
            try:
                analyzer = AnalyzerCls(root=self.root, languages=self.languages)
                result = analyzer.analyze()
            except Exception as e:
                result = MetricResult.from_score(
                    AnalyzerCls.name,
                    AnalyzerCls.display_name,
                    0.0,
                    f"Analyzer error: {e}",
                    [],
                    getattr(AnalyzerCls, "weight", 1.0),
                )
            results.append(result)

        return MetricReport(
            repo_url=self.repo_url,
            owner=self.owner,
            name=self.name,
            languages=self.languages,
            default_branch=self.default_branch,
            metrics=results,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )
