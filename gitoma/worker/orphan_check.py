"""G18 (abandoned-helper) + G19 (echo-chamber) — two CPG-based
structural critics batched in one module.

Both detect "orphan symbols" introduced by a patch — symbols that
exist but aren't connected to the rest of the codebase. The two
flavors:

* **G18 abandoned-helper**: a symbol the patch KEPT but whose last
  callers were deleted by the patch. Likely either the patch
  should also remove the helper, or the patch removed the wrong
  caller. Single-file scope in v1 (see sprint plan for
  cross-file deferral).

* **G19 echo-chamber**: NEW public symbols added by the patch
  that ONLY call each other — nothing existing in the codebase
  calls them. PR claims "added X" but X is dead from the
  outside. Repo-wide scope (uses the AFTER cpg_index built
  before PHASE 2).

Both opt-in (default off — false-positive risk on libraries +
new entry points). Both produce LLM-feedback strings via
``render_for_llm()`` so a single retry round can address every
orphan in the patch.

Pure / deterministic / no LLM. Defensive: any failure (CPG
missing, file unreadable, parse error) → return None silently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gitoma.cpg._base import Reference, RefKind, Symbol, SymbolKind
from gitoma.cpg.diff import DEFINING_KINDS, INDEXABLE_EXTS, index_text_to_storage
from gitoma.cpg.storage import Storage

__all__ = [
    "G18Conflict", "G18Result",
    "G19Conflict", "G19Result",
    "check_g18_abandoned_helpers",
    "check_g19_echo_chamber",
    "is_g18_enabled", "is_g19_enabled",
]


# ── Env opt-in ────────────────────────────────────────────────────


def is_g18_enabled() -> bool:
    return (os.environ.get("GITOMA_G18_ABANDONED") or "").lower() in (
        "1", "on", "true", "yes",
    )


def is_g19_enabled() -> bool:
    return (os.environ.get("GITOMA_G19_ECHO_CHAMBER") or "").lower() in (
        "1", "on", "true", "yes",
    )


# ── G18 result types ──────────────────────────────────────────────


@dataclass(frozen=True)
class G18Conflict:
    """One symbol the patch left abandoned: kept defined but no
    longer referenced in its own file. Single-file scope v1."""

    file: str
    symbol_name: str
    symbol_kind: str
    refs_before: int
    refs_after: int


@dataclass(frozen=True)
class G18Result:
    conflicts: tuple[G18Conflict, ...]

    def render_for_llm(self) -> str:
        lines = [
            "Your patch left ABANDONED HELPERS — public symbols that "
            "USED TO be referenced inside their own file but are now "
            "uncallable from anywhere in this file. Either:",
            "  * Remove the helper too (it's dead code now), OR",
            "  * Restore the caller you deleted (the patch likely "
            "    removed the wrong code).",
            "",
        ]
        for c in self.conflicts:
            lines.append(
                f"  * {c.file}: `{c.symbol_kind} {c.symbol_name}` had "
                f"{c.refs_before} reference(s) before this patch, now "
                f"has {c.refs_after}."
            )
        return "\n".join(lines)


# ── G19 result types ──────────────────────────────────────────────


@dataclass(frozen=True)
class G19Conflict:
    """One new symbol whose only callers come from patch-internal
    files (the symbol's own file or other newly-populated touched
    files). v1 reports internal callers BY FILE — the position-based
    caller-symbol resolution is deferred to a future Reference
    schema extension. The signal is still actionable: 'every caller
    of X lives in code your patch added/created'."""

    file: str
    symbol_name: str
    symbol_kind: str
    qualified_name: str
    internal_caller_count: int
    internal_caller_files: tuple[str, ...]


@dataclass(frozen=True)
class G19Result:
    conflicts: tuple[G19Conflict, ...]

    def render_for_llm(self) -> str:
        lines = [
            "Your patch added ECHO-CHAMBER SYMBOLS — new public "
            "symbols whose only callers come from code YOUR PATCH "
            "added (the symbol's own new file or other newly-"
            "populated files in this patch). Nothing existing in "
            "the codebase touches them; they look like work but "
            "integrate with nothing. Either:",
            "  * Add a real entry-point integration (a call from "
            "    existing code), OR",
            "  * Drop the chain — if no existing code needs these, "
            "    they're dead code on day one.",
            "",
        ]
        for c in self.conflicts:
            files_list = ", ".join(c.internal_caller_files[:5])
            if len(c.internal_caller_files) > 5:
                files_list += f" (+{len(c.internal_caller_files)-5} more)"
            lines.append(
                f"  * {c.file}: `{c.symbol_kind} {c.symbol_name}` "
                f"({c.qualified_name}) called only from patch-added "
                f"files [{files_list}] — no external callers."
            )
        return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────


def _is_indexable(rel_path: str) -> bool:
    return any(rel_path.endswith(ext) for ext in INDEXABLE_EXTS)


def _public_defining_symbols_in_storage(
    storage: Storage, rel_path: str,
) -> dict[tuple[str, SymbolKind], Symbol]:
    """Return ``{(qualified_name, kind): Symbol}`` for public defining
    symbols defined in ``rel_path`` within ``storage``. The
    qualified_name+kind tuple is the cross-storage identity key."""
    out: dict[tuple[str, SymbolKind], Symbol] = {}
    for sym in storage.get_symbols_in_file(rel_path):
        if not sym.is_public:
            continue
        if sym.kind not in DEFINING_KINDS:
            continue
        out[(sym.qualified_name, sym.kind)] = sym
    return out


def _count_refs_to_name_in_storage(
    storage: Storage, rel_path: str, name: str,
) -> int:
    """Count CALL / IMPORT_FROM refs in ``rel_path`` whose
    ``raw_name`` matches ``name``. Cross-file refs not counted (we
    only have the single file's storage). Conservative: text match
    on raw_name, not symbol_id (which only resolves within the
    same storage)."""
    count = 0
    for ref in storage.get_refs_in_file(rel_path):
        if ref.raw_name != name:
            continue
        if ref.kind not in (RefKind.CALL, RefKind.IMPORT_FROM):
            continue
        count += 1
    return count


# ── G18 abandoned-helper ──────────────────────────────────────────


def check_g18_abandoned_helpers(
    repo_root: Path,
    touched: list[str],
    originals: dict[str, str] | None,
) -> G18Result | None:
    """Return None when:
      * G18 not enabled via env
      * No touched indexable files
      * No originals available
      * No abandoned helpers found

    Single-file scope: catches "patch removed all in-file callers
    of a function but kept the function". Cross-file abandons are
    not detected (sprint plan doc'd limitation).
    """
    if not is_g18_enabled():
        return None
    if not touched or originals is None:
        return None

    conflicts: list[G18Conflict] = []
    for rel in touched:
        if not _is_indexable(rel):
            continue
        before_content = originals.get(rel)
        if before_content is None:
            # File didn't exist before — `create` action; G18
            # has nothing to compare against (no abandoned helpers
            # possible from a fresh file).
            continue
        try:
            after_content = (repo_root / rel).read_text(errors="replace")
        except OSError:
            continue
        try:
            before_storage = index_text_to_storage(rel, before_content)
            after_storage = index_text_to_storage(rel, after_content)
        except Exception:  # noqa: BLE001 — defensive
            continue

        before_syms = _public_defining_symbols_in_storage(before_storage, rel)
        after_syms = _public_defining_symbols_in_storage(after_storage, rel)

        # Symbols KEPT: present in BOTH (matched on qname+kind).
        # For each kept symbol, compare ref count by name.
        for key, _sym in before_syms.items():
            if key not in after_syms:
                continue  # deleted, not abandoned
            qname, kind = key
            leaf = qname.rsplit(".", 1)[-1]
            before_refs = _count_refs_to_name_in_storage(
                before_storage, rel, leaf,
            )
            after_refs = _count_refs_to_name_in_storage(
                after_storage, rel, leaf,
            )
            if before_refs > 0 and after_refs == 0:
                conflicts.append(G18Conflict(
                    file=rel,
                    symbol_name=leaf,
                    symbol_kind=kind.value,
                    refs_before=before_refs,
                    refs_after=after_refs,
                ))

        before_storage.close()
        after_storage.close()

    if not conflicts:
        return None
    return G18Result(conflicts=tuple(conflicts))


# ── G19 echo-chamber ──────────────────────────────────────────────


def check_g19_echo_chamber(
    repo_root: Path,
    touched: list[str],
    originals: dict[str, str] | None,
    cpg_index: Any = None,
) -> G19Result | None:
    """Return None when:
      * G19 not enabled via env
      * No CPG index available (need repo-wide caller info)
      * No new public symbols introduced
      * No echo-chamber symbols found

    File-level v1 heuristic: a new symbol is an echo chamber when
    EVERY caller's FILE is among the "newly-populated touched files"
    — files where the BEFORE state had NO public defining symbols
    AND the patch added some (i.e., new files OR previously-empty
    files). Catches the common case (PR adds a new module containing
    multiple functions that only call each other) without needing
    position-based caller-symbol resolution (which would require a
    schema extension on Reference).

    Trade-off: false-NEGATIVES on patches that add new symbols to
    an EXISTING file with pre-existing public symbols (we'd treat
    other-file callers as "external" even though they may also be
    patch-added). False-POSITIVES are heavily suppressed.

    Symbols with 0 total callers are NOT flagged — that's G16
    (truly dead code), not G19.
    """
    if not is_g19_enabled():
        return None
    if cpg_index is None:
        return None
    if not touched or originals is None:
        return None

    # Phase 1: collect new public-symbol qualified_names + identify
    # "newly-populated" files (BEFORE empty of public defining
    # symbols, AFTER has some). A caller from such a file is
    # treated as "patch-added code" for echo-chamber purposes.
    new_qnames: set[str] = set()
    new_symbols_by_qname: dict[str, tuple[str, str]] = {}
    newly_populated_files: set[str] = set()

    for rel in touched:
        if not _is_indexable(rel):
            continue
        before_content = originals.get(rel, "")
        try:
            after_content = (repo_root / rel).read_text(errors="replace")
        except OSError:
            continue
        try:
            before_storage = index_text_to_storage(rel, before_content)
            after_storage = index_text_to_storage(rel, after_content)
        except Exception:  # noqa: BLE001
            continue
        before_syms = _public_defining_symbols_in_storage(before_storage, rel)
        after_syms = _public_defining_symbols_in_storage(after_storage, rel)
        for key, sym in after_syms.items():
            if key not in before_syms:
                qname = sym.qualified_name
                new_qnames.add(qname)
                new_symbols_by_qname[qname] = (rel, sym.kind.value)
        if not before_syms and after_syms:
            newly_populated_files.add(rel)
        before_storage.close()
        after_storage.close()

    if not new_qnames:
        return None

    # Phase 2: per new symbol, classify callers by FILE.
    conflicts: list[G19Conflict] = []
    for qname in sorted(new_qnames):
        leaf = qname.rsplit(".", 1)[-1]
        candidates = [
            s for s in cpg_index.get_symbol(leaf)
            if s.qualified_name == qname and s.kind in DEFINING_KINDS
        ]
        for sym in candidates:
            try:
                callers = cpg_index.callers_of(sym.id)
            except Exception:  # noqa: BLE001
                continue
            if not callers:
                # G16 territory; skip.
                continue
            internal_files: list[str] = []
            external_files: list[str] = []
            for caller_ref in callers:
                if caller_ref.file == sym.file or \
                        caller_ref.file in newly_populated_files:
                    internal_files.append(caller_ref.file)
                else:
                    external_files.append(caller_ref.file)
            if not external_files and internal_files:
                file, kind = new_symbols_by_qname.get(qname, ("?", "?"))
                # Render: list the unique caller files that are
                # patch-internal — meaningful even though we can't
                # name the calling SYMBOL precisely (file-level v1).
                conflicts.append(G19Conflict(
                    file=file,
                    symbol_name=leaf,
                    symbol_kind=kind,
                    qualified_name=qname,
                    internal_caller_count=len(internal_files),
                    internal_caller_files=tuple(
                        sorted(set(internal_files)),
                    ),
                ))

    if not conflicts:
        return None
    return G19Result(conflicts=tuple(conflicts))
