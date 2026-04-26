"""Tests for QUALITY_VERTICAL — second vertical, registered to prove
the Castelletto Taglio A architecture works for >1 vertical."""

from __future__ import annotations

import pytest

from gitoma.verticals import VERTICALS
from gitoma.verticals.quality import QUALITY_VERTICAL


# ── Registry presence (the architectural test) ─────────────────────


def test_quality_is_registered() -> None:
    """The whole point of the refactor: adding `quality.py` and ONE
    line in __init__.py is enough to register a new vertical. If this
    test fails, the registry-driven flow is broken."""
    assert "quality" in VERTICALS
    assert VERTICALS["quality"] is QUALITY_VERTICAL


# ── Path scope ─────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    # In-scope: lint/format/type-check config
    (".prettierrc",                True),
    (".prettierrc.json",           True),
    (".eslintrc.js",               True),
    ("eslint.config.mjs",          True),
    ("biome.json",                 True),
    ("tsconfig.json",              True),
    (".ruff.toml",                 True),
    ("ruff.toml",                  True),
    ("setup.cfg",                  True),
    ("mypy.ini",                   True),
    (".pylintrc",                  True),
    (".editorconfig",              True),
    (".pre-commit-config.yaml",    True),
    (".golangci.yml",              True),
    ("rustfmt.toml",               True),
    ("clippy.toml",                True),
    # Out-of-scope: source / docs / tests / CI / build manifests
    ("src/main.py",                False),
    ("README.md",                  False),
    ("docs/intro.md",              False),
    ("tests/test_x.py",            False),
    (".github/workflows/ci.yml",   False),
    ("package.json",               False),
    ("Cargo.toml",                 False),
    ("pyproject.toml",             False),  # excluded by design (sub-section logic deferred)
    ("",                           False),
])
def test_quality_scope_path_decisions(path: str, expected: bool) -> None:
    assert QUALITY_VERTICAL.is_path_in_scope(path) is expected


def test_quality_scope_does_not_extend_to_subtree() -> None:
    """Quality config files are matched by EXACT basename. A file
    named .eslintrc nested deep in the tree should still match
    (root_names normalises basename, not path); but a .py file under
    a config-named directory should not."""
    # Nested basename match by DESIGN (config files can live in
    # sub-projects in monorepos, e.g. packages/foo/.eslintrc).
    assert QUALITY_VERTICAL.is_path_in_scope("packages/foo/.eslintrc") is True
    # But a non-config-named file at top level is OUT.
    assert QUALITY_VERTICAL.is_path_in_scope("config.py") is False


# ── Metric narrowing ───────────────────────────────────────────────


def test_quality_metric_allow_list() -> None:
    assert QUALITY_VERTICAL.metric_allow_list == frozenset({"code_quality"})


# ── Prompt addendum sanity ─────────────────────────────────────────


def test_quality_addendum_marks_scope_and_forbids_source() -> None:
    addendum = QUALITY_VERTICAL.prompt_addendum
    assert "quality" in addendum.lower()
    # The addendum must be EXPLICIT about what's not allowed —
    # otherwise the planner reads the allow-list as a hint.
    assert "do not" in addendum.lower()
    # Must mention at least one common config file family the
    # operator might recognise (sanity check on the prose).
    assert ".eslintrc" in addendum or ".ruff.toml" in addendum


def test_quality_skips_auto_fix_ci() -> None:
    """Quality vertical never edits CI; fix-CI is moot."""
    assert QUALITY_VERTICAL.no_auto_fix_ci is True


# ── CLI command wiring (the architectural acceptance test) ─────────


def test_quality_cli_command_was_generated() -> None:
    """If the registry-driven CLI factory works, a `gitoma quality`
    Typer command appears WITHOUT any edit to ``commands/__init__.py``,
    ``commands/_vertical.py``, ``run.py``, or ``scope_filter.py``.
    This is the "1 file = 1 vertical" property the refactor exists
    to deliver."""
    # Ensure CLI module-load side-effects fired.
    import gitoma.cli.commands  # noqa: F401
    from gitoma.cli._app import app

    registered_names = {cmd.name for cmd in app.registered_commands}
    assert "quality" in registered_names
