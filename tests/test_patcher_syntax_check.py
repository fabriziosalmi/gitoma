"""Tests for ``validate_post_write_syntax`` — per-file syntax check on
files the patcher just wrote. Catches authoring slop the language
compiler never sees (TOML/JSON/YAML config files)."""

from __future__ import annotations

from pathlib import Path

from gitoma.worker.patcher import validate_post_write_syntax


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


# ── Clean-pass cases ────────────────────────────────────────────────────


def test_valid_toml_returns_none(tmp_path: Path) -> None:
    rel = _write(tmp_path, "pyproject.toml", '[tool.x]\nname = "ok"\n')
    assert validate_post_write_syntax(tmp_path, [rel]) is None


def test_valid_json_returns_none(tmp_path: Path) -> None:
    rel = _write(tmp_path, "package.json", '{"name": "ok", "version": "1"}\n')
    assert validate_post_write_syntax(tmp_path, [rel]) is None


def test_valid_yaml_returns_none(tmp_path: Path) -> None:
    rel = _write(tmp_path, "ci.yml", "name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
    # If PyYAML isn't installed, the function silently passes (yaml is
    # optional) — either outcome is acceptable here.
    assert validate_post_write_syntax(tmp_path, [rel]) is None


def test_unrelated_extensions_skipped(tmp_path: Path) -> None:
    """``.md``, ``.txt``, ``.go``, ``.rs``, ``.js`` have no per-file
    parser here — they go through BuildAnalyzer or live unchecked."""
    rels = [
        _write(tmp_path, "README.md", "## broken markdown ((("),
        _write(tmp_path, "main.go", "this is not valid go"),
        _write(tmp_path, "src/lib.rs", "fn () { ;; ;; ;; }"),
    ]
    assert validate_post_write_syntax(tmp_path, rels) is None


def test_python_files_now_validated(tmp_path: Path) -> None:
    """``.py`` is now checked here too via ``py_compile`` — rung-3 v16
    caught the refiner silently corrupting src/db.py (triple-quote
    truncated to empty-string-plus-bare-bracket) on its own apply
    path which doesn't go through BuildAnalyzer. A patcher-level
    Python check covers both worker and refiner with one helper."""
    rel = _write(tmp_path, "src/x.py", "def broken(:\n    pass\n")
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    assert result[0] == "src/x.py"
    assert "SyntaxError" in result[1]


def test_valid_python_passes(tmp_path: Path) -> None:
    rel = _write(tmp_path, "src/x.py", "def fine():\n    return 42\n")
    assert validate_post_write_syntax(tmp_path, [rel]) is None


def test_v16_actual_corruption_caught(tmp_path: Path) -> None:
    """The exact corruption the v16 refiner shipped: changed a Python
    triple-quoted string to an empty-string-plus-bare-bracket
    sequence. Python sees the empty string then a stray operator."""
    body = (
        "import sqlite3\n"
        "def init_schema(conn):\n"
        "    conn.execute(\n"
        "        \"\">\n"
        "        CREATE TABLE x (id INT)\n"
        "        \"\"\n"
        "    )\n"
    )
    rel = _write(tmp_path, "src/db.py", body)
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    assert result[0] == "src/db.py"


def test_missing_file_silently_skipped(tmp_path: Path) -> None:
    """A delete patch produces ``touched`` entries that no longer
    exist on disk — those aren't errors, the parser just has nothing
    to read."""
    assert validate_post_write_syntax(tmp_path, ["gone.toml"]) is None


def test_empty_touched_list_returns_none(tmp_path: Path) -> None:
    assert validate_post_write_syntax(tmp_path, []) is None


# ── Failure cases ───────────────────────────────────────────────────────


def test_invalid_toml_returns_path_and_error(tmp_path: Path) -> None:
    """The exact rung-3 v12 corruption: bare identifier where TOML
    requires a quoted string."""
    rel = _write(
        tmp_path, "pyproject.toml",
        "[tool.coverage.config]\nsource = src\nbranch = True\n",
    )
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    bad_path, msg = result
    assert bad_path == "pyproject.toml"
    assert "TOMLDecodeError" in msg or "Invalid" in msg or "value" in msg.lower()


def test_invalid_json_returns_path_and_error(tmp_path: Path) -> None:
    rel = _write(
        tmp_path, "package.json",
        '{"name": "broken", "scripts": {,}}',  # leading comma
    )
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    bad_path, msg = result
    assert bad_path == "package.json"
    assert "JSONDecodeError" in msg or "Expecting" in msg


def test_first_failure_short_circuits(tmp_path: Path) -> None:
    """Iteration order matches the input list — return the FIRST bad
    file, not the last. Important for actionable error messages."""
    good = _write(tmp_path, "good.json", "{}")
    bad = _write(tmp_path, "bad.toml", "[unclosed section\n")
    later_bad = _write(tmp_path, "also-bad.json", "not json at all")

    result = validate_post_write_syntax(tmp_path, [good, bad, later_bad])
    assert result is not None
    bad_path, _ = result
    assert bad_path == "bad.toml"


def test_directory_in_touched_list_skipped(tmp_path: Path) -> None:
    """``is_file()`` filters out anything that isn't a regular file —
    no crash on a stray dir entry, no false-positive."""
    (tmp_path / "subdir.toml").mkdir()  # a DIR named like a TOML file
    assert validate_post_write_syntax(tmp_path, ["subdir.toml"]) is None


def test_extension_match_is_case_insensitive(tmp_path: Path) -> None:
    """``Pyproject.TOML`` is a manifest too — the OS doesn't care
    about case on macOS/Windows, our check shouldn't either."""
    rel = _write(tmp_path, "Config.TOML", "broken = (((\n")
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    assert result[0] == "Config.TOML"


def test_nested_path_reported_as_relative(tmp_path: Path) -> None:
    """The error message references the rel-path the worker gave us,
    not an absolute path — so the LLM's retry prompt can target the
    same path it just emitted."""
    rel = _write(tmp_path, "config/sub/dir/broken.json", "[1, 2,")
    result = validate_post_write_syntax(tmp_path, [rel])
    assert result is not None
    bad_path, _ = result
    assert bad_path == "config/sub/dir/broken.json"
