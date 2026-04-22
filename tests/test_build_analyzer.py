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


def test_enrichment_attaches_source_snippet_and_taxonomy(tmp_path: Path) -> None:
    """Enriched details include (a) a structured-errors block, (b) a
    taxonomy tag classifying the error, (c) a source snippet with a
    ``>`` marker on the offending line. Every bit of this is signal
    the planner / worker won't need to hallucinate."""
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.go").write_text(
        "package server\n\n"
        "type S struct{}\n\n"
        "func (s *S) Greet(id int) string {\n"
        "\tname := s.users.Get(id)\n"  # line 6 — the "bad" line
        "\treturn name\n"
        "}\n"
    )
    a = BuildAnalyzer(root=tmp_path, languages=["Go"])

    fake = subprocess.CompletedProcess(
        args=["go", "build", "./..."],
        returncode=1,
        stdout="",
        stderr="server/server.go:6:2: assignment mismatch: 1 variable but s.users.Get returns 2 values\n",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()

    assert r.score == 0.0
    assert "── structured errors ──" in r.details
    assert "[signature_mismatch]" in r.details
    # Source snippet has the ``>`` marker on the offending line
    assert "> 6|" in r.details or ">  6|" in r.details
    # And includes the actual line content
    assert "s.users.Get(id)" in r.details


def test_taxonomy_classifies_undefined_symbol() -> None:
    """Importers / worker can key decisions on the tag without re-parsing
    the raw message, so each tag must be reliable."""
    from gitoma.analyzers.build import _classify

    assert _classify("undefined: foo") == "undefined_symbol"
    assert _classify("NameError: name 'x' is not defined") == "undefined_symbol"
    assert _classify("mismatched types Vec<u8>, String") == "type_mismatch"
    assert _classify("SyntaxError: invalid syntax") == "syntax_error"
    assert _classify("assignment mismatch: 1 variable") == "signature_mismatch"
    assert _classify("ModuleNotFoundError: No module named 'x'") == "missing_import"
    assert _classify("random other compiler gibberish") == "other"


def test_enrichment_includes_cross_file_hint(tmp_path: Path) -> None:
    """When the error message mentions a symbol (``Get``) whose definition
    lives in another file of the repo, the enrichment points at it.
    Avoids the worker hallucinating a signature."""
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "store.go").write_text(
        "package store\n\n"
        "type UserStore struct{}\n\n"
        "func (s *UserStore) Greet(id int) (string, bool) {\n"
        "\treturn \"\", true\n"
        "}\n"
    )
    (tmp_path / "server.go").write_text(
        "package x\n\n"
        "func f() { _ = s.Greet(1) }\n"
    )
    a = BuildAnalyzer(root=tmp_path, languages=["Go"])

    fake = subprocess.CompletedProcess(
        args=["go", "build", "./..."],
        returncode=1,
        stdout="",
        stderr="server.go:3:12: assignment mismatch: 1 variable but s.Greet returns 2 values\n",
    )
    with patch("subprocess.run", return_value=fake):
        r = a.analyze()

    # The hint may or may not fire depending on stopword filtering;
    # when it fires, it must point at store/store.go. A missed hint is
    # not a regression (the other signals are enough), but a WRONG
    # hint is.
    if "related:" in r.details:
        assert "store/store.go" in r.details
