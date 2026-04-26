"""Vertical-mode scope filter — narrows plan + audit to a domain.

The first vertical (`gitoma docs`) was motivated by the lws dry-run
on 2026-04-26: a full-pass `gitoma run` produced 6 tasks across
Security/CodeQuality/TestSuite/CI/Documentation/ProjectStructure
metrics, of which 3 had hallucinated subtasks (Security flagged
template `password` placeholders as hardcoded; TestSuite proposed
`jest.config.js` for a pure-Python repo; Documentation proposed
adding MkDocs to a repo that already has Jekyll Pages). A vertical
mode would have skipped 5 of 6 tasks at audit time and narrowed
the planner to the one concern the operator actually wanted.

Architecture: two pure functions.

  * **`filter_metrics_to_doc_scope(report)`** — drop every metric
    that isn't in the doc-relevant set. Mutates the report in
    place; returns it for chaining. Called BEFORE `planner.plan()`
    so the LLM only sees what's in scope.

  * **`filter_plan_to_doc_scope(plan)`** — drop every subtask
    whose ``file_hints`` contain ANY non-doc path. Same shape as
    Layer-B (`banish_readme_only_subtasks`) but generalised to
    the broader doc allow-list. Called AFTER `planner.plan()`
    AND after Layer-A/B/G9, as the last gate before worker apply.

Both are deterministic, sub-1ms, no LLM. The doc scope is
defined by ``DOC_FILE_EXTENSIONS`` + ``DOC_PATH_PREFIXES`` +
``DOC_ROOT_NAMES`` so future verticals can copy the shape with
their own allow-list.

Activated by ``GITOMA_SCOPE=docs`` env var (set by the
`gitoma docs` CLI command). The full-pass `gitoma run` keeps
the env unset and behaves as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gitoma.analyzers.base import MetricReport
from gitoma.planner.task import SubTask, Task, TaskPlan

__all__ = [
    "DOC_FILE_EXTENSIONS",
    "DOC_PATH_PREFIXES",
    "DOC_ROOT_NAMES",
    "DOC_METRIC_NAMES",
    "is_doc_path",
    "filter_plan_to_doc_scope",
    "filter_metrics_to_doc_scope",
    "active_scope",
]


# File extensions we consider "docs" (case-insensitive match on suffix).
DOC_FILE_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".mdx", ".rst", ".txt", ".adoc",
})

# Path prefixes (forward-slash normalised) under which everything is
# treated as docs regardless of file extension. Covers VitePress /
# Jekyll / Sphinx / MkDocs / Docusaurus conventions.
DOC_PATH_PREFIXES: tuple[str, ...] = (
    "docs/", "doc/", "documentation/", "website/",
)

# Root-level filenames we treat as docs even without a doc extension.
# Includes the standard top-of-repo project meta files.
DOC_ROOT_NAMES: frozenset[str] = frozenset({
    "README", "README.md", "README.rst", "README.txt",
    "CHANGELOG", "CHANGELOG.md", "CHANGES", "CHANGES.md",
    "CONTRIBUTING", "CONTRIBUTING.md",
    "CODE_OF_CONDUCT", "CODE_OF_CONDUCT.md",
    "SECURITY", "SECURITY.md",
    "AUTHORS", "AUTHORS.md", "MAINTAINERS", "MAINTAINERS.md",
    "ROADMAP", "ROADMAP.md",
    "Readme.md", "ReadMe.md",
})

# Metric ``name``s the docs vertical exposes to the planner. Pulled
# from the analyzer registry — Documentation + README Quality.
# Other metrics are dropped from the report so the planner never
# even considers them.
DOC_METRIC_NAMES: frozenset[str] = frozenset({
    "documentation", "docs", "readme", "readme_quality",
})


def is_doc_path(path: str) -> bool:
    """Return ``True`` when the path is a doc file under the
    vertical's heuristic. Covers: doc-extension files, root-level
    project meta files, anything under ``docs/`` etc. Conservative:
    when uncertain, returns ``False`` (the calling filter drops
    the subtask, which is the safe direction)."""
    if not path:
        return False
    p = Path(path)
    norm = path.replace("\\", "/")
    # Doc extension wins immediately.
    if p.suffix.lower() in DOC_FILE_EXTENSIONS:
        return True
    # Root project-meta files (no extension or non-doc extension).
    if p.name in DOC_ROOT_NAMES or p.name.upper() in DOC_ROOT_NAMES:
        return True
    # Anything under a doc-prefixed directory.
    for prefix in DOC_PATH_PREFIXES:
        if norm.startswith(prefix) or f"/{prefix}" in f"/{norm}":
            return True
    return False


def filter_metrics_to_doc_scope(report: MetricReport) -> dict[str, Any] | None:
    """Mutate ``report.metrics`` to keep only doc-related metrics.
    Returns a summary dict on filter (for trace), or ``None`` when
    the report was already doc-only / empty.

    Rationale: if Build Integrity is failing, that's NOT the docs
    vertical's job to fix. Same for Test Results, Security, etc.
    Filtering BEFORE the planner runs means the planner prompt
    doesn't even mention those failures — no hallucination
    possible.
    """
    before = [m.name for m in report.metrics]
    kept = [m for m in report.metrics if m.name in DOC_METRIC_NAMES]
    if len(kept) == len(report.metrics):
        return None
    dropped = [n for n in before if n not in {m.name for m in kept}]
    report.metrics = kept
    return {
        "metrics_kept": [m.name for m in kept],
        "metrics_dropped": dropped,
        "scope": "docs",
    }


def filter_plan_to_doc_scope(plan: TaskPlan) -> dict[str, Any] | None:
    """Drop every subtask whose ``file_hints`` contain ANY non-doc
    path (and tasks left empty by the drops). Returns a summary
    dict on filter, or ``None`` on no-op.

    Stricter than Layer-B (which only drops README-ONLY subtasks):
    here we drop ANY subtask whose file_hints include a non-doc
    path. A subtask hinting both ``README.md`` and ``src/main.py``
    gets dropped — under the docs vertical, source edits are
    out-of-scope by definition.

    Subtasks with an EMPTY ``file_hints`` (no file specified) are
    KEPT — those are typically ``verify`` actions (e.g. "run
    pytest") that don't write anything; the worker will either
    no-op them or surface a soft error.
    """
    dropped: list[dict[str, Any]] = []
    for task in plan.tasks:
        keep: list[SubTask] = []
        for sub in task.subtasks:
            hints = sub.file_hints or []
            if hints and not all(is_doc_path(h) for h in hints):
                dropped.append({
                    "task_id": task.id,
                    "subtask_id": sub.id,
                    "title": sub.title,
                    "file_hints": list(hints),
                })
                continue
            keep.append(sub)
        task.subtasks = keep
    if not dropped:
        return None
    before_task_count = len(plan.tasks)
    plan.tasks = [t for t in plan.tasks if t.subtasks]
    return {
        "scope": "docs",
        "dropped_subtasks": dropped,
        "drop_count": len(dropped),
        "tasks_removed": before_task_count - len(plan.tasks),
    }


def active_scope() -> str | None:
    """Return the active vertical scope from ``GITOMA_SCOPE`` env,
    or ``None`` for the default full-pass mode. Currently
    recognises ``"docs"``; other values are passed through
    unchanged so future verticals can opt in without touching
    this helper."""
    raw = (os.environ.get("GITOMA_SCOPE") or "").strip().lower()
    return raw or None
