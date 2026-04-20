"""Project structure — .gitignore, editorconfig, contributing, changelog, templates."""

from __future__ import annotations

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


class StructureAnalyzer(BaseAnalyzer):
    name = "structure"
    display_name = "Project Structure"
    weight = 1.0

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        found: list[str] = []

        checks = [
            (".gitignore", 0.15, ".gitignore"),
            ("CONTRIBUTING.md", 0.12, "CONTRIBUTING.md"),
            ("CHANGELOG.md", 0.10, "CHANGELOG.md"),
            (".editorconfig", 0.08, ".editorconfig"),
            (".pre-commit-config.yaml", 0.10, "pre-commit config"),
            (".github/ISSUE_TEMPLATE", 0.10, "Issue templates"),
            (".github/pull_request_template.md", 0.10, "PR template"),
            (".github/CODEOWNERS", 0.05, "CODEOWNERS"),
        ]

        for path, pts, label in checks:
            if self.file_exists(path):
                score += pts
                found.append(label)
            else:
                suggestions.append(f"Add {label} ({path})")

        # .gitignore quality
        gitignore = self.read(".gitignore") or ""
        if gitignore:
            lines = [l.strip() for l in gitignore.split("\n") if l.strip() and not l.startswith("#")]
            if len(lines) < 5:
                suggestions.append(".gitignore exists but has very few entries — use gitignore.io to generate a proper one")
            elif len(lines) >= 15:
                score += 0.10
                found.append(".gitignore quality")
            else:
                score += 0.05

        # Docker
        if self.file_exists("Dockerfile", "docker-compose.yml", "docker-compose.yaml"):
            score += 0.10
            found.append("Docker")

        details = f"Found: {', '.join(found)}" if found else "Minimal project structure"
        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
