"""Tests for G15 — sibling-config reconciliation.

Replays the b2v PR #32 failure mode + per-extractor + per-matrix-rule
unit coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.sibling_config import (
    SiblingConfigConflict,
    SiblingConfigResult,
    _check_end_of_line,
    _check_indent,
    _check_quotes,
    _check_semi,
    _editorconfig_root,
    _is_quality_config,
    _normalize_eslint_rule,
    _parse_editorconfig,
    _parse_eslint_json,
    _parse_package_json,
    _parse_prettier_json,
    check_sibling_config,
)


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


# ── _is_quality_config ────────────────────────────────────────────


@pytest.mark.parametrize("rel,expected", [
    (".editorconfig",          True),
    (".prettierrc",            True),
    (".prettierrc.json",       True),
    (".prettierrc.yml",        True),
    (".eslintrc",              True),
    (".eslintrc.json",         True),
    (".eslintrc.cjs",          True),
    ("eslint.config.js",       True),
    ("eslint.config.mjs",      True),
    ("package.json",           True),
    ("src/.prettierrc.json",   True),     # subproject quality config
    ("README.md",              False),
    ("config.toml",            False),
    ("tsconfig.json",          False),    # not a quality config
    ("src/main.py",            False),
])
def test_is_quality_config(rel: str, expected: bool) -> None:
    assert _is_quality_config(rel) is expected


# ── _parse_editorconfig ───────────────────────────────────────────


def test_parse_editorconfig_picks_relevant_keys() -> None:
    src = (
        "root = true\n"
        "\n"
        "[*]\n"
        "indent_style = space\n"
        "indent_size = 4\n"
        "end_of_line = lf\n"
        "insert_final_newline = true\n"  # ignored — not in our key set
        "\n"
        "[*.md]\n"
        "indent_size = 2\n"
    )
    parsed = _parse_editorconfig(src)
    assert parsed["*"]["indent_size"] == 4
    assert parsed["*"]["indent_style"] == "space"
    assert parsed["*"]["end_of_line"] == "lf"
    assert "insert_final_newline" not in parsed["*"]
    assert parsed["*.md"]["indent_size"] == 2


def test_parse_editorconfig_handles_indent_tab() -> None:
    """`indent_size = tab` is valid editorconfig syntax — preserved
    verbatim so the indent comparator can decide."""
    src = "[*]\nindent_size = tab\nindent_style = tab\n"
    parsed = _parse_editorconfig(src)
    assert parsed["*"]["indent_size"] == "tab"


def test_editorconfig_root_section_picker() -> None:
    """``[*]`` section wins; falls back to JS/TS-glob sections."""
    parsed = {"*": {"indent_size": 4}, "*.{js,ts}": {"indent_size": 2}}
    assert _editorconfig_root(parsed) == {"indent_size": 4}
    only_js = {"*.{js,ts}": {"indent_size": 2}}
    assert _editorconfig_root(only_js) == {"indent_size": 2}


# ── _parse_prettier_json ──────────────────────────────────────────


def test_parse_prettier_extracts_relevant_keys() -> None:
    parsed = _parse_prettier_json(
        '{"tabWidth": 2, "semi": false, "singleQuote": true, "ignored": "x"}'
    )
    assert parsed == {"tabWidth": 2, "semi": False, "singleQuote": True}


def test_parse_prettier_invalid_json_returns_empty() -> None:
    """Defensive: malformed = empty dict, not crash."""
    assert _parse_prettier_json("{not valid") == {}


# ── _parse_eslint_json + normalisation ────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("always",                          "always"),
    ("never",                           "never"),
    (["error", "always"],               "always"),
    (["warn", "single"],                "single"),
    (["error", "single", {"avoidEscape": True}], "single"),
    (2,                                 "unknown"),
    ({},                                "unknown"),
])
def test_normalize_eslint_rule(raw, expected: str) -> None:
    assert _normalize_eslint_rule("semi", raw) == expected


def test_parse_eslint_json_extracts_semi_quotes() -> None:
    parsed = _parse_eslint_json(
        '{"rules": {"semi": ["error", "always"], '
        '"quotes": ["error", "double"], "no-unused-vars": "error"}}'
    )
    assert parsed == {"semi": "always", "quotes": "double"}


def test_parse_eslint_no_rules_returns_empty() -> None:
    assert _parse_eslint_json('{"env": {"node": true}}') == {}


# ── _parse_package_json — embedded prettier / eslintConfig ────────


def test_parse_package_json_picks_embedded_prettier() -> None:
    parsed = _parse_package_json(
        '{"name": "x", "prettier": {"tabWidth": 4, "semi": true}}'
    )
    assert parsed == {"prettier": {"tabWidth": 4, "semi": True}}


def test_parse_package_json_picks_embedded_eslintConfig() -> None:
    parsed = _parse_package_json(
        '{"name": "x", "eslintConfig": '
        '{"rules": {"semi": ["error", "always"]}}}'
    )
    assert parsed == {"eslint": {"semi": "always"}}


# ── Comparator: _check_indent ─────────────────────────────────────


def test_check_indent_conflict_detected() -> None:
    out = _check_indent({"indent_size": 4}, {"tabWidth": 2})
    assert out is not None
    assert out["key"] == "indent_size↔tabWidth"
    assert out["values"] == {"editorconfig": "4", "prettier": "2"}
    assert "indent_size=4" in out["message"]


def test_check_indent_compatible_returns_none() -> None:
    assert _check_indent({"indent_size": 2}, {"tabWidth": 2}) is None


def test_check_indent_one_side_missing_returns_none() -> None:
    """Absent on either side = no conflict (operator omitted)."""
    assert _check_indent({}, {"tabWidth": 2}) is None
    assert _check_indent({"indent_size": 4}, {}) is None


def test_check_indent_non_int_returns_none() -> None:
    """``indent_size = tab`` skipped — comparator can't decide."""
    assert _check_indent({"indent_size": "tab"}, {"tabWidth": 2}) is None


# ── Comparator: _check_semi ──────────────────────────────────────


def test_check_semi_eslint_always_vs_prettier_false() -> None:
    out = _check_semi({"semi": "always"}, {"semi": False})
    assert out is not None
    assert out["values"] == {"eslint": "always", "prettier": "False"}


def test_check_semi_eslint_never_vs_prettier_true() -> None:
    out = _check_semi({"semi": "never"}, {"semi": True})
    assert out is not None


def test_check_semi_compatible_returns_none() -> None:
    assert _check_semi({"semi": "always"}, {"semi": True}) is None
    assert _check_semi({"semi": "never"}, {"semi": False}) is None


def test_check_semi_unknown_eslint_skipped() -> None:
    """Unparseable ESLint rule shape → bail (don't false-positive)."""
    assert _check_semi({"semi": "unknown"}, {"semi": True}) is None


# ── Comparator: _check_quotes ────────────────────────────────────


def test_check_quotes_eslint_double_vs_prettier_singletrue() -> None:
    out = _check_quotes({"quotes": "double"}, {"singleQuote": True})
    assert out is not None
    assert out["key"] == "quotes↔singleQuote"


def test_check_quotes_compatible_single() -> None:
    assert _check_quotes({"quotes": "single"}, {"singleQuote": True}) is None


def test_check_quotes_compatible_double() -> None:
    assert _check_quotes({"quotes": "double"}, {"singleQuote": False}) is None


# ── Comparator: _check_end_of_line ───────────────────────────────


def test_check_eol_lf_vs_crlf() -> None:
    out = _check_end_of_line({"end_of_line": "lf"}, {"endOfLine": "crlf"})
    assert out is not None


def test_check_eol_auto_treated_as_compatible() -> None:
    """Prettier's ``endOfLine: "auto"`` defers — never conflicts."""
    assert _check_end_of_line(
        {"end_of_line": "lf"}, {"endOfLine": "auto"},
    ) is None


# ── Top-level check_sibling_config ────────────────────────────────


def test_no_quality_config_touched_returns_none(tmp_path: Path) -> None:
    """Patches that don't touch any quality config → silent pass."""
    _populate(tmp_path, {"src/main.py": "x = 1\n"})
    assert check_sibling_config(tmp_path, ["src/main.py"]) is None


def test_quality_config_with_no_siblings_returns_none(
    tmp_path: Path,
) -> None:
    """Touched quality config but no siblings to reconcile against."""
    _populate(tmp_path, {".prettierrc.json": '{"tabWidth": 2}\n'})
    assert check_sibling_config(tmp_path, [".prettierrc.json"]) is None


def test_replay_b2v_pr32_detects_three_conflicts(tmp_path: Path) -> None:
    """The whole reason this guard exists. Replay the PR #32 shape:
    .editorconfig (indent 4) + .eslintrc (semi always, quotes
    double) + .prettierrc shipped (tabWidth 2, semi false,
    singleQuote true). Expect 3 conflicts."""
    _populate(tmp_path, {
        ".editorconfig": (
            "[*]\n"
            "indent_style = space\n"
            "indent_size = 4\n"
            "end_of_line = lf\n"
        ),
        ".eslintrc.json": (
            '{"rules": {"semi": ["error", "always"], '
            '"quotes": ["error", "double"]}}\n'
        ),
        ".prettierrc.json": (
            '{"tabWidth": 2, "semi": false, "singleQuote": true}\n'
        ),
    })
    result = check_sibling_config(tmp_path, [".prettierrc.json"])
    assert result is not None
    assert len(result.conflicts) == 3
    keys = {c.key for c in result.conflicts}
    assert keys == {
        "indent_size↔tabWidth",
        "semi",
        "quotes↔singleQuote",
    }


def test_replay_pr32_orientation_correct(tmp_path: Path) -> None:
    """Critical: touched_value must reflect the touched file's
    actual value, not the comparator's canonical ordering. The
    LLM feedback hinges on this."""
    _populate(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 4\n",
        ".prettierrc.json": '{"tabWidth": 2}\n',
    })
    result = check_sibling_config(tmp_path, [".prettierrc.json"])
    assert result is not None
    conflict = next(
        c for c in result.conflicts if c.key == "indent_size↔tabWidth"
    )
    assert conflict.touched_file == ".prettierrc.json"
    assert conflict.sibling_file == ".editorconfig"
    assert conflict.touched_value == "2"   # Prettier's tabWidth
    assert conflict.sibling_value == "4"   # editorconfig's indent_size


def test_compatible_configs_return_none(tmp_path: Path) -> None:
    """Operator wrote consistent configs across files — no conflict."""
    _populate(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 2\n",
        ".eslintrc.json": '{"rules": {"semi": ["error", "always"]}}\n',
        ".prettierrc.json": '{"tabWidth": 2, "semi": true}\n',
    })
    assert check_sibling_config(tmp_path, [".prettierrc.json"]) is None


def test_package_json_embedded_prettier_conflict(tmp_path: Path) -> None:
    """package.json with embedded ``prettier`` block can also
    conflict with .editorconfig sibling."""
    _populate(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 4\n",
        "package.json": (
            '{"name": "x", "prettier": {"tabWidth": 2}}\n'
        ),
    })
    result = check_sibling_config(tmp_path, ["package.json"])
    assert result is not None
    assert any(c.key == "indent_size↔tabWidth" for c in result.conflicts)


def test_malformed_config_skipped_silently(tmp_path: Path) -> None:
    """Defensive: a malformed sibling config → skip, not crash."""
    _populate(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 4\n",
        ".prettierrc.json": "{not valid json\n",  # touched but malformed
    })
    # Touched file unparseable → no conflict surfaces (silent skip)
    assert check_sibling_config(tmp_path, [".prettierrc.json"]) is None


def test_skipdirs_honored(tmp_path: Path) -> None:
    """Sibling discovery walks the tree but skips node_modules /
    .venv / dist / build / target — siblings inside those don't
    count as repo state."""
    _populate(tmp_path, {
        ".prettierrc.json": '{"tabWidth": 2}\n',
        "node_modules/foo/.editorconfig": "[*]\nindent_size = 4\n",
        ".venv/lib/.editorconfig": "[*]\nindent_size = 8\n",
    })
    # Only thing inside skipdirs → no real sibling
    assert check_sibling_config(tmp_path, [".prettierrc.json"]) is None


def test_render_for_llm_includes_all_conflicts(tmp_path: Path) -> None:
    """The LLM feedback must list every conflict so a single retry
    can fix all at once (cheaper than per-conflict iterations)."""
    _populate(tmp_path, {
        ".editorconfig": "[*]\nindent_size = 4\n",
        ".eslintrc.json": '{"rules": {"semi": ["error", "always"]}}\n',
        ".prettierrc.json": '{"tabWidth": 2, "semi": false}\n',
    })
    result = check_sibling_config(tmp_path, [".prettierrc.json"])
    assert result is not None
    rendered = result.render_for_llm()
    assert "indent_size↔tabWidth" in rendered
    assert "semi" in rendered
    assert ".editorconfig" in rendered
    assert ".eslintrc.json" in rendered
