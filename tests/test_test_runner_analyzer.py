"""TestRunnerAnalyzer — runs the project's tests at audit time so the
planner sees CONCRETE failing-test file paths, not just "Test Suite
present at 35%". Closes the bug-C planner blindness loop where 7 rung-3
runs all generated cosmetic tasks while ``src/db.py`` was broken.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from gitoma.analyzers.test_runner import (
    TestRunnerAnalyzer,
    _count_passing,
    _parse_failing,
)


# ── Skip / soft-pass ─────────────────────────────────────────────────────────


def test_no_marker_files_softpasses(tmp_path: Path) -> None:
    """Empty repo → silent pass. The analyzer must NEVER become a
    reason to abort a run on a stack we can't recognise."""
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Haskell"])
    r = a.analyze()
    assert r.score == 1.0
    assert r.status == "pass"
    assert "skipped" in r.details.lower()


def test_toolchain_missing_softpasses(tmp_path: Path) -> None:
    """``FileNotFoundError`` from subprocess (pytest / cargo / go missing
    in PATH) MUST NOT block a run. Dev machines without every toolchain
    installed are normal."""
    (tmp_path / "go.mod").write_text("module x\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Go"])
    with patch("subprocess.run", side_effect=FileNotFoundError("go")):
        r = a.analyze()
    assert r.score == 1.0
    assert r.status == "pass"
    assert "not in PATH" in r.details


def test_timeout_softpasses(tmp_path: Path) -> None:
    """Slow tests → soft-pass with diagnostic, never block. Big monorepos
    with multi-minute test suites should not stall the audit phase."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname=\"x\"\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Rust"])
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cargo", timeout=90)):
        r = a.analyze()
    assert r.score == 1.0
    assert "timed out" in r.details


# ── Pass path ───────────────────────────────────────────────────────────────


def test_pytest_clean_run_returns_pass(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Python"])
    fake = subprocess.CompletedProcess(
        args=["pytest"], returncode=0,
        stdout="3 passed in 0.05s\n", stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 1.0
    assert r.status == "pass"
    assert "tests passing" in r.details
    assert "(3 test(s))" in r.details


def test_cargo_clean_run_returns_pass(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname=\"x\"\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Rust"])
    fake = subprocess.CompletedProcess(
        args=["cargo"], returncode=0,
        stdout="test result: ok. 5 passed; 0 failed; 0 ignored\n", stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 1.0
    assert "(5 test(s))" in r.details


# ── Fail path with parsed failing tests ─────────────────────────────────────


def test_pytest_failures_listed_with_file_paths(tmp_path: Path) -> None:
    """The crucial rung-3 case: failing tests must surface in details
    with their exact paths so the planner can target the source files."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Python"])
    fake = subprocess.CompletedProcess(
        args=["pytest"], returncode=1, stdout=(
            "tests/test_db.py::test_no_sql_injection FAILED\n"
            "tests/test_db.py::test_no_sql_injection_via_comment FAILED\n"
            "1 passed, 2 failed in 0.05s\n"
        ), stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert r.status == "fail"
    assert "TESTS FAILING (2)" in r.details
    assert "tests/test_db.py::test_no_sql_injection" in r.details
    assert "tests/test_db.py::test_no_sql_injection_via_comment" in r.details
    # Planner-facing imperative — direct evidence the operator can act on
    assert "T001 MUST target" in r.details


def test_cargo_failures_listed(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname=\"x\"\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Rust"])
    fake = subprocess.CompletedProcess(
        args=["cargo"], returncode=1, stdout=(
            "test calculator::divides_cleanly ... FAILED\n"
            "test calculator::reports_zero_denom ... ok\n"
            "test result: FAILED. 1 passed; 1 failed\n"
        ), stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert "TESTS FAILING (1)" in r.details
    assert "calculator::divides_cleanly" in r.details


def test_node_failures_listed_with_time_suffix(tmp_path: Path) -> None:
    """``node --test`` TTY output uses ``✖ name (Xms)`` for failures.
    Header lines like ``✖ failing tests:`` (no time suffix) MUST be
    filtered out — caught live on rung-4 v1, where the header was
    captured as a fake fourth failure."""
    (tmp_path / "package.json").write_text('{"name":"x","scripts":{"test":"node --test"}}')
    a = TestRunnerAnalyzer(root=tmp_path, languages=["JavaScript"])
    fake = subprocess.CompletedProcess(
        args=["npm"], returncode=1, stdout=(
            "✔ normal name renders inside a div (0.4ms)\n"
            "✖ ampersand is escaped (0.3ms)\n"
            "✖ script tag is text (0.1ms)\n"
            "✖ failing tests:\n"  # this is a HEADER, not a test
            "ℹ pass 1\n"
            "ℹ fail 2\n"
        ), stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert "TESTS FAILING (2)" in r.details, (
        f"header line was likely captured as fake test; details={r.details!r}"
    )
    assert "ampersand is escaped" in r.details
    assert "script tag is text" in r.details
    # The header itself must NOT appear as a "failure"
    assert "failing tests:" not in r.details


def test_node_failures_in_raw_tap_format(tmp_path: Path) -> None:
    """Some node test runners emit raw TAP. ``not ok N - name`` is the
    canonical fail line — no time suffix to filter on, but the ``not ok``
    prefix is unambiguous."""
    (tmp_path / "package.json").write_text('{"name":"x","scripts":{"test":"node --test"}}')
    a = TestRunnerAnalyzer(root=tmp_path, languages=["JavaScript"])
    fake = subprocess.CompletedProcess(
        args=["npm"], returncode=1, stdout=(
            "TAP version 13\n"
            "ok 1 - first test\n"
            "not ok 2 - second test fails\n"
            "not ok 3 - third also fails\n"
        ), stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert "second test fails" in r.details
    assert "third also fails" in r.details


def test_go_failures_listed(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Go"])
    fake = subprocess.CompletedProcess(
        args=["go"], returncode=1, stdout=(
            "--- FAIL: TestGreetKnownUser (0.00s)\n"
            "    server_test.go:18: assignment mismatch\n"
            "FAIL\tgitoma-bench-rung-1/server\n"
        ), stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert "TestGreetKnownUser" in r.details


def test_failure_with_unparseable_output_still_reports_fail(tmp_path: Path) -> None:
    """Non-zero exit + parser couldn't extract failures (e.g., compile
    error before tests run) — still report fail with raw tail. We'd
    rather over-flag than under-flag."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    a = TestRunnerAnalyzer(root=tmp_path, languages=["Python"])
    fake = subprocess.CompletedProcess(
        args=["pytest"], returncode=2,
        stdout="", stderr="ImportError: no module named foo\n",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()
    assert r.score == 0.0
    assert "TESTS FAILED" in r.details
    assert "ImportError" in r.details


# ── Parser unit tests (isolated, no subprocess) ─────────────────────────────


def test_pytest_parser_handles_trailing_reason_block() -> None:
    """Caught live on rung-3 v7: pytest summary lines often carry a
    trailing ``- AssertionError: ...`` reason. The parser MUST capture
    the test path even when followed by arbitrary text."""
    text = (
        "FAILED tests/test_db.py::test_no_sql_injection - AssertionError: leak\n"
        "FAILED tests/test_db.py::test_two - AssertionError: another fail\n"
        "2 failed, 2 passed in 0.01s\n"
    )
    out = _parse_failing("pytest", text, "")
    assert "tests/test_db.py::test_no_sql_injection" in out
    assert "tests/test_db.py::test_two" in out
    assert len(out) == 2


def test_pytest_parser_extracts_failed_and_error() -> None:
    text = (
        "FAILED tests/test_a.py::test_foo\n"
        "ERROR tests/test_b.py::test_bar\n"
        "FAILED tests/test_a.py::test_baz\n"
    )
    out = _parse_failing("pytest", text, "")
    assert "tests/test_a.py::test_foo" in out
    assert "tests/test_b.py::test_bar" in out
    assert "tests/test_a.py::test_baz" in out
    assert len(out) == 3


def test_pytest_parser_dedupes() -> None:
    text = "FAILED a::x\nFAILED a::x\nFAILED a::x\n"
    assert _parse_failing("pytest", text, "") == ["a::x"]


def test_cargo_parser_handles_summary_block() -> None:
    text = (
        "running 2 tests\n"
        "test a ... FAILED\n"
        "test b ... FAILED\n"
        "\nfailures:\n"
        "    a\n"
        "    b\n"
    )
    out = _parse_failing("cargo", text, "")
    assert "a" in out and "b" in out


def test_pytest_pass_count() -> None:
    assert _count_passing("pytest", "5 passed in 0.1s\n", "") == 5
    assert _count_passing("pytest", "no tests ran\n", "") == 0


# ── Weight + planner-relevance ──────────────────────────────────────────────


def test_weight_high_enough_to_dominate() -> None:
    """Weight = 4 so a failing Test Results dominates the weighted
    overall_score. Build (5) still wins when both fail — Build comes
    first in the analysis pipeline."""
    assert TestRunnerAnalyzer.weight >= 3.0
    # And below Build (5) so Build wins on tie-break.
    from gitoma.analyzers.build import BuildAnalyzer
    assert TestRunnerAnalyzer.weight <= BuildAnalyzer.weight
