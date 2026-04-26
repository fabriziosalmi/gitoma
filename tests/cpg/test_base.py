"""Tests for CPG-lite v0 base data model — pure data + helpers.

Stays free of any storage / parsing / I/O coupling. If these break
the v0 data shape changed and downstream layers must be reviewed."""

from __future__ import annotations

import pytest

from gitoma.cpg._base import (
    Reference,
    RefKind,
    Symbol,
    SymbolKind,
    compose_qualified_name,
)


# ── Enums ──────────────────────────────────────────────────────────


def test_symbol_kind_values_are_strings_for_sqlite_storage() -> None:
    """Storage layer persists the enum value as a TEXT column. The
    string subclass property guarantees ``str(kind) == kind.value``
    is meaningful and round-trips through SQLite."""
    assert SymbolKind.FUNCTION.value == "function"
    assert SymbolKind.CLASS.value == "class"
    assert SymbolKind.METHOD.value == "method"
    assert SymbolKind.MODULE.value == "module"
    assert SymbolKind.ASSIGNMENT.value == "assignment"
    assert SymbolKind.IMPORT.value == "import"
    # v0.5-slim additions for TypeScript declarations.
    assert SymbolKind.INTERFACE.value == "interface"
    assert SymbolKind.TYPE_ALIAS.value == "type_alias"


def test_ref_kind_values_are_strings() -> None:
    assert RefKind.CALL.value == "call"
    assert RefKind.ATTRIBUTE_ACCESS.value == "attribute_access"
    assert RefKind.NAME_LOAD.value == "name_load"
    assert RefKind.IMPORT_FROM.value == "import_from"
    assert RefKind.INHERITANCE.value == "inheritance"


def test_symbol_kind_round_trips_via_value() -> None:
    """Storage will write ``kind.value`` and read back ``kind``;
    ``SymbolKind(value)`` must reverse the trip without ambiguity."""
    for kind in SymbolKind:
        assert SymbolKind(kind.value) is kind


# ── Symbol dataclass ───────────────────────────────────────────────


def _mk_symbol() -> Symbol:
    return Symbol(
        id=1,
        file="gitoma/foo.py",
        line=10,
        col=0,
        kind=SymbolKind.FUNCTION,
        name="bar",
        qualified_name="gitoma.foo.bar",
        parent_id=None,
        is_public=True,
    )


def test_symbol_is_frozen() -> None:
    """Symbols flow through SQLite + dict caches; mutability would
    break invariants silently. Frozen dataclass is the safety net."""
    sym = _mk_symbol()
    with pytest.raises((AttributeError, Exception)):
        sym.name = "other"  # type: ignore[misc]


def test_symbol_equality_value_based() -> None:
    """Two Symbols with identical fields compare equal — dataclass
    default. Important: deduplication code (e.g. multiple defs of
    the same name in conditional branches) relies on this."""
    a = _mk_symbol()
    b = _mk_symbol()
    assert a == b
    assert hash(a) == hash(b)


def test_symbol_language_defaults_to_python_for_back_compat() -> None:
    """v0 callers don't pass ``language``; default = ``"python"``
    so the v0.5-slim schema change is backward-compatible."""
    sym = _mk_symbol()
    assert sym.language == "python"


def test_symbol_language_can_be_overridden_for_typescript() -> None:
    sym = Symbol(
        id=0, file="x.ts", line=1, col=0,
        kind=SymbolKind.INTERFACE, name="User",
        qualified_name="x.User", parent_id=None, is_public=True,
        language="typescript",
    )
    assert sym.language == "typescript"
    assert sym.kind is SymbolKind.INTERFACE


def test_symbol_with_parent_id_chains_to_class() -> None:
    """Methods carry parent_id pointing to their class Symbol."""
    method = Symbol(
        id=2,
        file="gitoma/x.py",
        line=20,
        col=4,
        kind=SymbolKind.METHOD,
        name="run",
        qualified_name="gitoma.x.Worker.run",
        parent_id=1,  # the class Symbol
        is_public=True,
    )
    assert method.parent_id == 1


# ── Reference dataclass ────────────────────────────────────────────


def test_reference_resolved_carries_symbol_id() -> None:
    ref = Reference(
        symbol_id=42,
        raw_name="bar",
        file="gitoma/x.py",
        line=15,
        col=8,
        kind=RefKind.CALL,
    )
    assert ref.symbol_id == 42


def test_reference_unresolved_carries_none() -> None:
    """Built-ins / star-imports / dynamic refs leave symbol_id None.
    The resolver MUST be allowed to return unresolved references —
    forcing a lookup would either lie (pick a wrong match) or
    drop the ref entirely (lose the textual signal)."""
    ref = Reference(
        symbol_id=None,
        raw_name="print",
        file="gitoma/x.py",
        line=10,
        col=4,
        kind=RefKind.CALL,
    )
    assert ref.symbol_id is None
    assert ref.raw_name == "print"


def test_reference_is_frozen() -> None:
    ref = Reference(
        symbol_id=None, raw_name="x", file="a.py", line=1, col=0,
        kind=RefKind.NAME_LOAD,
    )
    with pytest.raises((AttributeError, Exception)):
        ref.symbol_id = 1  # type: ignore[misc]


# ── compose_qualified_name helper ──────────────────────────────────


def test_compose_qualified_name_full_chain() -> None:
    assert compose_qualified_name(("gitoma", "cpg", "queries", "CPGIndex")) == \
        "gitoma.cpg.queries.CPGIndex"


def test_compose_qualified_name_skips_empty() -> None:
    """Module-root symbols pass an empty leaf; helper must not emit
    a trailing dot."""
    assert compose_qualified_name(("gitoma", "x", "")) == "gitoma.x"
    assert compose_qualified_name(("", "")) == ""


def test_compose_qualified_name_single_part() -> None:
    assert compose_qualified_name(("foo",)) == "foo"


def test_compose_qualified_name_handles_empty_tuple() -> None:
    assert compose_qualified_name(()) == ""
