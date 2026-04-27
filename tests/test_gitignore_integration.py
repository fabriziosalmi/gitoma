"""Tests for gitoma↔occam-gitignore integration.

Most are pure-function tests against the integration module; the
CLI command itself is exercised live (no value in mocking the whole
git+gh stack)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitoma.integrations.occam_gitignore import (
    DEFAULT_RULES_TABLE,
    DEFAULT_TEMPLATES_DIR,
    GenerateResult,
    OccamGitignoreUnavailable,
    diff_against_existing,
    generate_for_repo,
    is_available,
    version_info,
)


# ── Constants sanity ──────────────────────────────────────────────


def test_default_data_paths_documented() -> None:
    """Defaults are stable references for v1; future versions may
    relocate data files but these constants must keep their meaning."""
    assert "occam-gitignore" in str(DEFAULT_TEMPLATES_DIR)
    assert "templates" in str(DEFAULT_TEMPLATES_DIR)
    assert "rules_table.json" in str(DEFAULT_RULES_TABLE)


# ── is_available ──────────────────────────────────────────────────


def test_is_available_when_installed() -> None:
    """In the dev env occam-gitignore-core IS installed (we made
    it a hard dev dep). The check should return True. If this test
    ever fails the dev env regressed."""
    assert is_available() is True


def test_is_available_returns_false_when_import_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock the import failure to verify the graceful-skip path."""
    import builtins
    real_import = builtins.__import__

    def _import_blocker(name, *args, **kwargs):
        if name == "occam_gitignore_core":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import_blocker)
    assert is_available() is False


def test_is_available_returns_false_when_data_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Bogus OCCAM_GITIGNORE_DATA_DIR → no data files → unavailable."""
    monkeypatch.setenv("OCCAM_GITIGNORE_DATA_DIR", str(tmp_path / "nonexistent"))
    assert is_available() is False


# ── version_info ─────────────────────────────────────────────────


def test_version_info_returns_three_components() -> None:
    info = version_info()
    assert info is not None
    for key in ("core", "templates", "rules_table"):
        assert key in info
        assert isinstance(info[key], str)
        assert len(info[key]) > 0


def test_version_info_returns_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OCCAM_GITIGNORE_DATA_DIR", str(tmp_path / "missing"))
    assert version_info() is None


# ── generate_for_repo on synthetic trees ─────────────────────────


def test_generate_python_repo(tmp_path: Path) -> None:
    """A repo with pyproject.toml + a .py file → python feature
    detected → output mentions __pycache__/."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "main.py").write_text("x = 1\n")
    result = generate_for_repo(tmp_path)
    assert isinstance(result, GenerateResult)
    assert "python" in result.features
    assert "__pycache__/" in result.content
    assert result.content_hash.startswith("sha256:")


def test_generate_node_repo(tmp_path: Path) -> None:
    """package.json → node feature → output mentions node_modules/."""
    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    (tmp_path / "index.js").write_text("module.exports = 1;\n")
    result = generate_for_repo(tmp_path)
    assert "node" in result.features
    assert "node_modules/" in result.content


def test_generate_polyglot_repo(tmp_path: Path) -> None:
    """Mix of Python + Node + Rust → multiple features detected."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    (tmp_path / "Cargo.toml").write_text("[package]\nname=\"x\"\n")
    result = generate_for_repo(tmp_path)
    assert {"python", "node", "rust"}.issubset(set(result.features))


def test_generate_empty_repo_yields_no_features(tmp_path: Path) -> None:
    """Empty repo → empty feature list. The fingerprinter doesn't
    auto-add 'common' when there are no files at all (it adds it
    only when something else triggers, alongside specific
    detectors). Output is still valid (header-only) but the user
    likely doesn't want this PR; the CLI shows it in --dry-run."""
    result = generate_for_repo(tmp_path)
    assert result.features == ()


def test_generate_is_deterministic(tmp_path: Path) -> None:
    """Two runs on the same tree → byte-identical content + hash."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "main.py").write_text("x = 1\n")
    a = generate_for_repo(tmp_path)
    b = generate_for_repo(tmp_path)
    assert a.content == b.content
    assert a.content_hash == b.content_hash


def test_generate_carries_provenance_metadata(tmp_path: Path) -> None:
    """The result must expose templates_version + rules_table_version
    so the PR body can cite them for reproducibility."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    result = generate_for_repo(tmp_path)
    assert result.templates_version.startswith("sha256:")
    assert result.rules_table_version.startswith("sha256:")
    assert result.core_version  # any non-empty version string


def test_generate_skips_pruned_dirs(tmp_path: Path) -> None:
    """Files inside node_modules/.venv etc. should NOT count toward
    the file tree (otherwise they'd false-positive features)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    # If node_modules counted, "node" would be detected too.
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.json").write_text('{"name": "fake"}\n')
    result = generate_for_repo(tmp_path)
    assert "python" in result.features
    # node_modules content was pruned → no node feature
    assert "node" not in result.features


def test_generate_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OCCAM_GITIGNORE_DATA_DIR", str(tmp_path / "missing"))
    with pytest.raises(OccamGitignoreUnavailable):
        generate_for_repo(tmp_path)


# ── diff_against_existing ────────────────────────────────────────


def test_diff_returns_none_on_match(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("foo\nbar\n")
    assert diff_against_existing(tmp_path, "foo\nbar\n") is None


def test_diff_returns_text_on_drift(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("old\n")
    diff = diff_against_existing(tmp_path, "new\n")
    assert diff is not None
    assert "old" in diff
    assert "new" in diff


def test_diff_handles_missing_existing(tmp_path: Path) -> None:
    """Repo with NO existing .gitignore → diff vs empty string →
    returns the full new content as a diff (not None)."""
    diff = diff_against_existing(tmp_path, "new\nfile\n")
    assert diff is not None
    assert "new" in diff


def test_diff_empty_generated_returns_none_when_existing_empty(
    tmp_path: Path,
) -> None:
    """Edge case: both empty → no diff."""
    (tmp_path / ".gitignore").write_text("")
    assert diff_against_existing(tmp_path, "") is None


# ── CLI command surface (smoke) ──────────────────────────────────


def test_cli_command_registered() -> None:
    """``gitoma gitignore`` should appear in the global Typer app.
    Typer derives the command name from the callback function when
    no explicit name= was passed to @app.command()."""
    import gitoma.cli.commands  # noqa: F401
    from gitoma.cli._app import app
    names: set[str] = set()
    for cmd in app.registered_commands:
        if cmd.name:
            names.add(cmd.name)
        elif cmd.callback is not None:
            names.add(cmd.callback.__name__)
    assert "gitignore" in names
