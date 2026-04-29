"""Tests for the Semgrep static-analysis client wrapper.

Pure-function tests + silent-fail-open invariant. No live semgrep
binary needed — subprocess is mocked when present, and the
binary-missing path is exercised directly."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitoma.integrations.semgrep_scan import (
    DEFAULT_MAX_CHARS,
    SemgrepClient,
    SemgrepConfig,
    SemgrepFinding,
    render_findings_block,
)


# ── SemgrepConfig.from_env ────────────────────────────────────────


def test_config_disabled_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEMGREP_BIN", "definitely-not-a-real-binary-xyz")
    cfg = SemgrepConfig.from_env()
    assert cfg.enabled is False


def test_config_enabled_when_binary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use 'sh' as a stand-in: it's always on PATH."""
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    cfg = SemgrepConfig.from_env()
    assert cfg.enabled is True
    assert cfg.binary.endswith("/sh") or cfg.binary == "sh"


def test_config_default_timeout_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.delenv("SEMGREP_TIMEOUT_S", raising=False)
    cfg = SemgrepConfig.from_env()
    assert cfg.timeout_s == 60.0


def test_config_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.setenv("SEMGREP_TIMEOUT_S", "30")
    cfg = SemgrepConfig.from_env()
    assert cfg.timeout_s == 30.0


def test_config_invalid_timeout_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.setenv("SEMGREP_TIMEOUT_S", "not-a-number")
    cfg = SemgrepConfig.from_env()
    assert cfg.timeout_s == 60.0


def test_config_clamps_minimum_timeout_to_5s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid pathological 0.1s timeouts that would make every scan fail."""
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.setenv("SEMGREP_TIMEOUT_S", "0.5")
    cfg = SemgrepConfig.from_env()
    assert cfg.timeout_s >= 5.0


def test_config_default_config_is_p_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default changed from 'auto' to 'p/default' on 2026-04-29 EVE
    after bench-supply-chain build surfaced that '--config auto'
    requires '--metrics on' (incompatible with our metrics-off
    privacy posture). 'p/default' works fine with metrics off."""
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.delenv("SEMGREP_CONFIG", raising=False)
    cfg = SemgrepConfig.from_env()
    assert cfg.config == "p/default"


def test_config_custom_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMGREP_BIN", "sh")
    monkeypatch.setenv("SEMGREP_CONFIG", "p/security-audit")
    cfg = SemgrepConfig.from_env()
    assert cfg.config == "p/security-audit"


# ── Silent fail-open contract ─────────────────────────────────────


def test_disabled_client_scan_returns_empty(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="nope", config="auto", enabled=False)
    client = SemgrepClient(cfg)
    assert client.scan(tmp_path) == []


def test_scan_zero_max_findings_returns_empty(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    assert client.scan(tmp_path, max_findings=0) == []


def test_scan_nonexistent_dir_returns_empty() -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    assert client.scan("/definitely/not/a/real/path/xyz") == []


def test_scan_timeout_returns_empty(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("semgrep", 60)):
        assert client.scan(tmp_path) == []


def test_scan_oserror_returns_empty(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    with patch("subprocess.run", side_effect=OSError("missing binary")):
        assert client.scan(tmp_path) == []


def test_scan_non_json_output_returns_empty(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    fake_proc = MagicMock(returncode=0, stdout="not json at all", stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        assert client.scan(tmp_path) == []


def test_scan_non_dict_root_returns_empty(tmp_path: Path) -> None:
    """semgrep should always emit a JSON object — but defend anyway."""
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    fake_proc = MagicMock(returncode=0, stdout="[1, 2, 3]", stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        assert client.scan(tmp_path) == []


def test_scan_exit_code_2_returns_empty(tmp_path: Path) -> None:
    """Exit code 2+ means semgrep itself errored — don't trust output."""
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    fake_proc = MagicMock(
        returncode=2,
        stdout='{"results": [{"check_id": "x", "path": "y", "extra": {"severity": "ERROR"}}]}',
        stderr="config error",
    )
    with patch("subprocess.run", return_value=fake_proc):
        assert client.scan(tmp_path) == []


# ── Result parsing ────────────────────────────────────────────────


def _make_proc(results: list[dict], returncode: int = 1) -> MagicMock:
    return MagicMock(
        returncode=returncode,
        stdout=json.dumps({"results": results}),
        stderr="",
    )


def test_scan_parses_basic_finding(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {
            "check_id": "python.lang.security.dangerous-eval",
            "path": "foo.py",
            "start": {"line": 42},
            "extra": {
                "severity": "ERROR",
                "message": "Found eval() call which is dangerous",
            },
        },
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    f = out[0]
    assert f.rule_id == "python.lang.security.dangerous-eval"
    assert f.path == "foo.py"
    assert f.line == 42
    assert f.severity == "ERROR"
    assert "eval" in f.message


def test_scan_extracts_cwe_from_metadata(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {
            "check_id": "x",
            "path": "y.py",
            "start": {"line": 1},
            "extra": {
                "severity": "ERROR",
                "message": "m",
                "metadata": {"cwe": ["CWE-79", "CWE-200"]},
            },
        },
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert out[0].cwe == ("CWE-79", "CWE-200")


def test_scan_handles_string_cwe_metadata(tmp_path: Path) -> None:
    """Some rules emit CWE as a single string, not a list."""
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {
            "check_id": "x", "path": "y.py", "start": {"line": 1},
            "extra": {"severity": "WARNING", "message": "m",
                      "metadata": {"cwe": "CWE-89"}},
        },
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert out[0].cwe == ("CWE-89",)


def test_scan_skips_findings_without_check_id_or_path(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {"check_id": "", "path": "y", "extra": {"severity": "ERROR"}},  # no rule_id
        {"check_id": "x", "path": "", "extra": {"severity": "ERROR"}},  # no path
        {"check_id": "x", "path": "y", "start": {"line": 1},
         "extra": {"severity": "ERROR", "message": "m"}},
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 1
    assert out[0].rule_id == "x"


def test_scan_sorts_by_severity_then_path_line(tmp_path: Path) -> None:
    """ERROR before WARNING before INFO; within same severity, path:line."""
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {"check_id": "info1", "path": "z.py", "start": {"line": 1},
         "extra": {"severity": "INFO", "message": "i"}},
        {"check_id": "warn1", "path": "b.py", "start": {"line": 1},
         "extra": {"severity": "WARNING", "message": "w"}},
        {"check_id": "err1", "path": "a.py", "start": {"line": 5},
         "extra": {"severity": "ERROR", "message": "e1"}},
        {"check_id": "err2", "path": "a.py", "start": {"line": 1},
         "extra": {"severity": "ERROR", "message": "e2"}},
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    # ERROR comes first; within ERROR, a.py:1 before a.py:5
    assert out[0].rule_id == "err2"
    assert out[1].rule_id == "err1"
    assert out[2].rule_id == "warn1"
    assert out[3].rule_id == "info1"


def test_scan_caps_at_max_findings(tmp_path: Path) -> None:
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {"check_id": f"r{i}", "path": "p", "start": {"line": i},
         "extra": {"severity": "ERROR", "message": f"m{i}"}}
        for i in range(50)
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path, max_findings=10)
    assert len(out) == 10


def test_scan_handles_missing_extra_or_start(tmp_path: Path) -> None:
    """Defensive: rule entries with weird structure must not crash."""
    cfg = SemgrepConfig(binary="sh", config="auto", enabled=True)
    client = SemgrepClient(cfg)
    proc = _make_proc([
        {"check_id": "x", "path": "y.py"},  # no start, no extra
        {"check_id": "x2", "path": "y.py", "start": "not-a-dict",
         "extra": "also-not-a-dict"},
    ])
    with patch("subprocess.run", return_value=proc):
        out = client.scan(tmp_path)
    assert len(out) == 2
    assert out[0].line == 0
    assert out[0].severity == "INFO"  # default when missing


# ── render_findings_block ─────────────────────────────────────────


def test_render_empty_returns_empty_string() -> None:
    assert render_findings_block([]) == ""


def test_render_groups_by_severity() -> None:
    findings = [
        SemgrepFinding("r1", "a.py", 1, "ERROR", "m1"),
        SemgrepFinding("r2", "b.py", 2, "WARNING", "m2"),
        SemgrepFinding("r3", "c.py", 3, "INFO", "m3"),
    ]
    out = render_findings_block(findings)
    # ERROR header before WARNING before INFO
    assert out.index("### ERROR") < out.index("### WARNING")
    assert out.index("### WARNING") < out.index("### INFO")


def test_render_includes_rule_id_path_line() -> None:
    findings = [SemgrepFinding("python.eval-dangerous", "foo.py", 42, "ERROR", "Found eval()")]
    out = render_findings_block(findings)
    assert "foo.py:42" in out
    assert "python.eval-dangerous" in out
    assert "Found eval()" in out


def test_render_includes_cwe_when_present() -> None:
    findings = [
        SemgrepFinding("r", "a", 1, "ERROR", "m", cwe=("CWE-79",)),
    ]
    out = render_findings_block(findings)
    assert "CWE-79" in out


def test_render_truncates_message_at_100_chars() -> None:
    long_msg = "x" * 500
    findings = [SemgrepFinding("r", "a", 1, "ERROR", long_msg)]
    out = render_findings_block(findings)
    # Message slot capped at 100 chars
    assert "x" * 101 not in out


def test_render_truncates_when_over_budget() -> None:
    findings = [
        SemgrepFinding(f"r{i}", f"path/file{i}.py", i, "ERROR", f"message {i}")
        for i in range(200)
    ]
    out = render_findings_block(findings, max_chars=400)
    assert len(out) <= 500  # close to budget plus the truncation marker
    assert "more" in out


def test_render_default_budget_const() -> None:
    findings = [
        SemgrepFinding(f"r{i}", "a.py", i, "ERROR", f"m{i}")
        for i in range(10)
    ]
    out = render_findings_block(findings)
    assert len(out) <= DEFAULT_MAX_CHARS + 100


# ── SemgrepFinding dataclass ──────────────────────────────────────


def test_finding_is_frozen() -> None:
    f = SemgrepFinding("r", "p", 1, "ERROR", "m")
    with pytest.raises(Exception):
        f.path = "x"  # type: ignore[misc]


def test_finding_default_cwe_is_empty_tuple() -> None:
    f = SemgrepFinding("r", "p", 1, "ERROR", "m")
    assert f.cwe == ()


# ── Metrics adaptation (added 2026-04-29 EVE post-bench-supply-chain) ──


def test_scan_uses_metrics_off_for_static_config(tmp_path: Path) -> None:
    """For any non-`auto` config, the cmd must include `--metrics off`
    to preserve the privacy posture."""
    cfg = SemgrepConfig(
        binary="sh", config="p/default", enabled=True,
    )
    client = SemgrepClient(cfg)
    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout='{"results": []}', stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        client.scan(tmp_path)
    assert "--metrics" in captured["cmd"]
    idx = captured["cmd"].index("--metrics")
    assert captured["cmd"][idx + 1] == "off"


def test_scan_uses_metrics_on_for_auto_config(tmp_path: Path) -> None:
    """`--config auto` requires `--metrics on` per semgrep's design;
    the wrapper must adapt to avoid silent-bail."""
    cfg = SemgrepConfig(
        binary="sh", config="auto", enabled=True,
    )
    client = SemgrepClient(cfg)
    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout='{"results": []}', stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        client.scan(tmp_path)
    assert "--metrics" in captured["cmd"]
    idx = captured["cmd"].index("--metrics")
    assert captured["cmd"][idx + 1] == "on"
