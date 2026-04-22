"""BuildAnalyzer smoke tests.

We assert the analyzer's CONTRACT: graceful skip when toolchain is
missing, score=0 with error-details when the build fails, score=1
when the build is clean. No live toolchain required — we fake
subprocess responses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from gitoma.analyzers.build import BuildAnalyzer


def test_skips_gracefully_when_toolchain_missing(tmp_path: Path) -> None:
    """``FileNotFoundError`` from subprocess → soft-pass, NOT a failed build.

    The analyzer must never become a reason to abort a run on a
    developer machine that happens not to have `go` / `cargo` / etc.
    """
    (tmp_path / "go.mod").write_text("module x\n")
    a = BuildAnalyzer(root=tmp_path, languages=["Go"])

    with patch("subprocess.run", side_effect=FileNotFoundError("go")):
        r = a.analyze()

    assert r.score == 1.0
    assert r.status == "pass"
    assert "skipped" in r.details.lower()


def test_go_build_failure_reports_error_lines(tmp_path: Path) -> None:
    """A non-zero return code with stderr → score 0 + fail + error text
    propagated into ``details``. The planner reads ``details`` so this
    is what lets it target the right files."""
    (tmp_path / "go.mod").write_text("module x\n")
    a = BuildAnalyzer(root=tmp_path, languages=["Go"])

    fake = subprocess.CompletedProcess(
        args=["go", "build", "./..."],
        returncode=1,
        stdout="",
        stderr="./main.go:10:5: undefined: foo\n./lib.go:3:1: syntax error\n",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()

    assert r.score == 0.0
    assert r.status == "fail"
    assert "BUILD FAILED" in r.details
    assert "main.go:10" in r.details
    assert "syntax error" in r.details


def test_go_build_clean_passes(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    a = BuildAnalyzer(root=tmp_path, languages=["Go"])

    fake = subprocess.CompletedProcess(
        args=["go", "build", "./..."], returncode=0, stdout="", stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()

    assert r.score == 1.0
    assert r.status == "pass"
    assert "builds clean" in r.details


def test_python_syntax_check_catches_bad_file(tmp_path: Path) -> None:
    """Python fallback path via ``py_compile``. We write one broken and
    one clean file, then assert the analyzer reports the broken one."""
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def (oops\n")  # syntax error

    a = BuildAnalyzer(root=tmp_path, languages=["Python"])
    r = a.analyze()

    assert r.score == 0.0
    assert r.status == "fail"
    assert "Python BUILD FAILED" in r.details
    assert "bad.py" in r.details


def test_no_recognised_toolchain_soft_passes(tmp_path: Path) -> None:
    """Exotic language without a command entry → soft-pass. We never
    want a false-positive ``fail`` on repos we simply can't check."""
    a = BuildAnalyzer(root=tmp_path, languages=["Haskell", "Zig"])
    r = a.analyze()
    assert r.score == 1.0
    assert r.status == "pass"
    assert "skipped" in r.details.lower()


def test_weight_is_high_enough_to_dominate() -> None:
    """Weight = 5.0 so a failing Build Integrity can't be washed out by
    9 other analyzers at weight 1.0 each in the overall_score."""
    assert BuildAnalyzer.weight >= 4.0
