"""Tests for the vertical registry — Castelletto Taglio A.

These tests guard the contract between the registry and its three
consumers (CLI factory, audit scope filter, plan scope filter): the
registry must be a stable dict-like lookup with case-insensitive
get + ``None`` for unknown / empty input.
"""

from __future__ import annotations

import pytest

from gitoma.verticals import VERTICALS, Vertical, get_vertical
from gitoma.verticals.docs import DOCS_VERTICAL


def test_docs_is_registered() -> None:
    assert "docs" in VERTICALS
    assert VERTICALS["docs"] is DOCS_VERTICAL


def test_get_vertical_returns_known() -> None:
    assert get_vertical("docs") is DOCS_VERTICAL


def test_get_vertical_is_case_insensitive() -> None:
    assert get_vertical("DOCS") is DOCS_VERTICAL
    assert get_vertical("  Docs  ") is DOCS_VERTICAL


def test_get_vertical_returns_none_for_unknown() -> None:
    assert get_vertical("nonexistent") is None
    assert get_vertical("") is None
    assert get_vertical(None) is None


def test_registered_values_are_vertical_instances() -> None:
    """Catches the mistake of putting a class or factory in the dict
    instead of a constructed Vertical instance."""
    for name, vert in VERTICALS.items():
        assert isinstance(vert, Vertical)
        # The key must match the vertical's declared name so the CLI
        # command name and the env-var value stay in sync.
        assert name == vert.name
