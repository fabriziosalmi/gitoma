"""Ψ-lite — universal fitness function (Γ + Ω components only).

The "lite" subset of the horizon Ψ-score (memory:
``project_idea_universal_fitness_function``). Pure-math, no LLM,
no network. Composes two signals into a single 0-1 quality score
per patch:

  * **Γ (gamma) — grounding score**: fraction of patch's added
    "evidence tokens" (framework names in docs, package refs in
    JS configs) that are GROUNDED in the repo's fingerprint
    (declared deps + frameworks). 1.0 when no evidence to score
    (e.g. pure-source-code patch with no fingerprint signal).
    Reuses G11/G12 extraction logic; ψ-lite is the SCALAR layer
    above those binary guards.

  * **Ω (omega) — slop penalty**: heuristic count of known-bad
    surface patterns in the new content (literal ``\\n`` in
    code blocks, triple-blank lines, fence-wrapped non-doc files,
    near-empty file). Normalised 0-1; higher = more slop.

Final score: ``Ψ = α·Γ - λ·Ω``. Aggregated across touched files
as ``min(Ψ_per_file)`` — the WORST file dominates patch quality.

Architecturally Ψ-lite slots BETWEEN the structural guards
(G2/G7/G10/G11/G12/G13/G14, BuildAnalyzer, G8) and the LLM
critics (panel, devil, Q&A). On low Ψ → revert+retry without
burning critic LLM tokens on a low-quality patch. Saves cost on
borderline patches that survive structural guards but are
clearly meh.

Opt-in via ``GITOMA_PSI_LITE=on`` (default off). Threshold via
``GITOMA_PSI_LITE_THRESHOLD`` (default 0.5, clamped 0.0-1.0).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Reuse the framework-mention map from G11 — Ψ-lite Γ counts the
# SAME tokens but assigns a continuous score instead of a
# binary flag. This is intentional: same evidence base, finer
# verdict.
from gitoma.worker.content_grounding import (
    DOC_EXTENSIONS as _CG_DOC_EXTENSIONS,
    DOC_FRAMEWORK_PATTERNS as _CG_PATTERNS,
)
from gitoma.worker.config_grounding import (
    CONFIG_FILE_BASENAMES as _CFG_BASENAMES,
    NODE_BUILTINS as _CFG_BUILTINS,
    _extract_package_refs as _cfg_extract_refs,
    _normalise_package as _cfg_normalise,
)

__all__ = [
    "compute_psi_lite",
    "evaluate_psi_gate",
    "DEFAULT_ALPHA",
    "DEFAULT_LAMBDA",
    "DEFAULT_THRESHOLD",
]


DEFAULT_ALPHA: float = 1.0
DEFAULT_LAMBDA: float = 1.0
DEFAULT_THRESHOLD: float = 0.5


# ── Slop pattern regexes ────────────────────────────────────────────

# Pattern A: Literal `\n` (backslash-n text) inside fenced code blocks.
# G13 enforces this with a hard threshold of 2+ on a line; Ω counts
# every occurrence and normalises (so even one `\n` literal contributes
# a small penalty).
_FENCE_BLOCK_RE = re.compile(r"```[\w-]*\n(.*?)\n```", re.S)

# Pattern B: 3+ consecutive blank lines (visual slop, often produced
# by LLM patch generation).
_TRIPLE_BLANK_RE = re.compile(r"\n\n\n\n+")

# Pattern C: trailing whitespace on a line (sloppy formatting).
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")

# Pattern D: source-code file (not doc/config) whose entire content
# is wrapped in a markdown fence — the "model returned the file as
# a code block instead of as a file" failure mode.
_SOURCE_LIKE_EXTS = frozenset({
    ".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".java", ".kt", ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
})


def _gamma_doc(content: str, declared: set[str], all_deps: set[str]) -> float:
    """Γ for a doc file: fraction of framework mentions that ground.
    Returns 1.0 when no framework mentions found (no evidence to
    judge, presume innocent)."""
    grounded = 0
    total = 0
    for pattern, fw_id in _CG_PATTERNS.items():
        if not re.search(pattern, content, flags=re.IGNORECASE):
            continue
        total += 1
        fw_lower = fw_id.lower()
        if (
            fw_lower in declared
            or fw_lower in all_deps
            or any(fw_lower in d for d in all_deps)
        ):
            grounded += 1
    if total == 0:
        return 1.0
    return grounded / total


def _gamma_config(content: str, npm_deps: set[str]) -> float:
    """Γ for a JS/TS config file: fraction of package refs that
    appear in npm deps. Returns 1.0 when no refs found OR when
    no npm deps declared (can't ground without evidence)."""
    refs = _cfg_extract_refs(content)
    grounded = 0
    total = 0
    for ref in refs:
        pkg = _cfg_normalise(ref)
        if pkg is None:
            continue
        pkg_lower = pkg.lower()
        if pkg_lower in _CFG_BUILTINS:
            continue
        total += 1
        if pkg_lower in npm_deps:
            grounded += 1
    if total == 0 or not npm_deps:
        return 1.0
    return grounded / total


def _gamma_score(rel: str, content: str, fingerprint: dict[str, Any] | None) -> float:
    """Per-file Γ. Routes to doc / config scorer based on path.
    Source files return 1.0 (Ψ-lite has no source-grounding signal
    until CPG-lite ships)."""
    if not fingerprint or not fingerprint.get("manifest_files"):
        return 1.0
    suffix = Path(rel).suffix.lower()
    if suffix in _CG_DOC_EXTENSIONS:
        declared = {
            str(x).lower() for x in (fingerprint.get("declared_frameworks") or [])
        }
        all_deps: set[str] = set()
        for lang_deps in (fingerprint.get("declared_deps") or {}).values():
            for d in lang_deps or []:
                all_deps.add(str(d).lower())
        return _gamma_doc(content, declared, all_deps)
    if Path(rel).name in _CFG_BASENAMES:
        npm_deps = {
            d.lower()
            for d in (fingerprint.get("declared_deps") or {}).get("npm") or []
        }
        return _gamma_config(content, npm_deps)
    # Source files / unknown — neutral.
    return 1.0


def _omega_score(rel: str, content: str) -> float:
    """Per-file Ω, normalised 0-1. Higher = more slop. Each pattern
    contributes a hit count divided by a tolerance; sum is clamped
    to 1.0 so no single pattern can saturate the score."""
    hits_score = 0.0

    # A. Literal \n inside fenced blocks — each occurrence = 0.1.
    for body in _FENCE_BLOCK_RE.findall(content):
        for line in body.split("\n"):
            hits_score += min(0.5, line.count("\\n") * 0.1)

    # B. Triple+ blank lines — each occurrence = 0.05.
    triples = len(_TRIPLE_BLANK_RE.findall(content))
    hits_score += min(0.3, triples * 0.05)

    # C. Trailing whitespace — fraction of lines, capped at 0.2.
    trailing = len(_TRAILING_SPACE_RE.findall(content))
    line_count = max(1, content.count("\n"))
    hits_score += min(0.2, (trailing / line_count))

    # D. Source-code file wrapped in markdown fence — 0.6 hit
    #    (very strong signal of "model returned wrong format").
    if Path(rel).suffix.lower() in _SOURCE_LIKE_EXTS:
        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            hits_score += 0.6

    # E. Near-empty post-modify (worker emitted ~nothing) — 0.4 hit
    #    when content < 5 chars and file isn't ~empty by intent.
    if len(content.strip()) < 5 and Path(rel).suffix.lower() in _SOURCE_LIKE_EXTS:
        hits_score += 0.4

    return min(1.0, hits_score)


def _resolve_alpha() -> float:
    raw = os.environ.get("GITOMA_PSI_ALPHA", "")
    try:
        v = float(raw) if raw else DEFAULT_ALPHA
    except ValueError:
        return DEFAULT_ALPHA
    return max(0.0, min(10.0, v))


def _resolve_lambda() -> float:
    raw = os.environ.get("GITOMA_PSI_LAMBDA", "")
    try:
        v = float(raw) if raw else DEFAULT_LAMBDA
    except ValueError:
        return DEFAULT_LAMBDA
    return max(0.0, min(10.0, v))


def _resolve_threshold() -> float:
    raw = os.environ.get("GITOMA_PSI_LITE_THRESHOLD", "")
    try:
        v = float(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD
    return max(0.0, min(1.0, v))


def _is_enabled() -> bool:
    return (os.environ.get("GITOMA_PSI_LITE") or "").lower() in ("1", "on", "true", "yes")


def compute_psi_lite(
    root: Path,
    touched: list[str],
    fingerprint: dict[str, Any] | None,
    alpha: float | None = None,
    lambda_: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Compute Ψ-lite across touched files. Returns ``(psi, breakdown)``
    where ``breakdown`` carries per-file Γ/Ω detail for telemetry.

    Aggregation: ``min`` across files (worst file dominates). Empty
    or all-skipped touched list returns ``(1.0, {})``.

    Pure function — does not consult env vars; caller passes alpha/
    lambda explicitly or accepts defaults. ``evaluate_psi_gate``
    wraps this with env-var resolution + threshold check.
    """
    a = alpha if alpha is not None else DEFAULT_ALPHA
    l = lambda_ if lambda_ is not None else DEFAULT_LAMBDA
    if not touched:
        return 1.0, {}
    per_file: dict[str, dict[str, float]] = {}
    for rel in touched:
        full = root / rel
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        gamma = _gamma_score(rel, content, fingerprint)
        omega = _omega_score(rel, content)
        per_file[rel] = {"gamma": gamma, "omega": omega}
    if not per_file:
        return 1.0, {}
    psi_per_file = {
        rel: a * d["gamma"] - l * d["omega"]
        for rel, d in per_file.items()
    }
    psi = min(psi_per_file.values())
    return psi, {
        "per_file": per_file,
        "psi_per_file": psi_per_file,
        "alpha": a,
        "lambda": l,
        "psi": psi,
        "weakest_file": min(psi_per_file, key=psi_per_file.get),
    }


def evaluate_psi_gate(
    root: Path,
    touched: list[str],
    fingerprint: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Env-driven gate over ``compute_psi_lite``. Returns
    ``(weakest_file, message, breakdown)`` when Ψ < threshold AND
    feature is enabled, else ``None``.

    Opt-in via ``GITOMA_PSI_LITE=on``. When disabled, always
    returns ``None`` (silent pass — no overhead beyond the env
    var read).
    """
    if not _is_enabled():
        return None
    alpha = _resolve_alpha()
    lambda_ = _resolve_lambda()
    threshold = _resolve_threshold()
    psi, breakdown = compute_psi_lite(root, touched, fingerprint, alpha, lambda_)
    if not breakdown:
        return None
    if psi >= threshold:
        return None
    weakest = breakdown["weakest_file"]
    weak_detail = breakdown["per_file"][weakest]
    msg = (
        f"Ψ-lite score {psi:.2f} below threshold {threshold:.2f} "
        f"(weakest file {weakest!r}: Γ={weak_detail['gamma']:.2f}, "
        f"Ω={weak_detail['omega']:.2f}). The patch passed structural "
        f"guards but scores low on grounding/slop heuristics. "
        f"Re-emit with content that grounds against fingerprint deps "
        f"and avoids known slop patterns (literal '\\n' in code blocks, "
        f"triple-blank lines, source files wrapped in markdown fences)."
    )
    return (weakest, msg, breakdown)
