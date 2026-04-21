"""Dataclasses for the critic panel — kept narrow on purpose.

The Finding shape mirrors the schema used in
``tests/fixtures/slop_audit_b2v_pr10.json`` so a future regression test can
match LLM output against the golden audit without a second translation
layer. If you change a field here, update the fixture comment too.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# Severity ladder — keep in sync with the fixture and with whatever the
# cockpit eventually renders. Order matters (worst first) for sorting.
Severity = Literal["blocker", "major", "minor", "nit"]


@dataclass
class Finding:
    """One item flagged by one persona on one patch.

    ``persona`` is the source persona name (``"dev"``, ``"arch"``, …).
    ``severity`` follows the fixed ladder above.
    ``category`` is a short slug like ``"broken_configuration"`` or
    ``"redundant_duplicate"`` — taxonomy is informal for now; if it stabilises
    we can promote it to an Enum later.
    ``file`` / ``line_range`` are best-effort; LLMs frequently miss precise
    lines, so we accept ``None`` rather than fabricate.
    ``summary`` is the human sentence that ends up in the trace + log.
    """
    persona: str
    severity: Severity
    category: str
    summary: str
    file: str | None = None
    line_range: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tuple → list for JSON friendliness
        if self.line_range is not None:
            d["line_range"] = list(self.line_range)
        return d


@dataclass
class PanelResult:
    """The full output of one panel run on one subtask diff.

    ``verdict`` is the orchestration outcome (not a per-finding severity):
      * ``no_op``           — panel didn't run (mode=off, or empty diff)
      * ``advisory_logged`` — panel ran, findings logged, commit proceeds
      * ``refined_accepted`` — refinement happened and meta-eval kept it
      * ``refined_rejected`` — refinement happened but original was kept
                               (used for the "devil's_advocate_ignored" log)
    Iteration 1 only ever returns ``no_op`` or ``advisory_logged`` — the
    refinement paths land in iteration 2/3.
    """
    subtask_id: str
    verdict: Literal["no_op", "advisory_logged", "refined_accepted", "refined_rejected"]
    personas_called: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Token accounting — populated when the LLMClient surfaces usage.
    # Tuple of (prompt_tokens, completion_tokens). Set to None if usage
    # wasn't reported by the backend (some self-hosted deploys don't).
    tokens_extra: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "verdict": self.verdict,
            "personas_called": list(self.personas_called),
            "findings": [f.to_dict() for f in self.findings],
            "tokens_extra": (
                {"prompt": self.tokens_extra[0], "completion": self.tokens_extra[1]}
                if self.tokens_extra is not None
                else None
            ),
        }

    def has_blocker(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)
