"""Tests for G20 — TOML/INI syntax validator.

Mix of unit tests on the parsers + replay scenarios from the
bench-blast PR #1 (closed 2026-04-26) that motivated this guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.config_syntax import (
    INI_BASENAMES,
    TOML_BASENAMES,
    ConfigSyntaxError,
    ConfigSyntaxResult,
    _check_ini,
    _check_toml,
    _classify,
    _extract_location,
    check_config_syntax,
)


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


# ── Family classifier ─────────────────────────────────────────────


@pytest.mark.parametrize("rel,expected", [
    ("pyproject.toml", "toml"),
    (".ruff.toml", "toml"),
    ("ruff.toml", "toml"),
    ("Cargo.toml", "toml"),
    ("clippy.toml", "toml"),
    ("rustfmt.toml", "toml"),
    ("setup.cfg", "ini"),
    ("tox.ini", "ini"),
    (".flake8", "ini"),
    (".pylintrc", "ini"),
    ("mypy.ini", "ini"),
    ("src/.ruff.toml", "toml"),    # subdir TOML still classified
    ("packages/foo/setup.cfg", "ini"),
    # Files we don't validate
    ("README.md", None),
    (".prettierrc.json", None),
    ("package.json", None),
    ("Makefile", None),
    # Generic fallback by extension
    ("custom.toml", "toml"),
    ("custom.ini", "ini"),
    ("custom.cfg", "ini"),
])
def test_classify(rel: str, expected: str | None) -> None:
    assert _classify(rel) == expected


def test_basename_sets_have_no_overlap() -> None:
    """A file should be classified as TOML XOR INI, never both."""
    assert TOML_BASENAMES.isdisjoint(INI_BASENAMES)


# ── _check_toml ──────────────────────────────────────────────────


def test_toml_valid_returns_none() -> None:
    src = '[project]\nname = "x"\nversion = "1.0"\n'
    assert _check_toml("pyproject.toml", src) is None


def test_toml_unclosed_string_detected() -> None:
    src = '[project]\nname = "missing close\n'
    err = _check_toml("pyproject.toml", src)
    assert err is not None
    assert err.format == "toml"
    assert err.file == "pyproject.toml"
    assert err.message  # parser's own error string preserved


def test_toml_invalid_table_syntax_detected() -> None:
    """Unclosed bracket in a table header."""
    src = "[project\nname = 'x'\n"
    err = _check_toml("pyproject.toml", src)
    assert err is not None
    assert err.line is not None  # tomllib reports line


def test_toml_duplicate_key_detected() -> None:
    """Duplicate keys in the same table → TOMLDecodeError."""
    src = '[project]\nname = "x"\nname = "y"\n'
    err = _check_toml("pyproject.toml", src)
    assert err is not None


def test_toml_leading_whitespace_is_lenient() -> None:
    """Documenting reality: tomllib ACCEPTS leading whitespace on
    keys (some TOML parsers in the wild are stricter, but stdlib
    tomllib is lenient). G20 catches actual syntax errors only —
    not cosmetic indentation. This test pins the behaviour so we
    don't regress on it later."""
    src = '[tool.mypy]\nstrict = true\n use_lenient = true\n'
    assert _check_toml("pyproject.toml", src) is None


# ── _check_ini ───────────────────────────────────────────────────


def test_ini_valid_returns_none() -> None:
    src = "[section]\nkey = value\n"
    assert _check_ini("setup.cfg", src) is None


def test_ini_missing_section_header_detected() -> None:
    """The actual bench-blast PR #1 syntax issue (closed 2026-04-26):
    an INI-style coverage block at top-level with no section header."""
    src = "key = value without section\n"
    err = _check_ini("setup.cfg", src)
    assert err is not None
    assert err.format == "ini"
    assert "section" in err.message.lower()


def test_ini_duplicate_section_detected() -> None:
    src = "[a]\nkey = 1\n[a]\nkey = 2\n"
    err = _check_ini("setup.cfg", src)
    assert err is not None


def test_ini_duplicate_option_detected() -> None:
    src = "[a]\nkey = 1\nkey = 2\n"
    err = _check_ini("setup.cfg", src)
    assert err is not None


# ── Location extraction ──────────────────────────────────────────


@pytest.mark.parametrize("msg,expected_line", [
    ("Invalid statement (at line 5, column 2)", 5),
    ("[line  3]: parsing error", 3),
    ("Bad bracket at line 12", 12),
    ("no location info here", None),
])
def test_extract_location_line(msg: str, expected_line: int | None) -> None:
    line, _col = _extract_location(msg)
    assert line == expected_line


def test_extract_location_column_present() -> None:
    line, col = _extract_location("Bad token (at line 4, column 7)")
    assert line == 4
    assert col == 7


# ── End-to-end check_config_syntax ───────────────────────────────


def test_no_config_touched_returns_none(tmp_path: Path) -> None:
    """Patches that don't touch any TOML/INI → silent pass."""
    _populate(tmp_path, {"src/main.py": "x = 1\n"})
    assert check_config_syntax(tmp_path, ["src/main.py"]) is None


def test_clean_config_returns_none(tmp_path: Path) -> None:
    _populate(tmp_path, {
        "pyproject.toml": '[project]\nname = "x"\n',
        "setup.cfg": "[metadata]\nname = x\n",
    })
    assert check_config_syntax(tmp_path, ["pyproject.toml", "setup.cfg"]) is None


def test_replay_bench_blast_pr1_setup_cfg(tmp_path: Path) -> None:
    """Replay the bench-blast PR #1 INI failure — coverage-style
    block missing a section header. G20 must catch this."""
    _populate(tmp_path, {
        "setup.cfg": (
            "include = mypackage\n"      # no [section] above this
            "exclude = tests/*\n"
        ),
    })
    result = check_config_syntax(tmp_path, ["setup.cfg"])
    assert result is not None
    assert len(result.errors) == 1
    assert result.errors[0].file == "setup.cfg"
    assert result.errors[0].format == "ini"


def test_multiple_broken_configs_all_reported(tmp_path: Path) -> None:
    """Multi-file patch with several broken configs → all surface
    in ONE result so the LLM gets the full picture in one retry."""
    _populate(tmp_path, {
        "pyproject.toml": '[project\nname = "x"\n',  # unclosed bracket
        "setup.cfg": "key = no_section\n",            # missing [section]
        "tox.ini": "[testenv]\ndeps = ok\n",          # clean — should NOT appear
    })
    result = check_config_syntax(
        tmp_path, ["pyproject.toml", "setup.cfg", "tox.ini"],
    )
    assert result is not None
    assert len(result.errors) == 2
    files = {e.file for e in result.errors}
    assert files == {"pyproject.toml", "setup.cfg"}


def test_render_for_llm_includes_all_errors(tmp_path: Path) -> None:
    _populate(tmp_path, {
        "pyproject.toml": '[project\n',
        "setup.cfg": "no_section\n",
    })
    result = check_config_syntax(
        tmp_path, ["pyproject.toml", "setup.cfg"],
    )
    assert result is not None
    rendered = result.render_for_llm()
    assert "INVALID SYNTAX" in rendered
    assert "pyproject.toml" in rendered
    assert "setup.cfg" in rendered


def test_unreadable_file_skipped_silently(tmp_path: Path) -> None:
    """File listed in `touched` but missing on disk → skip, not crash.
    Defensive against race conditions or stale touched lists."""
    # touched references a path that doesn't exist
    assert check_config_syntax(tmp_path, ["nonexistent.toml"]) is None


def test_non_config_files_in_touched_are_skipped(tmp_path: Path) -> None:
    """Even if a touched path matches no TOML/INI rule, no error;
    the guard only fires for files it knows how to parse."""
    _populate(tmp_path, {
        "src/main.py": "x = 1\n",
        "package.json": '{"name": "x"}\n',
    })
    assert check_config_syntax(
        tmp_path, ["src/main.py", "package.json"],
    ) is None


# ── Result type sanity ───────────────────────────────────────────


def test_result_carries_errors_tuple() -> None:
    err = ConfigSyntaxError(
        file="setup.cfg", format="ini", line=1, column=None,
        message="bad",
    )
    result = ConfigSyntaxResult(errors=(err,))
    assert len(result.errors) == 1
    assert result.errors[0] is err
