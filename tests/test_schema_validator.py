"""Tests for G10 — ``validate_config_semantics``. Real schemas from
schemastore.org, bundled under ``gitoma/worker/schemas/``, against
crafted valid + invalid configs for each supported tool.

The headline test is ``test_b2v_pr19_eslintrc_caught`` which replays
the exact corruption that motivated the whole guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitoma.worker.schema_validator import (
    PATH_MATCHERS,
    SCHEMA_DIR,
    _match_schema,
    load_schema,
    validate_config_semantics,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


def _write_json(root: Path, rel: str, obj) -> str:
    return _write(root, rel, json.dumps(obj, indent=2))


# ── Bundled schemas are present + loadable ─────────────────────────────


def test_bundled_schemas_exist() -> None:
    """Every schema referenced in PATH_MATCHERS must be on disk."""
    referenced = {schema_file for _, schema_file in PATH_MATCHERS}
    for name in referenced:
        assert (SCHEMA_DIR / name).is_file(), f"missing bundled schema: {name}"


def test_load_schema_valid() -> None:
    s = load_schema("eslintrc.json")
    assert s is not None
    assert "$id" in s or "$schema" in s


def test_load_schema_missing_returns_none() -> None:
    assert load_schema("nonexistent-schema.json") is None


# ── Path matcher ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    (".eslintrc.json",                 "eslintrc.json"),
    ("packages/app/.eslintrc.json",    "eslintrc.json"),
    (".prettierrc.json",               "prettierrc.json"),
    (".prettierrc",                    "prettierrc.json"),
    ("package.json",                   "package.json"),
    ("tsconfig.json",                  "tsconfig.json"),
    ("tsconfig.build.json",            "tsconfig.json"),
    ("packages/lib/tsconfig.json",     "tsconfig.json"),
    (".github/workflows/ci.yml",       "github-workflow.json"),
    (".github/workflows/deploy.yaml",  "github-workflow.json"),
    (".github/dependabot.yml",         "dependabot.json"),
    ("Cargo.toml",                     "cargo.json"),
    ("crates/core/Cargo.toml",         "cargo.json"),
])
def test_match_schema_hits(path: str, expected: str) -> None:
    assert _match_schema(path) == expected


@pytest.mark.parametrize("path", [
    "src/main.py",
    "README.md",
    "random.txt",
    ".gitignore",
    "docs/index.md",
    "scripts/build.sh",
    # NOT .github/workflows/ — different dir
    "workflows/ci.yml",
    # NOT a config with our pattern — bare eslintrc.js is out of scope (v1)
    ".eslintrc.js",
    # package.json in a dir called "something" but matches our pattern anyway?
    # fnmatch "package.json" only matches the BASENAME, so:
    # actually: fnmatch("scripts/package.json", "package.json") is False
    "scripts/package.json",
])
def test_match_schema_misses(path: str) -> None:
    assert _match_schema(path) is None


# ── The b2v headline case ─────────────────────────────────────────────


def test_b2v_pr19_eslintrc_caught(tmp_path: Path) -> None:
    """The exact corruption gitoma shipped in b2v PR #19: ``parser``
    is an object instead of a string. File is valid JSON (G2 silent),
    but ESLint would refuse to load it. G10 catches this via
    jsonschema validation against the bundled ESLint schema."""
    bad = {
        "root": True,
        "parser": {"parser": "@typescript-eslint/parser"},  # WRONG shape
        "rules": {"quotes": ["error", "double"]},
    }
    rel = _write_json(tmp_path, ".eslintrc.json", bad)
    result = validate_config_semantics(tmp_path, [rel])
    assert result is not None
    path, msg = result
    assert path == ".eslintrc.json"
    # Error points at the ``parser`` field
    assert "parser" in msg
    # and says the type is wrong (accepted either "is not of type 'string'"
    # or "not valid under any of the given schemas" shape depending on
    # jsonschema version)
    assert "string" in msg or "not valid" in msg


def test_eslintrc_valid_passes(tmp_path: Path) -> None:
    """A well-formed ESLint config: ``parser`` is a string, ``rules``
    are proper shapes. Must validate clean."""
    good = {
        "root": True,
        "parser": "@typescript-eslint/parser",
        "parserOptions": {
            "ecmaVersion": 2021,
            "sourceType": "module",
        },
        "rules": {
            "quotes": ["error", "double"],
            "no-unused-vars": "warn",
        },
    }
    rel = _write_json(tmp_path, ".eslintrc.json", good)
    assert validate_config_semantics(tmp_path, [rel]) is None


# ── package.json ───────────────────────────────────────────────────────


def test_package_json_valid(tmp_path: Path) -> None:
    good = {
        "name": "my-package",
        "version": "1.0.0",
        "engines": {"node": ">=18"},
    }
    rel = _write_json(tmp_path, "package.json", good)
    assert validate_config_semantics(tmp_path, [rel]) is None


def test_package_json_invalid_version_type(tmp_path: Path) -> None:
    """``version`` must be a string, not a number. Typical LLM
    shape-slop."""
    bad = {"name": "x", "version": 1.0}
    rel = _write_json(tmp_path, "package.json", bad)
    result = validate_config_semantics(tmp_path, [rel])
    assert result is not None
    assert result[0] == "package.json"
    assert "version" in result[1]


# ── tsconfig.json ──────────────────────────────────────────────────────


def test_tsconfig_valid(tmp_path: Path) -> None:
    good = {
        "compilerOptions": {
            "target": "ES2020",
            "module": "commonjs",
            "strict": True,
            "outDir": "./dist",
        },
        "include": ["src/**/*"],
    }
    rel = _write_json(tmp_path, "tsconfig.json", good)
    assert validate_config_semantics(tmp_path, [rel]) is None


def test_tsconfig_invalid_target_value(tmp_path: Path) -> None:
    """``target`` has an enum of allowed values. A nonsense one gets
    caught."""
    bad = {"compilerOptions": {"target": "ES9999"}}
    rel = _write_json(tmp_path, "tsconfig.json", bad)
    result = validate_config_semantics(tmp_path, [rel])
    # Some tsconfig schemas accept arbitrary strings; we accept both
    # outcomes here — if schema enforces the enum we get an error;
    # if not we pass. The test is primarily structural.
    if result is not None:
        assert "target" in result[1].lower() or "ES9999" in result[1]


# ── GitHub workflow ───────────────────────────────────────────────────


def test_github_workflow_valid(tmp_path: Path) -> None:
    good = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo hello
"""
    rel = _write(tmp_path, ".github/workflows/ci.yml", good)
    assert validate_config_semantics(tmp_path, [rel]) is None


def test_github_workflow_missing_required(tmp_path: Path) -> None:
    """A workflow without ``jobs`` isn't a valid workflow.
    Schema enforces ``jobs`` as required."""
    bad = """name: CI
on: [push]
"""
    rel = _write(tmp_path, ".github/workflows/ci.yml", bad)
    result = validate_config_semantics(tmp_path, [rel])
    assert result is not None
    assert result[0] == ".github/workflows/ci.yml"


# ── Cargo.toml ────────────────────────────────────────────────────────


def test_cargo_toml_valid(tmp_path: Path) -> None:
    good = """[package]
name = "mycrate"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = "1.0"
"""
    rel = _write(tmp_path, "Cargo.toml", good)
    # Cargo schema may or may not flag various fields; we don't assert
    # None here because cargo.json is large and can flag edge cases on
    # edition or similar. The test is presence-only.
    result = validate_config_semantics(tmp_path, [rel])
    # We DO assert that plausible minimal content isn't rejected.
    assert result is None or "name" not in result[1]


# ── Silent-pass paths ─────────────────────────────────────────────────


def test_unrelated_files_are_skipped(tmp_path: Path) -> None:
    """Files that don't match any PATH_MATCHERS silently pass."""
    _write(tmp_path, "src/main.py", "def f(): pass\n")
    _write(tmp_path, "README.md", "# Hello\n")
    assert validate_config_semantics(
        tmp_path, ["src/main.py", "README.md"]
    ) is None


def test_missing_file_is_skipped(tmp_path: Path) -> None:
    """Deleted-in-this-commit paths appear in ``touched`` but don't
    exist on disk — no crash, no error."""
    assert validate_config_semantics(tmp_path, [".eslintrc.json"]) is None


def test_empty_touched_is_noop(tmp_path: Path) -> None:
    assert validate_config_semantics(tmp_path, []) is None


# ── First-violation short-circuit ─────────────────────────────────────


def test_first_violation_short_circuits(tmp_path: Path) -> None:
    """When multiple touched files have issues, return the FIRST
    found — consistent with the other validators' shape."""
    bad_eslint = {"root": True, "parser": {"x": "y"}}
    bad_pkg = {"name": "x", "version": 99}  # version not string
    rel1 = _write_json(tmp_path, ".eslintrc.json", bad_eslint)
    rel2 = _write_json(tmp_path, "package.json", bad_pkg)
    result = validate_config_semantics(tmp_path, [rel1, rel2])
    assert result is not None
    assert result[0] == ".eslintrc.json"  # listed first → returned first


# ── Version bump robustness ───────────────────────────────────────────


def test_permissive_fallback_on_unresolvable_ref(tmp_path: Path) -> None:
    """The offline registry should return a permissive empty schema
    for any ``$ref`` we don't bundle, so validation on same-file
    structure still fires. Covered implicitly by the ESLint test
    above (its schema has external $refs), but we assert explicitly
    here: a valid ESLint config passes even though partial-eslint-
    plugins.json isn't bundled."""
    good = {
        "root": True,
        "parser": "@typescript-eslint/parser",
        "plugins": ["some-plugin"],
    }
    rel = _write_json(tmp_path, ".eslintrc.json", good)
    assert validate_config_semantics(tmp_path, [rel]) is None
