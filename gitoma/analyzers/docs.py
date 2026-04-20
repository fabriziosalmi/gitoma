"""Documentation / docstring coverage analyzer."""

from __future__ import annotations

import ast
from pathlib import Path

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


def _python_docstring_ratio(root: Path) -> tuple[float, str]:
    """Compute ratio of public functions/classes that have docstrings."""
    py_files = [
        f for f in root.rglob("*.py")
        if ".git" not in f.parts and "test" not in f.name
    ]
    if not py_files:
        return 0.5, "No Python source files"

    total = 0
    documented = 0
    for path in py_files[:20]:
        try:
            tree = ast.parse(path.read_text(errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                    if name.startswith("_"):
                        continue  # skip private
                    total += 1
                    if (
                        node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)
                    ):
                        documented += 1
        except Exception:
            pass

    if total == 0:
        return 0.5, "No public functions/classes found"

    ratio = documented / total
    detail = f"{documented}/{total} public symbols have docstrings ({ratio:.0%})"
    return round(ratio, 2), detail


class DocsAnalyzer(BaseAnalyzer):
    name = "docs"
    display_name = "Documentation"
    weight = 0.9

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        details_parts: list[str] = []

        # ── Docstring coverage (Python) ────────────────────────────────────
        if "Python" in self.languages:
            doc_ratio, doc_detail = _python_docstring_ratio(self.root)
            score += doc_ratio * 0.40
            details_parts.append(doc_detail)
            if doc_ratio < 0.5:
                suggestions.append(
                    f"Only {doc_ratio:.0%} of public functions have docstrings. "
                    "Add Google-style or NumPy-style docstrings"
                )

        # ── Docs directory / site ──────────────────────────────────────────
        docs_dir = self.file_exists("docs", "doc", "documentation", "wiki")
        if docs_dir:
            score += 0.20
            details_parts.append(f"{docs_dir}/ directory")
        else:
            suggestions.append("Add a docs/ directory or GitHub Wiki for project documentation")

        # ── MkDocs / Sphinx / Docusaurus ───────────────────────────────────
        doc_tool = self.file_exists("mkdocs.yml", "mkdocs.yaml", "conf.py", "docusaurus.config.js")
        if doc_tool:
            score += 0.15
            details_parts.append(f"doc tool: {doc_tool}")
        else:
            suggestions.append(
                "Consider adding MkDocs (mkdocs.yml) or Sphinx for auto-generated docs"
            )

        # ── Go / Rust / JS docs ────────────────────────────────────────────
        if "Go" in self.languages:
            go_files = list(self.root.rglob("*.go"))[:10]
            go_commented = 0
            for f in go_files:
                content = f.read_text(errors="replace")
                if "// " in content:
                    go_commented += 1
            if go_commented:
                score += 0.10
                details_parts.append(f"Go: {go_commented} files with comments")

        if "Rust" in self.languages:
            rs_files = list(self.root.rglob("*.rs"))[:10]
            rs_doc_commented = sum(
                1 for f in rs_files if "///" in f.read_text(errors="replace")
            )
            if rs_doc_commented:
                score += 0.10
                details_parts.append(f"Rust: {rs_doc_commented} files with doc comments")
            else:
                suggestions.append("Add Rust doc comments (///) to public items (rustdoc)")

        if "JavaScript" in self.languages or "TypeScript" in self.languages:
            jsdoc = self.file_exists("jsdoc.json", ".jsdoc.json")
            if jsdoc:
                score += 0.10
                details_parts.append("JSDoc configured")
            else:
                # Check if any JS/TS file has JSDoc comments
                js_files = list(self.root.rglob("*.ts"))[:10] + list(self.root.rglob("*.js"))[:5]
                if any("/**" in f.read_text(errors="replace") for f in js_files if ".git" not in f.parts):
                    score += 0.05
                    details_parts.append("JSDoc comments present")

        details = ", ".join(details_parts) if details_parts else "Minimal documentation found"
        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
