"""Tests for the v0.5-expansion Rust AST indexer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.rust_indexer import (
    RUST_LANGUAGE,
    _parse_use_argument,
    index_rust_file,
    rust_module_qualified_name_for,
)
from gitoma.cpg.storage import Storage


def _write(tmp_path: Path, rel: str, src: str) -> tuple[Path, str]:
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src)
    return abs_path, rel


# ── module qname ───────────────────────────────────────────────────


def test_module_qname_basic() -> None:
    assert rust_module_qualified_name_for("src/handlers/user.rs") == \
        "src.handlers.user"


def test_module_qname_mod_rs_collapses() -> None:
    """Rust 2015-style ``foo/mod.rs`` is the entry for module ``foo``."""
    assert rust_module_qualified_name_for("src/handlers/mod.rs") == \
        "src.handlers"


def test_module_qname_main_rs() -> None:
    assert rust_module_qualified_name_for("src/main.rs") == "src.main"


# ── _parse_use_argument helper ────────────────────────────────────


@pytest.mark.parametrize("arg,expected", [
    ("std::collections::HashMap",
     [("std::collections", "HashMap")]),
    ("std::io",
     [("std", "io")]),
    ("crate::types::{User, Repo}",
     [("crate::types", "User"), ("crate::types", "Repo")]),
    ("crate::types::{User, Repo as DataRepo}",
     [("crate::types", "User"), ("crate::types", "DataRepo")]),
    ("crate::handlers::*",
     [("crate::handlers", "*")]),
    ("foo",
     [("", "foo")]),
])
def test_parse_use_argument(arg: str, expected: list[tuple[str, str]]) -> None:
    assert sorted(_parse_use_argument(arg)) == sorted(expected)


# ── Module symbol ──────────────────────────────────────────────────


def test_emits_module_symbol(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "x.rs", "pub fn foo() {}\n")
    s = Storage()
    n = index_rust_file(abs_path, rel, s)
    assert n >= 1
    modules = [m for m in s.get_symbols_in_file(rel)
               if m.kind is SymbolKind.MODULE]
    assert len(modules) == 1
    assert modules[0].language == RUST_LANGUAGE


def test_empty_file_emits_module_only(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "empty.rs", "")
    s = Storage()
    n = index_rust_file(abs_path, rel, s)
    assert n == 1


# ── Functions ─────────────────────────────────────────────────────


def test_pub_function_is_public(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.rs",
                           "pub fn helper(x: i32) -> i32 { x + 1 }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.is_public is True
    assert func.signature == "(x: i32) -> i32"
    assert func.language == RUST_LANGUAGE


def test_non_pub_function_is_private(tmp_path: Path) -> None:
    """Rust default is private — no `pub` keyword = private. The
    Python-style underscore heuristic does NOT apply."""
    abs_path, rel = _write(tmp_path, "f.rs",
                           "fn internal_helper() {}\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.is_public is False


def test_function_signature_no_args_no_return(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.rs", "pub fn tick() {}\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.signature == "()"


# ── struct / enum / trait ──────────────────────────────────────────


def test_pub_struct_is_class(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "u.rs",
                           "pub struct User { pub id: u64 }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    cls = next(sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.CLASS)
    assert cls.name == "User"
    assert cls.is_public is True


def test_enum_is_class(tmp_path: Path) -> None:
    """Rust enums map to CLASS in v0.5 (no ENUM SymbolKind)."""
    abs_path, rel = _write(tmp_path, "e.rs",
                           "pub enum Status { Ok, Err(String) }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    cls = next(sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.CLASS)
    assert cls.name == "Status"


def test_pub_trait_is_interface(tmp_path: Path) -> None:
    src = (
        "pub trait Greeter {\n"
        "    fn greet(&self) -> String;\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "g.rs", src)
    s = Storage()
    index_rust_file(abs_path, rel, s)
    iface = next(sym for sym in s.get_symbols_in_file(rel)
                 if sym.kind is SymbolKind.INTERFACE)
    assert iface.name == "Greeter"
    assert iface.is_public is True
    # Trait methods declared via function_signature_item
    methods = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.METHOD]
    assert any(m.name == "greet" for m in methods)


# ── impl block ────────────────────────────────────────────────────


def test_impl_methods_get_parent_id_of_target_struct(tmp_path: Path) -> None:
    src = (
        "pub struct User { pub id: u64 }\n"
        "impl User {\n"
        "    pub fn new(id: u64) -> Self { User { id } }\n"
        "    fn private_helper(&self) {}\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "u.rs", src)
    s = Storage()
    index_rust_file(abs_path, rel, s)
    user = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.CLASS and sym.name == "User")
    methods = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.METHOD]
    assert {m.name for m in methods} == {"new", "private_helper"}
    assert all(m.parent_id == user.id for m in methods)
    new_method = next(m for m in methods if m.name == "new")
    assert new_method.is_public is True
    assert new_method.qualified_name == "u.User.new"


def test_impl_trait_for_records_inheritance_ref(tmp_path: Path) -> None:
    src = (
        "pub struct User;\n"
        "impl Greeter for User {\n"
        "    fn greet(&self) -> String { String::new() }\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "u.rs", src)
    s = Storage()
    index_rust_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    inherit = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.raw_name == "Greeter" for r in inherit)


# ── const / static → ASSIGNMENT ───────────────────────────────────


def test_pub_const_is_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.rs",
                           "pub const MAX: u32 = 100;\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert len(assigns) == 1
    assert assigns[0].name == "MAX"
    assert assigns[0].is_public is True


def test_pub_static_is_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "s.rs",
                           'pub static NAME: &str = "x";\n')
    s = Storage()
    index_rust_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert any(a.name == "NAME" for a in assigns)


# ── use declaration → IMPORT ──────────────────────────────────────


def test_simple_use_recorded(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.rs",
                           "use std::collections::HashMap;\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert ("std::collections", "HashMap", 1) in imports


def test_brace_use_with_alias(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.rs",
                           "use crate::types::{User, Repo as DataRepo};\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    bound = {b for _, b, _ in s.get_imports_for_file(rel)}
    assert bound == {"User", "DataRepo"}


# ── References ────────────────────────────────────────────────────


def test_call_expression_emits_call_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.rs",
                           "pub fn caller() { helper(); }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "helper" for c in calls)


def test_method_call_via_field_expression(tmp_path: Path) -> None:
    """`obj.method()` — `method` is the called name."""
    abs_path, rel = _write(tmp_path, "c.rs",
                           "pub fn caller(obj: &User) { obj.greet(); }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "greet" for c in calls)


def test_scoped_call_records_leaf(tmp_path: Path) -> None:
    """`Foo::bar()` — `bar` is the called name (the scope is `Foo`)."""
    abs_path, rel = _write(tmp_path, "c.rs",
                           "pub fn caller() { String::new(); }\n")
    s = Storage()
    index_rust_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "new" for c in calls)
