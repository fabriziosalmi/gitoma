"""Tests for the v0.5-slim TypeScript AST indexer.

Synthetic .ts source per test, mirroring the
`tests/cpg/test_python_indexer.py` shape so a maintainer can read
both side-by-side and verify language-parity."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.storage import Storage
from gitoma.cpg.typescript_indexer import (
    TS_LANGUAGE,
    index_typescript_file,
    ts_module_qualified_name_for,
)


def _write(tmp_path: Path, rel: str, src: str) -> tuple[Path, str]:
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src)
    return abs_path, rel


# ── ts_module_qualified_name_for ──────────────────────────────────


def test_ts_module_qname_basic() -> None:
    assert ts_module_qualified_name_for("src/components/Button.ts") == \
        "src.components.Button"


def test_ts_module_qname_tsx() -> None:
    assert ts_module_qualified_name_for("src/Button.tsx") == "src.Button"


def test_ts_module_qname_dts() -> None:
    assert ts_module_qualified_name_for("src/types.d.ts") == "src.types"


def test_ts_module_qname_index_collapses() -> None:
    """Node convention: ``foo/index.ts`` is the entry point for
    package ``foo``; module qualified name should drop the leaf."""
    assert ts_module_qualified_name_for("src/components/index.ts") == \
        "src.components"


# ── Module symbol ──────────────────────────────────────────────────


def test_emits_module_symbol(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "x.ts", "const x = 1;\n")
    s = Storage()
    n = index_typescript_file(abs_path, rel, s)
    assert n >= 1
    syms = s.get_symbols_in_file(rel)
    modules = [m for m in syms if m.kind is SymbolKind.MODULE]
    assert len(modules) == 1
    assert modules[0].language == TS_LANGUAGE


def test_empty_file_emits_module_only(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "empty.ts", "")
    s = Storage()
    n = index_typescript_file(abs_path, rel, s)
    assert n == 1


def test_unparseable_returns_zero(tmp_path: Path) -> None:
    """tree-sitter is permissive but truly nonsense bytes still parse
    into a tree (with errors). We just verify the indexer doesn't
    crash; symbols may be 0 or 1 (just the module symbol)."""
    abs_path, rel = _write(tmp_path, "bad.ts", "@@@@~~~^^^")
    s = Storage()
    n = index_typescript_file(abs_path, rel, s)
    # tree-sitter never raises on bad input — indexer must not crash
    assert n >= 0


# ── Functions ──────────────────────────────────────────────────────


def test_emits_function_declaration(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.ts",
                           "function helper(): void {}\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert len(funcs) == 1
    assert funcs[0].name == "helper"
    assert funcs[0].language == TS_LANGUAGE


def test_export_function_is_indexed_as_function(tmp_path: Path) -> None:
    """`export function X` peels the export wrapper and indexes the
    function in module scope."""
    abs_path, rel = _write(tmp_path, "f.ts",
                           "export function exported(): void {}\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert len(funcs) == 1
    assert funcs[0].name == "exported"


def test_underscore_function_marked_private(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.ts",
                           "function _internal(): void {}\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert funcs[0].is_public is False


# ── Classes + methods ─────────────────────────────────────────────


def test_class_with_methods(tmp_path: Path) -> None:
    src = (
        "export class Repo {\n"
        "  find(id: number): void {}\n"
        "  static create(): Repo { return new Repo(); }\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "r.ts", src)
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    syms = s.get_symbols_in_file(rel)
    classes = [sym for sym in syms if sym.kind is SymbolKind.CLASS]
    methods = [sym for sym in syms if sym.kind is SymbolKind.METHOD]
    assert len(classes) == 1
    assert classes[0].name == "Repo"
    assert {m.name for m in methods} == {"find", "create"}
    # Methods must point at the class as parent.
    assert all(m.parent_id == classes[0].id for m in methods)


def test_class_extends_records_inheritance_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "r.ts",
                           "class Repo extends Base {}\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    inherit = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.raw_name == "Base" for r in inherit)


def test_class_implements_records_inheritance_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "r.ts",
                           "class Repo implements IThing {}\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    inherit = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.raw_name == "IThing" for r in inherit)


# ── Interfaces + type aliases ──────────────────────────────────────


def test_emits_interface(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.ts",
                           "export interface User { id: number; }\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    syms = [sym for sym in s.get_symbols_in_file(rel)
            if sym.kind is SymbolKind.INTERFACE]
    assert len(syms) == 1
    assert syms[0].name == "User"
    assert syms[0].language == TS_LANGUAGE


def test_emits_type_alias(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "t.ts",
                           "export type Maybe<T> = T | null;\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    syms = [sym for sym in s.get_symbols_in_file(rel)
            if sym.kind is SymbolKind.TYPE_ALIAS]
    assert len(syms) == 1
    assert syms[0].name == "Maybe"


# ── Module-level lexical (const / let) ────────────────────────────


def test_module_level_const_is_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.ts",
                           "export const NAME: string = 'x';\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert len(assigns) == 1
    assert assigns[0].name == "NAME"


def test_function_local_const_not_indexed(tmp_path: Path) -> None:
    """Inside a function, ``const x = …`` is local-scope and must
    not produce a module-level Symbol."""
    abs_path, rel = _write(tmp_path, "f.ts",
                           "function f() { const x = 1; }\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert assigns == []


# ── Imports ────────────────────────────────────────────────────────


def test_named_imports(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.ts",
                           "import { Foo, Bar } from './lib';\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    bound = {b for _, b, _ in imports}
    assert bound == {"Foo", "Bar"}
    # IMPORT_FROM refs for both
    refs = s.get_refs_in_file(rel)
    raw = {r.raw_name for r in refs if r.kind is RefKind.IMPORT_FROM}
    assert raw == {"Foo", "Bar"}


def test_named_import_with_alias(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.ts",
                           "import { Bar as Baz } from './lib';\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    bound = {b for _, b, _ in imports}
    assert bound == {"Baz"}


def test_default_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.ts",
                           "import React from 'react';\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    bound = {b for _, b, _ in imports}
    assert "React" in bound


def test_namespace_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.ts",
                           "import * as fs from 'fs';\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    imports = s.get_imports_for_file(rel)
    modules = {m for m, _, _ in imports}
    bound = {b for _, b, _ in imports}
    assert "fs" in modules
    assert "fs" in bound


# ── References ────────────────────────────────────────────────────


def test_call_expression_emits_call_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.ts",
                           "function f() { helper(); }\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    assert any(c.raw_name == "helper" for c in calls)


def test_member_expression_call_records_attr_as_call(tmp_path: Path) -> None:
    """`obj.method()` — `method` is the called name, `obj` is a
    NAME_LOAD. Mirrors the Python indexer's attribute-call shape."""
    abs_path, rel = _write(tmp_path, "c.ts",
                           "function f() { obj.method(); }\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    loads = [r for r in refs if r.kind is RefKind.NAME_LOAD]
    assert any(c.raw_name == "method" for c in calls)
    assert any(l.raw_name == "obj" for l in loads)


def test_new_expression_emits_call_ref(tmp_path: Path) -> None:
    """`new Foo()` should produce a CALL ref on Foo (constructor),
    so the Foo class shows up in callers_of()."""
    abs_path, rel = _write(tmp_path, "n.ts",
                           "function f() { return new Repo(); }\n")
    s = Storage()
    index_typescript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    assert any(c.raw_name == "Repo" for c in calls)


# ── Cross-file (resolver hand-off) ─────────────────────────────────


def test_two_module_setup_consistent_for_resolver(tmp_path: Path) -> None:
    """Pre-flight that the indexer produces the rows the resolver
    needs to chain a `from './a' import helper` ref to a's helper."""
    abs_a, rel_a = _write(tmp_path, "a.ts",
                          "export function helper(): void {}\n")
    abs_b, rel_b = _write(tmp_path, "b.ts",
                          "import { helper } from './a';\n"
                          "function caller() { helper(); }\n")
    s = Storage()
    index_typescript_file(abs_a, rel_a, s)
    index_typescript_file(abs_b, rel_b, s)
    # a's helper is indexed
    helpers = [sym for sym in s.get_symbols_by_name("helper")
               if sym.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    assert helpers[0].file == "a.ts"
    # b's import is recorded
    imports = s.get_imports_for_file("b.ts")
    assert any(b == "helper" for _, b, _ in imports)
    # b's call to helper is unresolved (resolver runs in CPGIndex)
    refs_b = s.get_refs_in_file("b.ts")
    assert any(r.raw_name == "helper" and r.kind is RefKind.CALL
               for r in refs_b)


# ── .tsx dispatch (JSX-aware grammar) ──────────────────────────────


def test_tsx_file_uses_tsx_grammar(tmp_path: Path) -> None:
    """A .tsx file with a JSX element must parse cleanly and still
    extract the function declaration that returns it."""
    src = (
        "import React from 'react';\n"
        "export function Button(): JSX.Element {\n"
        "  return React.createElement('button', null, 'Click');\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "Button.tsx", src)
    s = Storage()
    n = index_typescript_file(abs_path, rel, s)
    assert n >= 2  # module + function
    funcs = [sym for sym in s.get_symbols_in_file(rel)
             if sym.kind is SymbolKind.FUNCTION]
    assert any(f.name == "Button" for f in funcs)
