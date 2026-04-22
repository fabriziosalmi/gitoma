"""Dataclasses + strict Pydantic models for the critic panel.

Two-layer split (intentional, see Ω_Agent ¬I axiom — "no untyped
LLM output"):

  * **Dataclasses** (``Finding``, ``PanelResult``) — internal
    representation, persisted to state.json via ``asdict()``. Stable
    contract, backwards-compat-friendly.

  * **Pydantic models** (``_LLMFindingModel``, ``LLMPanelOutput``,
    ``LLMRefinerOutput``, ``LLMMetaVerdict``) — STRICT schema
    validation at the LLM-output boundary. Replaces the old
    best-effort "regex-extract a JSON block, dict.get with defaults"
    pattern that silently degraded on schema drift.

The boundary is one-way: parser receives raw LLM text → validates
with the Pydantic model → constructs the dataclass on success →
persists. On Pydantic validation failure, the parser returns empty
list / conservative-default (matches existing fail-soft behaviour);
the trace records the failure.

The Finding shape (dataclass) mirrors
``tests/fixtures/slop_audit_b2v_pr10.json``. If you change a field
here, update the fixture comment too.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    ``axiom`` is the iter-6 categorisation: ``¬M`` (anti-mutation), ``¬S``
    (anti-hope), ``¬A`` (anti-ambiguity), or ``¬O`` (anti-opacity). Optional
    on the dataclass so legacy code keeps working — the devil's prompt
    requires it, the panel personas may emit it, dashboard metrics
    aggregate per-axiom when present.
    """
    persona: str
    severity: Severity
    category: str
    summary: str
    file: str | None = None
    line_range: tuple[int, int] | None = None
    axiom: str | None = None

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
            # iter 6 metric layer: per-axiom finding count. Always
            # 4 keys (zero on absent) so downstream aggregation has a
            # stable shape across runs / repos.
            "axiom_profile": self.axiom_profile(),
        }

    def has_blocker(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)

    def axiom_profile(self) -> dict[str, int]:
        """Return the {¬M:n, ¬S:n, ¬A:n, ¬O:n} count of findings by axiom.

        Findings without an axiom tag (legacy / panel personas pre-iter-6)
        are NOT counted — the profile measures how many findings the
        upgraded categorisation captured, not the total."""
        profile: dict[str, int] = {"¬M": 0, "¬S": 0, "¬A": 0, "¬O": 0}
        for f in self.findings:
            if f.axiom in profile:
                profile[f.axiom] += 1
        return profile


# ── Pydantic strict models for the LLM-output boundary ──────────────────────
#
# These models validate raw LLM output before it crosses into our internal
# dataclasses. Strict mode (``extra="forbid"``) intentionally rejects
# unknown keys — if the model invents new fields, the trace gets the
# error and the parser returns empty/default. NEVER silently accept.
#
# Field validators normalise minor schema drift (case-insensitive enums,
# trimmed strings) without opening the gate to fully invalid output.


class _StrictModel(BaseModel):
    """Base for all LLM-boundary models. Strict by default."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Canonical axiom symbols. Kept here (not imported from axioms.py) so
# critic/types.py has no circular dependency on axioms.py — the latter
# imports Rule from antislop, and Rule is independent of types.
_AXIOM_SYMBOLS = frozenset({"¬M", "¬S", "¬A", "¬O"})
# Tolerant input synonyms — small models often emit ASCII variants of
# the unicode symbols. Map to canonical at validation time.
_AXIOM_SYNONYMS: dict[str, str] = {
    "¬m": "¬M", "¬s": "¬S", "¬a": "¬A", "¬o": "¬O",
    "!m": "¬M", "!s": "¬S", "!a": "¬A", "!o": "¬O",
    "not m": "¬M", "not s": "¬S", "not a": "¬A", "not o": "¬O",
    "anti_mutation": "¬M", "anti_hope": "¬S",
    "anti_ambiguity": "¬A", "anti_opacity": "¬O",
    "anti-mutation": "¬M", "anti-hope": "¬S",
    "anti-ambiguity": "¬A", "anti-opacity": "¬O",
    "m": "¬M", "s": "¬S", "a": "¬A", "o": "¬O",
}


class LLMFinding(_StrictModel):
    """One finding as emitted by the LLM (panel persona OR devil).

    ``axiom`` is the iter-6 categorisation. Optional for backwards
    compat with iter 1-5 panel personas that don't emit it; the devil
    prompt (iter 6) requires it. When present it must be one of
    {¬M, ¬S, ¬A, ¬O} — synonyms (ASCII, English names, single letters)
    are normalised to canonical at validation time.
    """
    severity: Severity
    category: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    file: str | None = None
    line_range: tuple[int, int] | None = None
    axiom: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, v: Any) -> Any:
        # Tolerant on case + small synonym drift (small models occasionally
        # emit "minor" as "low" or "high" instead of major). We map a small
        # closed set; anything else stays as-is and triggers the Literal
        # validator which rejects it cleanly.
        if not isinstance(v, str):
            return v
        v = v.strip().lower()
        return {"low": "minor", "high": "major", "critical": "blocker"}.get(v, v)

    @field_validator("axiom", mode="before")
    @classmethod
    def _normalise_axiom(cls, v: Any) -> Any:
        """Map common synonyms to the canonical ¬M/¬S/¬A/¬O symbols.
        Anything else (including unparseable input) stays None — the
        finding survives but is uncategorised in the dashboard."""
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        s = v.strip()
        if s in _AXIOM_SYMBOLS:
            return s
        return _AXIOM_SYNONYMS.get(s.lower())  # None if no match


class LLMPanelOutput(_StrictModel):
    """The full payload one persona returns: ``{"findings": [...]}``."""
    findings: list[LLMFinding] = Field(default_factory=list)


class LLMDevilOutput(_StrictModel):
    """The devil's advocate output. Same shape as a persona but allows
    an optional ``defense`` field for the "no findings, here's why"
    case spelt out in the prompt."""
    findings: list[LLMFinding] = Field(default_factory=list)
    defense: str | None = None


class LLMPatchAction(_StrictModel):
    """One file edit emitted by the refiner. Mirrors ``apply_patches``
    schema. ``content`` is allowed empty for ``delete`` actions."""
    action: Literal["create", "modify", "delete"]
    path: str = Field(min_length=1, max_length=500)
    content: str = ""

    @field_validator("path")
    @classmethod
    def _no_path_traversal(cls, v: str) -> str:
        # ``apply_patches`` enforces this too, but catching it at the
        # validation layer means the LLM output never reaches the
        # patcher in the first place — closes one ¬O surface.
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError(f"path must be relative and within the repo: {v!r}")
        return v


class LLMRefinerOutput(_StrictModel):
    """The refiner's structured response."""
    patches: list[LLMPatchAction] = Field(default_factory=list)
    commit_message: str = Field(default="", max_length=200)


class LLMMetaVerdict(_StrictModel):
    """The meta-eval's verdict between v0 and v1."""
    winner: Literal["v0", "v1", "tie"]
    rationale: str = Field(default="", max_length=300)

    @field_validator("winner", mode="before")
    @classmethod
    def _normalise_winner(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        return v.strip().lower()


__all__ = [
    "Finding",
    "LLMDevilOutput",
    "LLMFinding",
    "LLMMetaVerdict",
    "LLMPanelOutput",
    "LLMPatchAction",
    "LLMRefinerOutput",
    "PanelResult",
    "Severity",
    "ValidationError",
]
