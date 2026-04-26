"""Tests for Ψ-lite scoring + gate.

Covers Γ (grounding), Ω (slop), aggregation, and the env-driven
gate. Headline test: contrasts a known-clean patch shape against
the verbatim hallucinated b2v PR #27 patch shape (literal `\n` in
bash blocks + invented framework references)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.psi_score import (
    DEFAULT_THRESHOLD,
    _gamma_score,
    _omega_score,
    compute_psi_lite,
    evaluate_psi_gate,
)


def _b2v_fp() -> dict:
    return {
        "manifest_files": ["Cargo.toml", "package.json"],
        "declared_deps": {
            "rust": ["clap", "serde", "tokio"],
            "npm": ["vitepress"],
            "python": [],
            "go": [],
        },
        "declared_frameworks": ["clap", "serde"],
    }


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


# ── Γ — grounding ────────────────────────────────────────────────────


def test_gamma_doc_fully_grounded() -> None:
    """All mentioned frameworks are in fingerprint → Γ = 1.0."""
    g = _gamma_score("README.md", "Built on Clap and Serde.", _b2v_fp())
    assert g == 1.0


def test_gamma_doc_no_mentions_returns_one() -> None:
    """No framework mentions to score → defaults to 1.0 (presume
    innocent — can't penalize what isn't claimed)."""
    g = _gamma_score("README.md", "# Title\nA tool for X.\n", _b2v_fp())
    assert g == 1.0


def test_gamma_doc_fully_hallucinated() -> None:
    """Mentions React + Redux in a Rust-only repo → Γ = 0.0."""
    g = _gamma_score("README.md", "Frontend uses React with Redux.", _b2v_fp())
    assert g == 0.0


def test_gamma_doc_mixed() -> None:
    """One grounded + one hallucinated → Γ = 0.5."""
    g = _gamma_score("README.md", "Built on Clap. Also uses React.", _b2v_fp())
    assert g == pytest.approx(0.5, abs=0.01)


def test_gamma_config_grounded() -> None:
    """JS config importing vitepress (declared) → Γ = 1.0."""
    g = _gamma_score("vite.config.js", "import 'vitepress'", _b2v_fp())
    assert g == 1.0


def test_gamma_config_ungrounded_plugin() -> None:
    """prettier config referencing undeclared plugin → Γ = 0.0."""
    g = _gamma_score(
        "prettier.config.js",
        "module.exports = { plugins: ['undeclared-plugin'] };",
        _b2v_fp(),
    )
    assert g == 0.0


def test_gamma_source_neutral() -> None:
    """Source files have no Γ signal in Ψ-lite (CPG-lite would
    add it). Always returns 1.0."""
    g = _gamma_score("src/main.rs", "fn main() { /* anything */ }", _b2v_fp())
    assert g == 1.0


def test_gamma_no_fingerprint_neutral() -> None:
    """Occam disabled → Γ = 1.0 (Ψ-lite goes neutral, doesn't
    block runs without the grounding signal)."""
    g = _gamma_score("README.md", "Frontend uses React.", None)
    assert g == 1.0


def test_gamma_no_manifests_neutral() -> None:
    """Greenfield repo (fingerprint exists but no manifests
    detected) → Γ = 1.0."""
    fp_empty = {
        "manifest_files": [],
        "declared_deps": {},
        "declared_frameworks": [],
    }
    g = _gamma_score("README.md", "Frontend uses React.", fp_empty)
    assert g == 1.0


# ── Ω — slop ─────────────────────────────────────────────────────────


def test_omega_clean_content_zero() -> None:
    """Plain clean content → Ω = 0."""
    o = _omega_score("README.md", "# Title\n\nSome prose.\n")
    assert o == 0.0


def test_omega_literal_newline_in_code_block() -> None:
    """Literal `\\n` inside a fenced bash block → Ω penalty."""
    body = "```bash\nfoo \\n bar \\n baz\n```\n"
    o = _omega_score("README.md", body)
    assert o > 0.0
    # Three `\n` text occurrences = 0.3 contribution from this pattern,
    # capped at 0.5 per code block.
    assert o == pytest.approx(0.2, abs=0.01)


def test_omega_triple_blank_lines() -> None:
    """Triple+ blank lines = visual slop."""
    o = _omega_score("x.md", "a\n\n\n\nb\n")
    assert o > 0.0


def test_omega_source_wrapped_in_markdown_fence() -> None:
    """A .py file whose entire content is a markdown code fence —
    classic 'model returned wrong format' signature → Ω heavy."""
    body = "```python\ndef f(): pass\n```"
    o = _omega_score("src/main.py", body)
    assert o >= 0.6


def test_omega_near_empty_source_file() -> None:
    """A source file that's essentially empty post-modify → Ω hit."""
    o = _omega_score("src/main.py", "x")
    assert o >= 0.4


def test_omega_clamped_at_one() -> None:
    """Multiple slop patterns combined cannot exceed 1.0."""
    body = "```python\n```"  # source-wrapped + near-empty
    o = _omega_score("src/main.py", body)
    assert 0.0 <= o <= 1.0


# ── compute_psi_lite — aggregation ──────────────────────────────────


def test_psi_clean_patch_high(tmp_path: Path) -> None:
    """A fully-grounded, slop-free doc patch should score Ψ ≈ 1.0."""
    rel = _write(tmp_path, "README.md", "# Title\nBuilt on Clap.\n")
    psi, br = compute_psi_lite(tmp_path, [rel], _b2v_fp())
    assert psi >= 0.9
    assert br["psi"] == psi
    assert br["weakest_file"] == rel


def test_psi_hallucinated_patch_low(tmp_path: Path) -> None:
    """The b2v PR #27 shape: literal `\\n` corruption + framework
    hallucination. Ψ should be well below threshold."""
    body = "Frontend uses React.\n```bash\nfoo \\n bar \\n baz\n```\n"
    rel = _write(tmp_path, "README.md", body)
    psi, br = compute_psi_lite(tmp_path, [rel], _b2v_fp())
    assert psi < 0.5
    assert br["per_file"][rel]["gamma"] == 0.0
    assert br["per_file"][rel]["omega"] > 0.0


def test_psi_min_aggregation(tmp_path: Path) -> None:
    """Multiple files: the WORST Ψ wins. One clean + one bad →
    aggregate = bad."""
    a = _write(tmp_path, "good.md", "Uses Clap.")
    b = _write(tmp_path, "bad.md", "Uses React + Redux + Vue.")
    psi, br = compute_psi_lite(tmp_path, [a, b], _b2v_fp())
    assert br["weakest_file"] == "bad.md"
    assert psi == br["psi_per_file"]["bad.md"]


def test_psi_empty_touched_returns_one(tmp_path: Path) -> None:
    psi, br = compute_psi_lite(tmp_path, [], _b2v_fp())
    assert psi == 1.0
    assert br == {}


def test_psi_no_existing_files_returns_one(tmp_path: Path) -> None:
    """Touched paths with no on-disk files (deleted in prior subtask)
    — silent pass."""
    psi, br = compute_psi_lite(tmp_path, ["gone.md"], _b2v_fp())
    assert psi == 1.0


# ── evaluate_psi_gate — env-driven gate ──────────────────────────────


def test_gate_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without GITOMA_PSI_LITE env, gate is off → always None."""
    monkeypatch.delenv("GITOMA_PSI_LITE", raising=False)
    body = "Frontend uses React + Redux + Vue + Angular.\n```bash\nx \\n y \\n z\n```"
    rel = _write(tmp_path, "README.md", body)
    assert evaluate_psi_gate(tmp_path, [rel], _b2v_fp()) is None


def test_gate_enabled_blocks_low_psi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With GITOMA_PSI_LITE=on, low-Ψ patch returns the block tuple."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    body = "Frontend uses React.\n```bash\nfoo \\n bar \\n baz\n```\n"
    rel = _write(tmp_path, "README.md", body)
    result = evaluate_psi_gate(tmp_path, [rel], _b2v_fp())
    assert result is not None
    weakest, msg, breakdown = result
    assert weakest == "README.md"
    assert "Ψ-lite score" in msg
    assert "below threshold" in msg
    assert breakdown["psi"] < DEFAULT_THRESHOLD


def test_gate_enabled_passes_clean_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """High-Ψ patch passes the gate cleanly."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    rel = _write(tmp_path, "README.md", "# Title\nBuilt on Clap.\n")
    assert evaluate_psi_gate(tmp_path, [rel], _b2v_fp()) is None


def test_gate_threshold_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting GITOMA_PSI_LITE_THRESHOLD raises the bar — borderline
    patches that would pass at 0.5 fail at 0.9."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    monkeypatch.setenv("GITOMA_PSI_LITE_THRESHOLD", "0.9")
    # Half-grounded patch: Γ=0.5, Ω=0 → Ψ=0.5
    rel = _write(tmp_path, "README.md", "Built on Clap. Also uses React.")
    result = evaluate_psi_gate(tmp_path, [rel], _b2v_fp())
    # At threshold 0.9, this 0.5 should fail
    assert result is not None


def test_gate_alpha_lambda_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tuning weights changes scoring without code changes."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    monkeypatch.setenv("GITOMA_PSI_LAMBDA", "5.0")  # punish slop hard
    body = "Built on Clap.\n```bash\nfoo \\n bar \\n baz\n```\n"  # Γ=1, Ω=0.2
    rel = _write(tmp_path, "README.md", body)
    # With λ=5 and Ω≈0.2 → Ψ ≈ 1 - 5*0.2 = 0.0, below 0.5
    result = evaluate_psi_gate(tmp_path, [rel], _b2v_fp())
    assert result is not None


def test_gate_invalid_threshold_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage env var → default threshold preserved."""
    monkeypatch.setenv("GITOMA_PSI_LITE", "on")
    monkeypatch.setenv("GITOMA_PSI_LITE_THRESHOLD", "not-a-number")
    rel = _write(tmp_path, "README.md", "Built on Clap.")
    # Default threshold 0.5; clean patch → no block
    assert evaluate_psi_gate(tmp_path, [rel], _b2v_fp()) is None
