"""Ψ-full v1 — bench artifact generator.

Re-runs the three worked-example scenarios from
``project_psi_full_calibration.md`` against the actual code and
dumps the verdict + component breakdown into ``replay_results.txt``.

This is a deterministic walkthrough (no LLM, no network), so the
output is committed alongside this script — future maintainers can
diff after retuning to spot calibration drift immediately.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


OUTPUT_PATH = Path(__file__).parent / "replay_results.txt"


def main() -> None:
    os.environ["GITOMA_PSI_FULL"] = "on"
    from gitoma.cpg import build_index
    from gitoma.worker.psi_score import evaluate_psi_gate

    lines: list[str] = []
    lines.append("Ψ-full v1 — calibration walkthrough replay")
    lines.append("=" * 64)
    lines.append("")
    lines.append("Defaults: α=1.0, β=0.5, γ=0.3, λ=1.0,")
    lines.append("          threshold=1.0, phi_hard_min=0.20")
    lines.append("")
    lines.append("=" * 64)

    # ── Example A — decent patch on a Python module ─────────
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
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
        (tdp / "lib.py").write_text(after)
        idx = build_index(tdp)
        result = evaluate_psi_gate(
            tdp, ["lib.py"], None,
            originals={"lib.py": original}, cpg_index=idx,
        )
        lines.append("")
        lines.append("### Example A — modify one function body")
        lines.append("Expected from walkthrough: PASS (Ψ ~ 1.14)")
        if result is None:
            lines.append("Actual: PASS (Ψ ≥ 1.0 threshold; gate returned None)")
        else:
            _, msg, br = result
            lines.append(f"Actual: BLOCK")
            lines.append(f"  Ψ = {br['psi']:.3f}")
            lines.append(f"  Components: {br['components']}")
            lines.append(f"  Message: {msg[:200]}")
        lines.append("")
        lines.append("-" * 64)

    # ── Example B — lazy patch (PR #32-class shape) ────────
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # 41 callers of LLMClient → Φ ≈ 0.21
        callers_src = "\n".join(
            f"def caller{i}(): LLMClient()" for i in range(40)
        )
        (tdp / "client.py").write_text("class LLMClient: pass\n")
        (tdp / "callers.py").write_text(
            "from client import LLMClient\n" + callers_src
        )
        idx = build_index(tdp)
        # The "lazy" modification: rename the class (signature change
        # without updating callers — exactly the failure mode).
        original = "class LLMClient: pass\n"
        after = (
            "class LLMClient:\n"
            "    def __init__(self):\n"
            "        self.model = 'gpt-4'\n"
        )
        (tdp / "client.py").write_text(after)
        idx = build_index(tdp)  # rebuild for ΔI before+after
        result = evaluate_psi_gate(
            tdp, ["client.py"], None,
            originals={"client.py": original}, cpg_index=idx,
        )
        lines.append("")
        lines.append("### Example B — touch a hub class (~40 callers)")
        lines.append("Expected: Φ ≈ 0.21, Ψ-full sneaks above 1.0?")
        if result is None:
            lines.append("Actual: PASS (Φ above hard-min; Ψ ≥ 1.0)")
        else:
            _, msg, br = result
            lines.append("Actual: BLOCK")
            lines.append(f"  Ψ = {br['psi']:.3f}")
            lines.append(f"  Components: {br['components']}")
            lines.append(f"  Message: {msg[:200]}")
        lines.append("")
        lines.append("-" * 64)

    # ── Example C — bad-grounding doc (combined gate) ──────
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bad_doc = (
            "# Architecture\n\n"
            "This stack is NextJS + Tailwind + Redux + Apollo + Vercel.\n"
            "Auth via Auth0. Tests via Cypress. Storybook for components.\n"
        )
        (tdp / "docs").mkdir()
        (tdp / "docs" / "arch.md").write_text(bad_doc)
        fingerprint = {
            "declared_frameworks": [],
            "manifest_files": ["pyproject.toml"],
            "npm_packages": [],
            "py_packages": [],
        }
        result = evaluate_psi_gate(
            tdp, ["docs/arch.md"], fingerprint,
            originals={}, cpg_index=None,
        )
        lines.append("")
        lines.append("### Example C — doc with foreign frameworks (Γ→0)")
        lines.append("Expected: BLOCK (Γ=0, structural floor 0.8 < 1.0)")
        if result is None:
            lines.append("Actual: PASS")
        else:
            _, msg, br = result
            lines.append("Actual: BLOCK")
            lines.append(f"  Ψ = {br['psi']:.3f}")
            lines.append(f"  Components: {br['components']}")
            lines.append(f"  Message: {msg[:200]}")
        lines.append("")
        lines.append("-" * 64)

    # ── Example D — hub touch over 100 callers (hard-min) ──
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        callers_src = "\n".join(f"def cc{i}(): hub()" for i in range(100))
        (tdp / "lib.py").write_text("def hub(): pass\n")
        (tdp / "callers.py").write_text(
            "from lib import hub\n" + callers_src
        )
        idx = build_index(tdp)
        result = evaluate_psi_gate(
            tdp, ["lib.py"], None,
            originals={"lib.py": "def hub(): pass\n"}, cpg_index=idx,
        )
        lines.append("")
        lines.append("### Example D — touch 100-caller hub (hard-min path)")
        lines.append("Expected: BLOCK via Φ < 0.20 hard-min")
        if result is None:
            lines.append("Actual: PASS (hard-min did NOT trigger)")
        else:
            _, msg, br = result
            lines.append("Actual: BLOCK")
            lines.append(f"  Φ = {br['components']['Phi']:.3f}")
            lines.append(f"  hard-min trigger: {'hard-min' in msg}")
            lines.append(f"  Message: {msg[:200]}")
        lines.append("")
        lines.append("-" * 64)

    OUTPUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} lines to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
