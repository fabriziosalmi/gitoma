"""Parity tests for DOCS_VERTICAL — Castelletto Taglio A.

Goal: prove the new declarative ``DOCS_VERTICAL`` is byte-equivalent
to the legacy module-level constants in
:mod:`gitoma.planner.scope_filter`. If these tests pass, the
refactor preserves behavior; the rest of the system can switch to
the registry without functional drift.
"""

from __future__ import annotations

import pytest

from gitoma.planner.scope_filter import (
    DOC_FILE_EXTENSIONS,
    DOC_METRIC_NAMES,
    DOC_PATH_PREFIXES,
    DOC_ROOT_NAMES,
    is_doc_path,
)
from gitoma.verticals.docs import DOCS_VERTICAL


# ── Constant-by-constant parity ────────────────────────────────────


def test_extensions_parity() -> None:
    assert DOCS_VERTICAL.file_allow_list.extensions == DOC_FILE_EXTENSIONS


def test_path_prefixes_parity() -> None:
    assert DOCS_VERTICAL.file_allow_list.path_prefixes == DOC_PATH_PREFIXES


def test_root_names_parity() -> None:
    assert DOCS_VERTICAL.file_allow_list.root_names == DOC_ROOT_NAMES


def test_metric_allow_list_parity() -> None:
    assert DOCS_VERTICAL.metric_allow_list == DOC_METRIC_NAMES


# ── Path-by-path round-trip parity ────────────────────────────────


@pytest.mark.parametrize("path", [
    "README.md",
    "README",
    "README.rst",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "docs/index.md",
    "docs/guide/quickstart.md",
    "doc/getting-started.rst",
    "documentation/architecture.adoc",
    "website/blog/post.mdx",
    "notes.txt",
    "src/main.py",
    "src/main.rs",
    "config.yaml",
    "package.json",
    "Cargo.toml",
    ".github/workflows/ci.yml",
    "docs/hooks.py",
    "",
    # Edge cases not in the legacy test corpus — useful for catching
    # case-sensitivity drift between the two implementations.
    "Readme.md",
    "ReadMe.md",
    "packages/foo/docs/intro.md",
    "deep/nested/docs/x.md",
])
def test_legacy_and_vertical_agree_on_path(path: str) -> None:
    """For every path the legacy ``is_doc_path`` decided on, the new
    ``DOCS_VERTICAL.is_path_in_scope`` must agree. If this breaks, the
    refactor is no longer behavior-preserving and downstream filters
    will start producing different plans."""
    assert DOCS_VERTICAL.is_path_in_scope(path) is is_doc_path(path)


# ── Vertical metadata sanity ───────────────────────────────────────


def test_docs_vertical_has_useful_summary() -> None:
    assert "docs" in DOCS_VERTICAL.summary.lower()
    assert len(DOCS_VERTICAL.summary) > 20


def test_docs_vertical_prompt_addendum_marks_scope() -> None:
    """The addendum is what stops the planner from emitting source-
    code subtasks in the first place. Must mention the active
    vertical AND list the allowed file kinds."""
    addendum = DOCS_VERTICAL.prompt_addendum
    assert "docs" in addendum.lower()
    assert "README" in addendum
    # Must be explicit about what's NOT allowed — otherwise the
    # planner reads the allow-list as a hint rather than a rule.
    assert "do not" in addendum.lower() or "not propose" in addendum.lower()


def test_docs_vertical_skips_auto_fix_ci() -> None:
    """Docs vertical never touches CI; fix-CI phase is moot. The CLI
    factory passes this flag to ``run_full_pipeline``."""
    assert DOCS_VERTICAL.no_auto_fix_ci is True


def test_docs_vertical_does_not_disable_any_guard() -> None:
    """Defaults: the docs vertical wants the FULL guard stack.
    Disabling guards is the rare path; if a future change disables
    one here, the test must be updated explicitly."""
    assert DOCS_VERTICAL.guards_disabled == frozenset()
