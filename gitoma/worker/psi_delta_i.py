"""Ψ-full v1 — ΔI component (structural conservativeness).

Per touched file:
  * **action=create** (file not in originals): contributes 1.0 — a
    new file IS its own structure; there's nothing to perturb.
  * **action=delete** (originals[file] != "" but file gone post-patch):
    contributes 0.0 (max entropy injected). Opt-out via
    ``GITOMA_PSI_DELETE_ALLOWED=on`` for refactor passes that
    legitimately remove dead code.
  * **action=modify**: re-index the BEFORE content + re-index the
    AFTER content as throwaway in-memory ``Storage``. Compute
    Δsymbols + Δrefs normalised, then ``ΔI_file = 1 - mean(Δs, Δr)``.

Aggregate ΔI = simple mean across files. Files outside the
indexable set contribute 1.0 (no structural delta to measure).

Pure function. Re-indexing is the cost: ~5-50ms per file pair on
typical Python modules. Cap at 5 files; beyond, contribute 1.0
with a trace note (avoids pathological cost on big patches).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gitoma.cpg._base import SymbolKind
from gitoma.cpg.go_indexer import index_go_file
from gitoma.cpg.javascript_indexer import index_javascript_file
from gitoma.cpg.python_indexer import index_python_file
from gitoma.cpg.rust_indexer import index_rust_file
from gitoma.cpg.storage import Storage
from gitoma.cpg.typescript_indexer import index_typescript_file

__all__ = ["compute_delta_i", "MAX_REINDEX_FILES"]


MAX_REINDEX_FILES = 5
"""Hard cap on per-evaluation re-indexing cost. A patch touching
50 files would otherwise pay 50× re-index in the gate evaluation.
Beyond cap, files contribute ΔI=1.0 + a trace warning."""


_INDEXABLE_EXTS = (
    ".py",                             # v0
    ".ts", ".tsx",                     # v0.5-slim
    ".js", ".mjs", ".cjs", ".rs",      # v0.5-expansion
    ".go",                             # v0.5-expansion-go
)


def _delete_allowed() -> bool:
    return (os.environ.get("GITOMA_PSI_DELETE_ALLOWED") or "").lower() in (
        "1", "on", "true", "yes",
    )


def _index_text_to_storage(rel_path: str, content: str) -> Storage:
    """Build a throwaway in-memory Storage from a single file's
    content. Writes the content to a temp file because the indexers
    take a Path (they read+parse it themselves). Returns the
    populated Storage."""
    import tempfile
    storage = Storage()
    suffix = "".join(Path(rel_path).suffixes) or ".py"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        if rel_path.endswith(".py"):
            index_python_file(tmp_path, rel_path, storage)
        elif rel_path.endswith((".ts", ".tsx")):
            index_typescript_file(tmp_path, rel_path, storage)
        elif rel_path.endswith((".js", ".mjs", ".cjs")):
            index_javascript_file(tmp_path, rel_path, storage)
        elif rel_path.endswith(".rs"):
            index_rust_file(tmp_path, rel_path, storage)
        elif rel_path.endswith(".go"):
            index_go_file(tmp_path, rel_path, storage)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return storage


def _count_relevant(storage: Storage, rel_path: str) -> tuple[int, int]:
    """Returns ``(symbol_count, ref_count)`` for the indexed file.
    Only counts public defining symbols + their references — same
    filter the renderer uses, so ΔI tracks "the structural skeleton
    a downstream caller could see"."""
    syms = storage.get_symbols_in_file(rel_path)
    relevant_kinds = {
        SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.METHOD,
        SymbolKind.ASSIGNMENT, SymbolKind.INTERFACE,
        SymbolKind.TYPE_ALIAS,
    }
    sym_count = sum(1 for s in syms if s.kind in relevant_kinds)
    ref_count = len(storage.get_refs_in_file(rel_path))
    return sym_count, ref_count


def compute_delta_i(
    touched_files: list[str],
    originals: dict[str, str] | None,
    repo_root: Path,
) -> tuple[float, dict[str, Any]]:
    """Returns ``(delta_i in [0,1], breakdown)``.

    Args:
        touched_files: relative paths the patch wrote/created/deleted.
        originals: ``{path: original_content}`` for files that
            EXISTED before the patch. Files in ``touched_files`` but
            NOT in originals → action=create. ``None`` → no signal
            available (degrade to 1.0 across all files).
        repo_root: used to read post-patch content.
    """
    if not touched_files:
        return 1.0, {
            "per_file": [],
            "files_neutral": 0,
            "files_capped": 0,
            "delete_allowed": _delete_allowed(),
        }
    if originals is None:
        return 1.0, {
            "per_file": [],
            "files_neutral": len(touched_files),
            "files_capped": 0,
            "delete_allowed": _delete_allowed(),
        }

    delete_allowed = _delete_allowed()
    per_file: list[dict[str, Any]] = []
    neutral = 0
    capped = 0
    file_scores: list[float] = []

    for path in touched_files:
        # Cap check
        if len(file_scores) + len([p for p in per_file
                                    if p["delta_i"] != 1.0]) >= MAX_REINDEX_FILES:
            capped += 1
            file_scores.append(1.0)
            continue

        # Non-indexable: neutral
        if not any(path.endswith(ext) for ext in _INDEXABLE_EXTS):
            neutral += 1
            file_scores.append(1.0)
            continue

        original = originals.get(path)
        abs_path = repo_root / path

        # action=create — file is new, no "before" structure
        if original is None or original == "":
            per_file.append({
                "file": path, "action": "create", "delta_i": 1.0,
            })
            file_scores.append(1.0)
            continue

        # action=delete — file existed, now gone
        if not abs_path.exists():
            score = 1.0 if delete_allowed else 0.0
            per_file.append({
                "file": path, "action": "delete", "delta_i": score,
            })
            file_scores.append(score)
            continue

        # action=modify — index BEFORE + AFTER, compare counts
        try:
            before_storage = _index_text_to_storage(path, original)
            after_content = abs_path.read_text(errors="replace")
            after_storage = _index_text_to_storage(path, after_content)
        except Exception:  # noqa: BLE001 — defensive on parser bugs
            neutral += 1
            file_scores.append(1.0)
            continue

        sym_before, ref_before = _count_relevant(before_storage, path)
        sym_after, ref_after = _count_relevant(after_storage, path)

        delta_s = (
            abs(sym_after - sym_before) /
            max(sym_after, sym_before, 1)
        )
        delta_r = (
            abs(ref_after - ref_before) /
            max(ref_after, ref_before, 1)
        )
        delta_i_file = 1.0 - (delta_s + delta_r) / 2.0
        # Bound to [0, 1] in case of pathological cases
        delta_i_file = max(0.0, min(1.0, delta_i_file))

        per_file.append({
            "file": path, "action": "modify",
            "delta_i": round(delta_i_file, 4),
            "symbols_before": sym_before, "symbols_after": sym_after,
            "refs_before": ref_before, "refs_after": ref_after,
        })
        file_scores.append(delta_i_file)

    if not file_scores:
        return 1.0, {
            "per_file": [],
            "files_neutral": neutral,
            "files_capped": capped,
            "delete_allowed": delete_allowed,
        }

    delta_i = sum(file_scores) / len(file_scores)
    return delta_i, {
        "per_file": per_file,
        "files_neutral": neutral,
        "files_capped": capped,
        "delete_allowed": delete_allowed,
    }
