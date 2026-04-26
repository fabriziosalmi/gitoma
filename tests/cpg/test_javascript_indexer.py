"""Tests for the v0.5-expansion JavaScript AST indexer.

Same shape as tests/cpg/test_typescript_indexer.py minus the
TypeScript-only constructs (interfaces / type aliases / type
annotations). Read both side-by-side when extending."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.javascript_indexer import (
    JS_LANGUAGE,
    index_javascript_file,
    js_module_qualified_name_for,
)
from gitoma.cpg.storage import Storage


def _write(tmp_path: Path, rel: str, src: str) -> tuple[Path, str]:
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src)
    return abs_path, rel


# ── js_module_qualified_name_for ──────────────────────────────────


def test_js_module_qname_basic() -> None:
    assert js_module_qualified_name_for("src/components/Button.js") == \
        "src.components.Button"


def test_js_module_qname_mjs() -> None:
    assert js_module_qualified_name_for("src/index.mjs") == "src"


def test_js_module_qname_cjs() -> None:
    assert js_module_qualified_name_for("lib/util.cjs") == "lib.util"


def test_js_module_qname_index_collapses() -> None:
    assert js_module_qualified_name_for("src/components/index.js") == \
        "src.components"


# ── Module symbol ──────────────────────────────────────────────────


def test_emits_module_symbol(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "x.js", "const x = 1;\n")
    s = Storage()
    n = index_javascript_file(abs_path, rel, s)
    assert n >= 1
    modules = [m for m in s.get_symbols_in_file(rel)
               if m.kind is SymbolKind.MODULE]
    assert len(modules) == 1
    assert modules[0].language == JS_LANGUAGE


def test_empty_file_emits_module_only(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "empty.js", "")
    s = Storage()
    n = index_javascript_file(abs_path, rel, s)
    assert n == 1


# ── Functions ──────────────────────────────────────────────────────


def test_function_with_params_signature(tmp_path: Path) -> None:
    """JS captures parameters but no return type."""
    abs_path, rel = _write(tmp_path, "f.js",
                           "export function helper(x, y = 5) { return x + y; }\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.signature == "(x, y = 5)"
    assert func.language == JS_LANGUAGE


def test_function_no_args_signature(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.js",
                           "function tick() {}\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.signature == "()"


def test_underscore_function_marked_private(tmp_path: Path) -> None:
    """JS doesn't have a hard-private convention; we use the same
    leading-underscore heuristic as Python / TS for consistency."""
    abs_path, rel = _write(tmp_path, "f.js",
                           "function _internal() {}\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    func = next(sym for sym in s.get_symbols_in_file(rel)
                if sym.kind is SymbolKind.FUNCTION)
    assert func.is_public is False


# ── Classes + methods ─────────────────────────────────────────────


def test_class_with_methods(tmp_path: Path) -> None:
    src = (
        "export class Repo {\n"
        "  find(id) { return null; }\n"
        "  static create() { return new Repo(); }\n"
        "}\n"
    )
    abs_path, rel = _write(tmp_path, "r.js", src)
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    syms = s.get_symbols_in_file(rel)
    classes = [sym for sym in syms if sym.kind is SymbolKind.CLASS]
    methods = [sym for sym in syms if sym.kind is SymbolKind.METHOD]
    assert len(classes) == 1
    assert {m.name for m in methods} == {"find", "create"}
    assert all(m.parent_id == classes[0].id for m in methods)


def test_class_extends_records_inheritance(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "r.js",
                           "class Repo extends Base {}\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    inherit = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.raw_name == "Base" for r in inherit)


# ── Module-level lexical (const / let) ────────────────────────────


def test_module_level_const_is_assignment(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.js",
                           "export const NAME = 'x';\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert len(assigns) == 1
    assert assigns[0].name == "NAME"


def test_function_local_const_not_indexed(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "f.js",
                           "function f() { const x = 1; }\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    assigns = [sym for sym in s.get_symbols_in_file(rel)
               if sym.kind is SymbolKind.ASSIGNMENT]
    assert assigns == []


# ── Imports ────────────────────────────────────────────────────────


def test_named_imports(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.js",
                           "import { Foo, Bar } from './lib';\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    bound = {b for _, b, _ in s.get_imports_for_file(rel)}
    assert bound == {"Foo", "Bar"}


def test_default_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.js",
                           "import React from 'react';\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    bound = {b for _, b, _ in s.get_imports_for_file(rel)}
    assert "React" in bound


def test_namespace_import(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "i.js",
                           "import * as fs from 'fs';\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    bound = {b for _, b, _ in s.get_imports_for_file(rel)}
    assert "fs" in bound


# ── References ────────────────────────────────────────────────────


def test_call_expression_emits_call_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.js",
                           "function f() { helper(); }\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "helper" for c in calls)


def test_member_expression_call_records_attr_as_call(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "c.js",
                           "function f() { obj.method(); }\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    refs = s.get_refs_in_file(rel)
    calls = [r for r in refs if r.kind is RefKind.CALL]
    loads = [r for r in refs if r.kind is RefKind.NAME_LOAD]
    assert any(c.raw_name == "method" for c in calls)
    assert any(l.raw_name == "obj" for l in loads)


def test_new_expression_emits_call_ref(tmp_path: Path) -> None:
    abs_path, rel = _write(tmp_path, "n.js",
                           "function f() { return new Repo(); }\n")
    s = Storage()
    index_javascript_file(abs_path, rel, s)
    calls = [r for r in s.get_refs_in_file(rel)
             if r.kind is RefKind.CALL]
    assert any(c.raw_name == "Repo" for c in calls)


# ── Cross-file (resolver hand-off) ─────────────────────────────────


def test_two_module_setup_consistent_for_resolver(tmp_path: Path) -> None:
    abs_a, rel_a = _write(tmp_path, "a.js",
                          "export function helper() {}\n")
    abs_b, rel_b = _write(tmp_path, "b.js",
                          "import { helper } from './a';\n"
                          "function caller() { helper(); }\n")
    s = Storage()
    index_javascript_file(abs_a, rel_a, s)
    index_javascript_file(abs_b, rel_b, s)
    helpers = [sym for sym in s.get_symbols_by_name("helper")
               if sym.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    assert any(b == "helper" for _, b, _ in s.get_imports_for_file("b.js"))
    refs_b = s.get_refs_in_file("b.js")
    assert any(r.raw_name == "helper" and r.kind is RefKind.CALL for r in refs_b)
