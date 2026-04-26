"""Tests for the vertical-mode declarative base — Castelletto Taglio A.

These tests exercise pure data + the scope predicate. They MUST stay
free of any registry / I/O / env coupling so a future refactor of the
registry doesn't drag them along."""

from __future__ import annotations

import pytest

from gitoma.verticals._base import Vertical, VerticalFileScope


# ── VerticalFileScope.is_in_scope ──────────────────────────────────


def test_extension_match() -> None:
    scope = VerticalFileScope(extensions=frozenset({".md", ".rst"}))
    assert scope.is_in_scope("README.md") is True
    assert scope.is_in_scope("docs/intro.rst") is True
    assert scope.is_in_scope("src/main.py") is False


def test_extension_match_is_case_insensitive_on_suffix() -> None:
    scope = VerticalFileScope(extensions=frozenset({".md"}))
    assert scope.is_in_scope("notes.MD") is True


def test_path_prefix_match_at_root() -> None:
    scope = VerticalFileScope(path_prefixes=("docs/",))
    assert scope.is_in_scope("docs/intro.md") is True
    assert scope.is_in_scope("docs/api/rest.md") is True
    assert scope.is_in_scope("src/intro.md") is False


def test_path_prefix_match_when_nested_below_repo_root() -> None:
    """A subtree match works when the prefix appears below the root.
    Useful for monorepo layouts (`packages/foo/docs/...`)."""
    scope = VerticalFileScope(path_prefixes=("docs/",))
    assert scope.is_in_scope("packages/foo/docs/intro.md") is True


def test_root_name_match_with_explicit_casing() -> None:
    scope = VerticalFileScope(root_names=frozenset({"README.md", "CHANGELOG.md"}))
    assert scope.is_in_scope("README.md") is True
    assert scope.is_in_scope("CHANGELOG.md") is True
    assert scope.is_in_scope("Other.md") is False


def test_root_name_match_uppercase_fallback() -> None:
    """The base scope normalises basenames to uppercase as a fallback
    so casual casings (Readme.md) match an allow-list entry that lists
    only README.MD. Tests existing scope_filter behavior."""
    scope = VerticalFileScope(root_names=frozenset({"README.MD"}))
    assert scope.is_in_scope("Readme.md") is True


def test_empty_path_returns_false() -> None:
    scope = VerticalFileScope(extensions=frozenset({".md"}))
    assert scope.is_in_scope("") is False


def test_windows_path_normalised_to_forward_slash() -> None:
    scope = VerticalFileScope(path_prefixes=("docs/",))
    assert scope.is_in_scope("docs\\api\\rest.md") is True


def test_no_match_channels_returns_false() -> None:
    """Empty extensions + empty prefixes + empty root_names = nothing
    matches. Useful sentinel for verticals that opt out of a channel."""
    scope = VerticalFileScope()
    assert scope.is_in_scope("README.md") is False
    assert scope.is_in_scope("docs/intro.md") is False


# ── Vertical dataclass surface ──────────────────────────────────────


def test_vertical_is_frozen_so_registry_lookups_are_safe() -> None:
    """Verticals live in a global registry; mutating one would silently
    affect every consumer. Frozen dataclass prevents accidents."""
    v = Vertical(
        name="docs",
        summary="docs only",
        file_allow_list=VerticalFileScope(extensions=frozenset({".md"})),
        metric_allow_list=frozenset({"documentation"}),
    )
    with pytest.raises((AttributeError, Exception)):
        v.name = "other"  # type: ignore[misc]


def test_vertical_is_path_in_scope_proxies_file_allow_list() -> None:
    v = Vertical(
        name="docs",
        summary="docs only",
        file_allow_list=VerticalFileScope(extensions=frozenset({".md"})),
        metric_allow_list=frozenset({"documentation"}),
    )
    assert v.is_path_in_scope("README.md") is True
    assert v.is_path_in_scope("src/main.py") is False


def test_vertical_defaults_safe_when_omitted() -> None:
    """`prompt_addendum` defaults to empty (no prompt change),
    `no_auto_fix_ci` defaults to True (most verticals don't touch CI),
    `guards_disabled` defaults to empty (full guard stack)."""
    v = Vertical(
        name="x",
        summary="x",
        file_allow_list=VerticalFileScope(),
        metric_allow_list=frozenset(),
    )
    assert v.prompt_addendum == ""
    assert v.no_auto_fix_ci is True
    assert v.guards_disabled == frozenset()
