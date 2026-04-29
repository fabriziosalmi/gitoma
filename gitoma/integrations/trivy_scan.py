"""Trivy supply-chain scanner — gitoma's 5th deterministic leg.

Trivy (`aquasecurity/trivy`) is a single-binary multi-purpose
scanner: dep-CVE detection across most package managers (pip, npm,
go modules, cargo, composer, gem, …), secrets detection, license
classification, and IaC misconfig checks (Dockerfile, K8s,
Terraform). One CLI, one JSON output, complementary signals to
semgrep (which covers in-code defects, not supply-chain).

Why gitoma needs this
---------------------
PHASE 1.6 (semgrep) tells the planner about ERROR-severity in-code
findings. What it doesn't surface:
  * Vulnerable dependencies (a `requests==2.20.0` with a known CVE)
  * Secrets that semgrep's auto-config doesn't catch
  * Dockerfile / K8s / Terraform misconfigurations
PHASE 1.8 closes those gaps. The planner emits "bump dep X to fix
CVE-Y" / "remove hardcoded credential in Z" subtasks instead of
generic "improve security" boilerplate.

Design contract (mirrors the other 4 spider legs)
--------------------------------------------------
* **Silent fail-open**. Binary missing OR scan fails → return [].
  gitoma never crashes because trivy misbehaved.
* **Bounded timeout**. Default 90s (a bit higher than semgrep's 60
  because trivy's first-run downloads its DB; cached on subsequent
  runs ~5s). Operator override via ``TRIVY_TIMEOUT_S``.
* **Bounded output**. Cap findings at ``max_findings`` per type
  (default 30) to protect prompt budget. Higher-severity first.
* **Read-only**. Scan never modifies the repo.

What we surface
---------------
Three finding types unified into one ``TrivyFinding`` shape:
  * vuln — dependency CVE
  * secret — leaked credential / API key / private key
  * misconfig — IaC misconfiguration

The `kind` field discriminates. Severity is normalised to
ERROR/WARNING/INFO matching semgrep so the prompt-render code can
share patterns. Trivy uses CRITICAL/HIGH/MEDIUM/LOW/UNKNOWN; we
collapse: CRITICAL/HIGH→ERROR, MEDIUM→WARNING, LOW/UNKNOWN→INFO.

Not exposed today
-----------------
* SBOM generation (operator concern, distinct workflow)
* Container image scanning (`trivy image …`) — gitoma operates on
  source repos, not built images; defer until needed
* IaC misconfig auto-fix — trivy can suggest fixes but emit-only
  for now
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "TrivyConfig",
    "TrivyFinding",
    "TrivyClient",
    "render_findings_block",
]


# ── Severity normalisation (Trivy → semgrep-compatible) ───────────


_TRIVY_SEVERITY_TO_NORM = {
    "CRITICAL": "ERROR",
    "HIGH": "ERROR",
    "MEDIUM": "WARNING",
    "LOW": "INFO",
    "UNKNOWN": "INFO",
}

_NORM_RANK = {
    "ERROR": 0,
    "WARNING": 1,
    "INFO": 2,
}


def _norm_severity(raw: str) -> str:
    return _TRIVY_SEVERITY_TO_NORM.get((raw or "").upper(), "INFO")


def _severity_key(sev: str) -> int:
    return _NORM_RANK.get(sev.upper(), 99)


# ── Config ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrivyConfig:
    """Resolved trivy invocation settings."""

    binary: str = "trivy"
    timeout_s: float = 90.0
    enabled: bool = True
    skip_dirs: tuple[str, ...] = ("node_modules", ".git", "vendor", "dist", "build")

    @classmethod
    def from_env(cls) -> "TrivyConfig":
        binary = (os.environ.get("TRIVY_BIN") or "trivy").strip()
        resolved = shutil.which(binary)
        enabled = resolved is not None
        try:
            timeout_s = float(os.environ.get("TRIVY_TIMEOUT_S") or "90")
        except ValueError:
            timeout_s = 90.0
        return cls(
            binary=resolved or binary,
            timeout_s=max(10.0, timeout_s),
            enabled=enabled,
        )


# ── Result type ───────────────────────────────────────────────────


@dataclass(frozen=True)
class TrivyFinding:
    """One trivy result, normalised across the 3 finding kinds."""

    kind: str               # "vuln" | "secret" | "misconfig"
    rule_id: str            # CVE-XXXX-YYYY / secret rule id / misconfig id
    target: str             # path or package context
    severity: str           # ERROR / WARNING / INFO (normalised)
    title: str              # short headline
    pkg_name: str = ""      # vulns only
    installed_version: str = ""  # vulns only
    fixed_version: str = ""      # vulns only — the planner can act on this
    line: int = 0           # secrets/misconfig only
    references: tuple[str, ...] = field(default_factory=tuple)


# ── Client (silent-fail-open) ─────────────────────────────────────


class TrivyClient:
    """Subprocess wrapper. Construction is cheap; trivy is invoked
    lazily on `scan()`. Every method returns a benign default on
    failure — silent-fail-open contract."""

    def __init__(self, config: TrivyConfig | None = None) -> None:
        self.config = config or TrivyConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def scan(
        self,
        repo_root: str | Path,
        *,
        max_findings: int = 30,
    ) -> list[TrivyFinding]:
        """Run trivy filesystem scan on ``repo_root``. Returns up to
        ``max_findings`` results sorted by severity (ERROR first)
        then by kind (vuln, secret, misconfig) then target.

        Returns ``[]`` when:
          * client disabled (binary not on PATH)
          * subprocess fails / times out
          * trivy emits non-JSON output
          * ``max_findings`` <= 0
          * repo_root invalid
        """
        if not self.config.enabled:
            return []
        if max_findings <= 0:
            return []
        root = Path(repo_root)
        if not root.is_dir():
            return []
        cmd = [
            self.config.binary,
            "fs",
            "--quiet",
            "--scanners", "vuln,secret,misconfig",
            "--format", "json",
            "--severity", "CRITICAL,HIGH,MEDIUM",  # skip LOW/UNKNOWN noise
            "--exit-code", "0",  # trivy returns 1 on findings; we read the JSON
        ]
        for d in self.config.skip_dirs:
            cmd.extend(["--skip-dirs", d])
        cmd.append(str(root))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_s,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        # Exit code: 0 always with --exit-code 0; defend anyway
        if proc.returncode not in (0, 1):
            return []
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        findings: list[TrivyFinding] = []
        results = data.get("Results")
        if not isinstance(results, list):
            return []

        for r in results:
            if not isinstance(r, dict):
                continue
            target = str(r.get("Target") or "")
            findings.extend(_parse_vulns(r, target))
            findings.extend(_parse_secrets(r, target))
            findings.extend(_parse_misconfigs(r, target))

        # Sort by severity (ERROR first), then kind, then target
        kind_order = {"vuln": 0, "secret": 1, "misconfig": 2}
        findings.sort(key=lambda f: (
            _severity_key(f.severity),
            kind_order.get(f.kind, 9),
            f.target, f.rule_id,
        ))
        return findings[:max_findings]


# ── Per-kind parsers (pure helpers) ───────────────────────────────


def _parse_vulns(result: dict, target: str) -> list[TrivyFinding]:
    out: list[TrivyFinding] = []
    vulns = result.get("Vulnerabilities") or []
    if not isinstance(vulns, list):
        return out
    for v in vulns:
        if not isinstance(v, dict):
            continue
        rule_id = str(v.get("VulnerabilityID") or "")
        if not rule_id:
            continue
        sev = _norm_severity(str(v.get("Severity") or ""))
        title = str(v.get("Title") or v.get("Description") or "")[:200]
        refs_raw = v.get("References") or ()
        refs = tuple(str(r) for r in refs_raw if isinstance(r, str))[:3]
        out.append(TrivyFinding(
            kind="vuln",
            rule_id=rule_id,
            target=target,
            severity=sev,
            title=title,
            pkg_name=str(v.get("PkgName") or ""),
            installed_version=str(v.get("InstalledVersion") or ""),
            fixed_version=str(v.get("FixedVersion") or ""),
            references=refs,
        ))
    return out


def _parse_secrets(result: dict, target: str) -> list[TrivyFinding]:
    out: list[TrivyFinding] = []
    secrets = result.get("Secrets") or []
    if not isinstance(secrets, list):
        return out
    for s in secrets:
        if not isinstance(s, dict):
            continue
        rule_id = str(s.get("RuleID") or s.get("Category") or "")
        if not rule_id:
            continue
        sev = _norm_severity(str(s.get("Severity") or "HIGH"))
        title = str(s.get("Title") or "")[:200]
        line = 0
        try:
            line = int(s.get("StartLine") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append(TrivyFinding(
            kind="secret",
            rule_id=rule_id,
            target=target,
            severity=sev,
            title=title,
            line=line,
        ))
    return out


def _parse_misconfigs(result: dict, target: str) -> list[TrivyFinding]:
    out: list[TrivyFinding] = []
    miscfg = result.get("Misconfigurations") or []
    if not isinstance(miscfg, list):
        return out
    for m in miscfg:
        if not isinstance(m, dict):
            continue
        rule_id = str(m.get("ID") or m.get("AVDID") or "")
        if not rule_id:
            continue
        sev = _norm_severity(str(m.get("Severity") or ""))
        title = str(m.get("Title") or m.get("Description") or "")[:200]
        line = 0
        cause = m.get("CauseMetadata") or {}
        if isinstance(cause, dict):
            try:
                line = int(cause.get("StartLine") or 0)
            except (TypeError, ValueError):
                line = 0
        out.append(TrivyFinding(
            kind="misconfig",
            rule_id=rule_id,
            target=target,
            severity=sev,
            title=title,
            line=line,
        ))
    return out


# ── Render for planner prompt ─────────────────────────────────────


# Hard ceiling on the rendered block. Aligned with PHASE 1.6
# semgrep block (~1.5KB) — supply-chain context shouldn't crowd
# out in-code findings.
DEFAULT_MAX_CHARS = 1500


def render_findings_block(
    findings: list[TrivyFinding],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Compact prompt block grouped by kind (vuln/secret/misconfig)
    then by severity. Empty string when ``findings`` is empty."""
    if not findings:
        return ""

    # Group by (kind, severity)
    by_kind: dict[str, dict[str, list[TrivyFinding]]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, {}).setdefault(f.severity.upper(), []).append(f)

    kind_titles = {
        "vuln": "DEPENDENCY VULNERABILITIES",
        "secret": "SECRETS DETECTED",
        "misconfig": "INFRASTRUCTURE MISCONFIGURATIONS",
    }

    lines: list[str] = []
    for kind in ("vuln", "secret", "misconfig"):
        if kind not in by_kind:
            continue
        lines.append(f"### {kind_titles[kind]}")
        sev_buckets = by_kind[kind]
        for sev in sorted(sev_buckets.keys(), key=_severity_key):
            for f in sev_buckets[sev]:
                line = _render_one(kind, f)
                lines.append(line)
        lines.append("")

    rendered = "\n".join(lines).rstrip()
    if len(rendered) <= max_chars:
        return rendered

    # Truncation: walk lines, append "…(N more)" notice when budget hit
    out_lines: list[str] = []
    used = 0
    shown = 0
    total = len(findings)
    for line in lines:
        if used + len(line) + 30 > max_chars:
            break
        out_lines.append(line)
        used += len(line) + 1
        if line.startswith("- "):
            shown += 1
    if shown < total:
        out_lines.append(f"…({total - shown} more)")
    return "\n".join(out_lines).rstrip()


def _render_one(kind: str, f: TrivyFinding) -> str:
    """Render one finding line. Format depends on kind because vulns
    have actionable bump-target version info that secrets/misconfigs
    don't."""
    if kind == "vuln":
        bump = f" → bump to {f.fixed_version}" if f.fixed_version else ""
        pkg = f"{f.pkg_name}@{f.installed_version}" if f.pkg_name else f.target
        return (
            f"- {pkg} ({f.severity}) `{f.rule_id}`{bump} — {f.title[:120]}"
        )
    # secret + misconfig share the path:line shape
    where = f"{f.target}:{f.line}" if f.line else f.target
    return f"- {where} ({f.severity}) `{f.rule_id}` — {f.title[:120]}"
