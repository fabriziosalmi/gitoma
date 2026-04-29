"""Tests for the G22 trivy-regression critic.

Pure-function tests on baseline computation + diff logic.
End-to-end ``check_g22_trivy_regression`` is exercised via mocked
TrivyClient.scan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitoma.integrations.trivy_scan import TrivyFinding
from gitoma.worker.trivy_regression import (
    G22Conflict,
    G22Result,
    check_g22_trivy_regression,
    compute_trivy_baseline_fingerprints,
    g22_severity_floor,
    g22_touched_only,
    is_g22_enabled,
)


# ── Env opt-in helpers ────────────────────────────────────────────


def test_is_g22_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G22_TRIVY", raising=False)
    assert is_g22_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_g22_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", val)
    assert is_g22_enabled() is True


def test_is_g22_enabled_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "0")
    assert is_g22_enabled() is False


def test_g22_severity_floor_default_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITOMA_G22_SEVERITY", raising=False)
    assert g22_severity_floor() == 0


def test_g22_severity_floor_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G22_SEVERITY", "warning")
    assert g22_severity_floor() == 1


def test_g22_severity_floor_unknown_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G22_SEVERITY", "garbage")
    assert g22_severity_floor() == 0


def test_g22_touched_only_default_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITOMA_G22_TOUCHED_ONLY", raising=False)
    assert g22_touched_only() is False


def test_g22_touched_only_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G22_TOUCHED_ONLY", "1")
    assert g22_touched_only() is True


# ── compute_trivy_baseline_fingerprints ───────────────────────────


def test_baseline_empty_input() -> None:
    assert compute_trivy_baseline_fingerprints([]) == set()


def test_baseline_extracts_rule_id_target() -> None:
    findings = [
        TrivyFinding("vuln", "CVE-1", "Pipfile.lock", "ERROR", "t",
                     pkg_name="requests", installed_version="2.20.0"),
        TrivyFinding("secret", "aws-key", ".env", "ERROR", "t", line=5),
    ]
    out = compute_trivy_baseline_fingerprints(findings)
    assert out == {("CVE-1", "Pipfile.lock"), ("aws-key", ".env")}


def test_baseline_severity_floor_error_excludes_warnings() -> None:
    findings = [
        TrivyFinding("vuln", "C1", "p", "ERROR", "t"),
        TrivyFinding("vuln", "C2", "p", "WARNING", "t"),
        TrivyFinding("misconfig", "M1", "Dockerfile", "INFO", "t"),
    ]
    out = compute_trivy_baseline_fingerprints(findings, severity_floor=0)
    assert out == {("C1", "p")}


def test_baseline_severity_floor_warning_includes_warnings() -> None:
    findings = [
        TrivyFinding("vuln", "C1", "p", "ERROR", "t"),
        TrivyFinding("vuln", "C2", "p", "WARNING", "t"),
        TrivyFinding("misconfig", "M1", "Dockerfile", "INFO", "t"),
    ]
    out = compute_trivy_baseline_fingerprints(findings, severity_floor=1)
    assert out == {("C1", "p"), ("C2", "p")}


def test_baseline_dedups_same_rule_target() -> None:
    """Two findings of the same (rule_id, target) collapse to one entry."""
    findings = [
        TrivyFinding("vuln", "CVE-1", "p", "ERROR", "t",
                     pkg_name="a", installed_version="1.0"),
        TrivyFinding("vuln", "CVE-1", "p", "ERROR", "t",
                     pkg_name="b", installed_version="2.0"),
    ]
    assert len(compute_trivy_baseline_fingerprints(findings)) == 1


def test_baseline_skips_empty_rule_id_or_target() -> None:
    findings = [
        TrivyFinding("vuln", "", "p", "ERROR", "t"),
        TrivyFinding("vuln", "r", "", "ERROR", "t"),
        TrivyFinding("vuln", "r", "p", "ERROR", "t"),
    ]
    out = compute_trivy_baseline_fingerprints(findings)
    assert out == {("r", "p")}


def test_baseline_handles_unknown_severity() -> None:
    findings = [TrivyFinding("vuln", "r", "p", "DEBUG", "t")]
    assert compute_trivy_baseline_fingerprints(findings) == set()


# ── check_g22 — silent-skip paths ─────────────────────────────────


def test_check_g22_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("GITOMA_G22_TRIVY", raising=False)
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g22_baseline_none_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Baseline=None means PHASE 1.8 didn't run → skip silently."""
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"], baseline_fingerprints=None,
    )
    assert out is None


def test_check_g22_empty_touched_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    out = check_g22_trivy_regression(
        tmp_path, [], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g22_invalid_repo_root_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    out = check_g22_trivy_regression(
        "/definitely/not/a/path/xyz", ["a.py"], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g22_disabled_client_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = False
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(), client=fake,
    )
    assert out is None


def test_check_g22_no_findings_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = []
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(), client=fake,
    )
    assert out is None


# ── check_g22 — diff logic ────────────────────────────────────────


def test_check_g22_baseline_finding_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("vuln", "CVE-1", "Pipfile.lock", "ERROR", "t"),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints={("CVE-1", "Pipfile.lock")},
        client=fake,
    )
    assert out is None


def test_check_g22_new_vuln_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    monkeypatch.delenv("GITOMA_G22_TOUCHED_ONLY", raising=False)
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding(
            "vuln", "CVE-9999", "Pipfile.lock", "ERROR",
            "Path traversal in requests",
            pkg_name="requests", installed_version="2.20.0",
            fixed_version="2.20.1",
        ),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.kind == "vuln"
    assert c.rule_id == "CVE-9999"
    assert c.fixed_version == "2.20.1"


def test_check_g22_new_secret_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("secret", "aws-key", ".env", "ERROR",
                     "AWS Access Key", line=5),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None
    assert out.conflicts[0].kind == "secret"
    assert out.conflicts[0].line == 5


def test_check_g22_new_misconfig_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("misconfig", "DS001", "Dockerfile", "ERROR",
                     "Use --no-cache", line=12),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None
    assert out.conflicts[0].kind == "misconfig"


def test_check_g22_severity_floor_filters_warnings_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    monkeypatch.delenv("GITOMA_G22_SEVERITY", raising=False)
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("vuln", "CVE-X", "p", "WARNING", "t"),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is None


def test_check_g22_severity_floor_warning_catches_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    monkeypatch.setenv("GITOMA_G22_SEVERITY", "warning")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("misconfig", "M1", "Dockerfile", "WARNING", "t"),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None


def test_check_g22_default_scope_includes_untouched_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Default G22 scope = whole repo, not touched-only. A new vuln in
    a manifest the patch didn't touch directly STILL fires."""
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    monkeypatch.delenv("GITOMA_G22_TOUCHED_ONLY", raising=False)
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("vuln", "CVE-X", "Pipfile.lock", "ERROR", "t"),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["src/foo.py"],  # patch only touched src/foo.py
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None  # Pipfile.lock vuln still caught


def test_check_g22_touched_only_filters_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    monkeypatch.setenv("GITOMA_G22_TOUCHED_ONLY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("vuln", "CVE-X", "Pipfile.lock", "ERROR", "t"),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["src/foo.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is None  # Pipfile.lock not in touched, skipped


def test_check_g22_multiple_kinds_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All 3 finding kinds aggregate into the same G22Result."""
    monkeypatch.setenv("GITOMA_G22_TRIVY", "1")
    fake = MagicMock()
    fake.enabled = True
    fake.scan.return_value = [
        TrivyFinding("vuln", "CVE-1", "Pipfile.lock", "ERROR", "t"),
        TrivyFinding("secret", "aws-key", ".env", "ERROR", "t", line=5),
        TrivyFinding("misconfig", "DS001", "Dockerfile", "ERROR", "t", line=1),
    ]
    out = check_g22_trivy_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake,
    )
    assert out is not None
    assert len(out.conflicts) == 3
    assert {c.kind for c in out.conflicts} == {"vuln", "secret", "misconfig"}


# ── G22Result.render_for_llm ──────────────────────────────────────


def test_render_empty_returns_empty() -> None:
    assert G22Result(conflicts=()).render_for_llm() == ""


def test_render_vuln_includes_bump_target() -> None:
    r = G22Result(conflicts=(
        G22Conflict("vuln", "CVE-1", "p", "ERROR", "Title",
                    pkg_name="requests", installed_version="2.20.0",
                    fixed_version="2.20.1"),
    ))
    out = r.render_for_llm()
    assert "requests@2.20.0" in out
    assert "bump to 2.20.1" in out


def test_render_secret_uses_path_line() -> None:
    r = G22Result(conflicts=(
        G22Conflict("secret", "aws", ".env", "ERROR", "AWS key", line=5),
    ))
    assert ".env:5" in r.render_for_llm()


def test_render_misconfig_uses_path_line() -> None:
    r = G22Result(conflicts=(
        G22Conflict("misconfig", "DS001", "Dockerfile", "ERROR", "t", line=12),
    ))
    assert "Dockerfile:12" in r.render_for_llm()


def test_render_lists_count_in_header() -> None:
    r = G22Result(conflicts=tuple(
        G22Conflict("vuln", f"CVE-{i}", "p", "ERROR", "t") for i in range(3)
    ))
    assert "3" in r.render_for_llm()


# ── Dataclass invariants ──────────────────────────────────────────


def test_g22_conflict_is_frozen() -> None:
    c = G22Conflict("vuln", "r", "t", "ERROR", "title")
    with pytest.raises(Exception):
        c.target = "x"  # type: ignore[misc]


def test_g22_result_is_frozen() -> None:
    r = G22Result(conflicts=())
    with pytest.raises(Exception):
        r.conflicts = ()  # type: ignore[misc]
