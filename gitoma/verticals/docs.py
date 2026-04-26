"""DOCS_VERTICAL — first concrete vertical, narrows pipeline to docs.

Mirrors the previous hardcoded constants in
:mod:`gitoma.planner.scope_filter` (``DOC_FILE_EXTENSIONS`` /
``DOC_PATH_PREFIXES`` / ``DOC_ROOT_NAMES`` / ``DOC_METRIC_NAMES``)
so the Castelletto Taglio A refactor is a behavior-preserving move
of those values into a single declarative record.

Motivation: ``lws`` dry-run on 2026-04-26 produced 6 tasks across
Security/CodeQuality/TestSuite/CI/Documentation/ProjectStructure
metrics, of which 3 were hallucinated (Security false-positive on
template placeholders; TestSuite proposing JS for a pure-Python
repo; Documentation proposing MkDocs to a repo already on Jekyll).
A docs-vertical run skips 5 of 6 tasks at audit time and narrows
the planner to exactly the concern the operator wanted.
"""

from __future__ import annotations

from gitoma.verticals._base import Vertical, VerticalFileScope

__all__ = ["DOCS_VERTICAL"]


DOCS_VERTICAL = Vertical(
    name="docs",
    summary="Run gitoma narrowed to the docs vertical (Markdown/reST/text + project meta files only).",
    file_allow_list=VerticalFileScope(
        extensions=frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"}),
        path_prefixes=("docs/", "doc/", "documentation/", "website/"),
        root_names=frozenset({
            "README", "README.md", "README.rst", "README.txt",
            "CHANGELOG", "CHANGELOG.md", "CHANGES", "CHANGES.md",
            "CONTRIBUTING", "CONTRIBUTING.md",
            "CODE_OF_CONDUCT", "CODE_OF_CONDUCT.md",
            "SECURITY", "SECURITY.md",
            "AUTHORS", "AUTHORS.md", "MAINTAINERS", "MAINTAINERS.md",
            "ROADMAP", "ROADMAP.md",
            "Readme.md", "ReadMe.md",
        }),
    ),
    metric_allow_list=frozenset({
        "documentation", "docs", "readme", "readme_quality",
    }),
    prompt_addendum=(
        "VERTICAL=docs ACTIVE. You may ONLY emit subtasks whose "
        "file_hints are documentation files: Markdown / reST / text "
        "under docs/ (or doc/, documentation/, website/), plus root-"
        "level project meta files (README, CHANGELOG, CONTRIBUTING, "
        "CODE_OF_CONDUCT, SECURITY, AUTHORS, MAINTAINERS, ROADMAP). "
        "Do NOT propose source-code edits, test changes, CI/workflow "
        "edits, or new build-tool installs (MkDocs/Sphinx/etc. when "
        "another doc tool is already detected) — those belong to a "
        "different vertical or to the full-pass `gitoma run` mode."
    ),
    no_auto_fix_ci=True,
)
