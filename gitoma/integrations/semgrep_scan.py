"""Semgrep static-analysis client — gitoma's 4th deterministic leg.

Semgrep (`semgrep/semgrep`) is a multi-language static analyzer with
2000+ community rules covering security (SQL injection, command
injection, XSS, hardcoded secrets, dangerous deserialization, …)
and code smells (dead branches, unused vars, anti-patterns). It
runs as a CLI binary and emits structured JSON.

Why gitoma needs this
---------------------
The audit phase already surfaces lint / build / test / coverage
metrics. What was missing: a security signal that the planner can
act on. Semgrep findings are concrete (rule id + file:line + message)
and high-severity ERRORs are nearly always actionable. Injecting the
top-N into the planner prompt as PHASE 1.6 lets the planner emit
subtasks that target real vulnerabilities rather than generic
"improve security" boilerplate.

Design contract (mirrors the other 3 spider legs)
--------------------------------------------------
* **Silent fail-open**. If `semgrep` binary is missing OR the scan
  errors / times out, return ``[]``. Running gitoma without semgrep
  must always work.
* **Short-ish timeout**. Default 60s — semgrep is slower than the
  other legs (it actually parses code) but faster than an LLM call.
  Operator can override via ``SEMGREP_TIMEOUT_S``.
* **Bounded output**. Cap findings at ``max_findings`` (default 30)
  to protect prompt budget. Higher-severity findings come first.
* **Read-only**. Scan never modifies the repo. Safe to run in
  parallel with other PHASE-1 audits.

Rule profile
------------
Defaults to ``--config=auto`` which fetches the registry's
language-appropriate ruleset for the files in the repo. Operators
can pin a specific config via ``SEMGREP_CONFIG`` (e.g.
``p/security-audit`` or a local path).

Not exposed today
-----------------
* Custom rule authoring (operator concern, out of scope here)
* Autofix (semgrep's --autofix is unreliable; gitoma's worker is
  the safer mutation path)
* SARIF output (JSON is enough for prompt injection)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SemgrepConfig",
    "SemgrepFinding",
    "SemgrepClient",
    "render_findings_block",
]


# ── Severity ordering ─────────────────────────────────────────────


# Semgrep severity levels in priority order (highest first). Anything
# else falls to the bottom.
_SEVERITY_RANK = {
    "ERROR": 0,
    "WARNING": 1,
    "INFO": 2,
}


def _severity_key(sev: str) -> int:
    return _SEVERITY_RANK.get(sev.upper(), 99)


# ── Config ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SemgrepConfig:
    """Resolved semgrep invocation settings."""

    binary: str = "semgrep"        # absolute path or PATH-resolvable name
    # ``--config`` value. Default ``p/default`` instead of ``auto``
    # because ``--config auto`` REQUIRES ``--metrics on`` (semgrep
    # phones home to semgrep.dev to pick the language-appropriate
    # ruleset). When metrics are forced off (privacy posture below),
    # ``auto`` silently bails with no scan output. Operators who
    # want ``auto`` MUST also set SEMGREP_CONFIG=auto AND we'll
    # enable metrics for that single config (see _build_cmd below).
    config: str = "p/default"
    timeout_s: float = 60.0
    enabled: bool = True
    max_target_bytes: int = 0       # 0 = semgrep default

    @classmethod
    def from_env(cls) -> "SemgrepConfig":
        binary = (os.environ.get("SEMGREP_BIN") or "semgrep").strip()
        # If the binary is not on PATH, mark as disabled so calls
        # don't pay the subprocess startup cost on every run.
        resolved = shutil.which(binary)
        enabled = resolved is not None
        try:
            timeout_s = float(os.environ.get("SEMGREP_TIMEOUT_S") or "60")
        except ValueError:
            timeout_s = 60.0
        config = (os.environ.get("SEMGREP_CONFIG") or "p/default").strip() or "p/default"
        return cls(
            binary=resolved or binary,
            config=config,
            timeout_s=max(5.0, timeout_s),
            enabled=enabled,
        )


# ── Result type ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SemgrepFinding:
    """One semgrep result, normalised."""

    rule_id: str
    path: str
    line: int
    severity: str           # "ERROR" / "WARNING" / "INFO" / other
    message: str
    cwe: tuple[str, ...] = field(default_factory=tuple)


# ── Client (silent-fail-open) ─────────────────────────────────────


class SemgrepClient:
    """Subprocess wrapper. Construction is cheap; the binary is
    invoked lazily on `scan()`. Every method returns a benign default
    on failure — the gitoma pipeline never crashes because semgrep
    misbehaved."""

    def __init__(self, config: SemgrepConfig | None = None) -> None:
        self.config = config or SemgrepConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def scan(
        self,
        repo_root: str | Path,
        *,
        max_findings: int = 30,
    ) -> list[SemgrepFinding]:
        """Run semgrep on ``repo_root`` and return up to ``max_findings``
        results, sorted by severity (ERROR first) then by path/line.

        Returns ``[]`` when:
          * client disabled (binary not on PATH)
          * subprocess fails / times out
          * semgrep emits non-JSON output
          * the requested ``max_findings`` is <= 0
        """
        if not self.config.enabled:
            return []
        if max_findings <= 0:
            return []
        root = Path(repo_root)
        if not root.is_dir():
            return []
        # ``--config auto`` requires ``--metrics on`` (semgrep phones
        # home to semgrep.dev to choose language-appropriate rules).
        # Any static config (``p/default``, ``p/security-audit``, a
        # local file path) works fine with metrics off. Bench
        # 2026-04-29 EVE: building bench-supply-chain corpus surfaced
        # this — every prior gitoma run had been silently bailing
        # because the default ``auto`` + ``--metrics off`` combination
        # is incompatible. Discovery: 0 findings on EVERY clean repo
        # was the symptom; fix is to detect and adapt.
        metrics_mode = "on" if self.config.config == "auto" else "off"
        cmd = [
            self.config.binary,
            "scan",
            "--config", self.config.config,
            "--json",
            "--quiet",
            "--no-git-ignore",  # respect .gitignore via git, not semgrep's heuristic
            "--metrics", metrics_mode,
            str(root),
        ]
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
        # Exit code: 0 = no findings, 1 = findings present, 2+ = error.
        # Both 0 and 1 produce parseable JSON.
        if proc.returncode not in (0, 1):
            return []
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        results = data.get("results")
        if not isinstance(results, list):
            return []

        findings: list[SemgrepFinding] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
            start = r.get("start") if isinstance(r.get("start"), dict) else {}
            rule_id = str(r.get("check_id") or "")
            path = str(r.get("path") or "")
            if not rule_id or not path:
                continue
            line = 0
            try:
                line = int(start.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            severity = str(extra.get("severity") or "INFO").upper()
            message = str(extra.get("message") or "").strip()
            metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
            cwe_raw = metadata.get("cwe") or ()
            if isinstance(cwe_raw, str):
                cwe_tuple: tuple[str, ...] = (cwe_raw,)
            elif isinstance(cwe_raw, list):
                cwe_tuple = tuple(str(c) for c in cwe_raw)
            else:
                cwe_tuple = ()
            findings.append(SemgrepFinding(
                rule_id=rule_id, path=path, line=line,
                severity=severity, message=message, cwe=cwe_tuple,
            ))

        # Sort by severity (ERROR first), then by path:line for stability
        findings.sort(key=lambda f: (_severity_key(f.severity), f.path, f.line))
        return findings[:max_findings]


# ── Render for planner prompt ─────────────────────────────────────


# Hard ceiling on the rendered block. Aligned with the other PHASE
# context blocks (skeleton ~2KB, scaffold ~1.2KB, layer0 ~1KB).
DEFAULT_MAX_CHARS = 1500


def render_findings_block(
    findings: list[SemgrepFinding],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Compact prompt block grouping findings by severity. Empty
    string when ``findings`` is empty."""
    if not findings:
        return ""

    by_sev: dict[str, list[SemgrepFinding]] = {}
    for f in findings:
        by_sev.setdefault(f.severity.upper(), []).append(f)

    # Order: ERROR → WARNING → INFO → others
    sev_order = sorted(by_sev.keys(), key=_severity_key)

    lines: list[str] = []
    for sev in sev_order:
        bucket = by_sev[sev]
        lines.append(f"### {sev} ({len(bucket)})")
        for f in bucket:
            cwe_str = f" [{','.join(f.cwe)}]" if f.cwe else ""
            # Trim message at 100 chars to stay budget-friendly
            msg = f.message[:100]
            lines.append(f"- {f.path}:{f.line} `{f.rule_id}`{cwe_str} — {msg}")
        lines.append("")

    rendered = "\n".join(lines).rstrip()
    if len(rendered) <= max_chars:
        return rendered

    # Truncation: walk lines, append a "…(N more)" notice when budget hit
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
