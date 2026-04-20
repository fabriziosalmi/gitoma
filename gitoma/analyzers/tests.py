"""Test coverage and test suite analyzer."""

from __future__ import annotations


from gitoma.analyzers.base import BaseAnalyzer, MetricResult


class TestsAnalyzer(BaseAnalyzer):
    name = "tests"
    display_name = "Test Suite"
    weight = 1.4

    PYTHON_TEST_PATTERNS = ["test_*.py", "*_test.py", "tests/**/*.py"]
    GO_TEST_DIRS = ["*_test.go"]
    RUST_TEST_MARKER = "#[test]"
    JS_CONFIG_FILES = ["jest.config.js", "jest.config.ts", "vitest.config.ts", "vitest.config.js"]

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        details_parts: list[str] = []

        # ── Python ─────────────────────────────────────────────────────────
        if "Python" in self.languages:
            py_test_files = (
                list(self.root.rglob("test_*.py")) + list(self.root.rglob("*_test.py"))
            )
            py_test_files = [f for f in py_test_files if ".git" not in f.parts]

            if py_test_files:
                score += 0.35
                details_parts.append(f"Python: {len(py_test_files)} test file(s)")
                # pytest config
                if self.file_exists("pytest.ini", "setup.cfg", "pyproject.toml"):
                    # Check if pytest is mentioned
                    for cfg in ["pytest.ini", "pyproject.toml", "setup.cfg"]:
                        content = self.read(cfg) or ""
                        if "pytest" in content:
                            score += 0.10
                            details_parts.append("pytest configured")
                            break
                # coverage config
                if self.file_exists(".coveragerc") or any(
                    "coverage" in (self.read(f) or "") for f in ["pyproject.toml", "setup.cfg"]
                ):
                    score += 0.05
                    details_parts.append("coverage config")
                else:
                    suggestions.append("Add .coveragerc or [coverage] section to pyproject.toml")
            else:
                suggestions.append("No Python test files found. Create tests/ with pytest test files")

        # ── Go ─────────────────────────────────────────────────────────────
        if "Go" in self.languages:
            go_test_files = [
                f for f in self.root.rglob("*_test.go") if ".git" not in f.parts
            ]
            if go_test_files:
                score += 0.35
                details_parts.append(f"Go: {len(go_test_files)} test file(s)")
            else:
                suggestions.append("No Go test files found (*_test.go). Add tests for each package")

        # ── Rust ───────────────────────────────────────────────────────────
        if "Rust" in self.languages:
            rust_files = [f for f in self.root.rglob("*.rs") if ".git" not in f.parts]
            has_tests = any(
                self.RUST_TEST_MARKER in (f.read_text(errors="replace"))
                for f in rust_files
            )
            if has_tests:
                score += 0.35
                details_parts.append("Rust: #[test] blocks found")
            else:
                suggestions.append("No #[test] blocks found in Rust files. Add unit tests")

        # ── JS / TS ────────────────────────────────────────────────────────
        if "JavaScript" in self.languages or "TypeScript" in self.languages:
            js_cfg = self.file_exists(*self.JS_CONFIG_FILES)
            js_test_files = (
                list(self.root.rglob("*.test.js"))
                + list(self.root.rglob("*.test.ts"))
                + list(self.root.rglob("*.spec.js"))
                + list(self.root.rglob("*.spec.ts"))
            )
            js_test_files = [f for f in js_test_files if ".git" not in f.parts]

            if js_test_files:
                score += 0.35
                details_parts.append(f"JS/TS: {len(js_test_files)} test file(s)")
            elif js_cfg:
                score += 0.10
                details_parts.append("Jest/Vitest configured (no tests yet)")
                suggestions.append("Jest/Vitest is configured but no test files found")
            else:
                suggestions.append(
                    "No JS/TS tests found. Add jest.config.js and *.test.{js,ts} files"
                )

        # ── Generic test dirs ──────────────────────────────────────────────
        if not score:
            test_dir = self.file_exists("tests", "test", "__tests__", "spec")
            if test_dir:
                score = 0.25
                details_parts.append(f"Test dir: {test_dir}/")
                suggestions.append("Test directory found but no recognized test files")

        if not details_parts:
            details = "No test infrastructure detected"
        else:
            details = ", ".join(details_parts)

        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
