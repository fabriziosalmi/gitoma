"""Skeletal Representation v1 — compressed signature view of the
indexed repo, designed to slot into the planner prompt.

Format (single block per file, files in alphabetical order):

    ## src/handler.py
    def process_request(req: dict) -> str
    def _internal()
    class RequestHandler
      def handle(self, req: Request) -> Response

    ## src/api.ts
    interface User
    function callApi(url: string): Promise<string>
    class ApiClient
      get(path: string): Promise<Response>

The planner gets a structural view of every file without paying the
full content cost. Token-budgeted (~5000 tokens / 20000 chars by
default) so even large repos fit.

Pure rendering — no I/O beyond CPG queries. Caller provides the
budget; renderer truncates with a marker when hit.
"""

from __future__ import annotations

from typing import Any

from gitoma.cpg._base import Symbol, SymbolKind
from gitoma.cpg.queries import CPGIndex

__all__ = ["render_skeleton", "DEFAULT_MAX_CHARS"]


DEFAULT_MAX_CHARS = 20000
"""~5000 tokens at the typical 4-char/token ratio. Picked so the
skeleton fits comfortably alongside RepoBrief + fingerprint +
metric_report + task rules in a 32k context window. Configurable
per-call AND via env in the wiring layer."""


# Rendered with their kind name as the line prefix. Order in this
# tuple = order they appear within a file's section.
_DEFINING_KINDS = (
    SymbolKind.CLASS,
    SymbolKind.INTERFACE,
    SymbolKind.TYPE_ALIAS,
    SymbolKind.FUNCTION,
    SymbolKind.METHOD,
    SymbolKind.ASSIGNMENT,
)


def render_skeleton(
    cpg_index: CPGIndex | None,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_private: bool = False,
) -> str:
    """Render a token-budgeted skeleton from the index. Returns an
    empty string when the index is None / empty / nothing to render
    (caller checks truthiness and skips injection)."""
    if cpg_index is None:
        return ""
    if max_chars <= 0:
        return ""

    files = _all_files_with_relevant_symbols(cpg_index, include_private)
    if not files:
        return ""

    sections: list[str] = []
    cumulative = 0
    omitted = 0
    omitted_symbols = 0
    for file_path in sorted(files):
        symbols = files[file_path]
        section = _render_file_section(file_path, symbols)
        # +1 for the joining newline
        if cumulative + len(section) + 1 > max_chars:
            omitted += 1
            omitted_symbols += len(symbols)
            continue
        sections.append(section)
        cumulative += len(section) + 1

    if not sections:
        return ""

    rendered = "\n".join(sections)
    if omitted > 0:
        rendered += (
            f"\n\n## ({omitted} file(s) omitted under "
            f"{max_chars}-char skeleton budget — "
            f"{omitted_symbols} additional symbols not shown)"
        )
    return rendered


def _all_files_with_relevant_symbols(
    cpg_index: CPGIndex,
    include_private: bool,
) -> dict[str, list[Symbol]]:
    """Walk every Symbol once, bucket by file, keep only the kinds
    we render. Empty files (or module-only) are pruned so the
    skeleton doesn't list bare ``## path/`` headers with no body."""
    # CPGIndex doesn't expose "all symbols" directly; use the
    # underlying storage. (storage.symbol_count() exists; we can
    # iterate via a query.)
    storage = cpg_index._storage  # noqa: SLF001 — internal helper
    rows = storage._conn.execute(  # noqa: SLF001
        "SELECT * FROM symbols ORDER BY file, line, col",
    ).fetchall()
    from gitoma.cpg.storage import _row_to_symbol
    by_file: dict[str, list[Symbol]] = {}
    for row in rows:
        sym = _row_to_symbol(row)
        if sym.kind not in _DEFINING_KINDS:
            continue
        if not include_private and not sym.is_public:
            continue
        by_file.setdefault(sym.file, []).append(sym)
    return by_file


def _render_file_section(file_path: str, symbols: list[Symbol]) -> str:
    """Render one ``## file`` block. Methods are nested under their
    parent class (matched via ``parent_id``)."""
    lines: list[str] = [f"## {file_path}"]
    # Build parent_id → [child symbols] for class members.
    children_by_parent: dict[int, list[Symbol]] = {}
    for sym in symbols:
        if sym.kind is SymbolKind.METHOD and sym.parent_id is not None:
            children_by_parent.setdefault(sym.parent_id, []).append(sym)

    # Top-level: anything that isn't a class member.
    method_ids = {
        s.id for sibs in children_by_parent.values() for s in sibs
    }
    for sym in symbols:
        if sym.id in method_ids:
            continue
        lines.append(_format_top_level(sym))
        if sym.kind is SymbolKind.CLASS and sym.id in children_by_parent:
            for method in children_by_parent[sym.id]:
                lines.append(f"  {_format_method(method)}")
    return "\n".join(lines)


def _format_top_level(sym: Symbol) -> str:
    """One-line render for a top-level symbol. The ``signature`` is
    appended to function/method names; for classes / interfaces /
    type aliases the kind+name IS the signature."""
    if sym.kind is SymbolKind.FUNCTION:
        return f"def {sym.name}{sym.signature or '()'}"
    if sym.kind is SymbolKind.CLASS:
        return f"class {sym.name}"
    if sym.kind is SymbolKind.INTERFACE:
        return f"interface {sym.name}"
    if sym.kind is SymbolKind.TYPE_ALIAS:
        return f"type {sym.name}"
    if sym.kind is SymbolKind.ASSIGNMENT:
        return f"{sym.name} = ..."
    if sym.kind is SymbolKind.METHOD:
        # Fallback: methods that escaped the class-grouping (e.g.
        # parent class wasn't indexed for some reason).
        return f"def {sym.name}{sym.signature or '()'}"
    return sym.name


def _format_method(sym: Symbol) -> str:
    """Method-line format (no `def` prefix; class context already
    shown via the parent's ``class X`` header)."""
    return f"{sym.name}{sym.signature or '()'}"
