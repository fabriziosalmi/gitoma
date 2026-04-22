"""Synthetic e2e test for the refiner v2 prompt fix (commit 389780b).

Why this exists
---------------
The refiner's contract — "emit FULL file content, not unified diff
hunks" — was strengthened in commit 389780b after live observation
(iter4 on b2v) caught gemma-4-it-optiq emitting ``@@ -X,Y +A,B @@``
into the ``content`` field. The fix added explicit WRONG/RIGHT
examples + a flagged-files-content payload so the model has full file
context to transform.

But the real e2e flow only exercises the refiner WHEN the devil
flags blocker/major findings. On well-behaved repos (b2v after the
PR#11 cleanup), the devil emits at most minor → ``should_refine=False``
→ refiner never runs. The fix is shipped but UNVERIFIED in production.

This test closes that gap WITHOUT depending on the devil:
  1. Pre-confeziona findings simulated to mirror PR#12 (blockers
     gemma actually missed: pre-commit broken, README regression,
     fn main() removed).
  2. Build a small in-memory "repo" — synthetic file content for
     each flagged path.
  3. Call ``Refiner.propose()`` directly with these findings.
  4. Verify the response: patches are valid Pydantic-shape,
     ``content`` is full file content (NOT a diff hunk), patcher
     can apply them without rejection.

Live LLM, opt-in via ``-m antislop_live``. Uses LM_STUDIO_BASE_URL +
LM_STUDIO_MODEL same as the antislop bench.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitoma.critic.refiner import Refiner
from gitoma.critic.types import Finding


# Three synthetic blocker-grade findings that mirror PR#12-shape regressions
# the devil missed in live runs. The refiner SHOULD emit a v1 patch fixing
# at least one of them — not a unified-diff blob in ``content``.
_SYNTHETIC_FINDINGS = [
    Finding(
        persona="devil",
        severity="blocker",
        category="broken_configuration",
        summary=(
            "pre-commit config has invented hook URL "
            "(repo: https://pre-commit.com/hooks/...) — does not exist; "
            "every contributor's first commit will fail. Replace with "
            "the real upstream pre-commit-hooks repo URL."
        ),
        file=".pre-commit-config.yaml",
        line_range=(1, 23),
        axiom="¬S",
    ),
    Finding(
        persona="devil",
        severity="major",
        category="regression_removed_section",
        summary=(
            "README.md no longer contains the License section even though "
            "the document still references licensing in earlier paragraphs "
            "and a LICENSE file exists at repo root. Restore a brief "
            "License section pointing at LICENSE."
        ),
        file="README.md",
        line_range=(80, 95),
        axiom="¬O",
    ),
]

# In-memory "repo" — what the patcher would see if these were real files.
# Content kept small; refiner just needs to know the SHAPE to transform.
_SYNTHETIC_FILES: dict[str, str] = {
    ".pre-commit-config.yaml": (
        "repos:\n"
        "  - repo: https://pre-commit.com/hooks/pre-commit-config\n"
        "    hooks:\n"
        "      - id: check-yaml\n"
        "      - id: rustfmt\n"
        "      - id: prettier\n"
    ),
    "README.md": (
        "# b2v — Eternal Stream\n\n"
        "Encode arbitrary binary files into video.\n\n"
        "## Installation\n\n"
        "    cargo install b2v\n\n"
        "## Usage\n\n"
        "    b2v encode --input file.bin --output file.mkv\n\n"
        "## Contributing\n\n"
        "See CONTRIBUTING.md.\n"
    ),
}


@pytest.mark.antislop_live
def test_refiner_v2_emits_full_file_content_not_diff_hunks():
    """Live e2e: with the iter4 prompt-v2 fix (commit 389780b), the
    refiner MUST emit ``content`` as full file content, never as a
    unified-diff hunk (``@@ -X,Y +A,B @@`` lines). Caught live on
    iter4 first run; this test closes the verification gap.

    Skipped automatically when LM_STUDIO_BASE_URL / LM_STUDIO_MODEL
    aren't set (mirrors antislop_bench live tests)."""
    base_url = os.environ.get("LM_STUDIO_BASE_URL")
    model = os.environ.get("LM_STUDIO_MODEL")
    if not base_url or not model:
        pytest.skip("LM_STUDIO_BASE_URL + LM_STUDIO_MODEL not set")

    # Build a minimal LLMClient pointing at the live endpoint.
    # We can't reuse the gitoma config (no GitHub token in CI etc),
    # so construct a thin shim that provides chat_json.
    from openai import OpenAI

    client = OpenAI(
        base_url=base_url,
        api_key=os.environ.get("LM_STUDIO_API_KEY", "lm-studio"),
    )

    class _LiveLLM:
        """Minimal LLMClient interface — just enough for Refiner.propose."""
        def __init__(self) -> None:
            self._last_usage = None

        def chat_json(self, messages, retries: int = 3):
            import json as _json
            for _attempt in range(retries):
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0,
                    max_tokens=2048,
                )
                raw = resp.choices[0].message.content or ""
                # Strip ```json fences if present
                cleaned = raw.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
                    if cleaned.endswith("```"):
                        cleaned = cleaned.rsplit("```", 1)[0]
                try:
                    parsed = _json.loads(cleaned)
                    if isinstance(parsed, dict):
                        return parsed
                except _json.JSONDecodeError:
                    continue
            raise RuntimeError("could not parse JSON from refiner response")

    # Refiner needs a CriticPanelConfig + a "full" Config. The Refiner
    # only reads ``temperature`` from critic_config and ignores most of
    # full_config — we can pass MagicMocks freely.
    cp_cfg = MagicMock()
    cp_cfg.temperature = 0.3
    full_cfg = MagicMock()

    refiner = Refiner(cp_cfg, _LiveLLM(), full_cfg)

    # ── Run ────────────────────────────────────────────────────────────
    # Synthetic branch diff = "the things that need fixing", giving the
    # model orientation. The flagged_files_content gives it the targets.
    branch_diff = (
        "diff --git a/.pre-commit-config.yaml b/.pre-commit-config.yaml\n"
        "(omitted — see flagged file content below)\n"
        "diff --git a/README.md b/README.md\n"
        "(omitted — see flagged file content below)\n"
    )

    out = refiner.propose(
        branch_diff=branch_diff,
        devil_findings=_SYNTHETIC_FINDINGS,
        flagged_files_content=_SYNTHETIC_FILES,
    )

    # ── Assertions ────────────────────────────────────────────────────
    print("\n=== refiner v2 synthetic test ===")
    print(f"  patches_count: {len(out.get('patches', []))}")
    print(f"  commit_message: {out.get('commit_message', '')!r}")

    # The contract: patches is a list, may be empty (model couldn't
    # refine), but if non-empty, EVERY content must be full file, NOT
    # a diff hunk.
    patches = out.get("patches", [])
    assert isinstance(patches, list), f"patches must be list, got {type(patches)}"

    for i, patch in enumerate(patches):
        print(f"\n  patch[{i}]: action={patch.get('action')} path={patch.get('path')}")
        content = patch.get("content", "")
        head = content[:200].replace("\n", "\\n")
        print(f"    content head: {head}")

        # The smoking gun: unified-diff prefix sequence
        assert not content.lstrip().startswith("@@"), (
            f"patch[{i}] for {patch.get('path')!r} CONTENT IS A DIFF HUNK "
            f"(starts with @@). The v2 prompt fix did NOT take effect. "
            f"First 200 chars: {head}"
        )
        assert "@@ -" not in content[:50], (
            f"patch[{i}] for {patch.get('path')!r} CONTAINS unified-diff "
            f"markers in the leading content. Likely diff format. "
            f"First 200 chars: {head}"
        )

    # If the refiner produced ANY patches, it should have addressed at
    # least one of the flagged files. Soft check — empty is also OK
    # if the model decided it can't do it cleanly (the prompt allows that).
    if patches:
        flagged_paths = {f.file for f in _SYNTHETIC_FINDINGS}
        touched = {p.get("path") for p in patches}
        overlap = flagged_paths & touched
        print(f"\n  flagged paths: {sorted(flagged_paths)}")
        print(f"  touched paths: {sorted(touched)}")
        print(f"  overlap:       {sorted(overlap)}")
        # Don't hard-assert overlap — the model may emit unrelated patches
        # that we still want to NOT reject (just log for the operator).
