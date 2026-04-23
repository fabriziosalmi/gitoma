"""Tests for G12 — ``validate_config_grounding``.

Headline test: ``test_b2v_pr21_prettier_config_caught`` replays the
exact ``prettier.config.js`` shipped in b2v PR #21 — references
``prettier-plugin-tailwindcss`` but tailwindcss isn't in npm deps."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.config_grounding import (
    CONFIG_FILE_BASENAMES,
    NODE_BUILTINS,
    _extract_package_refs,
    _is_config_file,
    _normalise_package,
    validate_config_grounding,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


def _b2v_fingerprint() -> dict:
    """Mirrors b2v's actual stack: Rust + a tiny package.json with
    only vitepress for docs. Catches the PR #21 case where prettier
    config referenced packages outside the declared deps."""
    return {
        "manifest_files": ["Cargo.toml", "package.json"],
        "declared_deps": {
            "rust": ["clap", "serde", "tokio", "anyhow"],
            "npm": ["vitepress"],
            "python": [],
            "go": [],
        },
        "declared_frameworks": ["clap", "serde"],
    }


def _react_fingerprint() -> dict:
    """A real React+TypeScript app fingerprint — used to verify
    valid configs pass clean against a populated dep set."""
    return {
        "manifest_files": ["package.json"],
        "declared_deps": {
            "rust": [],
            "npm": [
                "react", "react-dom", "vite", "@vitejs/plugin-react",
                "typescript", "tailwindcss", "prettier",
                "prettier-plugin-tailwindcss",
            ],
            "python": [], "go": [],
        },
        "declared_frameworks": ["react", "vite", "tailwindcss"],
    }


# ── The b2v PR #21 headline case ──────────────────────────────────────


def test_b2v_pr21_prettier_config_caught(tmp_path: Path) -> None:
    """Verbatim ``prettier.config.js`` from b2v PR #21 — references
    ``prettier-plugin-tailwindcss`` in the plugins array, but b2v's
    package.json only declares ``vitepress``. G12 must flag it with
    the literal + the resolved package + the declared sample."""
    pr21_body = """// Prettier configuration for JavaScript/TypeScript files
module.exports = {
  semi: false,
  singleQuote: true,
  trailingComma: 'es5',
  printWidth: 80,
  tabWidth: 2,
  endOfLine: 'auto',
  arrowParens: 'always',
  proseWrap: 'never',
  bracketSpacing: true,
  importOrder: ['^react/(.*)$', '^@/(.*)$', '^[a-z]'],
  importOrderType: 'namespace',
  plugins: ['prettier-plugin-tailwindcss']
};
"""
    rel = _write(tmp_path, "prettier.config.js", pr21_body)
    result = validate_config_grounding(tmp_path, [rel], _b2v_fingerprint())
    assert result is not None
    path, msg = result
    assert path == "prettier.config.js"
    # Cites the literal + the resolved package
    assert "prettier-plugin-tailwindcss" in msg
    # Cites the declared deps for context
    assert "vitepress" in msg


def test_valid_prettier_config_passes(tmp_path: Path) -> None:
    """Same shape as the bad config but every plugin IS declared in
    npm deps — must validate clean."""
    body = """module.exports = {
  semi: false,
  plugins: ['prettier-plugin-tailwindcss']
};
"""
    rel = _write(tmp_path, "prettier.config.js", body)
    assert validate_config_grounding(tmp_path, [rel], _react_fingerprint()) is None


# ── Extractor coverage ────────────────────────────────────────────────


@pytest.mark.parametrize("snippet,expected", [
    ("require('lodash')",                 {"lodash"}),
    ("const x = require(\"react-dom\")", {"react-dom"}),
    ("import x from 'react'",             {"react"}),
    ("import 'side-effect-pkg'",          {"side-effect-pkg"}),
    ("import { y } from 'lodash/fp'",     {"lodash/fp"}),
    ("plugins: ['a', 'b', 'c']",          {"a", "b", "c"}),
    ("presets: ['@babel/preset-env']",    {"@babel/preset-env"}),
])
def test_extract_package_refs_shapes(snippet: str, expected: set[str]) -> None:
    refs = _extract_package_refs(snippet)
    assert expected.issubset(refs), f"missing from {refs!r}: {expected - refs}"


def test_extract_skips_comments_in_practice() -> None:
    """The extractor is intentionally NAIVE about comments — this
    test documents the trade-off. A reference inside a // comment
    WILL get picked up. In practice the FP cost is low: if a
    commented-out import exists, the package is probably real and
    in deps anyway. If it isn't, surfacing it is arguably useful."""
    text = "// import 'some-old-pkg'\nimport real from 'react';"
    refs = _extract_package_refs(text)
    assert "react" in refs
    assert "some-old-pkg" in refs   # documented FP trade-off


def test_extract_finds_multiple_in_plugin_array() -> None:
    """Multi-plugin arrays — common in real prettier/eslint configs."""
    text = """plugins: [
        'prettier-plugin-tailwindcss',
        '@trivago/prettier-plugin-sort-imports',
        'prettier-plugin-organize-imports'
    ]"""
    refs = _extract_package_refs(text)
    assert "prettier-plugin-tailwindcss" in refs
    assert "@trivago/prettier-plugin-sort-imports" in refs
    assert "prettier-plugin-organize-imports" in refs


# ── Normaliser coverage ───────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("react",                "react"),
    ("react-dom",            "react-dom"),
    ("@vue/cli",             "@vue/cli"),
    ("@scope/pkg/sub/x",     "@scope/pkg"),
    ("lodash/fp/get",        "lodash"),
    ("./relative",           None),
    ("../parent",            None),
    ("/absolute/path",       None),
    ("",                     None),
    ("@",                    None),
    ("@scope-only",          None),
])
def test_normalise_package(name: str, expected) -> None:
    assert _normalise_package(name) == expected


# ── Builtin & relative skipping ───────────────────────────────────────


def test_node_builtins_pass(tmp_path: Path) -> None:
    """A config that imports ``fs`` / ``node:path`` / ``crypto``
    must validate clean against any fingerprint — these aren't
    npm packages."""
    body = """const fs = require('fs');
const path = require('node:path');
const { createHash } = require('crypto');
module.exports = { fs, path, createHash };
"""
    rel = _write(tmp_path, "vite.config.js", body)
    assert validate_config_grounding(tmp_path, [rel], _b2v_fingerprint()) is None


def test_relative_imports_pass(tmp_path: Path) -> None:
    """Relative paths point at sibling files in the repo, not packages."""
    body = """const local = require('./helpers');
const sibling = require('../config/base');
module.exports = { local, sibling };
"""
    rel = _write(tmp_path, "vite.config.js", body)
    assert validate_config_grounding(tmp_path, [rel], _b2v_fingerprint()) is None


def test_scoped_package_grounded(tmp_path: Path) -> None:
    """Scoped packages (``@scope/pkg``) are normalised before
    membership-test, so a config that imports ``@vitejs/plugin-react``
    grounds correctly when that scoped pkg is in npm deps."""
    body = "import react from '@vitejs/plugin-react';\nexport default { plugins: [react()] };"
    rel = _write(tmp_path, "vite.config.ts", body)
    assert validate_config_grounding(tmp_path, [rel], _react_fingerprint()) is None


def test_subpath_grounded(tmp_path: Path) -> None:
    """``import x from 'lodash/fp'`` — the npm dep is ``lodash``,
    the import has a sub-path. Must ground via the normaliser."""
    body = "const fp = require('lodash/fp');"
    rel = _write(tmp_path, "vite.config.js", body)
    fp = {
        "manifest_files": ["package.json"],
        "declared_deps": {"npm": ["lodash"], "rust": [], "python": [], "go": []},
    }
    assert validate_config_grounding(tmp_path, [rel], fp) is None


# ── Silent-pass paths ─────────────────────────────────────────────────


def test_no_fingerprint_silent_pass(tmp_path: Path) -> None:
    rel = _write(tmp_path, "prettier.config.js", "plugins: ['random-pkg']")
    assert validate_config_grounding(tmp_path, [rel], None) is None
    assert validate_config_grounding(tmp_path, [rel], {}) is None


def test_no_package_json_silent_pass(tmp_path: Path) -> None:
    """Pure-Rust repo with NO package.json — npm grounding can't
    apply. Even a JS config (rare in such repos) should silent-pass
    to avoid false-positives during the rare cross-stack tooling
    addition."""
    rel = _write(tmp_path, "prettier.config.js", "plugins: ['x']")
    fp = {
        "manifest_files": ["Cargo.toml"],     # no package.json
        "declared_deps": {"npm": [], "rust": ["clap"], "python": [], "go": []},
    }
    assert validate_config_grounding(tmp_path, [rel], fp) is None


def test_non_config_file_silent_pass(tmp_path: Path) -> None:
    """Random ``.js`` files (application code, tests, etc.) are
    OUT of scope — G12 only scans the closed CONFIG_FILE_BASENAMES
    set. Application code calling ``require('lodash')`` even when
    lodash isn't declared is npm's problem, not G12's."""
    rel = _write(tmp_path, "src/app.js", "const _ = require('definitely-not-installed');")
    assert validate_config_grounding(tmp_path, [rel], _react_fingerprint()) is None


def test_missing_file_silent_pass(tmp_path: Path) -> None:
    assert validate_config_grounding(
        tmp_path, ["prettier.config.js"], _b2v_fingerprint(),
    ) is None


def test_empty_touched_is_noop(tmp_path: Path) -> None:
    assert validate_config_grounding(tmp_path, [], _b2v_fingerprint()) is None


def test_clean_config_with_no_external_imports(tmp_path: Path) -> None:
    """A pure-data config with no requires/imports → nothing to ground."""
    body = """module.exports = {
  semi: false,
  singleQuote: true,
  printWidth: 100
};
"""
    rel = _write(tmp_path, "prettier.config.js", body)
    assert validate_config_grounding(tmp_path, [rel], _b2v_fingerprint()) is None


# ── First-violation short-circuit ─────────────────────────────────────


def test_first_violation_short_circuits(tmp_path: Path) -> None:
    """Two bad config files → return ONLY the first listed in
    ``touched`` — same shape as G2/G7/G10/G11."""
    rel1 = _write(tmp_path, "prettier.config.js", "plugins: ['ungrounded-a']")
    rel2 = _write(tmp_path, "tailwind.config.js", "plugins: ['ungrounded-b']")
    result = validate_config_grounding(tmp_path, [rel1, rel2], _b2v_fingerprint())
    assert result is not None
    assert result[0] == "prettier.config.js"


def test_one_clean_one_dirty_returns_dirty(tmp_path: Path) -> None:
    """Mix of grounded + ungrounded — returns the ungrounded one."""
    clean = _write(tmp_path, "prettier.config.js", "module.exports = { semi: false };")
    dirty = _write(tmp_path, "tailwind.config.js", "plugins: ['unknown-plugin']")
    result = validate_config_grounding(tmp_path, [clean, dirty], _b2v_fingerprint())
    assert result is not None
    assert result[0] == "tailwind.config.js"


# ── Constants sanity ──────────────────────────────────────────────────


def test_config_basenames_includes_common_tools() -> None:
    """Sanity: every "obviously a config" file we'd expect to scan
    is in the set. Add an entry to the set + a fixture if a new
    tool gets popular."""
    for must_have in (
        "prettier.config.js", "tailwind.config.js", "vite.config.ts",
        "next.config.js", "webpack.config.js", "playwright.config.ts",
    ):
        assert must_have in CONFIG_FILE_BASENAMES, f"missing: {must_have}"


def test_node_builtins_includes_node_prefix_variants() -> None:
    """Node 16+ supports the ``node:`` prefix (``node:fs`` ≡ ``fs``).
    Both must be skipped to avoid false-positives in modern configs."""
    assert "fs" in NODE_BUILTINS
    assert "node:fs" in NODE_BUILTINS
    assert "node:path" in NODE_BUILTINS


def test_is_config_file_requires_basename_match() -> None:
    """Path-prefix doesn't matter — only basename. A monorepo's
    ``packages/web/prettier.config.js`` is still a config."""
    assert _is_config_file("prettier.config.js")
    assert _is_config_file("packages/web/prettier.config.js")
    assert not _is_config_file("src/main.js")
    assert not _is_config_file("README.md")
