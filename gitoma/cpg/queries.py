"""CPG-lite v0 — public query API + reference resolver.

The :class:`CPGIndex` is the surface the worker (and tests) consume.
Wraps a :class:`Storage` populated by the indexer; runs a one-shot
``resolve()`` pass to back-fill ``symbol_id`` on every Reference
where the heuristic finds a defining Symbol.

Resolution heuristic for v0 (stays simple on purpose):

  1. **Same-file precedence.** A bare name ``foo`` is FIRST matched
     against Symbols defined in the same file. If exactly one
     candidate exists, take it.
  2. **Imported name.** If the file has an ``imports`` row whose
     ``bound_name`` matches the raw_name, look up the originating
     module and pick the Symbol from THAT file with that name.
  3. **Global fallback.** If exactly one Symbol with that name
     exists across the whole index, take it. (Saves the common
     case where every file's helper is unique.)
  4. **Otherwise leave unresolved.** Ambiguous matches (multiple
     candidates after the above rules) all stay unresolved — better
     to know nothing than to pick wrong.

The ``ATTRIBUTE_ACCESS`` and method-style ``CALL`` references are
NOT resolved in v0 (would need type inference / receiver tracking).
They stay in the index for textual queries but ``callers_of`` is
defined on direct call refs only.
"""

from __future__ import annotations

from typing import Iterable

from gitoma.cpg._base import Reference, RefKind, Symbol, SymbolKind
from gitoma.cpg.storage import Storage

__all__ = ["CPGIndex"]


class CPGIndex:
    """Public query interface over a populated :class:`Storage`.

    Instantiate AFTER all files have been indexed. Calls ``resolve()``
    once on construction so callers always see resolved refs.
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._resolve()

    # ── Public queries ─────────────────────────────────────────────

    def get_symbol(self, name: str) -> list[Symbol]:
        """All Symbols whose leaf ``name`` matches. Order: by file,
        then by line. Used by callers asking "where is X defined?"."""
        return sorted(
            self._storage.get_symbols_by_name(name),
            key=lambda s: (s.file, s.line, s.col),
        )

    def get_symbols_in_file(self, file_path: str) -> list[Symbol]:
        """All Symbols defined in ``file_path``, ordered by position."""
        return self._storage.get_symbols_in_file(file_path)

    def find_references(self, symbol_id: int) -> list[Reference]:
        """Every Reference whose ``symbol_id`` resolved to this
        Symbol. May be empty for symbols that are exported but never
        used in the indexed scope."""
        return self._storage.get_refs_to(symbol_id)

    def callers_of(self, symbol_id: int) -> list[Reference]:
        """Subset of :meth:`find_references` restricted to ``CALL``
        and ``IMPORT_FROM`` references — i.e. "who actually invokes
        or imports this?". Excludes attribute access and bare name
        loads which often appear in docstrings / type hints / etc."""
        return [r for r in self.find_references(symbol_id)
                if r.kind in (RefKind.CALL, RefKind.IMPORT_FROM)]

    def who_imports(self, file_path: str) -> list[tuple[str, int]]:
        """Files that import the module corresponding to ``file_path``.
        Returns ``(file, line)`` pairs ordered by file then line."""
        from gitoma.cpg.python_indexer import module_qualified_name_for
        module_name = module_qualified_name_for(file_path)
        if not module_name:
            return []
        return self._storage.files_importing(module_name)

    def call_graph_for(
        self, symbol_id: int, depth: int = 1,
    ) -> dict[int, list[int]]:
        """Build a shallow caller→called map rooted at ``symbol_id``.

        For v0: returns ``{symbol_id: [caller_id_1, caller_id_2, ...]}``
        plus, for each caller, its own ``callers_of`` list — capped at
        ``depth`` levels. Cycles guarded by visited set. Useful as a
        compact "blast radius" view for the worker prompt.
        """
        result: dict[int, list[int]] = {}
        visited: set[int] = set()
        frontier = [symbol_id]
        for _ in range(max(1, depth)):
            next_frontier: list[int] = []
            for sid in frontier:
                if sid in visited:
                    continue
                visited.add(sid)
                callers = self.callers_of(sid)
                caller_ids = [
                    c.symbol_id for c in callers
                    if c.symbol_id is not None
                ]
                result[sid] = caller_ids
                next_frontier.extend(caller_ids)
            frontier = next_frontier
            if not frontier:
                break
        return result

    # ── Stats (passthrough) ────────────────────────────────────────

    def file_count(self) -> int:
        return self._storage.file_count()

    def symbol_count(self) -> int:
        return self._storage.symbol_count()

    def reference_count(self) -> int:
        return self._storage.reference_count()

    # ── Reference resolver ────────────────────────────────────────

    def _resolve(self) -> None:
        """Walk every unresolved ref and back-fill ``symbol_id`` per
        the heuristic in the module docstring. Idempotent: re-running
        is a no-op because resolved refs are skipped by the iterator.
        """
        for rowid, raw_name, file in self._storage.iter_unresolved_refs():
            sid = self._resolve_one(raw_name, file)
            if sid is not None:
                self._storage.update_ref_symbol_id(rowid, sid)
        self._storage.commit()

    def _resolve_one(self, raw_name: str, file: str) -> int | None:
        # 1. Same-file precedence
        same_file = [
            sym for sym in self._storage.get_symbols_by_name(raw_name)
            if sym.file == file and sym.kind in _DEFINING_KINDS
        ]
        if len(same_file) == 1:
            return same_file[0].id

        # 2. Imported name
        from gitoma.cpg.python_indexer import module_qualified_name_for
        for module_name, bound_name, _line in self._storage.get_imports_for_file(file):
            if bound_name == "*":
                # Star-import is opaque in v0 — refuse to guess.
                continue
            if bound_name == raw_name:
                candidates = [
                    sym for sym in self._storage.get_symbols_by_name(raw_name)
                    if sym.file != file and sym.kind in _DEFINING_KINDS
                ]
                if len(candidates) == 1:
                    return candidates[0].id
                # Multiple candidates → disambiguate by module name.
                # Each candidate's file maps to a qualified module name
                # via the same helper the indexer uses; pick the one
                # whose module matches the import's module_name.
                module_matches = [
                    sym for sym in candidates
                    if module_qualified_name_for(sym.file) == module_name
                ]
                if len(module_matches) == 1:
                    return module_matches[0].id
                # TS path-based imports (./other, ../utils/helper):
                # resolve relative to the importing file's directory,
                # then test candidate paths with TS suffixes.
                if module_name.startswith(("./", "../")):
                    ts_match = _resolve_ts_path_match(
                        candidates, file, module_name,
                    )
                    if ts_match is not None:
                        return ts_match.id

        # 3. Global fallback — exactly one definition anywhere.
        global_defs = [
            sym for sym in self._storage.get_symbols_by_name(raw_name)
            if sym.kind in _DEFINING_KINDS
        ]
        if len(global_defs) == 1:
            return global_defs[0].id

        return None


_DEFINING_KINDS = frozenset({
    SymbolKind.FUNCTION, SymbolKind.CLASS,
    SymbolKind.METHOD, SymbolKind.ASSIGNMENT,
    # v0.5-slim: TS interfaces + type aliases ARE definitions for
    # resolution purposes — an import of an interface name should
    # resolve to its declaration.
    SymbolKind.INTERFACE, SymbolKind.TYPE_ALIAS,
})


def _resolve_ts_relative_path(
    importing_file: str, module_name: str,
) -> list[str]:
    """Return the list of candidate file paths a TS relative import
    of ``module_name`` from ``importing_file`` could resolve to.

    Mirrors Node's module resolution lite-version:
      * ``./foo`` → ``foo.ts`` / ``foo.tsx`` / ``foo/index.ts`` /
        ``foo/index.tsx`` in the importing file's directory.
      * ``../foo`` → same, one directory up.
      * Multi-segment ``./components/Button`` honours each segment.
    """
    from pathlib import PurePosixPath
    base = PurePosixPath(importing_file).parent
    target = (base / module_name).as_posix()
    # Normalise ``./`` and ``../`` segments without touching the FS.
    parts: list[str] = []
    for seg in target.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    base_path = "/".join(parts)
    return [
        f"{base_path}.ts",
        f"{base_path}.tsx",
        f"{base_path}/index.ts",
        f"{base_path}/index.tsx",
    ]


# Bound to CPGIndex below — defined at module level so unit tests
# can exercise the path math without instantiating an index.
def _resolve_ts_path_match(
    candidates: list[Symbol], importing_file: str, module_name: str,
) -> Symbol | None:
    """Pick the candidate Symbol whose file matches one of the
    canonical TS resolution paths. Returns ``None`` when zero or
    >1 match (ambiguity = unresolved, same as Python rule)."""
    targets = _resolve_ts_relative_path(importing_file, module_name)
    matches = [c for c in candidates if c.file in targets]
    if len(matches) == 1:
        return matches[0]
    return None
