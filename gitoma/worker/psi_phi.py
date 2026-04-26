"""Ψ-full v1 — Φ component (caller-impact-weighted safety).

For each touched file that CPG-lite can index (`.py` / `.ts` /
`.tsx`), query the index for public defining symbols, fetch their
caller counts, and score:

    phi_per_symbol = 1 / (1 + log(1 + caller_count))

Higher = safer (few or no callers); lower = riskier (many callers).
The aggregate Φ is the weighted mean across touched symbols (each
file weighted by its symbol count).

Files outside the indexable set (config JSON, markdown, etc.)
contribute Φ=1.0 — they have no symbols at risk by definition; the
existing Ω (slop) signal is what catches their failure modes.

Pure function, no I/O beyond CPG queries. CPG failures (None index,
missing file, etc.) degrade gracefully to Φ=1.0 with a breakdown
note — Ψ-full reduces to Ψ-lite shape when CPG is silent.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["compute_phi", "DEFAULT_PHI_HARD_MIN"]


DEFAULT_PHI_HARD_MIN = 0.20
"""When Φ active (CPG produced at least one symbol score),
Φ < this floor causes a hard-fail regardless of total Ψ.
Maps to ~e^(1/0.20-1) ≈ 54-caller hub (the "load-bearing
module" tier). See project_psi_full_calibration.md for the
worked-example rationale + the corrected math (initial 0.15
draft was 290-caller territory — too rare to ever fire)."""


_INDEXABLE_EXTS = (".py", ".ts", ".tsx")


def compute_phi(
    touched_files: list[str],
    cpg_index: Any = None,
) -> tuple[float, dict[str, Any]]:
    """Returns ``(phi_score in [0,1], breakdown)``.

    Breakdown keys:
      * ``per_file``  — list of ``{"file", "phi", "symbols": [{"name",
        "callers", "phi"}, ...]}`` entries (only for indexable files
        that contributed signal).
      * ``files_neutral`` — count of touched files that contributed
        Φ=1.0 (config / non-indexable / no symbols / no CPG).
      * ``cpg_active``    — bool: did the CPG actually contribute?
        When False, Φ=1.0 and consumers should treat as "no signal".
      * ``min_phi``       — minimum per-symbol phi seen (used by
        hard-min check). 1.0 when no signal.
    """
    if not touched_files:
        return 1.0, {
            "per_file": [],
            "files_neutral": 0,
            "cpg_active": False,
            "min_phi": 1.0,
        }
    if cpg_index is None:
        return 1.0, {
            "per_file": [],
            "files_neutral": len(touched_files),
            "cpg_active": False,
            "min_phi": 1.0,
        }

    per_file: list[dict[str, Any]] = []
    neutral_count = 0
    weighted_sum = 0.0
    weight_sum = 0.0
    min_phi_seen = 1.0
    cpg_contributed = False

    for path in touched_files:
        if not any(path.endswith(ext) for ext in _INDEXABLE_EXTS):
            neutral_count += 1
            continue
        try:
            symbols = cpg_index.get_symbols_in_file(path)
        except Exception:  # noqa: BLE001 — defensive
            neutral_count += 1
            continue
        # Only public defining symbols contribute (mirrors the
        # blast_radius renderer's _RELEVANT_KINDS logic).
        from gitoma.cpg._base import SymbolKind
        relevant = [
            s for s in symbols
            if s.is_public and s.kind in (
                SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.METHOD,
                SymbolKind.ASSIGNMENT, SymbolKind.INTERFACE,
                SymbolKind.TYPE_ALIAS,
            )
        ]
        if not relevant:
            neutral_count += 1
            continue

        cpg_contributed = True
        sym_breakdown: list[dict[str, Any]] = []
        per_sym_scores: list[float] = []
        for sym in relevant:
            try:
                callers = cpg_index.callers_of(sym.id)
            except Exception:  # noqa: BLE001
                callers = []
            ncallers = len(callers)
            phi_s = 1.0 / (1.0 + math.log(1.0 + ncallers))
            per_sym_scores.append(phi_s)
            min_phi_seen = min(min_phi_seen, phi_s)
            sym_breakdown.append({
                "name": sym.name,
                "callers": ncallers,
                "phi": round(phi_s, 4),
            })
        file_phi = sum(per_sym_scores) / len(per_sym_scores)
        weight = len(per_sym_scores)
        weighted_sum += file_phi * weight
        weight_sum += weight
        per_file.append({
            "file": path,
            "phi": round(file_phi, 4),
            "symbols": sym_breakdown,
        })

    if weight_sum == 0:
        # No indexable file contributed any symbol → neutral.
        return 1.0, {
            "per_file": per_file,
            "files_neutral": neutral_count,
            "cpg_active": False,
            "min_phi": 1.0,
        }

    phi = weighted_sum / weight_sum
    return phi, {
        "per_file": per_file,
        "files_neutral": neutral_count,
        "cpg_active": cpg_contributed,
        "min_phi": round(min_phi_seen, 4),
    }
