"""Tests for Ψ-full v1 Φ component (caller-impact-weighted safety).

Φ is the second component of Ψ-full = αΓ + βΦ + γΔI - λΩ. Higher Φ
means safer (touched symbols have few callers); lower Φ means
riskier (touched a hub symbol)."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitoma.cpg import build_index
from gitoma.cpg._base import Symbol, SymbolKind
from gitoma.worker.psi_phi import DEFAULT_PHI_HARD_MIN, compute_phi


# ── Default values & degraded paths ────────────────────────────────


def test_default_phi_hard_min_constant() -> None:
    """The 0.20 hard-min was chosen against worked examples (see
    project_psi_full_calibration.md). Maps to ~54-caller hub. If a
    future change moves it, update both this constant + the
    calibration walkthrough."""
    assert DEFAULT_PHI_HARD_MIN == 0.20


def test_no_touched_files_returns_neutral_phi() -> None:
    phi, breakdown = compute_phi([], cpg_index=None)
    assert phi == 1.0
    assert breakdown["cpg_active"] is False


def test_no_cpg_index_returns_neutral_phi() -> None:
    """When CPG isn't loaded (env off), Φ degrades to 1.0 so
    Ψ-full reduces to Ψ-lite shape — back-compat guarantee."""
    phi, breakdown = compute_phi(["src/x.py"], cpg_index=None)
    assert phi == 1.0
    assert breakdown["cpg_active"] is False
    assert breakdown["files_neutral"] == 1


def test_non_indexable_files_contribute_neutral(tmp_path: Path) -> None:
    """Config files (.json / .toml / .md) aren't indexed; they
    should add Φ=1.0 each, not penalise."""
    (tmp_path / "lib.py").write_text("def helper(): pass\n")
    idx = build_index(tmp_path)
    phi, breakdown = compute_phi(
        [".prettierrc", "config.toml", "README.md"], cpg_index=idx,
    )
    assert phi == 1.0
    assert breakdown["files_neutral"] == 3
    assert breakdown["cpg_active"] is False


# ── Real CPG index, real symbols ───────────────────────────────────


def _build(tmp_path: Path, files: dict[str, str]):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return build_index(tmp_path)


def test_phi_high_for_zero_caller_function(tmp_path: Path) -> None:
    """A patch touching a function with no callers should score
    near 1.0 — touching dead code is structurally safe."""
    idx = _build(tmp_path, {"a.py": "def lonely(): pass\n"})
    phi, breakdown = compute_phi(["a.py"], cpg_index=idx)
    # Single symbol, 0 callers → 1 / (1 + log(1)) = 1.0
    assert phi == pytest.approx(1.0, abs=1e-6)
    assert breakdown["cpg_active"] is True


def test_phi_decreases_with_caller_count(tmp_path: Path) -> None:
    """A 5-caller symbol should score LOWER than a 1-caller one."""
    cold = _build(tmp_path, {
        "a.py": "def cold(): pass\n",
        "b.py": "from a import cold\ndef u(): cold()\n",
    })
    cold_phi, _ = compute_phi(["a.py"], cpg_index=cold)

    # New tmp build with more callers
    hot = _build(tmp_path / "hot", {
        "a.py": "def hot(): pass\n",
        "b.py": (
            "from a import hot\n"
            + "\n".join(f"def u{i}(): hot()" for i in range(10))
            + "\n"
        ),
    })
    hot_phi, _ = compute_phi(["a.py"], cpg_index=hot)
    assert hot_phi < cold_phi
    # And 10+1 callers (10 calls + 1 import_from) should be safely
    # below the hard-min when applied to a single hub symbol.
    # Concretely: 1/(1+log(12)) ≈ 0.286 — well above 0.15 floor.
    assert hot_phi == pytest.approx(1.0 / (1.0 + math.log(1 + 11)), abs=1e-3)


def test_phi_aggregates_weighted_by_symbol_count(tmp_path: Path) -> None:
    """File A: 1 cold function (Φ=1.0). File B: 1 hot function with
    many callers. Average should be SHIFTED toward the hot file's
    score in proportion to symbol counts. Each file has 1 symbol →
    plain mean."""
    idx = _build(tmp_path, {
        "cold.py": "def cold(): pass\n",
        "hot.py": "def hot(): pass\n",
        "callers.py": (
            "from hot import hot\n"
            + "\n".join(f"def c{i}(): hot()" for i in range(5))
            + "\n"
        ),
    })
    phi, breakdown = compute_phi(
        ["cold.py", "hot.py"], cpg_index=idx,
    )
    # cold has phi=1.0 (no callers), hot has phi < 1.0
    # Mean falls strictly between cold and hot.
    assert phi < 1.0
    assert phi > 0.5
    files = {pf["file"] for pf in breakdown["per_file"]}
    assert files == {"cold.py", "hot.py"}


def test_phi_returns_min_seen_for_hard_min_check(tmp_path: Path) -> None:
    """Aggregate Φ may be high but a SINGLE symbol could be terrible
    — ``min_phi`` in the breakdown surfaces that, used by the
    hard-min gate independently of the average."""
    idx = _build(tmp_path, {
        "lib.py": (
            "def cold1(): pass\n"
            "def cold2(): pass\n"
            "def hub(): pass\n"
        ),
        "callers.py": (
            "from lib import hub\n"
            + "\n".join(f"def c{i}(): hub()" for i in range(20))
            + "\n"
        ),
    })
    phi, breakdown = compute_phi(["lib.py"], cpg_index=idx)
    assert breakdown["min_phi"] < phi  # min < average
    assert breakdown["min_phi"] < 0.5  # hub is genuinely hot


# ── TypeScript path (v0.5-slim) ────────────────────────────────────


def test_phi_works_on_typescript_files(tmp_path: Path) -> None:
    """Φ must work for .ts/.tsx exactly like .py — that's the whole
    point of CPG-lite v0.5-slim."""
    idx = _build(tmp_path, {
        "lib.ts": "export function helper(): void {}\n",
        "main.ts": (
            "import { helper } from './lib';\n"
            "function caller() { helper(); }\n"
        ),
    })
    phi, breakdown = compute_phi(["lib.ts"], cpg_index=idx)
    assert breakdown["cpg_active"] is True
    assert phi < 1.0  # 1 caller from main.ts (CALL + IMPORT_FROM = 2)


def test_phi_recognises_ts_interfaces(tmp_path: Path) -> None:
    """Interfaces are first-class definitions — touching User
    (with 5 importers) should reduce Φ below 1.0."""
    idx = _build(tmp_path, {
        "types.ts": "export interface User { id: number; }\n",
        "a.ts": "import { User } from './types';\nexport const x: User = { id: 1 };\n",
        "b.ts": "import { User } from './types';\nexport const y: User = { id: 2 };\n",
    })
    phi, breakdown = compute_phi(["types.ts"], cpg_index=idx)
    assert breakdown["cpg_active"] is True
    assert phi < 1.0


# ── Mixed Py + TS ──────────────────────────────────────────────────


def test_phi_mixes_py_and_ts(tmp_path: Path) -> None:
    """A patch touching both a .py and a .ts file should score both
    files and weight by symbol count."""
    idx = _build(tmp_path, {
        "lib.py": "def py_helper(): pass\n",
        "lib.ts": "export function tsHelper(): void {}\n",
    })
    phi, breakdown = compute_phi(["lib.py", "lib.ts"], cpg_index=idx)
    assert phi == pytest.approx(1.0, abs=1e-6)  # both have 0 callers
    assert len(breakdown["per_file"]) == 2


# ── Defensive: missing files / private symbols ────────────────────


def test_phi_skips_private_underscore_symbols(tmp_path: Path) -> None:
    """``_internal`` is private (leading underscore); shouldn't
    contribute to Φ — internal symbols can be refactored freely."""
    idx = _build(tmp_path, {"lib.py": "def _internal(): pass\n"})
    phi, breakdown = compute_phi(["lib.py"], cpg_index=idx)
    # Only private symbols → contributes neutrally
    assert phi == 1.0
    assert breakdown["cpg_active"] is False


def test_phi_handles_cpg_query_exception(tmp_path: Path) -> None:
    """If the CPG query raises (e.g. corrupted index), file
    contributes Φ=1.0 with files_neutral incremented, no crash."""
    bad_idx = MagicMock()
    bad_idx.get_symbols_in_file.side_effect = RuntimeError("kaboom")
    phi, breakdown = compute_phi(["a.py"], cpg_index=bad_idx)
    assert phi == 1.0
    assert breakdown["files_neutral"] == 1


# ── Calibration sanity: worked example values ─────────────────────


def test_calibration_example_b_phi_for_lazy_pr_shape(tmp_path: Path) -> None:
    """From project_psi_full_calibration.md example B: a PR #32-
    class lazy patch hits a symbol with ~40 callers and gets Φ≈0.21.
    Build that scenario synthetically and verify."""
    callers_src = "\n".join(
        f"def caller{i}():\n    LLMClient()\n" for i in range(40)
    )
    idx = _build(tmp_path, {
        "client.py": "class LLMClient: pass\n",
        "callers.py": "from client import LLMClient\n" + callers_src,
    })
    phi, breakdown = compute_phi(["client.py"], cpg_index=idx)
    # 40 calls + 1 import_from = 41 callers; phi = 1/(1+log(42)) ≈ 0.211
    expected = 1.0 / (1.0 + math.log(1 + 41))
    assert phi == pytest.approx(expected, abs=0.05)
    # And the min_phi surfaces the same number (single symbol)
    assert breakdown["min_phi"] == pytest.approx(expected, abs=0.05)
    # CRITICAL: this must STILL be above the 0.15 hard-min, just
    # barely. If retuning, the calibration walkthrough must update.
    assert phi > DEFAULT_PHI_HARD_MIN
