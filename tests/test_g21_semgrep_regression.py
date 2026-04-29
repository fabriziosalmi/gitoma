"""Tests for the G21 semgrep-regression critic.

Pure-function tests on baseline computation + diff logic. The
subprocess-invoking ``check_g21_semgrep_regression`` end-to-end is
exercised via mocked SemgrepClient.scan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitoma.integrations.semgrep_scan import SemgrepClient, SemgrepFinding
from gitoma.worker.semgrep_regression import (
    G21Conflict,
    G21Result,
    check_g21_semgrep_regression,
    compute_baseline_fingerprints,
    g21_severity_floor,
    is_g21_enabled,
)


# ── Env opt-in helpers ────────────────────────────────────────────


def test_is_g21_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G21_SEMGREP", raising=False)
    assert is_g21_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_g21_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("GITOMA_G21_SEMGREP", val)
    assert is_g21_enabled() is True


def test_is_g21_enabled_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "0")
    assert is_g21_enabled() is False


def test_g21_severity_floor_default_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITOMA_G21_SEVERITY", raising=False)
    assert g21_severity_floor() == 0  # ERROR


def test_g21_severity_floor_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G21_SEVERITY", "warning")
    assert g21_severity_floor() == 1


def test_g21_severity_floor_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G21_SEVERITY", "info")
    assert g21_severity_floor() == 2


def test_g21_severity_floor_unknown_falls_back_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G21_SEVERITY", "garbage")
    assert g21_severity_floor() == 0


# ── compute_baseline_fingerprints ─────────────────────────────────


def test_baseline_empty_input() -> None:
    assert compute_baseline_fingerprints([]) == set()


def test_baseline_extracts_rule_id_path() -> None:
    findings = [
        SemgrepFinding("rule.a", "foo.py", 10, "ERROR", "m1"),
        SemgrepFinding("rule.b", "bar.py", 20, "ERROR", "m2"),
    ]
    out = compute_baseline_fingerprints(findings)
    assert out == {("rule.a", "foo.py"), ("rule.b", "bar.py")}


def test_baseline_filters_by_severity_floor_error() -> None:
    """Default floor=ERROR (rank 0): only ERRORs in baseline."""
    findings = [
        SemgrepFinding("err1", "a.py", 1, "ERROR", "m"),
        SemgrepFinding("warn1", "b.py", 1, "WARNING", "m"),
        SemgrepFinding("info1", "c.py", 1, "INFO", "m"),
    ]
    out = compute_baseline_fingerprints(findings, severity_floor=0)
    assert out == {("err1", "a.py")}


def test_baseline_severity_floor_warning_includes_warnings() -> None:
    findings = [
        SemgrepFinding("err1", "a.py", 1, "ERROR", "m"),
        SemgrepFinding("warn1", "b.py", 1, "WARNING", "m"),
        SemgrepFinding("info1", "c.py", 1, "INFO", "m"),
    ]
    out = compute_baseline_fingerprints(findings, severity_floor=1)
    assert out == {("err1", "a.py"), ("warn1", "b.py")}


def test_baseline_dedups_by_rule_id_path() -> None:
    """Two findings of the same rule on the same file = one baseline entry."""
    findings = [
        SemgrepFinding("r", "a.py", 10, "ERROR", "m"),
        SemgrepFinding("r", "a.py", 99, "ERROR", "m"),  # same rule, different line
    ]
    assert len(compute_baseline_fingerprints(findings)) == 1


def test_baseline_skips_empty_rule_id_or_path() -> None:
    findings = [
        SemgrepFinding("", "a.py", 1, "ERROR", "m"),
        SemgrepFinding("r", "", 1, "ERROR", "m"),
        SemgrepFinding("r", "a.py", 1, "ERROR", "m"),
    ]
    out = compute_baseline_fingerprints(findings)
    assert out == {("r", "a.py")}


def test_baseline_handles_unknown_severity() -> None:
    """Unrecognised severities (rank=99) get excluded by any normal floor."""
    findings = [SemgrepFinding("r", "a.py", 1, "DEBUG", "m")]
    assert compute_baseline_fingerprints(findings) == set()


# ── check_g21_semgrep_regression — silent-skip paths ──────────────


def test_check_g21_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("GITOMA_G21_SEMGREP", raising=False)
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g21_baseline_none_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Baseline=None means PHASE 1.6 didn't run → skip silently."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"], baseline_fingerprints=None,
    )
    assert out is None


def test_check_g21_empty_touched_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    out = check_g21_semgrep_regression(
        tmp_path, [], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g21_invalid_repo_root_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    out = check_g21_semgrep_regression(
        "/definitely/not/a/path/xyz", ["a.py"], baseline_fingerprints=set(),
    )
    assert out is None


def test_check_g21_disabled_client_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the SemgrepClient is disabled (binary missing) → skip."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = False
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(), client=fake_client,
    )
    assert out is None


def test_check_g21_no_findings_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Scan returns [] (clean repo post-patch) → no regression."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = []
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"], baseline_fingerprints=set(), client=fake_client,
    )
    assert out is None


# ── check_g21 — diff logic (the meat) ─────────────────────────────


def test_check_g21_baseline_finding_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A finding present in baseline is NOT a regression."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("r", "a.py", 10, "ERROR", "m"),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints={("r", "a.py")},
        client=fake_client,
    )
    assert out is None


def test_check_g21_new_finding_in_touched_file_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A finding NOT in baseline AND in a touched file → G21 fires."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("new.rule", "a.py", 42, "ERROR",
                       "Found dangerous eval()", cwe=("CWE-95",)),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),  # empty baseline
        client=fake_client,
    )
    assert out is not None
    assert len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c.rule_id == "new.rule"
    assert c.path == "a.py"
    assert c.line == 42
    assert c.severity == "ERROR"
    assert c.cwe == ("CWE-95",)


def test_check_g21_new_finding_in_untouched_file_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The patch only touched a.py — a new finding on b.py is the
    operator's existing problem, not this patch's regression."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("r", "b.py", 1, "ERROR", "m"),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"],  # only a.py touched
        baseline_fingerprints=set(),
        client=fake_client,
    )
    assert out is None


def test_check_g21_severity_floor_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Default floor=ERROR: a new WARNING in a touched file does NOT trigger."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    monkeypatch.delenv("GITOMA_G21_SEVERITY", raising=False)
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("r", "a.py", 1, "WARNING", "style nit"),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake_client,
    )
    assert out is None


def test_check_g21_severity_floor_warning_does_trigger_on_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When floor lowered to WARNING, new WARNINGs become G21 fails."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    monkeypatch.setenv("GITOMA_G21_SEVERITY", "warning")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("r", "a.py", 1, "WARNING", "style nit"),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py"],
        baseline_fingerprints=set(),
        client=fake_client,
    )
    assert out is not None
    assert out.conflicts[0].severity == "WARNING"


def test_check_g21_multiple_new_findings_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All new findings in touched files come back as conflicts."""
    monkeypatch.setenv("GITOMA_G21_SEMGREP", "1")
    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.scan.return_value = [
        SemgrepFinding("rule.a", "a.py", 1, "ERROR", "m1"),
        SemgrepFinding("rule.b", "b.py", 1, "ERROR", "m2"),
        SemgrepFinding("baseline.rule", "c.py", 1, "ERROR", "m3"),
    ]
    out = check_g21_semgrep_regression(
        tmp_path, ["a.py", "b.py", "c.py"],
        baseline_fingerprints={("baseline.rule", "c.py")},
        client=fake_client,
    )
    assert out is not None
    assert len(out.conflicts) == 2
    assert {c.rule_id for c in out.conflicts} == {"rule.a", "rule.b"}


# ── G21Result.render_for_llm ──────────────────────────────────────


def test_render_empty_conflicts_returns_empty() -> None:
    r = G21Result(conflicts=())
    assert r.render_for_llm() == ""


def test_render_includes_path_line_rule_id_severity() -> None:
    r = G21Result(conflicts=(
        G21Conflict("rule.x", "foo.py", 42, "ERROR", "Found bad thing"),
    ))
    out = r.render_for_llm()
    assert "foo.py:42" in out
    assert "rule.x" in out
    assert "ERROR" in out
    assert "Found bad thing" in out


def test_render_includes_cwe_when_present() -> None:
    r = G21Result(conflicts=(
        G21Conflict("r", "a.py", 1, "ERROR", "m", cwe=("CWE-89",)),
    ))
    assert "CWE-89" in r.render_for_llm()


def test_render_truncates_long_message() -> None:
    """Messages capped at 160 chars to keep LLM feedback compact."""
    r = G21Result(conflicts=(
        G21Conflict("r", "a.py", 1, "ERROR", "x" * 500),
    ))
    out = r.render_for_llm()
    assert "x" * 161 not in out


def test_render_lists_count_in_header() -> None:
    r = G21Result(conflicts=tuple(
        G21Conflict(f"r{i}", "a.py", i, "ERROR", "m") for i in range(3)
    ))
    out = r.render_for_llm()
    assert "3" in out


# ── Dataclass invariants ──────────────────────────────────────────


def test_g21_conflict_is_frozen() -> None:
    c = G21Conflict("r", "a.py", 1, "ERROR", "m")
    with pytest.raises(Exception):
        c.path = "other"  # type: ignore[misc]


def test_g21_result_is_frozen() -> None:
    r = G21Result(conflicts=())
    with pytest.raises(Exception):
        r.conflicts = ()  # type: ignore[misc]
