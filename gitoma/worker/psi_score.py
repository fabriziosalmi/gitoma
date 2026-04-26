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
    originals: dict[str, str] | None = None,
    cpg_index: Any = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Env-driven gate over Ψ. Returns ``(weakest_file, message,
    breakdown)`` when Ψ < threshold (or hard-min violated) AND
    feature is enabled, else ``None``.

    Dispatches based on env:
      * ``GITOMA_PSI_FULL=on`` → Ψ-full (Γ + Φ + ΔI + Ω). Requires
        ``cpg_index`` and ``originals`` for full signal; degrades
        to Ψ-lite shape when either is missing.
      * ``GITOMA_PSI_LITE=on`` → Ψ-lite (Γ + Ω) — original behavior.
      * Neither set → silent pass (returns ``None``, no overhead).

    The ``originals`` and ``cpg_index`` kwargs are optional so the
    Ψ-lite code path stays callable with the original 3-arg
    signature (back-compat with v0.4 callers).
    """
    if _is_full_enabled():
        return _evaluate_psi_full_gate(
            root, touched, fingerprint, originals, cpg_index,
        )
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


# ── Ψ-full v1 — composes CPG-lite signal with Ψ-lite ──────────────


# Calibration constants — see project_psi_full_calibration.md for
# the worked-example rationale. Re-tuning these requires re-running
# the walkthrough; do NOT vibe-tune.
DEFAULT_BETA = 0.5
DEFAULT_GAMMA = 0.3
DEFAULT_FULL_THRESHOLD = 1.0
DEFAULT_PHI_HARD_MIN = 0.20


def _is_full_enabled() -> bool:
    return (os.environ.get("GITOMA_PSI_FULL") or "").lower() in (
        "1", "on", "true", "yes",
    )


def _resolve_beta() -> float:
    raw = os.environ.get("GITOMA_PSI_BETA", "")
    try:
        v = float(raw) if raw else DEFAULT_BETA
    except ValueError:
        return DEFAULT_BETA
    return max(0.0, min(10.0, v))


def _resolve_gamma() -> float:
    raw = os.environ.get("GITOMA_PSI_GAMMA", "")
    try:
        v = float(raw) if raw else DEFAULT_GAMMA
    except ValueError:
        return DEFAULT_GAMMA
    return max(0.0, min(10.0, v))


def _resolve_full_threshold() -> float:
    raw = os.environ.get("GITOMA_PSI_FULL_THRESHOLD", "")
    try:
        v = float(raw) if raw else DEFAULT_FULL_THRESHOLD
    except ValueError:
        return DEFAULT_FULL_THRESHOLD
    # Combined Ψ-full range is roughly 0..2; clamp wider than lite.
    return max(0.0, min(5.0, v))


def _resolve_phi_hard_min() -> float:
    raw = os.environ.get("GITOMA_PSI_PHI_HARD_MIN", "")
    try:
        v = float(raw) if raw else DEFAULT_PHI_HARD_MIN
    except ValueError:
        return DEFAULT_PHI_HARD_MIN
    return max(0.0, min(1.0, v))


def _evaluate_psi_full_gate(
    root: Path,
    touched: list[str],
    fingerprint: dict[str, Any] | None,
    originals: dict[str, str] | None,
    cpg_index: Any,
) -> tuple[str, str, dict[str, Any]] | None:
    """Inner Ψ-full evaluator. Combines:
      * Γ + Ω from compute_psi_lite (per-file → min aggregation)
      * Φ from compute_phi (global, weighted mean across symbols)
      * ΔI from compute_delta_i (global, mean across files)

    The combined Ψ formula uses GLOBAL Γ + Ω (= worst per-file from
    lite) plus global Φ + ΔI. This keeps Ψ-lite's "worst file
    dominates" property for the strings it knows about, while
    letting Φ + ΔI add a structural lens.

    Hard-min: Φ < phi_hard_min triggers a fail INDEPENDENTLY of total
    Ψ — a patch with great Γ + low Ω but a 100-caller hub touch
    should never pass.
    """
    from gitoma.worker.psi_phi import compute_phi
    from gitoma.worker.psi_delta_i import compute_delta_i

    alpha = _resolve_alpha()
    beta = _resolve_beta()
    gamma_w = _resolve_gamma()
    lambda_ = _resolve_lambda()
    threshold = _resolve_full_threshold()
    phi_hard_min = _resolve_phi_hard_min()

    # Lite components (Γ + Ω). Reuse compute_psi_lite — it returns
    # per-file detail; we aggregate by taking the min Γ and max Ω
    # across the report (worst signal, conservative).
    _psi_lite, lite_breakdown = compute_psi_lite(
        root, touched, fingerprint, alpha, lambda_,
    )
    if not lite_breakdown:
        # Nothing readable to score → silent pass (matches Ψ-lite
        # behavior on empty / missing-files patches).
        return None
    per_file = lite_breakdown["per_file"]
    gammas = [d["gamma"] for d in per_file.values()]
    omegas = [d["omega"] for d in per_file.values()]
    # Worst-file aggregation: min Γ (least grounded), max Ω (sloppiest).
    gamma_agg = min(gammas) if gammas else 1.0
    omega_agg = max(omegas) if omegas else 0.0

    phi, phi_breakdown = compute_phi(touched, cpg_index)
    delta_i, di_breakdown = compute_delta_i(touched, originals, root)

    psi = (
        alpha * gamma_agg
        + beta * phi
        + gamma_w * delta_i
        - lambda_ * omega_agg
    )

    # Identify the weakest file for the failure message — same
    # convention as Ψ-lite.
    weakest_file = min(
        per_file, key=lambda k: per_file[k]["gamma"] - per_file[k]["omega"],
    ) if per_file else "<unknown>"

    breakdown = {
        "psi": psi,
        "alpha": alpha, "beta": beta, "gamma": gamma_w, "lambda": lambda_,
        "threshold": threshold, "phi_hard_min": phi_hard_min,
        "components": {
            "Gamma": gamma_agg,
            "Phi": phi,
            "DeltaI": delta_i,
            "Omega": omega_agg,
        },
        "phi_breakdown": phi_breakdown,
        "delta_i_breakdown": di_breakdown,
        "lite_breakdown": lite_breakdown,
        "weakest_file": weakest_file,
        "phi_hard_min_active": phi_breakdown.get("cpg_active", False),
    }

    # Hard-min on Φ — only enforced when CPG actually contributed
    # signal (Φ defaulting to 1.0 when CPG is off should NOT trigger
    # the hard-min).
    if (
        breakdown["phi_hard_min_active"]
        and phi < phi_hard_min
    ):
        msg = (
            f"Ψ-full Φ={phi:.2f} below hard-min {phi_hard_min:.2f} "
            f"(min per-symbol φ={phi_breakdown.get('min_phi', 1.0):.2f}). "
            f"The patch touches a load-bearing symbol with too many "
            f"cross-file callers. Either preserve the symbol's signature "
            f"and behavior, or update every caller in the same patch."
        )
        return (weakest_file, msg, breakdown)

    if psi >= threshold:
        return None

    msg = (
        f"Ψ-full score {psi:.2f} below threshold {threshold:.2f} "
        f"(Γ={gamma_agg:.2f}, Φ={phi:.2f}, ΔI={delta_i:.2f}, "
        f"Ω={omega_agg:.2f}). The combined structural + grounding "
        f"signal indicates a patch that is either weakly grounded, "
        f"hits hot symbols, restructures heavily, or carries slop. "
        f"Re-emit with smaller scope or stronger grounding."
    )
    return (weakest_file, msg, breakdown)
