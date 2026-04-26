"""CPG-lite v0 — in-memory SQLite storage.

Persists :class:`Symbol` and :class:`Reference` records into an
in-memory SQLite database. Two motivations for SQLite vs plain
Python dicts:

* The query API (:mod:`gitoma.cpg.queries`) does table joins
  (refs → symbols, callers_of, who_imports) — SQL expresses these
  natively without rebuilding indexes by hand each query.
* The footprint stays bounded: ~50k symbols + 200k refs in a
  ~10MB process memory, well under any practical repo size we
  index in v0 (cap = 200 files).

In-memory only for v0 — no caching across runs. Persistence /
incremental rebuild is v0.1.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from gitoma.cpg._base import Reference, RefKind, Symbol, SymbolKind

__all__ = ["Storage"]


_SCHEMA = """
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    parent_id INTEGER,
    is_public INTEGER NOT NULL,
    language TEXT NOT NULL DEFAULT 'python'
);
CREATE TABLE refs (
    symbol_id INTEGER,
    raw_name TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE imports (
    file TEXT NOT NULL,
    module_name TEXT NOT NULL,
    bound_name TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE INDEX idx_sym_name ON symbols(name);
CREATE INDEX idx_sym_qname ON symbols(qualified_name);
CREATE INDEX idx_sym_file ON symbols(file);
CREATE INDEX idx_refs_target ON refs(symbol_id);
CREATE INDEX idx_refs_file ON refs(file);
CREATE INDEX idx_refs_raw_name ON refs(raw_name);
CREATE INDEX idx_imports_file ON imports(file);
CREATE INDEX idx_imports_bound ON imports(file, bound_name);
"""


class Storage:
    """SQLite-backed store for CPG records.

    Caller workflow:
      1. ``s = Storage()`` — opens an in-memory DB and applies schema.
      2. ``id = s.insert_symbol(symbol)`` — for every Symbol; the DB
         assigns the autoincrement ``id`` and the helper returns it
         so the caller can chain ``parent_id`` on subsequent inserts.
      3. ``s.insert_reference(ref)`` — Reference rows.
      4. ``s.insert_import(file, module, bound, line)`` — for every
         ``import`` / ``from ... import`` binding (used by the
         resolver in :mod:`queries`).
      5. After the indexer finishes, the resolver pass walks
         ``iter_unresolved_refs()`` and back-fills ``symbol_id``
         via ``update_ref_symbol_id()``.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # ── Inserts ────────────────────────────────────────────────────

    def insert_symbol(self, sym: Symbol) -> int:
        """Insert a Symbol; returns the assigned ``id``. The Symbol's
        own ``id`` field is ignored on insert (autoincrement wins) —
        callers typically pass ``id=0`` and use the return value."""
        cur = self._conn.execute(
            """
            INSERT INTO symbols
                (file, line, col, kind, name, qualified_name,
                 parent_id, is_public, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sym.file, sym.line, sym.col, sym.kind.value,
                sym.name, sym.qualified_name, sym.parent_id,
                1 if sym.is_public else 0,
                sym.language,
            ),
        )
        return int(cur.lastrowid or 0)

    def insert_reference(self, ref: Reference) -> None:
        """Insert a Reference. ``symbol_id`` may be ``None`` (resolved
        later) or already-known (e.g. import targets)."""
        self._conn.execute(
            """
            INSERT INTO refs (symbol_id, raw_name, file, line, col, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ref.symbol_id, ref.raw_name, ref.file,
                ref.line, ref.col, ref.kind.value,
            ),
        )

    def insert_import(
        self, file: str, module_name: str, bound_name: str, line: int,
    ) -> None:
        """Record an import binding so the resolver can map a
        ``raw_name`` in ``file`` to its origin module / symbol."""
        self._conn.execute(
            "INSERT INTO imports (file, module_name, bound_name, line) "
            "VALUES (?, ?, ?, ?)",
            (file, module_name, bound_name, line),
        )

    # ── Queries (low-level — public API lives in queries.py) ───────

    def get_symbol_by_id(self, sid: int) -> Symbol | None:
        row = self._conn.execute(
            "SELECT * FROM symbols WHERE id = ?", (sid,),
        ).fetchone()
        return _row_to_symbol(row) if row else None

    def get_symbols_by_name(self, name: str) -> list[Symbol]:
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE name = ?", (name,),
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def get_symbols_in_file(self, file: str) -> list[Symbol]:
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE file = ? ORDER BY line, col",
            (file,),
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def get_refs_to(self, symbol_id: int) -> list[Reference]:
        rows = self._conn.execute(
            "SELECT * FROM refs WHERE symbol_id = ? ORDER BY file, line, col",
            (symbol_id,),
        ).fetchall()
        return [_row_to_reference(r) for r in rows]

    def get_refs_in_file(self, file: str) -> list[Reference]:
        rows = self._conn.execute(
            "SELECT * FROM refs WHERE file = ? ORDER BY line, col", (file,),
        ).fetchall()
        return [_row_to_reference(r) for r in rows]

    def iter_unresolved_refs(self) -> Iterator[tuple[int, str, str]]:
        """Yield ``(rowid, raw_name, file)`` for every ref with a NULL
        ``symbol_id``. Used by the resolver pass."""
        for row in self._conn.execute(
            "SELECT rowid, raw_name, file FROM refs WHERE symbol_id IS NULL",
        ):
            yield (int(row["rowid"]), str(row["raw_name"]), str(row["file"]))

    def update_ref_symbol_id(self, rowid: int, symbol_id: int) -> None:
        self._conn.execute(
            "UPDATE refs SET symbol_id = ? WHERE rowid = ?",
            (symbol_id, rowid),
        )

    def get_imports_for_file(self, file: str) -> list[tuple[str, str, int]]:
        """Return ``(module_name, bound_name, line)`` triples for the
        ``imports`` rows of ``file``."""
        rows = self._conn.execute(
            "SELECT module_name, bound_name, line FROM imports WHERE file = ?",
            (file,),
        ).fetchall()
        return [(str(r["module_name"]), str(r["bound_name"]), int(r["line"]))
                for r in rows]

    def files_importing(self, module_name: str) -> list[tuple[str, int]]:
        """Files that contain an import row whose ``module_name``
        matches (or starts with — submodule case). Returns
        ``(file, line)`` pairs."""
        rows = self._conn.execute(
            "SELECT DISTINCT file, line FROM imports "
            "WHERE module_name = ? OR module_name LIKE ? "
            "ORDER BY file, line",
            (module_name, f"{module_name}.%"),
        ).fetchall()
        return [(str(r["file"]), int(r["line"])) for r in rows]

    # ── Stats ──────────────────────────────────────────────────────

    def symbol_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM symbols",
        ).fetchone()["n"])

    def reference_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM refs",
        ).fetchone()["n"])

    def file_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(DISTINCT file) AS n FROM symbols",
        ).fetchone()["n"])

    def commit(self) -> None:
        """Flush pending writes. Cheap on in-memory DB; called by the
        indexer between files for crash-safety on partial builds."""
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_symbol(row: sqlite3.Row) -> Symbol:
    return Symbol(
        id=int(row["id"]),
        file=str(row["file"]),
        line=int(row["line"]),
        col=int(row["col"]),
        kind=SymbolKind(row["kind"]),
        name=str(row["name"]),
        qualified_name=str(row["qualified_name"]),
        parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
        is_public=bool(row["is_public"]),
        language=str(row["language"]),
    )


def _row_to_reference(row: sqlite3.Row) -> Reference:
    return Reference(
        symbol_id=int(row["symbol_id"]) if row["symbol_id"] is not None else None,
        raw_name=str(row["raw_name"]),
        file=str(row["file"]),
        line=int(row["line"]),
        col=int(row["col"]),
        kind=RefKind(row["kind"]),
    )
