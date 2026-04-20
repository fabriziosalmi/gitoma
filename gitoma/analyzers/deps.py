"""Dependency freshness and manifest analyzer."""

from __future__ import annotations

import json
import re

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


class DepsAnalyzer(BaseAnalyzer):
    name = "deps"
    display_name = "Dependencies"
    weight = 1.0

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        details_parts: list[str] = []

        # ── Python ─────────────────────────────────────────────────────────
        if "Python" in self.languages:
            has_pyproject = self.file_exists("pyproject.toml")
            has_requirements = self.file_exists("requirements.txt", "requirements/base.txt")
            has_pipfile = self.file_exists("Pipfile")
            has_poetry = self.file_exists("poetry.lock")
            has_uv = self.file_exists("uv.lock")

            if has_pyproject:
                score += 0.25
                details_parts.append("pyproject.toml")
                if has_uv:
                    score += 0.10
                    details_parts.append("uv.lock")
                elif has_poetry:
                    score += 0.05
                    details_parts.append("poetry.lock")
            elif has_requirements:
                score += 0.15
                details_parts.append("requirements.txt")
                # Check for pinned versions
                req = self.read(has_requirements) or ""
                pinned = len(re.findall(r"==", req))
                if pinned > 0:
                    suggestions.append(
                        f"requirements.txt has {pinned} pinned deps (==). "
                        "Consider ranges (>=) for libraries, pin only for apps"
                    )
            elif has_pipfile:
                score += 0.12
                details_parts.append("Pipfile")
            else:
                suggestions.append(
                    "No Python dependency manifest found. "
                    "Add pyproject.toml or requirements.txt"
                )

        # ── Go ─────────────────────────────────────────────────────────────
        if "Go" in self.languages:
            if self.file_exists("go.mod"):
                score += 0.25
                details_parts.append("go.mod")
                if self.file_exists("go.sum"):
                    score += 0.05
                    details_parts.append("go.sum")
            else:
                suggestions.append("Missing go.mod — run go mod init to initialize Go modules")

        # ── Rust ───────────────────────────────────────────────────────────
        if "Rust" in self.languages:
            if self.file_exists("Cargo.toml"):
                score += 0.25
                details_parts.append("Cargo.toml")
                if self.file_exists("Cargo.lock"):
                    # Lock file in lib = bad, in bin = good
                    cargo = self.read("Cargo.toml") or ""
                    if "[[bin]]" in cargo or "[[example]]" in cargo:
                        score += 0.05
                        details_parts.append("Cargo.lock (binary)")
                    else:
                        # Library — Cargo.lock should NOT be committed
                        if ".gitignore" and "Cargo.lock" in (self.read(".gitignore") or ""):
                            score += 0.05
                        else:
                            suggestions.append(
                                "For Rust libraries, add Cargo.lock to .gitignore"
                            )
            else:
                suggestions.append("Missing Cargo.toml — not a valid Rust project?")

        # ── JS / TS ────────────────────────────────────────────────────────
        if "JavaScript" in self.languages or "TypeScript" in self.languages:
            pkg = self.file_exists("package.json")
            if pkg:
                score += 0.20
                details_parts.append("package.json")
                pkg_content = self.read(pkg) or "{}"
                try:
                    pkg_json = json.loads(pkg_content)
                    # Check for engines field
                    if "engines" in pkg_json:
                        score += 0.05
                        details_parts.append("engines field")
                    else:
                        suggestions.append("Add 'engines' field to package.json to specify Node.js version requirements")
                except Exception:
                    pass

                lockfile = self.file_exists("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")
                if lockfile:
                    score += 0.05
                    details_parts.append(lockfile)
                else:
                    suggestions.append("Commit a lockfile (package-lock.json / yarn.lock) for reproducible builds")
            else:
                suggestions.append("Missing package.json for JS/TS project")

        if not details_parts:
            details = "No dependency manifests found"
        else:
            details = ", ".join(details_parts)

        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
