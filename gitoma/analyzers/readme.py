"""README quality analyzer."""

from __future__ import annotations

import re

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


class ReadmeAnalyzer(BaseAnalyzer):
    name = "readme"
    display_name = "README Quality"
    weight = 1.2

    SECTIONS = ["install", "usage", "contributing", "license", "example", "feature"]
    BADGE_RE = re.compile(r"!\[.*?\]\(https?://", re.IGNORECASE)

    def analyze(self) -> MetricResult:
        readme_path = self.file_exists(
            "README.md", "README.rst", "README.txt", "readme.md", "Readme.md"
        )
        if not readme_path:
            return MetricResult.from_score(
                self.name, self.display_name, 0.0,
                "No README file found",
                ["Create a README.md with install, usage, and contributing sections"],
                self.weight,
            )

        content = self.read(readme_path) or ""
        content_lower = content.lower()
        lines = content.split("\n")

        score = 0.0
        suggestions: list[str] = []

        # Presence: 0.20 pts
        score += 0.20

        # Length (meaningful content): 0.15 pts
        non_empty = [line for line in lines if line.strip()]
        if len(non_empty) >= 50:
            score += 0.15
        elif len(non_empty) >= 20:
            score += 0.08
            suggestions.append("Expand README — it's quite short (< 20 meaningful lines)")
        else:
            suggestions.append("README is very thin. Add sections: Install, Usage, Contributing")

        # Key sections: 0.40 pts
        found_sections = [s for s in self.SECTIONS if s in content_lower]
        section_score = (len(found_sections) / len(self.SECTIONS)) * 0.40
        score += section_score
        missing = [s.capitalize() for s in self.SECTIONS if s not in content_lower]
        if missing:
            suggestions.append(f"Add missing sections: {', '.join(missing)}")

        # Badges: 0.15 pts
        badges = self.BADGE_RE.findall(content)
        if badges:
            score += 0.15
        else:
            suggestions.append("Add status badges (CI, coverage, version) to the README header")

        # H1 title: 0.10 pts
        if any(line.startswith("# ") for line in lines):
            score += 0.10
        else:
            suggestions.append("Add an H1 title (# Project Name) at the top of the README")

        details = (
            f"Found {len(found_sections)}/{len(self.SECTIONS)} key sections, "
            f"{len(badges)} badges, {len(non_empty)} lines"
        )
        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
