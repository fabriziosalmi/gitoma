"""G16 (dead-code-introduction) + G18 (abandoned-helper) + G19
(echo-chamber) — three CPG-based structural critics batched in
one module.

All three detect "orphan symbols" introduced by a patch — symbols
that exist but aren't connected to the rest of the codebase.
The three flavors:

* **G16 dead-code-introduction**: NEW public symbols added by the
  patch that have ZERO callers anywhere in the codebase. Pure
  dead code on day one. Distinct from G19 (which fires when the
  symbol HAS callers but they're all patch-added). Test files
  exempted via path heuristic — pytest discovers ``test_*``
  functions by reflection, never as call refs.

* **G18 abandoned-helper**: a symbol the patch KEPT but whose last
  callers were deleted by the patch. Likely either the patch
  should also remove the helper, or the patch removed the wrong
  caller. Single-file scope in v1 (see sprint plan for
  cross-file deferral).

* **G19 echo-chamber**: NEW public symbols added by the patch
  that have callers, but EVERY caller is patch-added code.
  PR claims "added X" but X is dead from the outside. Repo-wide
  scope (uses the AFTER cpg_index built before PHASE 2).

All three opt-in (default off — false-positive risk on
libraries, framework-discovery routes/fixtures, new entry
points). Each produces an LLM-feedback string via
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
    "G16Conflict", "G16Result",
    "G18Conflict", "G18Result",
    "G19Conflict", "G19Result",
    "check_g16_dead_code",
    "check_g18_abandoned_helpers",
    "check_g19_echo_chamber",
    "is_g16_enabled", "is_g18_enabled", "is_g19_enabled",
]


# ── Env opt-in ────────────────────────────────────────────────────


def is_g16_enabled() -> bool:
    return (os.environ.get("GITOMA_G16_DEAD_CODE") or "").lower() in (
        "1", "on", "true", "yes",
    )


def is_g18_enabled() -> bool:
    return (os.environ.get("GITOMA_G18_ABANDONED") or "").lower() in (
        "1", "on", "true", "yes",
    )


def is_g19_enabled() -> bool:
    return (os.environ.get("GITOMA_G19_ECHO_CHAMBER") or "").lower() in (
        "1", "on", "true", "yes",
    )


# ── Test-file heuristic (shared) ──────────────────────────────────


_TEST_PATH_FRAGMENTS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
)
_TEST_NAME_PREFIXES = ("test_",)
_TEST_NAME_SUFFIXES = (
    "_test.py", ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
    "_test.go", "_test.rs",
)


def _is_test_file(rel_path: str) -> bool:
    """Heuristic for test-file paths across Python/TS/JS/Rust/Go.

    Test functions are routinely "uncalled" — the test runner
    discovers them by reflection (pytest, vitest, jest, go test,
    cargo test). Any orphan-symbol critic that flags them
    explodes false-positively, so we exempt them.
    """
    norm = "/" + rel_path.lstrip("/")
    if any(frag in norm for frag in _TEST_PATH_FRAGMENTS):
        return True
    base = rel_path.rsplit("/", 1)[-1]
    if base.startswith(_TEST_NAME_PREFIXES):
        return True
    if base.endswith(_TEST_NAME_SUFFIXES):
        return True
    return False


# ── G16 result types ──────────────────────────────────────────────


@dataclass(frozen=True)
class G16Conflict:
    """One new public symbol the patch added that has zero callers
    anywhere in the codebase. Pure dead code."""

    file: str
    symbol_name: str
    symbol_kind: str
    qualified_name: str


@dataclass(frozen=True)
class G16Result:
    conflicts: tuple[G16Conflict, ...]

    def render_for_llm(self) -> str:
        lines = [
            "Your patch INTRODUCED DEAD CODE — public symbols added "
            "by this patch have ZERO callers anywhere in the "
            "codebase (this is stricter than G19's echo-chamber "
            "check, which fires on patch-internal callers; G16 "
            "fires when there are NO callers at all). Either:",
            "  * Remove the unused symbol(s), OR",
            "  * Add the call site that uses them, OR",
            "  * If this is a public API meant for external "
            "    consumers (library entry point, framework-discovered "
            "    route, plugin hook), document why it appears unused "
            "    and we'll relax the check.",
            "",
        ]
        for c in self.conflicts:
            lines.append(
                f"  * {c.file}: `{c.symbol_kind} {c.symbol_name}` "
                f"({c.qualified_name}) — 0 callers."
            )
        return "\n".join(lines)


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


# ── G16 dead-code-introduction ────────────────────────────────────


def check_g16_dead_code(
    repo_root: Path,
    touched: list[str],
    originals: dict[str, str] | None,
    cpg_index: Any = None,
) -> G16Result | None:
    """Return None when:
      * G16 not enabled via env
      * No CPG index available (need repo-wide caller info)
      * No originals available
      * No touched indexable files
      * No new public symbols introduced
      * No truly-dead new symbols found

    For each new public defining symbol added by the patch, query
    the AFTER cpg_index for callers. Zero callers → flagged.
    Test files exempted (pytest/vitest/jest/go test/cargo test
    discover by reflection, never as call refs).

    Distinct from G19 (echo-chamber): G19 fires when the new
    symbol HAS callers but they're all patch-added; G16 fires
    when there are NO callers at all. The two compose: a patch
    that adds a useless function is caught by G16; a patch that
    adds a self-calling clique is caught by G19.
    """
    if not is_g16_enabled():
        return None
    if cpg_index is None:
        return None
    if not touched or originals is None:
        return None

    # Phase 1: collect new public-symbol qualified_names per file,
    # skipping test files.
    new_symbols_by_qname: dict[str, tuple[str, str]] = {}

    for rel in touched:
        if not _is_indexable(rel):
            continue
        if _is_test_file(rel):
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
                new_symbols_by_qname[qname] = (rel, sym.kind.value)
        before_storage.close()
        after_storage.close()

    if not new_symbols_by_qname:
        return None

    # Phase 2: per new symbol, query cpg_index for callers. If 0 →
    # truly dead → flag. Per-symbol candidates are matched on
    # qualified_name + DEFINING_KINDS (same shape as G19).
    conflicts: list[G16Conflict] = []
    for qname in sorted(new_symbols_by_qname.keys()):
        leaf = qname.rsplit(".", 1)[-1]
        candidates = [
            s for s in cpg_index.get_symbol(leaf)
            if s.qualified_name == qname and s.kind in DEFINING_KINDS
        ]
        # Aggregate caller count across all candidates with this
        # qname (handles overload-like collisions). 0 across all
        # → truly dead.
        total_callers = 0
        for sym in candidates:
            try:
                callers = cpg_index.callers_of(sym.id)
            except Exception:  # noqa: BLE001
                continue
            total_callers += len(callers)
        if total_callers == 0:
            file, kind = new_symbols_by_qname[qname]
            conflicts.append(G16Conflict(
                file=file,
                symbol_name=leaf,
                symbol_kind=kind,
                qualified_name=qname,
            ))

    if not conflicts:
        return None
    return G16Result(conflicts=tuple(conflicts))


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
