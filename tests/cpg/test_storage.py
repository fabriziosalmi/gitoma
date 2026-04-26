"""Tests for CPG-lite v0 storage — SQLite in-memory schema +
insert/query helpers. Covers the data path; resolution logic and
public query API live in tests/cpg/test_queries.py."""

from __future__ import annotations

import pytest

from gitoma.cpg._base import Reference, RefKind, Symbol, SymbolKind
from gitoma.cpg.storage import Storage


def _mk_sym(
    name: str = "foo",
    kind: SymbolKind = SymbolKind.FUNCTION,
    file: str = "a.py",
    line: int = 1,
    parent_id: int | None = None,
    qualified_name: str | None = None,
) -> Symbol:
    return Symbol(
        id=0,
        file=file,
        line=line,
        col=0,
        kind=kind,
        name=name,
        qualified_name=qualified_name or name,
        parent_id=parent_id,
        is_public=not name.startswith("_"),
    )


def _mk_ref(
    raw_name: str = "foo",
    file: str = "b.py",
    line: int = 5,
    symbol_id: int | None = None,
    kind: RefKind = RefKind.CALL,
) -> Reference:
    return Reference(
        symbol_id=symbol_id, raw_name=raw_name, file=file,
        line=line, col=0, kind=kind,
    )


# ── Schema + boot ──────────────────────────────────────────────────


def test_storage_boots_with_empty_tables() -> None:
    s = Storage()
    assert s.symbol_count() == 0
    assert s.reference_count() == 0
    assert s.file_count() == 0


# ── Symbol inserts ─────────────────────────────────────────────────


def test_insert_symbol_assigns_autoincrement_id() -> None:
    s = Storage()
    sid1 = s.insert_symbol(_mk_sym("foo"))
    sid2 = s.insert_symbol(_mk_sym("bar"))
    assert sid1 >= 1
    assert sid2 == sid1 + 1


def test_get_symbol_by_id_round_trip() -> None:
    """Insert and read back must preserve every field including the
    SymbolKind enum type."""
    s = Storage()
    sid = s.insert_symbol(_mk_sym("Worker", kind=SymbolKind.CLASS, file="x.py", line=10))
    got = s.get_symbol_by_id(sid)
    assert got is not None
    assert got.name == "Worker"
    assert got.kind is SymbolKind.CLASS  # not the str "class"
    assert got.file == "x.py"
    assert got.line == 10


def test_get_symbol_by_id_returns_none_for_missing() -> None:
    s = Storage()
    assert s.get_symbol_by_id(9999) is None


def test_get_symbols_by_name_returns_all_matches() -> None:
    """Two functions named ``process`` in different files — both must
    come back when queried; resolver decides which one a reference
    means."""
    s = Storage()
    s.insert_symbol(_mk_sym("process", file="a.py"))
    s.insert_symbol(_mk_sym("process", file="b.py"))
    s.insert_symbol(_mk_sym("other", file="c.py"))
    matches = s.get_symbols_by_name("process")
    assert len(matches) == 2
    assert {m.file for m in matches} == {"a.py", "b.py"}


def test_get_symbols_in_file_ordered_by_position() -> None:
    s = Storage()
    s.insert_symbol(_mk_sym("z", file="a.py", line=30))
    s.insert_symbol(_mk_sym("a", file="a.py", line=10))
    s.insert_symbol(_mk_sym("m", file="a.py", line=20))
    syms = s.get_symbols_in_file("a.py")
    assert [x.name for x in syms] == ["a", "m", "z"]


def test_parent_id_persists_for_method() -> None:
    s = Storage()
    cls_id = s.insert_symbol(_mk_sym("Worker", kind=SymbolKind.CLASS))
    method_id = s.insert_symbol(_mk_sym(
        "run", kind=SymbolKind.METHOD, parent_id=cls_id,
        qualified_name="Worker.run",
    ))
    method = s.get_symbol_by_id(method_id)
    assert method is not None
    assert method.parent_id == cls_id


def test_language_defaults_to_python_for_back_compat() -> None:
    """v0 callers don't pass ``language``; the schema default
    keeps existing tests working unchanged."""
    s = Storage()
    sid = s.insert_symbol(_mk_sym("foo"))
    got = s.get_symbol_by_id(sid)
    assert got is not None
    assert got.language == "python"


def test_signature_defaults_to_empty_for_back_compat() -> None:
    """Skeletal v1 added the signature column; existing v0 callers
    that don't pass it must still work."""
    s = Storage()
    sid = s.insert_symbol(_mk_sym("foo"))
    got = s.get_symbol_by_id(sid)
    assert got is not None
    assert got.signature == ""


def test_signature_round_trips() -> None:
    """Signature text persists through SQLite — including special
    characters like parentheses / brackets / arrows."""
    s = Storage()
    sig = "(req: dict, *, timeout: float = 5.0) -> tuple[int, str]"
    sid = s.insert_symbol(Symbol(
        id=0, file="x.py", line=1, col=0,
        kind=SymbolKind.FUNCTION, name="run",
        qualified_name="x.run", parent_id=None,
        is_public=True, signature=sig,
    ))
    got = s.get_symbol_by_id(sid)
    assert got is not None
    assert got.signature == sig


def test_language_round_trips_for_typescript() -> None:
    """v0.5-slim TS records pass ``language="typescript"`` and
    must come back unchanged through SQLite."""
    s = Storage()
    sid = s.insert_symbol(Symbol(
        id=0, file="x.ts", line=1, col=0,
        kind=SymbolKind.INTERFACE, name="User",
        qualified_name="x.User", parent_id=None,
        is_public=True, language="typescript",
    ))
    got = s.get_symbol_by_id(sid)
    assert got is not None
    assert got.language == "typescript"
    assert got.kind is SymbolKind.INTERFACE


def test_is_public_round_trips_as_bool() -> None:
    """SQLite stores INTEGER but the helper must hand callers a real
    Python bool — downstream code uses it in conditionals."""
    s = Storage()
    pub_id = s.insert_symbol(_mk_sym("foo"))
    priv_id = s.insert_symbol(_mk_sym("_foo"))
    pub = s.get_symbol_by_id(pub_id)
    priv = s.get_symbol_by_id(priv_id)
    assert pub is not None and priv is not None
    assert pub.is_public is True
    assert priv.is_public is False


# ── Reference inserts ──────────────────────────────────────────────


def test_insert_reference_with_known_symbol_id() -> None:
    s = Storage()
    sid = s.insert_symbol(_mk_sym("foo", file="a.py"))
    s.insert_reference(_mk_ref("foo", file="b.py", symbol_id=sid))
    refs = s.get_refs_to(sid)
    assert len(refs) == 1
    assert refs[0].file == "b.py"
    assert refs[0].kind is RefKind.CALL


def test_insert_unresolved_reference_then_resolve() -> None:
    """Indexer emits refs with symbol_id=None; resolver back-fills
    via update_ref_symbol_id. Round-trip must reflect both."""
    s = Storage()
    sid = s.insert_symbol(_mk_sym("foo", file="a.py"))
    s.insert_reference(_mk_ref("foo", file="b.py", symbol_id=None))

    unresolved = list(s.iter_unresolved_refs())
    assert len(unresolved) == 1
    rowid, raw_name, file = unresolved[0]
    assert raw_name == "foo"
    assert file == "b.py"

    s.update_ref_symbol_id(rowid, sid)
    assert list(s.iter_unresolved_refs()) == []
    assert len(s.get_refs_to(sid)) == 1


def test_get_refs_in_file_returns_ordered() -> None:
    s = Storage()
    s.insert_reference(_mk_ref("z", file="x.py", line=30))
    s.insert_reference(_mk_ref("a", file="x.py", line=10))
    s.insert_reference(_mk_ref("m", file="x.py", line=20))
    refs = s.get_refs_in_file("x.py")
    assert [r.raw_name for r in refs] == ["a", "m", "z"]


# ── Imports ────────────────────────────────────────────────────────


def test_insert_and_get_imports_for_file() -> None:
    s = Storage()
    s.insert_import("a.py", "json", "json", line=1)
    s.insert_import("a.py", "os.path", "join", line=2)
    s.insert_import("b.py", "json", "json", line=1)
    imports = s.get_imports_for_file("a.py")
    assert sorted(imports) == sorted([
        ("json", "json", 1),
        ("os.path", "join", 2),
    ])


def test_files_importing_matches_module_and_submodule() -> None:
    """``files_importing("gitoma.cpg")`` must return files that
    import ``gitoma.cpg`` directly AND files that import
    ``gitoma.cpg.queries`` (submodule)."""
    s = Storage()
    s.insert_import("a.py", "gitoma.cpg", "cpg", line=1)
    s.insert_import("b.py", "gitoma.cpg.queries", "CPGIndex", line=1)
    s.insert_import("c.py", "json", "json", line=1)
    files = s.files_importing("gitoma.cpg")
    paths = {f for f, _ in files}
    assert paths == {"a.py", "b.py"}


# ── Stats ──────────────────────────────────────────────────────────


def test_symbol_and_file_counts() -> None:
    s = Storage()
    s.insert_symbol(_mk_sym("a", file="x.py"))
    s.insert_symbol(_mk_sym("b", file="x.py"))
    s.insert_symbol(_mk_sym("c", file="y.py"))
    assert s.symbol_count() == 3
    assert s.file_count() == 2


def test_reference_count() -> None:
    s = Storage()
    s.insert_reference(_mk_ref("foo"))
    s.insert_reference(_mk_ref("bar"))
    assert s.reference_count() == 2


# ── Lifecycle ──────────────────────────────────────────────────────


def test_close_releases_connection() -> None:
    s = Storage()
    s.insert_symbol(_mk_sym("foo"))
    s.close()
    with pytest.raises(Exception):
        s.symbol_count()
