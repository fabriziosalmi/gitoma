"""G22 — trivy supply-chain regression critic.

Mirrors G21 but for trivy findings: after a worker patch is applied,
re-scan the repo with trivy and compare against the pre-run baseline.
Any (rule_id, target) tuple that wasn't in the baseline AND falls
within scope is a NEW finding introduced by THIS patch. ERROR-severity
new findings cause the critic to fail (revert + retry).

Composes with PHASE 1.8 — that phase ALREADY scans the whole repo
to inject findings into the planner prompt; the baseline fingerprint
set is computed once and threaded through to the worker via the
``WorkerAgent(... trivy_baseline=...)`` constructor channel.

Why fingerprint = (rule_id, target):
  * For vulns: ``rule_id`` = CVE id (uniquely identifies the vuln);
    ``target`` = manifest file (Pipfile.lock, package-lock.json,
    etc). A new (CVE, manifest) pair means the patch introduced a
    vulnerable dep version into THAT manifest.
  * For secrets: ``rule_id`` = secret rule id (aws-access-key-id,
    github-pat, …); ``target`` = file containing the secret. A new
    (rule_id, file) pair means the patch leaked a credential into
    that file (line drift means line-level fingerprint is fragile).
  * For misconfigs: ``rule_id`` = misconfig id (DS001, AVD-DS-...);
    ``target`` = IaC file (Dockerfile, *.tf, *.yaml). A new pair
    means the patch introduced a misconfig in that file.

Same coarse-but-robust trade-off as G21 — line numbers shift, but
the (rule_id, target) tuple is stable across patches.

Scope filter: by default G22 scopes regression checks to ALL files
(not just touched ones), because patches that touch e.g. a Python
file can indirectly trigger a new finding in the manifest if the
import requires a new dep. This is the reverse of G21 where
file-scoped is sound (semgrep is intra-file). Operators can flip
``GITOMA_G22_TOUCHED_ONLY=1`` to scope to touched files for speed.

Opt-in via ``GITOMA_G22_TRIVY=1``. Default OFF because the extra
trivy invocation per patch attempt costs ~10-90s on a non-trivial
repo (trivy DB cache hits help — first run is slowest).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from gitoma.integrations.trivy_scan import TrivyClient, TrivyFinding

__all__ = [
    "G22Conflict",
    "G22Result",
    "compute_trivy_baseline_fingerprints",
    "is_g22_enabled",
    "g22_severity_floor",
    "g22_touched_only",
    "check_g22_trivy_regression",
]


# ── Env opt-in helpers ────────────────────────────────────────────


def is_g22_enabled() -> bool:
    """G22 default = OFF. Operator opt-in via ``GITOMA_G22_TRIVY=1``."""
    return (os.environ.get("GITOMA_G22_TRIVY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_SEVERITY_FLOOR_MAP = {"error": 0, "warning": 1, "info": 2}


def g22_severity_floor() -> int:
    """Return the minimum severity rank that triggers a G22 fail.
    0 = ERROR (default), 1 = WARNING, 2 = INFO. Configured via
    ``GITOMA_G22_SEVERITY=warning|info|error``."""
    raw = (os.environ.get("GITOMA_G22_SEVERITY") or "error").strip().lower()
    return _SEVERITY_FLOOR_MAP.get(raw, 0)


def g22_touched_only() -> bool:
    """When True, scope regression to ONLY the patch-touched files
    (faster but misses indirect-dep-add cases). Default False because
    a patch that adds an import line can pull in a new vulnerable
    dep — even though the patch didn't directly touch the manifest."""
    return (os.environ.get("GITOMA_G22_TOUCHED_ONLY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class G22Conflict:
    """One newly-introduced trivy finding."""

    kind: str               # "vuln" | "secret" | "misconfig"
    rule_id: str
    target: str
    severity: str
    title: str
    pkg_name: str = ""
    installed_version: str = ""
    fixed_version: str = ""
    line: int = 0


@dataclass(frozen=True)
class G22Result:
    """Returned only when at least one new finding crosses the
    severity floor. Worker treats a non-None as ``revert + retry``."""

    conflicts: tuple[G22Conflict, ...]

    def render_for_llm(self) -> str:
        """Format conflicts as actionable LLM feedback. Mirrors G21's
        shape but renders per-kind details (vulns get bump-target,
        secrets get path:line, misconfigs get path:line)."""
        if not self.conflicts:
            return ""
        lines = [
            f"G22 TRIVY REGRESSION — your patch introduced "
            f"{len(self.conflicts)} new supply-chain finding(s) "
            f"that did not exist in the pre-patch baseline:",
            "",
        ]
        for c in self.conflicts:
            lines.append(_render_conflict(c))
        lines.extend([
            "",
            "Vuln entries carry the fix-version — bump the dep to that "
            "version to clear them. Secrets must be removed or rotated. "
            "Misconfigs target specific lines in IaC files. Revise the "
            "patch to remove these or justify why they are acceptable.",
        ])
        return "\n".join(lines)


def _render_conflict(c: G22Conflict) -> str:
    """Per-kind render of one conflict."""
    if c.kind == "vuln":
        bump = f" → bump to {c.fixed_version}" if c.fixed_version else ""
        pkg = f"{c.pkg_name}@{c.installed_version}" if c.pkg_name else c.target
        return (
            f"  - {pkg} ({c.severity}) `{c.rule_id}`{bump} — {c.title[:160]}"
        )
    where = f"{c.target}:{c.line}" if c.line else c.target
    return f"  - {where} ({c.severity}) `{c.rule_id}` — {c.title[:160]}"


# ── Baseline + diff ───────────────────────────────────────────────


def compute_trivy_baseline_fingerprints(
    findings: Iterable[TrivyFinding],
    *,
    severity_floor: int = 0,
) -> set[tuple[str, str]]:
    """Reduce a list of trivy findings to a set of ``(rule_id, target)``
    tuples, keeping only those whose severity is AT OR ABOVE the floor.
    Severity ranks: ERROR=0, WARNING=1, INFO=2 (lower = more severe).

    Same shape as compute_baseline_fingerprints in semgrep_regression.py
    so the worker can use both critics interchangeably."""
    out: set[tuple[str, str]] = set()
    for f in findings:
        sev = (f.severity or "").upper()
        rank = _SEVERITY_FLOOR_MAP.get(sev.lower(), 99)
        if rank > severity_floor:
            continue
        if not f.rule_id or not f.target:
            continue
        out.add((f.rule_id, f.target))
    return out


def check_g22_trivy_regression(
    repo_root: str | Path,
    touched: Iterable[str],
    baseline_fingerprints: set[tuple[str, str]] | None,
    *,
    client: TrivyClient | None = None,
) -> G22Result | None:
    """Scan post-patch with trivy; return G22Result iff one or more
    new findings (= not in baseline) exist at or above the configured
    severity floor.

    When ``g22_touched_only()`` is True, only count findings whose
    target IS in the touched-files set. Default False (fingerprint-
    only scope, see module docstring).

    Returns ``None`` when:
      * G22 not enabled
      * baseline is None (PHASE 1.8 didn't run / binary missing)
      * no touched files / repo_root invalid
      * scan returns empty / errors
      * no new findings cross the severity floor
    """
    if not is_g22_enabled():
        return None
    if baseline_fingerprints is None:
        return None

    touched_set = {str(p) for p in touched if p}
    if not touched_set:
        return None

    root = Path(repo_root)
    if not root.is_dir():
        return None

    tv = client or TrivyClient()
    if not tv.enabled:
        return None

    floor = g22_severity_floor()
    touched_only = g22_touched_only()

    # Scan the whole repo (trivy resolves manifests; can't easily
    # scope to N files without losing dep-tree context). Cap higher
    # than PHASE 1.8's prompt cap so we catch all new findings.
    findings = tv.scan(root, max_findings=200)
    if not findings:
        return None

    conflicts: list[G22Conflict] = []
    for f in findings:
        sev_rank = _SEVERITY_FLOOR_MAP.get((f.severity or "").lower(), 99)
        if sev_rank > floor:
            continue
        if (f.rule_id, f.target) in baseline_fingerprints:
            continue
        if touched_only and f.target not in touched_set:
            continue
        conflicts.append(G22Conflict(
            kind=f.kind,
            rule_id=f.rule_id,
            target=f.target,
            severity=f.severity,
            title=f.title,
            pkg_name=f.pkg_name,
            installed_version=f.installed_version,
            fixed_version=f.fixed_version,
            line=f.line,
        ))

    if not conflicts:
        return None
    return G22Result(conflicts=tuple(conflicts))
