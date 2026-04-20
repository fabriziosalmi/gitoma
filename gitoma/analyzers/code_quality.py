"""Code quality analyzer — complexity, linting config."""

from __future__ import annotations

import ast
from pathlib import Path

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


def _python_complexity_score(root: Path) -> tuple[float, str]:
    """Compute average McCabe complexity for Python files (no radon dep)."""
    py_files = [
        f for f in root.rglob("*.py")
        if ".git" not in f.parts and "test" not in f.parts
    ]
    if not py_files:
        return 0.5, "No Python source files"

    total_funcs = 0
    complex_funcs = 0
    for path in py_files[:30]:  # sample max 30
        try:
            tree = ast.parse(path.read_text(errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    total_funcs += 1
                    # Rough complexity: count branches
                    branches = sum(
                        1 for n in ast.walk(node)
                        if isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.ExceptHandler,
                                          ast.With, ast.Assert, ast.comprehension))
                    )
                    if branches > 10:
                        complex_funcs += 1
        except Exception:
            pass

    if total_funcs == 0:
        return 0.5, "No functions found"

    ratio = complex_funcs / total_funcs
    score = max(0.0, 1.0 - ratio * 2)
    detail = f"{complex_funcs}/{total_funcs} complex functions (>10 branches)"
    return round(score, 2), detail


class CodeQualityAnalyzer(BaseAnalyzer):
    name = "code_quality"
    display_name = "Code Quality"
    weight = 1.1

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        details_parts: list[str] = []

        # ── Python ─────────────────────────────────────────────────────────
        if "Python" in self.languages:
            # Linting config
            has_ruff = self.file_exists("ruff.toml", ".ruff.toml") or (
                "ruff" in (self.read("pyproject.toml") or "")
            )
            has_flake = self.file_exists(".flake8", "setup.cfg") and (
                "flake8" in (self.read(".flake8") or self.read("setup.cfg") or "")
            )
            has_pylint = self.file_exists(".pylintrc", "pylintrc")
            has_mypy = self.file_exists(".mypy.ini", "mypy.ini") or "mypy" in (self.read("pyproject.toml") or "")

            if has_ruff:
                score += 0.20
                details_parts.append("ruff configured")
            elif has_flake:
                score += 0.12
                details_parts.append("flake8 configured")
            elif has_pylint:
                score += 0.10
                details_parts.append("pylint configured")
            else:
                suggestions.append("Add ruff for Python linting: pip install ruff && ruff check .")

            if has_mypy:
                score += 0.15
                details_parts.append("mypy configured")
            else:
                suggestions.append("Add mypy for static type checking (pyproject.toml [tool.mypy])")

            # Complexity
            cx_score, cx_detail = _python_complexity_score(self.root)
            score += cx_score * 0.25
            details_parts.append(cx_detail)

        # ── Go ─────────────────────────────────────────────────────────────
        if "Go" in self.languages:
            golangci = self.file_exists(".golangci.yml", ".golangci.yaml", ".golangci.toml")
            if golangci:
                score += 0.30
                details_parts.append("golangci-lint configured")
            else:
                suggestions.append("Add .golangci.yml for Go linting (golangci-lint)")
            # go.mod
            if self.file_exists("go.mod"):
                score += 0.10
                details_parts.append("go.mod present")

        # ── Rust ───────────────────────────────────────────────────────────
        if "Rust" in self.languages:
            clippy_toml = self.file_exists(".clippy.toml", "clippy.toml")
            rustfmt = self.file_exists("rustfmt.toml", ".rustfmt.toml")
            if clippy_toml:
                score += 0.20
                details_parts.append("clippy configured")
            else:
                suggestions.append("Add .clippy.toml for Rust linting configuration")
            if rustfmt:
                score += 0.10
                details_parts.append("rustfmt configured")
            else:
                suggestions.append("Add rustfmt.toml for consistent Rust formatting")

        # ── JS/TS ──────────────────────────────────────────────────────────
        if "JavaScript" in self.languages or "TypeScript" in self.languages:
            eslint = self.file_exists(
                ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                "eslint.config.js", "eslint.config.mjs",
            )
            prettier = self.file_exists(".prettierrc", ".prettierrc.json", "prettier.config.js")
            biome = self.file_exists("biome.json", "biome.jsonc")

            if biome:
                score += 0.30
                details_parts.append("Biome configured")
            elif eslint:
                score += 0.20
                details_parts.append("ESLint configured")
                if prettier:
                    score += 0.10
                    details_parts.append("Prettier configured")
                else:
                    suggestions.append("Add Prettier for consistent JS/TS formatting")
            else:
                suggestions.append("Add ESLint (.eslintrc.json) or Biome for JS/TS linting")

        # Pre-commit
        if self.file_exists(".pre-commit-config.yaml"):
            score += 0.10
            details_parts.append("pre-commit hooks")
        else:
            suggestions.append("Add .pre-commit-config.yaml to enforce quality on every commit")

        details = ", ".join(details_parts) if details_parts else "No linting/quality tooling found"
        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
