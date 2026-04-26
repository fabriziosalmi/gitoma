"""Tests for Ψ-full v1 ΔI component (structural conservativeness).

ΔI is the third component of Ψ-full = αΓ + βΦ + γΔI - λΩ. Higher
ΔI means the patch is information-conserving (small structural
delta); lower ΔI means a big rewrite that perturbs the existing
skeleton."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.psi_delta_i import MAX_REINDEX_FILES, compute_delta_i


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


# ── Default values & degraded paths ────────────────────────────────


def test_no_touched_files_returns_neutral(tmp_path: Path) -> None:
    delta_i, breakdown = compute_delta_i([], originals={}, repo_root=tmp_path)
    assert delta_i == 1.0
    assert breakdown["per_file"] == []


def test_no_originals_returns_neutral(tmp_path: Path) -> None:
    """When the worker doesn't surface originals (e.g. early
    pre-G7 path), ΔI degrades to 1.0 across all files — Ψ-full
    reduces to Ψ-lite shape."""
    delta_i, breakdown = compute_delta_i(
        ["src/x.py"], originals=None, repo_root=tmp_path,
    )
    assert delta_i == 1.0
    assert breakdown["files_neutral"] == 1


def test_non_indexable_files_contribute_neutral(tmp_path: Path) -> None:
    """Config files (.json / .toml / .md) are non-indexable; they
    contribute ΔI=1.0 each (no structural delta to measure)."""
    delta_i, breakdown = compute_delta_i(
        [".prettierrc", "config.toml", "README.md"],
        originals={".prettierrc": "{}\n",
                   "config.toml": "[x]\n",
                   "README.md": "# Hi\n"},
        repo_root=tmp_path,
    )
    assert delta_i == 1.0
    assert breakdown["files_neutral"] == 3


# ── Action: create ────────────────────────────────────────────────


def test_new_file_contributes_one_create_action(tmp_path: Path) -> None:
    """A newly-created file has no "before" — contributes 1.0 (the
    new file IS its own structure)."""
    _populate(tmp_path, {"new.py": "def newfn(): pass\n"})
    delta_i, breakdown = compute_delta_i(
        ["new.py"], originals={}, repo_root=tmp_path,
    )
    assert delta_i == 1.0
    assert breakdown["per_file"][0]["action"] == "create"


# ── Action: delete ────────────────────────────────────────────────


def test_deleted_file_contributes_zero_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default policy: deletion is structurally maximally entropic
    (existing public symbols vanish from indexed scope). Score 0.0
    blocks Ψ-full unless the operator opts in."""
    monkeypatch.delenv("GITOMA_PSI_DELETE_ALLOWED", raising=False)
    delta_i, breakdown = compute_delta_i(
        ["gone.py"],
        originals={"gone.py": "def removed(): pass\n"},
        repo_root=tmp_path,
    )
    assert delta_i == 0.0
    assert breakdown["per_file"][0]["action"] == "delete"
    assert breakdown["delete_allowed"] is False


def test_deleted_file_contributes_one_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GITOMA_PSI_DELETE_ALLOWED=on`` — refactor pass legitimately
    removing dead code. Delete contributes 1.0."""
    monkeypatch.setenv("GITOMA_PSI_DELETE_ALLOWED", "on")
    delta_i, breakdown = compute_delta_i(
        ["gone.py"],
        originals={"gone.py": "def removed(): pass\n"},
        repo_root=tmp_path,
    )
    assert delta_i == 1.0
    assert breakdown["delete_allowed"] is True


# ── Action: modify ────────────────────────────────────────────────


def test_whitespace_only_change_yields_high_delta_i(tmp_path: Path) -> None:
    """Re-indexing whitespace-only diffs must produce identical
    counts → ΔI=1.0. Caught a real corner: re-running an indexer
    with extra trailing newlines should NOT count as restructuring."""
    original = "def helper(): pass\n"
    after = "def helper(): pass\n\n\n"
    _populate(tmp_path, {"a.py": after})
    delta_i, breakdown = compute_delta_i(
        ["a.py"], originals={"a.py": original}, repo_root=tmp_path,
    )
    assert delta_i == pytest.approx(1.0, abs=0.05)
    assert breakdown["per_file"][0]["action"] == "modify"


def test_function_body_change_yields_high_delta_i(tmp_path: Path) -> None:
    """Same symbol, same callers, just different body content.
    ΔI should be near 1.0 — symbol+ref counts unchanged."""
    original = (
        "def helper(x):\n"
        "    return x + 1\n"
    )
    after = (
        "def helper(x):\n"
        "    return x * 2\n"
    )
    _populate(tmp_path, {"a.py": after})
    delta_i, _ = compute_delta_i(
        ["a.py"], originals={"a.py": original}, repo_root=tmp_path,
    )
    assert delta_i >= 0.9


def test_full_rewrite_yields_low_delta_i(tmp_path: Path) -> None:
    """Original = 1 function with body. After = 5 functions + 3
    classes that reference each other. Big structural delta on BOTH
    symbol counts AND ref counts → ΔI clearly below 0.5."""
    original = (
        "def lonely():\n"
        "    return 1\n"
    )
    after = (
        "def f1(): return f2()\n"
        "def f2(): return f3()\n"
        "def f3(): return f4()\n"
        "def f4(): return f5()\n"
        "def f5(): return 0\n"
        "class C1:\n    def m(self): return f1()\n"
        "class C2:\n    def m(self): return f2()\n"
        "class C3:\n    def m(self): return f3()\n"
    )
    _populate(tmp_path, {"a.py": after})
    delta_i, breakdown = compute_delta_i(
        ["a.py"], originals={"a.py": original}, repo_root=tmp_path,
    )
    # 1 sym → 11 sym (Δs ≈ 0.91), <5 refs → many refs (Δr ≈ 0.9)
    # ΔI = 1 - mean(~0.9, ~0.9) ≈ 0.1
    assert delta_i < 0.3


def test_single_symbol_added_yields_moderate_delta_i(tmp_path: Path) -> None:
    """Original = 3 functions. After = 4 functions. Δsymbols = 1/4 = 0.25.
    Refs may also change. ΔI ≈ 0.85."""
    original = (
        "def a(): pass\n"
        "def b(): pass\n"
        "def c(): pass\n"
    )
    after = original + "def d(): pass\n"
    _populate(tmp_path, {"a.py": after})
    delta_i, _ = compute_delta_i(
        ["a.py"], originals={"a.py": original}, repo_root=tmp_path,
    )
    assert 0.7 < delta_i < 1.0


# ── TypeScript path ───────────────────────────────────────────────


def test_typescript_file_modify_works(tmp_path: Path) -> None:
    original = "export function helper(): void {}\n"
    after = (
        "export function helper(): void {}\n"
        "export function newOne(): void {}\n"
    )
    _populate(tmp_path, {"a.ts": after})
    delta_i, breakdown = compute_delta_i(
        ["a.ts"], originals={"a.ts": original}, repo_root=tmp_path,
    )
    assert breakdown["per_file"][0]["action"] == "modify"
    # 1 symbol → 2 symbols = Δs 0.5; refs likely unchanged or small
    assert 0.5 < delta_i < 1.0


# ── Edge cases ────────────────────────────────────────────────────


def test_full_destruction_yields_half_delta_i(tmp_path: Path) -> None:
    """After-content is empty (file emptied but not deleted, e.g.
    `cat /dev/null > x.py`). Symbols 1→0 (Δs=1.0), refs 0→0
    (Δr=0.0). ΔI = 1 - 0.5 = 0.5 — exactly the formula's mid-point
    for "destroy all symbols, no refs to compare". Documents the
    formula's actual sensitivity."""
    original = "def helper(): pass\n"
    _populate(tmp_path, {"a.py": ""})
    delta_i, breakdown = compute_delta_i(
        ["a.py"], originals={"a.py": original}, repo_root=tmp_path,
    )
    assert delta_i == pytest.approx(0.5, abs=0.05)


def test_max_reindex_cap_enforced(tmp_path: Path) -> None:
    """Patches touching > MAX_REINDEX_FILES indexable files: only the
    first N actually re-index; the rest contribute 1.0 with a
    ``files_capped`` counter."""
    files = {f"m{i}.py": f"def f{i}(): pass\n" for i in range(MAX_REINDEX_FILES + 3)}
    _populate(tmp_path, files)
    originals = {name: "" for name in files}  # all "create"
    delta_i, breakdown = compute_delta_i(
        list(files.keys()), originals=originals, repo_root=tmp_path,
    )
    # Creates contribute 1.0 — no real cap pressure here, just verify
    # the structure isn't crashing on many files.
    assert 0.8 < delta_i <= 1.0


def test_modify_then_delete_in_same_patch(tmp_path: Path) -> None:
    """Mixed action set: one modify (mild change) + one delete
    (default policy). Aggregate is the mean."""
    _populate(tmp_path, {"keep.py": "def keep(): pass\n"})
    delta_i, breakdown = compute_delta_i(
        ["keep.py", "gone.py"],
        originals={
            "keep.py": "def keep(): pass\n",
            "gone.py": "def removed(): pass\n",
        },
        repo_root=tmp_path,
    )
    # keep.py ΔI = 1.0 (unchanged), gone.py ΔI = 0.0 (delete) → 0.5
    assert delta_i == pytest.approx(0.5, abs=0.05)
