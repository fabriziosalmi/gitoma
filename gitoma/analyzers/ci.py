"""CI/CD pipeline analyzer."""

from __future__ import annotations


from gitoma.analyzers.base import BaseAnalyzer, MetricResult


class CIAnalyzer(BaseAnalyzer):
    name = "ci"
    display_name = "CI/CD Pipeline"
    weight = 1.5

    GH_ACTIONS_DIR = ".github/workflows"
    OTHER_CI_FILES = [
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "Jenkinsfile",
        ".travis.yml",
        "azure-pipelines.yml",
        "bitbucket-pipelines.yml",
        "codecov.yml",
        ".codecov.yml",
    ]

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        details_parts: list[str] = []

        # GitHub Actions workflows
        gha_dir = self.root / self.GH_ACTIONS_DIR
        workflow_files: list[str] = []
        if gha_dir.is_dir():
            workflow_files = [f.name for f in gha_dir.glob("*.yml")] + [
                f.name for f in gha_dir.glob("*.yaml")
            ]

        if workflow_files:
            score += 0.5
            details_parts.append(f"GH Actions: {len(workflow_files)} workflow(s)")

            # Check for test/lint/coverage workflows
            wf_content = " ".join(
                (self.root / self.GH_ACTIONS_DIR / f).read_text(errors="replace")
                for f in workflow_files
                if (self.root / self.GH_ACTIONS_DIR / f).exists()
            ).lower()

            if any(k in wf_content for k in ["pytest", "test", "jest", "cargo test", "go test"]):
                score += 0.2
                details_parts.append("tests in CI")
            else:
                suggestions.append("Add a test step to your CI workflow")

            if any(k in wf_content for k in ["coverage", "codecov", "coveralls"]):
                score += 0.1
                details_parts.append("coverage reporting")
            else:
                suggestions.append("Add coverage reporting (codecov, coveralls) to CI")

            if any(k in wf_content for k in ["lint", "ruff", "eslint", "clippy", "golangci"]):
                score += 0.1
                details_parts.append("linting in CI")
            else:
                suggestions.append("Add a linting step to your CI workflow")

            if any(k in wf_content for k in ["dependabot", "renovate"]):
                score += 0.1
                details_parts.append("dep updates")
        else:
            # Check other CI systems
            for ci_file in self.OTHER_CI_FILES:
                if self.file_exists(ci_file):
                    score += 0.4
                    details_parts.append(ci_file)
                    break
            else:
                suggestions.append(
                    "No CI/CD pipeline found. Create .github/workflows/ci.yml with "
                    "test + lint steps"
                )
                suggestions.append("Consider enabling Dependabot for automatic dependency updates")

        # Dependabot config
        if self.file_exists(".github/dependabot.yml", ".github/dependabot.yaml"):
            score += 0.1
            details_parts.append("Dependabot enabled")
        elif score > 0 and "Dependabot" not in str(suggestions):
            suggestions.append("Add .github/dependabot.yml to automate dependency updates")

        details = ", ".join(details_parts) if details_parts else "No CI/CD system detected"
        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
