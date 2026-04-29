"""Tests for the Trivy supply-chain scanner client wrapper.

Pure-function tests + silent-fail-open invariant. No live trivy
binary needed — subprocess is mocked, and the binary-missing path
is exercised directly."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitoma.integrations.trivy_scan import (
    DEFAULT_MAX_CHARS,
    TrivyClient,
    TrivyConfig,
    TrivyFinding,
    render_findings_block,
)


# ── TrivyConfig.from_env ──────────────────────────────────────────


def test_config_disabled_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIVY_BIN", "definitely-not-a-real-binary-xyz")
    cfg = TrivyConfig.from_env()
    assert cfg.enabled is False


def test_config_enabled_when_binary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use 'sh' as a stand-in: always on PATH."""
    monkeypatch.setenv("TRIVY_BIN", "sh")
    cfg = TrivyConfig.from_env()
    assert cfg.enabled is True


def test_config_default_timeout_90s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIVY_BIN", "sh")
    monkeypatch.delenv("TRIVY_TIMEOUT_S", raising=False)
    cfg = TrivyConfig.from_env()
    assert cfg.timeout_s == 90.0


def test_config_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIVY_BIN", "sh")
    monkeypatch.setenv("TRIVY_TIMEOUT_S", "120")
    cfg = TrivyConfig.from_env()
    assert cfg.timeout_s == 120.0


def test_config_invalid_timeout_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIVY_BIN", "sh")
    monkeypatch.setenv("TRIVY_TIMEOUT_S", "not-a-number")
    cfg = TrivyConfig.from_env()
    assert cfg.timeout_s == 90.0


def test_config_clamps_minimum_timeout_to_10s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trivy first-run downloads its DB (~5s); 0.5s would always fail."""
    monkeypatch.setenv("TRIVY_BIN", "sh")
    monkeypatch.setenv("TRIVY_TIMEOUT_S", "0.5")
    cfg = TrivyConfig.from_env()
    assert cfg.timeout_s >= 10.0


# ── Silent fail-open contract ─────────────────────────────────────


def test_disabled_client_scan_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="nope", enabled=False)
    client = TrivyClient(cfg)
    assert client.scan(tmp_path) == []


def test_scan_zero_max_findings_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    assert client.scan(tmp_path, max_findings=0) == []


def test_scan_nonexistent_dir_returns_empty() -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    assert client.scan("/definitely/not/a/real/path/xyz") == []


def test_scan_timeout_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("trivy", 90)):
        assert client.scan(tmp_path) == []


def test_scan_oserror_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    with patch("subprocess.run", side_effect=OSError("missing binary")):
        assert client.scan(tmp_path) == []


def test_scan_non_json_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    fake = MagicMock(returncode=0, stdout="not json", stderr="")
    with patch("subprocess.run", return_value=fake):
        assert client.scan(tmp_path) == []


def test_scan_non_dict_root_returns_empty(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    fake = MagicMock(returncode=0, stdout="[1, 2]", stderr="")
    with patch("subprocess.run", return_value=fake):
        assert client.scan(tmp_path) == []


def test_scan_exit_code_2_returns_empty(tmp_path: Path) -> None:
    """Exit code 2+ means trivy itself errored — don't trust output."""
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    fake = MagicMock(returncode=2, stdout='{"Results": []}', stderr="config error")
    with patch("subprocess.run", return_value=fake):
        assert client.scan(tmp_path) == []


def test_scan_no_results_field_returns_empty(tmp_path: Path) -> None:
    """Trivy output without Results field → safe empty."""
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    fake = MagicMock(returncode=0, stdout='{}', stderr="")
    with patch("subprocess.run", return_value=fake):
        assert client.scan(tmp_path) == []


# ── Vulnerability parsing ─────────────────────────────────────────


def _make_proc(results: list[dict]) -> MagicMock:
    return MagicMock(
        returncode=0, stdout=json.dumps({"Results": results}), stderr="",
    )


def test_scan_parses_vuln(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "Pipfile.lock",
        "Vulnerabilities": [{
            "VulnerabilityID": "CVE-2023-12345",
            "PkgName": "requests",
            "InstalledVersion": "2.20.0",
            "FixedVersion": "2.20.1",
            "Severity": "HIGH",
            "Title": "Path traversal in requests",
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    f = out[0]
    assert f.kind == "vuln"
    assert f.rule_id == "CVE-2023-12345"
    assert f.pkg_name == "requests"
    assert f.installed_version == "2.20.0"
    assert f.fixed_version == "2.20.1"
    assert f.severity == "ERROR"  # HIGH normalises to ERROR


def test_severity_normalisation_critical_high_become_error(
    tmp_path: Path,
) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"VulnerabilityID": "C1", "Severity": "CRITICAL", "Title": "t"},
            {"VulnerabilityID": "H1", "Severity": "HIGH", "Title": "t"},
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert all(f.severity == "ERROR" for f in out)


def test_severity_normalisation_medium_becomes_warning(
    tmp_path: Path,
) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"VulnerabilityID": "M1", "Severity": "MEDIUM", "Title": "t"},
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert out[0].severity == "WARNING"


def test_severity_normalisation_low_unknown_become_info(
    tmp_path: Path,
) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"VulnerabilityID": "L1", "Severity": "LOW", "Title": "t"},
            {"VulnerabilityID": "U1", "Severity": "UNKNOWN", "Title": "t"},
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert all(f.severity == "INFO" for f in out)


def test_scan_skips_vuln_without_id(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"PkgName": "foo", "Severity": "HIGH"},  # no VulnerabilityID
            {"VulnerabilityID": "CVE-1", "Severity": "HIGH", "Title": "t"},
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    assert out[0].rule_id == "CVE-1"


def test_scan_keeps_vuln_references_capped(tmp_path: Path) -> None:
    """References list gets truncated at 3 entries to keep prompt tight."""
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [{
            "VulnerabilityID": "CVE-1", "Severity": "HIGH",
            "Title": "t", "References": [f"https://ref{i}.com" for i in range(20)],
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out[0].references) <= 3


# ── Secret parsing ────────────────────────────────────────────────


def test_scan_parses_secret(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "config/.env",
        "Secrets": [{
            "RuleID": "aws-access-key-id",
            "Severity": "HIGH",
            "Title": "AWS Access Key ID",
            "StartLine": 5,
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    f = out[0]
    assert f.kind == "secret"
    assert f.target == "config/.env"
    assert f.line == 5


def test_scan_secret_falls_back_to_category_when_ruleid_missing(
    tmp_path: Path,
) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "config/.env",
        "Secrets": [{
            "Category": "AWSCredentials", "Severity": "HIGH", "Title": "t",
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    assert out[0].rule_id == "AWSCredentials"


# ── Misconfig parsing ─────────────────────────────────────────────


def test_scan_parses_misconfig(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "Dockerfile",
        "Misconfigurations": [{
            "ID": "DS001",
            "Severity": "MEDIUM",
            "Title": "Specify --no-cache option",
            "CauseMetadata": {"StartLine": 12},
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    f = out[0]
    assert f.kind == "misconfig"
    assert f.rule_id == "DS001"
    assert f.line == 12
    assert f.severity == "WARNING"  # MEDIUM normalises to WARNING


def test_scan_misconfig_falls_back_to_avdid(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "Dockerfile",
        "Misconfigurations": [{
            "AVDID": "AVD-DS-0001", "Severity": "HIGH", "Title": "t",
        }],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert out[0].rule_id == "AVD-DS-0001"


# ── Sort + cap ────────────────────────────────────────────────────


def test_scan_sorts_severity_then_kind(tmp_path: Path) -> None:
    """ERROR before WARNING; within same severity, vuln before secret
    before misconfig."""
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"VulnerabilityID": "vM", "Severity": "MEDIUM", "Title": "t"},
            {"VulnerabilityID": "vH", "Severity": "HIGH", "Title": "t"},
        ],
        "Secrets": [
            {"RuleID": "sH", "Severity": "HIGH", "Title": "t"},
        ],
        "Misconfigurations": [
            {"ID": "mH", "Severity": "HIGH", "Title": "t"},
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    # All ERROR first, in kind order: vH, sH, mH; then vM
    assert [f.rule_id for f in out] == ["vH", "sH", "mH", "vM"]


def test_scan_caps_at_max_findings(tmp_path: Path) -> None:
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([{
        "Target": "x",
        "Vulnerabilities": [
            {"VulnerabilityID": f"CVE-{i}", "Severity": "HIGH", "Title": "t"}
            for i in range(50)
        ],
    }])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path, max_findings=5)
    assert len(out) == 5


def test_scan_aggregates_across_results(tmp_path: Path) -> None:
    """Multiple Result entries (one per scanned target) all aggregate."""
    cfg = TrivyConfig(binary="sh", enabled=True)
    client = TrivyClient(cfg)
    proc = _make_proc([
        {"Target": "a", "Vulnerabilities": [
            {"VulnerabilityID": "C1", "Severity": "HIGH", "Title": "t"}]},
        {"Target": "b", "Vulnerabilities": [
            {"VulnerabilityID": "C2", "Severity": "HIGH", "Title": "t"}]},
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert {f.rule_id for f in out} == {"C1", "C2"}


# ── render_findings_block ─────────────────────────────────────────


def test_render_empty_returns_empty_string() -> None:
    assert render_findings_block([]) == ""


def test_render_groups_by_kind() -> None:
    findings = [
        TrivyFinding("vuln", "CVE-1", "x", "ERROR", "vt"),
        TrivyFinding("secret", "S1", "y", "ERROR", "st"),
        TrivyFinding("misconfig", "M1", "z", "ERROR", "mt"),
    ]
    out = render_findings_block(findings)
    assert "DEPENDENCY VULNERABILITIES" in out
    assert "SECRETS DETECTED" in out
    assert "INFRASTRUCTURE MISCONFIGURATIONS" in out
    # vuln before secret before misconfig
    assert out.index("DEPENDENCY") < out.index("SECRETS")
    assert out.index("SECRETS") < out.index("INFRASTRUCTURE")


def test_render_vuln_includes_bump_target() -> None:
    findings = [TrivyFinding(
        "vuln", "CVE-1", "x", "ERROR", "Path traversal",
        pkg_name="requests", installed_version="2.20.0",
        fixed_version="2.20.1",
    )]
    out = render_findings_block(findings)
    assert "requests@2.20.0" in out
    assert "bump to 2.20.1" in out
    assert "CVE-1" in out


def test_render_secret_uses_path_line_format() -> None:
    findings = [TrivyFinding(
        "secret", "aws-key", ".env", "ERROR", "AWS key", line=5,
    )]
    out = render_findings_block(findings)
    assert ".env:5" in out


def test_render_misconfig_uses_path_line_format() -> None:
    findings = [TrivyFinding(
        "misconfig", "DS001", "Dockerfile", "WARNING", "no-cache",
        line=12,
    )]
    out = render_findings_block(findings)
    assert "Dockerfile:12" in out


def test_render_truncates_when_over_budget() -> None:
    findings = [
        TrivyFinding("vuln", f"CVE-{i}", "p", "ERROR", "t",
                     pkg_name=f"pkg{i}", installed_version="1.0")
        for i in range(200)
    ]
    out = render_findings_block(findings, max_chars=400)
    assert len(out) <= 500
    assert "more" in out


def test_render_default_budget_const() -> None:
    findings = [
        TrivyFinding("vuln", f"CVE-{i}", "p", "ERROR", "t",
                     pkg_name=f"pkg{i}", installed_version="1.0")
        for i in range(10)
    ]
    out = render_findings_block(findings)
    assert len(out) <= DEFAULT_MAX_CHARS + 100


# ── TrivyFinding dataclass ────────────────────────────────────────


def test_finding_is_frozen() -> None:
    f = TrivyFinding("vuln", "r", "t", "ERROR", "title")
    with pytest.raises(Exception):
        f.target = "x"  # type: ignore[misc]


def test_finding_default_optional_fields() -> None:
    f = TrivyFinding("vuln", "r", "t", "ERROR", "title")
    assert f.pkg_name == ""
    assert f.installed_version == ""
    assert f.fixed_version == ""
    assert f.line == 0
    assert f.references == ()
