"""G21 — semgrep-regression critic.

After a worker patch is applied, scan the touched files with semgrep
and compare against the pre-run baseline. Any (rule_id, path) tuple
that wasn't in the baseline is a NEW finding introduced by THIS
patch. ERROR-severity new findings cause the critic to fail (revert
+ retry); WARNING / INFO are tolerated.

Composes with PHASE 1.6 — that phase ALREADY scans the whole repo
to inject findings into the planner prompt, so the baseline
fingerprint set is computed once at PHASE 1.6 time and threaded
through to the worker via the existing constructor channel
(``WorkerAgent(... semgrep_baseline=...)``). When PHASE 1.6 is
disabled OR the binary is missing, baseline is ``None`` and G21
silently skips — no extra semgrep cost.

Why fingerprint = (rule_id, path) instead of (rule_id, path, line):
patches shift line numbers within a file. Tracking line would
false-positive on every line drift after an inserted comment.
Tracking only (rule_id, path) is coarse but robust: a NEW
(rule_id, path) tuple in a touched file means semgrep found
something that genuinely wasn't there before. The trade-off:
two findings of the SAME rule on the SAME file are folded —
acceptable because the LLM feedback message lists the actual
finding details so the worker can still target the right line.

Why ERROR-only by default: the community ruleset's WARNING/INFO
findings include style nits that aren't worth blocking patches
over. Operators who want stricter behavior can flip
``GITOMA_G21_SEVERITY=warning`` to also block WARNING-severity
new findings.

Opt-in via ``GITOMA_G21_SEMGREP=1``. Default OFF because the
extra subprocess invocations per patch attempt cost ~5-30s on a
non-trivial repo. Operators who want regression gating turn it on
for the runs that need it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from gitoma.integrations.semgrep_scan import SemgrepClient, SemgrepFinding

__all__ = [
    "G21Conflict",
    "G21Result",
    "compute_baseline_fingerprints",
    "is_g21_enabled",
    "g21_severity_floor",
    "check_g21_semgrep_regression",
]


# ── Env opt-in helpers ────────────────────────────────────────────


def is_g21_enabled() -> bool:
    """G21 default = OFF. Operator opt-in via ``GITOMA_G21_SEMGREP=1``."""
    return (os.environ.get("GITOMA_G21_SEMGREP") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_SEVERITY_FLOOR_MAP = {"error": 0, "warning": 1, "info": 2}


def g21_severity_floor() -> int:
    """Return the minimum severity rank that triggers a G21 fail.
    0 = ERROR (default), 1 = WARNING, 2 = INFO. Configured via
    ``GITOMA_G21_SEVERITY=warning|info|error``."""
    raw = (os.environ.get("GITOMA_G21_SEVERITY") or "error").strip().lower()
    return _SEVERITY_FLOOR_MAP.get(raw, 0)


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class G21Conflict:
    """One newly-introduced semgrep finding."""

    rule_id: str
    path: str
    line: int
    severity: str
    message: str
    cwe: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class G21Result:
    """Returned only when at least one new finding crosses the
    severity floor. Worker treats a non-None as ``revert + retry``."""

    conflicts: tuple[G21Conflict, ...]

    def render_for_llm(self) -> str:
        """Format conflicts as actionable LLM feedback. Mirrors the
        shape of G16/G18/G19's ``render_for_llm()`` output."""
        if not self.conflicts:
            return ""
        lines = [
            f"G21 SEMGREP REGRESSION — your patch introduced "
            f"{len(self.conflicts)} new finding(s) that did not exist "
            f"in the pre-patch baseline:",
            "",
        ]
        for c in self.conflicts:
            cwe_str = f" [{','.join(c.cwe)}]" if c.cwe else ""
            lines.append(
                f"  - {c.path}:{c.line} `{c.rule_id}` ({c.severity}){cwe_str}"
            )
            lines.append(f"    {c.message[:160]}")
        lines.extend([
            "",
            "Each entry above is a real, locatable defect introduced by "
            "the patch. Revise the patch to remove them OR justify why "
            "they are acceptable (and the operator will decide).",
        ])
        return "\n".join(lines)


# ── Baseline + diff ───────────────────────────────────────────────


def compute_baseline_fingerprints(
    findings: Iterable[SemgrepFinding],
    *,
    severity_floor: int = 0,
) -> set[tuple[str, str]]:
    """Reduce a list of findings to a set of ``(rule_id, path)`` tuples,
    keeping only those whose severity is AT OR ABOVE the floor.
    Severity ranks: ERROR=0, WARNING=1, INFO=2 (lower = more severe).

    The returned set is the baseline of pre-existing issues that the
    worker should NOT be blamed for. Anything outside the floor is
    excluded — the worker's regression gate matches its own floor."""
    out: set[tuple[str, str]] = set()
    for f in findings:
        sev = (f.severity or "").upper()
        rank = _SEVERITY_FLOOR_MAP.get(sev.lower(), 99)
        if rank > severity_floor:
            continue
        if not f.rule_id or not f.path:
            continue
        out.add((f.rule_id, f.path))
    return out


def check_g21_semgrep_regression(
    repo_root: str | Path,
    touched: Iterable[str],
    baseline_fingerprints: set[tuple[str, str]] | None,
    *,
    client: SemgrepClient | None = None,
) -> G21Result | None:
    """Scan touched files post-patch; return G21Result iff one or
    more new findings (= not in baseline) exist at or above the
    configured severity floor.

    Returns ``None`` when:
      * G21 not enabled
      * baseline is None (PHASE 1.6 didn't run / binary missing)
      * no touched files / repo_root invalid
      * scan returns empty / errors
      * no new findings cross the severity floor

    The post-patch scan runs against the SAME ruleset as the
    baseline (semgrep config picked up from the env), so rule_id
    sets are comparable.
    """
    if not is_g21_enabled():
        return None
    if baseline_fingerprints is None:
        return None

    touched_set = {str(p) for p in touched if p}
    if not touched_set:
        return None

    root = Path(repo_root)
    if not root.is_dir():
        return None

    sg = client or SemgrepClient()
    if not sg.enabled:
        return None

    floor = g21_severity_floor()

    # Scan the WHOLE repo (semgrep doesn't have an efficient
    # "scan only these N files" mode that respects --config=auto's
    # language detection), then filter to touched files. Cost is
    # the same as PHASE 1.6's baseline scan; cached results would
    # be unsound (we're scanning post-patch state, not pre).
    findings = sg.scan(root, max_findings=200)
    if not findings:
        return None

    conflicts: list[G21Conflict] = []
    for f in findings:
        sev_rank = _SEVERITY_FLOOR_MAP.get((f.severity or "").lower(), 99)
        if sev_rank > floor:
            continue
        if f.path not in touched_set:
            continue
        if (f.rule_id, f.path) in baseline_fingerprints:
            continue
        conflicts.append(G21Conflict(
            rule_id=f.rule_id,
            path=f.path,
            line=f.line,
            severity=f.severity,
            message=f.message,
            cwe=f.cwe,
        ))

    if not conflicts:
        return None
    return G21Result(conflicts=tuple(conflicts))
