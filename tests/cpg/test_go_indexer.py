"""Tests for the Go AST indexer (CPG-lite v0.5-expansion-go)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.go_indexer import (
    GO_LANGUAGE,
    _is_exported,
    _receiver_type_name,
    go_module_qualified_name_for,
    index_go_file,
)
from gitoma.cpg.storage import Storage


def _write(tmp_path: Path, rel: str, src: str) -> tuple[Path, str]:
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src)
    return abs_path, rel


# ── module qname ───────────────────────────────────────────────────


def test_module_qname_basic() -> None:
    assert go_module_qualified_name_for("internal/handlers/user.go") == \
        "internal.handlers.user"


def test_module_qname_main_keeps_name() -> None:
    """Unlike Rust mod.rs, Go has no special-case file name to
    collapse — main.go stays main."""
    assert go_module_qualified_name_for("main.go") == "main"


def test_module_qname_strips_extension() -> None:
    assert go_module_qualified_name_for("pkg/foo.go") == "pkg.foo"


# ── _is_exported helper ────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("MaxRetries", True),
    ("NewRepo", True),
    ("X", True),
    ("maxRetries", False),
    ("newRepo", False),
    ("_internal", False),
    ("init", False),
    ("main", False),
    ("", False),
])
def test_is_exported(name: str, expected: bool) -> None:
    assert _is_exported(name) is expected


# ── _receiver_type_name helper ────────────────────────────────────


@pytest.mark.parametrize("receiver_text,expected", [
    ("(r *Repo)", "Repo"),
    ("(s Server)", "Server"),
    ("(r *Repo[T])", "Repo"),
    ("(*Repo)", "Repo"),
    ("(_ *Server)", "Server"),
    ("()", None),
])
def test_receiver_type_name(receiver_text: str, expected: str | None) -> None:
    assert _receiver_type_name(receiver_text) == expected


# ── Module symbol ──────────────────────────────────────────────────


def test_emits_module_symbol(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "x.go", "package main\nfunc Foo() {}\n")
    s = Storage()
    n = index_go_file(abs_path, rel, s)
    assert n >= 1
    modules = [m for m in s.get_symbols_in_file(rel)
               if m.kind is SymbolKind.MODULE]
    assert len(modules) == 1
    assert modules[0].language == GO_LANGUAGE


def test_empty_file_emits_module_only(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "empty.go", "")
    s = Storage()
    n = index_go_file(abs_path, rel, s)
    assert n == 1


# ── Functions: visibility via capital letter ──────────────────────


def test_capital_function_is_public(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.go",
                           "package x\nfunc Helper(x int) int { return x }\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.is_public is True
    assert func.signature == "(x int) int"


def test_lowercase_function_is_private(tmp_path: Path) -> None:
    """Go convention: lowercase name = unexported. NOT the
    Python/TS underscore convention."""
    abs_path, rel = _write(tmp_path, "f.go",
                           "package x\nfunc helper() {}\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.is_public is False


def test_function_signature_no_args_no_return(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.go",
                           "package x\nfunc Tick() {}\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.signature == "()"


# ── Type declarations ─────────────────────────────────────────────


def test_struct_is_class(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "u.go",
                           "package x\ntype User struct { ID int }\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    cls = next(sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.CLASS)
    assert cls.name == "User"
    assert cls.is_public is True


def test_interface_is_interface(tmp_path: Path) -> None:
    src = (
        "package x\n"
        "type Greeter interface {\n"
        "    Greet() string\n"
        "    Setup(opts string) error\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "g.go", src)
    s = Storage()
    index_go_file(abs_path, rel, s)
    iface = next(sym for sym in s.get_symbols_in_file(rel)
                 if sym.kind is SymbolKind.INTERFACE)
    assert iface.name == "Greeter"
    methods = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.METHOD]
    method_names = {m.name for m in methods}
    assert {"Greet", "Setup"}.issubset(method_names)
    # Interface methods are linked to the interface via parent_id
    assert all(m.parent_id == iface.id for m in methods)


def test_type_alias_kind(tmp_path: Path) -> None:
    """`type Status int` → SymbolKind.TYPE_ALIAS (not struct/interface)."""
    abs_path, rel = _write(tmp_path, "t.go",
                           "package x\ntype Status int\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    alias = next(sym for sym in s.get_symbols_in_file(rel)
                 if sym.kind is SymbolKind.TYPE_ALIAS)
    assert alias.name == "Status"


def test_parenthesized_type_block(tmp_path: Path) -> None:
    """`type ( First struct{}; Second interface{} )` → 2 symbols."""
    abs_path, rel = _write(tmp_path, "t.go",
                           "package x\ntype (\n  First struct{}\n  Second interface{}\n)\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    syms = s.get_symbols_in_file(rel)
    classes = {sym.name for sym in syms if sym.kind is SymbolKind.CLASS}
    interfaces = {sym.name for sym in syms if sym.kind is SymbolKind.INTERFACE}
    assert "First" in classes
    assert "Second" in interfaces


# ── Methods (receiver functions) ──────────────────────────────────


def test_pointer_receiver_method_chained_to_struct(tmp_path: Path) -> None:
    src = (
        "package x\n"
        "type Repo struct { items []int }\n"
        "func (r *Repo) Find(id int) (int, error) { return 0, nil }\n"
    )
    abs_path, rel = _write(tmp_path, "r.go", src)
    s = Storage()
    index_go_file(abs_path, rel, s)
    repo = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.CLASS and sym.name == "Repo")
    method = next(sym for sym in s.get_symbols_in_file(rel)
                  if sym.kind is SymbolKind.METHOD and sym.name == "Find")
    assert method.parent_id == repo.id
    assert method.is_public is True
    assert method.qualified_name == "r.Repo.Find"
    assert method.signature == "(id int) (int, error)"


def test_value_receiver_method_chained(tmp_path: Path) -> None:
    src = (
        "package x\n"
        "type Repo struct{}\n"
        "func (r Repo) lower() bool { return false }\n"
    )
    abs_path, rel = _write(tmp_path, "r.go", src)
    s = Storage()
    index_go_file(abs_path, rel, s)
    repo = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.CLASS)
    method = next(sym for sym in s.get_symbols_in_file(rel)
                  if sym.kind is SymbolKind.METHOD and sym.name == "lower")
    assert method.parent_id == repo.id
    assert method.is_public is False  # lowercase = unexported


def test_method_on_generic_receiver_strips_generics(tmp_path: Path) -> None:
    """`func (r *Repo[T]) Find()` — receiver type lookup strips [T]."""
    src = (
        "package x\n"
        "type Repo[T any] struct{}\n"
        "func (r *Repo[T]) Find() {}\n"
    )
    abs_path, rel = _write(tmp_path, "r.go", src)
    s = Storage()
    index_go_file(abs_path, rel, s)
    repo = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.CLASS and sym.name == "Repo")
    method = next(sym for sym in s.get_symbols_in_file(rel)
                  if sym.kind is SymbolKind.METHOD and sym.name == "Find")
    assert method.parent_id == repo.id


# ── const / var → ASSIGNMENT ──────────────────────────────────────


def test_top_level_const_is_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.go",
                           "package x\nconst MaxRetries = 5\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert len(assigns) == 1
    assert assigns[0].name == "MaxRetries"
    assert assigns[0].is_public is True


def test_parenthesized_const_block_emits_each(tmp_path: Path) -> None:
    """`const ( A = 1; B = 2 )` → 2 ASSIGNMENT symbols."""
    abs_path, rel = _write(tmp_path, "c.go",
                           "package x\nconst (\n    A = 1\n    B = 2\n)\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    assigns = {sym.name for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT}
    assert {"A", "B"}.issubset(assigns)


def test_parenthesized_var_block_emits_each(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "v.go",
                           "package x\nvar (\n    X int\n    Y string\n)\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    assigns = {sym.name for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT}
    assert {"X", "Y"}.issubset(assigns)


# ── Imports ────────────────────────────────────────────────────────


def test_simple_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.go",
                           'package x\nimport "fmt"\n')
    s = Storage()
    index_go_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert ("fmt", "fmt", 2) in imports


def test_aliased_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.go",
                           'package x\nimport log "github.com/sirupsen/logrus"\n')
    s = Storage()
    index_go_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    assert any(m == "github.com/sirupsen/logrus" and b == "log"
               for m, b, _ in imports)


def test_paren_import_block(tmp_path: Path) -> None:
    src = (
        'package x\n'
        'import (\n'
        '    "fmt"\n'
        '    log "github.com/sirupsen/logrus"\n'
        '    "github.com/foo/bar/baz"\n'
        ')\n'
    )
    abs_path, rel = _write(tmp_path, "i.go", src)
    s = Storage()
    index_go_file(abs_path, rel, s)
    bound = {b for _, b, _ in s.get_imports_for_file(rel)}
    # Bare imports bind the LAST path segment
    assert bound == {"fmt", "log", "baz"}


# ── References ────────────────────────────────────────────────────


def test_call_expression_emits_call_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.go",
                           "package x\nfunc Caller() { Helper() }\n")
    s = Storage()
    index_go_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "Helper" for c in calls)


def test_selector_call_records_field_as_call(tmp_path: Path) -> None:
    """`fmt.Println("hi")` — Println is the called name."""
    abs_path, rel = _write(tmp_path, "c.go",
                           'package x\nfunc Caller() { fmt.Println("hi") }\n')
    s = Storage()
    index_go_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    loads = [r for r in refs if r.kind is RefKind.NAME_LOAD]
    assert any(c.raw_name == "Println" for c in calls)
    # Operand `fmt` shows up as a NAME_LOAD
    assert any(l.raw_name == "fmt" for l in loads)
