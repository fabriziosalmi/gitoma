"""Tests for evaluate_psi_gate dispatcher with Ψ-full active.

Covers the GITOMA_PSI_FULL=on path including dispatch precedence,
component combination, threshold gating, hard-min behavior, and
back-compat with Ψ-lite when CPG isn't loaded."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.worker.psi_score import (
    DEFAULT_BETA,
    DEFAULT_FULL_THRESHOLD,
    DEFAULT_GAMMA,
    DEFAULT_PHI_HARD_MIN,
    evaluate_psi_gate,
)


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


# ── Default constants — frozen calibration ───────────────────────


def test_default_calibration_constants_match_memory() -> None:
    """Calibration is documented in project_psi_full_calibration.md.
    If these change, re-run the worked-example walkthrough AND
    update the memory file."""
    assert DEFAULT_BETA == 0.5
    assert DEFAULT_GAMMA == 0.3
    assert DEFAULT_FULL_THRESHOLD == 1.0
    assert DEFAULT_PHI_HARD_MIN == 0.20


# ── Dispatch precedence ──────────────────────────────────────────


def test_disabled_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither LITE nor FULL → silent pass."""
    monkeypatch.delenv("GITOMA_PSI_LITE", raising=False)
    monkeypatch.delenv("GITOMA_PSI_FULL", raising=False)
    _populate(tmp_path, {"a.py": "def x(): pass\n"})
    assert evaluate_psi_gate(tmp_path, ["a.py"], None) is None


def test_full_takes_precedence_over_lite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both env vars are set, FULL wins. Lite-only callers get
    full behavior — opt-in to lite by NOT setting FULL."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    _populate(tmp_path, {"a.py": "def x(): pass\n"})
    # Build a real CPG so full has signal; pass empty originals so
    # ΔI is high (create action).
    idx = build_index(tmp_path)
    result = evaluate_psi_gate(
        tmp_path, ["a.py"], None,
        originals={}, cpg_index=idx,
    )
    # Single trivial public function with 0 callers → high Φ;
    # default content has high Γ for non-doc files (no foreign
    # framework refs), low Ω. Should pass.
    assert result is None


# ── Back-compat: Ψ-lite still works with 3-arg signature ─────────


def test_lite_still_works_with_three_arg_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    monkeypatch.delenv("GITOMA_PSI_FULL", raising=False)
    _populate(tmp_path, {"a.py": "def x(): pass\n"})
    # 3-arg call (back-compat with v0.4 Worker code).
    result = evaluate_psi_gate(tmp_path, ["a.py"], None)
    # Default behavior: no slop, decent grounding → no failure.
    assert result is None


# ── Ψ-full graceful degradation when CPG missing ─────────────────


def test_full_degrades_to_lite_shape_without_cpg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FULL on but cpg_index=None → Φ=1.0, ΔI=1.0 (since originals
    also missing) → Ψ-full reduces to Γ + 0.5 + 0.3 - Ω. With
    decent content it should still pass."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    _populate(tmp_path, {"a.py": "def x(): pass\n"})
    result = evaluate_psi_gate(
        tmp_path, ["a.py"], None,
        originals=None, cpg_index=None,
    )
    assert result is None


# ── Hard-min on Φ ────────────────────────────────────────────────


def test_phi_hard_min_blocks_hot_symbol_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build a synthetic hub: 100 callers of a single function.
    Touching that function should trigger phi_hard_min < 0.15
    block, regardless of total Ψ."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    callers_src = "\n".join(
        f"def caller{i}(): hub()" for i in range(100)
    )
    _populate(tmp_path, {
        "lib.py": "def hub(): pass\n",
        "callers.py": "from lib import hub\n" + callers_src,
    })
    idx = build_index(tmp_path)
    result = evaluate_psi_gate(
        tmp_path, ["lib.py"], None,
        originals={"lib.py": "def hub(): pass\n"},
        cpg_index=idx,
    )
    assert result is not None
    weakest, msg, breakdown = result
    assert "Φ=" in msg or "Phi" in str(breakdown)
    assert "hard-min" in msg
    assert breakdown["components"]["Phi"] < DEFAULT_PHI_HARD_MIN


def test_phi_hard_min_skipped_when_cpg_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CPG is silent (Φ defaults to 1.0), the hard-min must
    NOT trigger — otherwise every Ψ-full evaluation without CPG
    would always pass through that branch."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    monkeypatch.setenv("GITOMA_PSI_PHI_HARD_MIN", "0.99")  # very high
    _populate(tmp_path, {".prettierrc": '{"semi": false}\n'})
    result = evaluate_psi_gate(
        tmp_path, [".prettierrc"], None,
        originals={".prettierrc": ""}, cpg_index=None,
    )
    # Φ defaults to 1.0 (no CPG), hard-min not active → no fail
    # via that path.
    assert result is None or "hard-min" not in result[1]


# ── Combined Ψ-full failure modes ────────────────────────────────


def test_full_blocks_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construct a patch with poor Γ + medium Φ + medium ΔI + some Ω
    so combined Ψ < 0.7 threshold."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    # A doc file referring to a fictional framework — low Γ.
    # Long enough that omega's near-empty penalty doesn't fire.
    bad_doc = (
        "# Architecture\n\n"
        "This project uses NextJS + Tailwind + Redux Toolkit + "
        "Apollo Client + Storybook + Cypress.\n"
        "We deploy via Vercel and run preview environments on every "
        "pull request automatically.\n"
        "Authentication is handled via Auth0 with social providers.\n"
    )
    _populate(tmp_path, {"docs/arch.md": bad_doc})
    fingerprint = {
        "declared_frameworks": [],
        "manifest_files": ["pyproject.toml"],
        "npm_packages": [],
        "py_packages": [],
    }
    result = evaluate_psi_gate(
        tmp_path, ["docs/arch.md"], fingerprint,
        originals={}, cpg_index=None,
    )
    assert result is not None
    weakest, msg, breakdown = result
    assert breakdown["components"]["Gamma"] < 0.5
    assert "Ψ-full" in msg


def test_full_passes_for_clean_python_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A modest function-body change to a Python file with no hot
    callers and no slop → Ψ-full passes comfortably."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    original = "def helper(x):\n    return x + 1\n"
    after = "def helper(x):\n    return x * 2\n"
    _populate(tmp_path, {"a.py": after})
    idx = build_index(tmp_path)
    result = evaluate_psi_gate(
        tmp_path, ["a.py"], None,
        originals={"a.py": original}, cpg_index=idx,
    )
    assert result is None


# ── Worked-example walkthrough validation ────────────────────────


def test_calibration_example_a_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Example A from project_psi_full_calibration.md: a decent patch
    on a Python module passes with margin (Ψ ~ 1.14 in the
    walkthrough). Verify we're in the same ballpark."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    # Modest module with a few functions, modify one body.
    original = (
        "def a(): return 1\n"
        "def b(): return 2\n"
        "def c(): return 3\n"
    )
    after = (
        "def a(): return 100\n"
        "def b(): return 2\n"
        "def c(): return 3\n"
    )
    _populate(tmp_path, {"lib.py": after})
    idx = build_index(tmp_path)
    result = evaluate_psi_gate(
        tmp_path, ["lib.py"], None,
        originals={"lib.py": original}, cpg_index=idx,
    )
    # Should pass — single body change, no hot symbols, no slop.
    assert result is None


def test_calibration_example_c_blocked_by_combined_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Example C variant: a doc patch with Γ=0 (foreign frameworks)
    + Ω > 0 (slop). For doc files Φ defaults to 1.0 and ΔI to 1.0
    (no CPG signal), so Ψ = 0 + 0.5 + 0.3 - Ω = 0.8 - Ω. With
    Ω > 0, Ψ falls below the 1.0 threshold → BLOCK. This is the
    "lazy doc" failure mode — bad grounding alone won't fail with
    Ψ-lite at threshold 0.5, but Ψ-full's higher floor catches
    it via the combined score."""
    monkeypatch.setenv("GITOMA_PSI_FULL", "on")
    # Doc with foreign frameworks (Γ → 0) + literal `\n` slop (Ω > 0)
    bad_doc = (
        "# Architecture\n\n"
        "This stack is NextJS + Tailwind + Redux + Apollo + Vercel.\n"
        "Auth via Auth0. Tests via Cypress. Storybook for components.\n"
        "Some bug: log line was 'foo\\nbar' before normalisation.\n"
    )
    _populate(tmp_path, {"docs/arch.md": bad_doc})
    fingerprint = {
        "declared_frameworks": [],
        "manifest_files": ["pyproject.toml"],
        "npm_packages": [],
        "py_packages": [],
    }
    result = evaluate_psi_gate(
        tmp_path, ["docs/arch.md"], fingerprint,
        originals={}, cpg_index=None,
    )
    # Γ=0 + structural floor 0.8 - Ω → blocks at threshold 1.0
    # ONLY if Ω > 0. If Ω=0 we score 0.8 → blocks too. Either way
    # the combined gate trips.
    assert result is not None
    weakest, msg, breakdown = result
    assert "Ψ-full" in msg
    assert breakdown["components"]["Gamma"] < 0.3
