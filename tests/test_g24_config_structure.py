"""Tests for the G24 config-structure critic.

Bench evidence: qwen3-8b PR #10 (gitoma-bench-blast, 2026-04-29 EVE
A/B run) emitted `[tool.poetry] dependencies = [...]` — a LIST
where Poetry requires a TABLE. G23 silent-passed because poetry is
in the typo-only catalog; G24 catches the structural shape error
that G23 doesn't reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.config_structure import (
    G24Conflict,
    G24Result,
    check_g24_config_structure,
    is_g24_enabled,
)


# ── Env opt-in ────────────────────────────────────────────────────


def test_is_g24_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G24_CONFIG_STRUCTURE", raising=False)
    assert is_g24_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_g24_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", val)
    assert is_g24_enabled() is True


def test_is_g24_enabled_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "0")
    assert is_g24_enabled() is False


# ── Silent-skip paths ─────────────────────────────────────────────


def test_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("GITOMA_G24_CONFIG_STRUCTURE", raising=False)
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_no_pyproject_in_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    out = check_g24_config_structure(tmp_path, ["src/foo.py"], None)
    assert out is None


def test_invalid_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    out = check_g24_config_structure(
        "/definitely/not/a/path", ["pyproject.toml"], None,
    )
    assert out is None


def test_pyproject_doesnt_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pyproject_unparseable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text("[[[broken")
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pyproject_no_relevant_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """pyproject with only [build-system] = no rules to apply → pass."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


# ── The bench-blast PR #10 case ───────────────────────────────────


def test_catches_poetry_dependencies_as_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The exact qwen3-8b PR #10 bug — `[tool.poetry] dependencies = [...]`
    parses as a LIST when Poetry requires a TABLE."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\ndependencies = [\n  "pytest",\n  "mypy",\n]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.section_path == "tool.poetry.dependencies"
    assert c.actual_type == "list"
    assert c.expected_type == "table"


def test_valid_poetry_dependencies_table_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The CORRECT poetry shape — pyproject must pass."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n'
        '[tool.poetry.dependencies]\npytest = "^7.0"\nmypy = "^1.0"\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_catches_poetry_scripts_as_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nscripts = ["mybin"]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert out.conflicts[0].section_path == "tool.poetry.scripts"


def test_catches_poetry_extras_as_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nextras = ["dev", "test"]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert out.conflicts[0].section_path == "tool.poetry.extras"


# ── PEP-621 [project] (opposite polarity) ─────────────────────────


def test_pep621_project_dependencies_as_table_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """PEP-621 has the OPPOSITE convention: `[project] dependencies` IS a
    list (array of PEP-508 strings). Catch when LLM emits it as a table."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        '[project.dependencies]\nrequests = "^2.31"\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    c = out.conflicts[0]
    assert c.section_path == "project.dependencies"
    assert c.actual_type == "table"
    assert c.expected_type == "list"


def test_pep621_project_dependencies_as_list_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Correct PEP-621 shape: list of PEP-508 strings."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2.31", "click"]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pep621_project_optional_dependencies_must_be_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`[project.optional-dependencies]` MUST be table mapping group→list."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'optional-dependencies = ["pytest", "mypy"]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert out.conflicts[0].section_path == "project.optional-dependencies"


def test_pep621_authors_as_table_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`authors` is an ARRAY of `{name, email}` tables, not a single table."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        '[project.authors]\nname = "X"\nemail = "x@y.z"\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert out.conflicts[0].section_path == "project.authors"


def test_pep621_authors_as_array_of_tables_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'authors = [{ name = "X", email = "x@y.z" }]\n'
    )
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], None)
    assert out is None


# ── Diff mode (originals provided) ────────────────────────────────


def test_diff_mode_only_flags_new_violations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When originals contain a pre-existing wrong-shape section,
    G24 leaves it alone — only flags violations NEW in this patch."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    pre_existing_bad = (
        '[tool.poetry]\ndependencies = ["was-already-broken"]\n'
    )
    (tmp_path / "pyproject.toml").write_text(pre_existing_bad)
    originals = {"pyproject.toml": pre_existing_bad}
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], originals)
    assert out is None  # pre-existing violation not flagged


def test_diff_mode_flags_newly_introduced_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Patch introduced a structure violation that wasn't there."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\ndependencies = ["wrong-shape"]\n'  # patch added this
    )
    originals = {"pyproject.toml": '[tool.poetry]\nname = "x"\n'}  # baseline clean
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], originals)
    assert out is not None
    assert out.conflicts[0].section_path == "tool.poetry.dependencies"


def test_diff_mode_flags_when_section_brand_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Section didn't exist in baseline at all; patch added it wrong."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\nscripts = ["mybin"]\n'
    )
    originals = {"pyproject.toml": '[tool.poetry]\nname = "x"\n'}
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], originals)
    assert out is not None
    assert out.conflicts[0].section_path == "tool.poetry.scripts"


def test_diff_mode_handles_new_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """File created by the patch (originals[path] = None) → all
    violations are 'new' relative to nothing."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\ndependencies = ["wrong"]\n'
    )
    originals = {"pyproject.toml": None}
    out = check_g24_config_structure(tmp_path, ["pyproject.toml"], originals)
    assert out is not None


# ── Multi-file ────────────────────────────────────────────────────


def test_multi_file_aggregates_conflicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Monorepo case: violations across multiple pyproject.toml all surface."""
    monkeypatch.setenv("GITOMA_G24_CONFIG_STRUCTURE", "1")
    (tmp_path / "pkg1").mkdir()
    (tmp_path / "pkg2").mkdir()
    (tmp_path / "pkg1" / "pyproject.toml").write_text(
        '[tool.poetry]\ndependencies = ["wrong"]\n'
    )
    (tmp_path / "pkg2" / "pyproject.toml").write_text(
        '[tool.poetry]\nscripts = ["wrong"]\n'
    )
    out = check_g24_config_structure(
        tmp_path,
        ["pkg1/pyproject.toml", "pkg2/pyproject.toml"],
        None,
    )
    assert out is not None
    files = {c.file for c in out.conflicts}
    assert "pkg1/pyproject.toml" in files
    assert "pkg2/pyproject.toml" in files


# ── Render output ─────────────────────────────────────────────────


def test_render_empty_returns_empty() -> None:
    assert G24Result(conflicts=()).render_for_llm() == ""


def test_render_includes_path_types_intent() -> None:
    r = G24Result(conflicts=(
        G24Conflict("pyproject.toml", "tool.poetry.dependencies",
                    expected_type="table", actual_type="list",
                    intent="Use the table form"),
    ))
    out = r.render_for_llm()
    assert "tool.poetry.dependencies" in out
    assert "TABLE" in out
    assert "LIST" in out
    assert "Use the table form" in out


def test_render_lists_count_in_header() -> None:
    r = G24Result(conflicts=tuple(
        G24Conflict("pyproject.toml", f"tool.poetry.k{i}",
                    expected_type="table", actual_type="list",
                    intent="x")
        for i in range(3)
    ))
    assert "3" in r.render_for_llm()


# ── Dataclass invariants ──────────────────────────────────────────


def test_g24_conflict_is_frozen() -> None:
    c = G24Conflict("p.toml", "tool.poetry.dependencies",
                    expected_type="table", actual_type="list", intent="x")
    with pytest.raises(Exception):
        c.actual_type = "table"  # type: ignore[misc]


def test_g24_result_is_frozen() -> None:
    r = G24Result(conflicts=())
    with pytest.raises(Exception):
        r.conflicts = ()  # type: ignore[misc]
