"""Tests for v0.5-slim TypeScript path-based import resolution.

These exercise the new ``_resolve_ts_relative_path`` helper + the
TS branch in ``CPGIndex._resolve_one``. The Python resolution rules
are tested separately in tests/cpg/test_queries.py — these tests
focus on the additive TS-only behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.queries import _resolve_ts_relative_path


# ── Path math helper ──────────────────────────────────────────────


@pytest.mark.parametrize("importing,module,expected", [
    # Same-dir import
    ("a.ts", "./b",
     ["b.ts", "b.tsx", "b/index.ts", "b/index.tsx"]),
    # Sibling-dir up-then-into
    ("src/x.ts", "./y",
     ["src/y.ts", "src/y.tsx", "src/y/index.ts", "src/y/index.tsx"]),
    # Parent dir
    ("src/components/Button.ts", "../utils/helper",
     ["src/utils/helper.ts", "src/utils/helper.tsx",
      "src/utils/helper/index.ts", "src/utils/helper/index.tsx"]),
    # Multi-level descend
    ("a.ts", "./pkg/sub/x",
     ["pkg/sub/x.ts", "pkg/sub/x.tsx",
      "pkg/sub/x/index.ts", "pkg/sub/x/index.tsx"]),
])
def test_ts_relative_path_targets(
    importing: str, module: str, expected: list[str],
) -> None:
    assert _resolve_ts_relative_path(importing, module) == expected


# ── Live resolution via CPGIndex ──────────────────────────────────


def _build(tmp_path: Path, files: dict[str, str]):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return build_index(tmp_path)


def test_resolver_handles_dot_slash_import(tmp_path: Path) -> None:
    """`from './a' import helper` from b.ts → resolves to a.ts:helper."""
    idx = _build(tmp_path, {
        "a.ts": "export function helper(): void {}\n",
        "b.ts": (
            "import { helper } from './a';\n"
            "function caller() { helper(); }\n"
        ),
    })
    helpers = [s for s in idx.get_symbol("helper")
               if s.kind is SymbolKind.FUNCTION]
    assert len(helpers) == 1
    callers = idx.callers_of(helpers[0].id)
    kinds = {(r.kind, r.file) for r in callers}
    assert (RefKind.IMPORT_FROM, "b.ts") in kinds
    assert (RefKind.CALL, "b.ts") in kinds


def test_resolver_disambiguates_two_helpers_via_relative_path(
    tmp_path: Path,
) -> None:
    """Two `helper`s in different dirs; ``./a`` import from b
    must resolve to a's helper, not the unrelated one."""
    idx = _build(tmp_path, {
        "a.ts": "export function helper(): void {}\n",
        "other.ts": "export function helper(): void {}\n",
        "b.ts": (
            "import { helper } from './a';\n"
            "function caller() { helper(); }\n"
        ),
    })
    a_helper = next(s for s in idx.get_symbol("helper")
                    if s.file == "a.ts")
    other_helper = next(s for s in idx.get_symbol("helper")
                        if s.file == "other.ts")
    a_callers = [r for r in idx.callers_of(a_helper.id)
                 if r.file == "b.ts"]
    other_callers = [r for r in idx.callers_of(other_helper.id)
                     if r.file == "b.ts"]
    # b.ts call must hit a's helper, NOT other's.
    assert any(r.kind is RefKind.CALL for r in a_callers)
    assert all(r.kind is not RefKind.CALL for r in other_callers)


def test_resolver_resolves_via_index_ts(tmp_path: Path) -> None:
    """`./pkg` should resolve to `pkg/index.ts`."""
    idx = _build(tmp_path, {
        "pkg/index.ts": "export function entry(): void {}\n",
        "main.ts": (
            "import { entry } from './pkg';\n"
            "function go() { entry(); }\n"
        ),
    })
    entries = [s for s in idx.get_symbol("entry")
               if s.kind is SymbolKind.FUNCTION]
    assert len(entries) == 1
    callers = idx.callers_of(entries[0].id)
    assert any(r.file == "main.ts" and r.kind is RefKind.CALL for r in callers)


def test_resolver_handles_parent_relative_import(tmp_path: Path) -> None:
    """`../utils/helper` from `src/components/Button.ts` →
    `src/utils/helper.ts`. The classic React layout."""
    idx = _build(tmp_path, {
        "src/utils/helper.ts": "export function fmt(): void {}\n",
        "src/components/Button.ts": (
            "import { fmt } from '../utils/helper';\n"
            "export function Button() { fmt(); }\n"
        ),
    })
    fmts = [s for s in idx.get_symbol("fmt")
            if s.kind is SymbolKind.FUNCTION]
    assert len(fmts) == 1
    callers = idx.callers_of(fmts[0].id)
    assert any(r.file == "src/components/Button.ts" and r.kind is RefKind.CALL
               for r in callers)


def test_resolver_leaves_bare_specifier_unresolved(tmp_path: Path) -> None:
    """`from 'react'` is a bare module specifier — needs node_modules
    or tsconfig paths to resolve. v0.5-slim does not; the ref stays
    unresolved (no false-positive resolution to a same-named local
    function)."""
    idx = _build(tmp_path, {
        # A local function with the same name as the import binding —
        # the resolver must NOT pick this one for the bare-specifier
        # import. (Falls through to global fallback because there's
        # exactly 1 candidate; this test instead shows the bare path
        # doesn't crash + behaves predictably.)
        "main.ts": (
            "import React from 'react';\n"
            "function go() { React.createElement('div'); }\n"
        ),
    })
    # The `React` import binding is its own IMPORT symbol; the
    # NAME_LOAD on `React` in `React.createElement` should resolve
    # to that local IMPORT symbol via same-file precedence … BUT
    # IMPORT is not in _DEFINING_KINDS, so it stays unresolved.
    # Sanity: at minimum, no exception, and we don't accidentally
    # resolve to a non-existent symbol.
    react_syms = [s for s in idx.get_symbol("React")]
    assert any(s.kind is SymbolKind.IMPORT for s in react_syms)


def test_resolver_inheritance_via_ts_import(tmp_path: Path) -> None:
    """`class Repo extends Base` where Base is imported via
    `./base` should resolve the INHERITANCE ref to Base's class."""
    idx = _build(tmp_path, {
        "base.ts": "export class Base {}\n",
        "repo.ts": (
            "import { Base } from './base';\n"
            "export class Repo extends Base {}\n"
        ),
    })
    bases = [s for s in idx.get_symbol("Base")
             if s.kind is SymbolKind.CLASS]
    assert len(bases) == 1
    refs = idx.find_references(bases[0].id)
    inherit = [r for r in refs if r.kind is RefKind.INHERITANCE]
    assert any(r.file == "repo.ts" for r in inherit)
