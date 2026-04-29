"""Tests for the G23 config-keys critic.

Real bench evidence (gemma-4-e4b emitting `suppress_missing_imports`
in PR #8 of `gitoma-bench-blast`) drives the test cases. Pure
filesystem fixtures — no LLM, no subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.config_keys import (
    G23Conflict,
    G23Result,
    check_g23_config_keys,
    is_g23_enabled,
)


# ── Env opt-in ────────────────────────────────────────────────────


def test_is_g23_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G23_CONFIG_KEYS", raising=False)
    assert is_g23_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_g23_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", val)
    assert is_g23_enabled() is True


def test_is_g23_enabled_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "0")
    assert is_g23_enabled() is False


# ── Silent-skip paths ─────────────────────────────────────────────


def test_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("GITOMA_G23_CONFIG_KEYS", raising=False)
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_no_pyproject_in_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    out = check_g23_config_keys(tmp_path, ["src/foo.py"], None)
    assert out is None


def test_invalid_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    out = check_g23_config_keys(
        "/definitely/not/a/path", ["pyproject.toml"], None,
    )
    assert out is None


def test_pyproject_doesnt_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Touched file claimed but missing from disk → silent skip
    (G20 handles parse / IO errors)."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pyproject_unparseable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Broken TOML = G20's job; G23 skips silently."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text("[[[broken")
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pyproject_no_tool_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pure [project] / [build-system] pyproject = nothing to validate."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_pyproject_unknown_tool_section_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tools we don't catalog → silent passthrough (default-allow)."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.some-experimental-tool]\nweird-knob = true\nfuture-key = 42\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


# ── The bench-blast PR #8 cases ───────────────────────────────────


def test_catches_mypy_suppress_missing_imports_typo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The exact PR #8 bug — `suppress_missing_imports` should be
    `ignore_missing_imports`."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.10"\nsuppress_missing_imports = true\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.section == "tool.mypy"
    assert c.key == "suppress_missing_imports"
    assert c.is_typo is True
    assert "ignore_missing_imports" in c.suggestion


def test_catches_invented_coverage_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The other PR #8 bug — `[tool.coverage]` with invented keys."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage]\nignore_patterns = [".venv/"]\nrun_if_covered = true\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    keys_caught = {c.key for c in out.conflicts}
    assert "ignore_patterns" in keys_caught
    assert "run_if_covered" in keys_caught


def test_catches_ruff_underscore_typo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Ruff uses dash-case (`line-length`), not snake_case."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline_length = 88\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is not None
    assert any(
        c.key == "line_length" and "line-length" in c.suggestion
        for c in out.conflicts
    )


# ── Valid configs pass through ────────────────────────────────────


def test_valid_mypy_config_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\n'
        'ignore_missing_imports = true\n'
        'warn_unused_ignores = true\nstrict = true\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_valid_ruff_config_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 88\ntarget-version = "py312"\n'
        '[tool.ruff.lint]\nselect = ["E", "F"]\nignore = ["E501"]\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_valid_pytest_config_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        'testpaths = ["tests"]\naddopts = "-q"\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


def test_valid_coverage_config_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\nsource = ["src"]\n'
        '[tool.coverage.report]\nshow_missing = true\nfail_under = 90\n'
    )
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], None)
    assert out is None


# ── Diff mode (originals provided) ────────────────────────────────


def test_diff_mode_only_flags_new_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When originals contain a pre-existing typo, G23 leaves it
    alone — only flags keys NEW in this patch."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\n'
        'suppress_missing_imports = true\n'  # was already there
    )
    originals = {
        "pyproject.toml": (
            '[tool.mypy]\npython_version = "3.12"\n'
            'suppress_missing_imports = true\n'  # in baseline too
        ),
    }
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], originals)
    assert out is None  # pre-existing typo not flagged


def test_diff_mode_flags_newly_introduced_typo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Patch ADDED a typo that wasn't there before → flag it."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\n'
        'suppress_missing_imports = true\n'  # patch added this
    )
    originals = {
        "pyproject.toml": (
            '[tool.mypy]\npython_version = "3.12"\n'  # baseline = clean
        ),
    }
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], originals)
    assert out is not None
    assert out.conflicts[0].key == "suppress_missing_imports"


def test_diff_mode_handles_new_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """File created by the patch (originals[path] = None) → all
    its tool keys are 'new'."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage]\nignore_patterns = [".venv/"]\n'
    )
    originals = {"pyproject.toml": None}
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], originals)
    assert out is not None


def test_diff_mode_baseline_unparseable_falls_back_to_flag_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pre-patch file existed but was unparseable → diff mode
    degrades to flagging all current tool keys (defensive)."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\nsuppress_missing_imports = true\n'
    )
    originals = {"pyproject.toml": "[[[broken pre-patch"}
    out = check_g23_config_keys(tmp_path, ["pyproject.toml"], originals)
    # Old version unparseable → old_data=None → all current keys flagged
    assert out is not None


# ── Render output ─────────────────────────────────────────────────


def test_render_empty_returns_empty() -> None:
    assert G23Result(conflicts=()).render_for_llm() == ""


def test_render_includes_file_section_key() -> None:
    r = G23Result(conflicts=(
        G23Conflict("pyproject.toml", "tool.mypy", "bad_key",
                    "real_key", is_typo=True),
    ))
    out = r.render_for_llm()
    assert "pyproject.toml" in out
    assert "tool.mypy" in out
    assert "bad_key" in out
    assert "real_key" in out


def test_render_distinguishes_typo_vs_unknown() -> None:
    r = G23Result(conflicts=(
        G23Conflict("pyproject.toml", "tool.mypy", "x", "y", is_typo=True),
        G23Conflict("pyproject.toml", "tool.ruff", "z", "(unknown)",
                    is_typo=False),
    ))
    out = r.render_for_llm()
    assert "typo" in out
    assert "unknown key" in out


def test_render_lists_conflict_count_in_header() -> None:
    r = G23Result(conflicts=tuple(
        G23Conflict("pyproject.toml", "tool.mypy", f"k{i}", "x",
                    is_typo=False)
        for i in range(5)
    ))
    assert "5 invalid" in r.render_for_llm()


# ── Multi-file ────────────────────────────────────────────────────


def test_multi_file_flags_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the patch touches multiple pyproject.toml (e.g. monorepo
    sub-packages), G23 flags issues across all of them."""
    monkeypatch.setenv("GITOMA_G23_CONFIG_KEYS", "1")
    (tmp_path / "pkg1").mkdir()
    (tmp_path / "pkg2").mkdir()
    (tmp_path / "pkg1" / "pyproject.toml").write_text(
        '[tool.mypy]\nsuppress_missing_imports = true\n'
    )
    (tmp_path / "pkg2" / "pyproject.toml").write_text(
        '[tool.coverage]\nrun_if_covered = true\n'
    )
    out = check_g23_config_keys(
        tmp_path,
        ["pkg1/pyproject.toml", "pkg2/pyproject.toml"],
        None,
    )
    assert out is not None
    files = {c.file for c in out.conflicts}
    assert "pkg1/pyproject.toml" in files
    assert "pkg2/pyproject.toml" in files


# ── Dataclass invariants ──────────────────────────────────────────


def test_g23_conflict_is_frozen() -> None:
    c = G23Conflict("p.toml", "tool.mypy", "k", "s", is_typo=True)
    with pytest.raises(Exception):
        c.key = "other"  # type: ignore[misc]


def test_g23_result_is_frozen() -> None:
    r = G23Result(conflicts=())
    with pytest.raises(Exception):
        r.conflicts = ()  # type: ignore[misc]
