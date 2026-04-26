"""CPG-lite v0 — BLAST RADIUS prompt block renderer.

When the worker is about to modify a Python file, query the CPG for
every Symbol defined in that file and the callers of each one, then
render a compact text block the LLM can read before patching:

    == BLAST RADIUS (CPG-lite) ==
    Modifying gitoma/foo.py touches the following defined symbols.
    The CALLERS listed are other files that will be affected by
    signature / behavior changes.

      foo() — function (gitoma/foo.py:10)
        called from gitoma/bar.py:20, gitoma/baz.py:5
      _internal() — function (gitoma/foo.py:30)
        no callers in indexed scope.

Pure rendering. No I/O, no DB writes. Caller decides where to inject
the result string (the worker passes it via
``worker_user_prompt(extra_context_block=...)``).
"""

from __future__ import annotations

from gitoma.cpg._base import RefKind, SymbolKind
from gitoma.cpg.queries import CPGIndex

__all__ = ["render_blast_radius_block", "MAX_CALLERS_PER_SYMBOL"]


MAX_CALLERS_PER_SYMBOL = 5
"""Cap how many callers we list per symbol — keeps prompt size
predictable on heavily-used symbols (``LLMClient`` has 40+ callers
in gitoma; listing all of them would dwarf the patch context)."""

_RELEVANT_KINDS = frozenset({
    SymbolKind.FUNCTION, SymbolKind.CLASS, SymbolKind.METHOD,
    SymbolKind.ASSIGNMENT,
})


def render_blast_radius_block(
    file_paths: list[str], index: CPGIndex,
) -> str:
    """Render a BLAST RADIUS block for the given Python file paths.

    Returns ``""`` when the input has no Python files OR when no
    relevant symbols are found — the caller must check truthiness
    and skip injection in those cases (rather than emitting an
    empty block that the LLM would just ignore).

    Args:
        file_paths: repo-relative paths the worker is about to touch.
            Non-Python entries are silently skipped.
        index: the CPGIndex built once at run start.
    """
    py_files = [p for p in file_paths if p.endswith(".py")]
    if not py_files:
        return ""

    sections: list[str] = []
    for file in py_files:
        symbols = [
            s for s in index.get_symbols_in_file(file)
            if s.kind in _RELEVANT_KINDS and s.is_public
        ]
        if not symbols:
            continue
        lines = [f"\nModifying {file} touches the following defined symbols:"]
        for sym in symbols:
            callers = index.callers_of(sym.id)
            cross_file = [c for c in callers if c.file != file]
            kind_label = sym.kind.value
            lines.append(
                f"  {sym.name}() — {kind_label} ({sym.file}:{sym.line})"
            )
            if not cross_file:
                lines.append("    no cross-file callers in indexed scope.")
                continue
            shown = cross_file[:MAX_CALLERS_PER_SYMBOL]
            caller_strs = [
                f"{c.file}:{c.line}" for c in shown
            ]
            suffix = ""
            if len(cross_file) > MAX_CALLERS_PER_SYMBOL:
                suffix = f" (+{len(cross_file) - MAX_CALLERS_PER_SYMBOL} more)"
            lines.append(
                f"    called from {', '.join(caller_strs)}{suffix}"
            )
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "== BLAST RADIUS (CPG-lite) ==\n"
        "The CPG-lite index found callers / consumers of symbols "
        "defined in the files you are about to modify. Treat any "
        "signature change to a listed symbol as a cross-file edit: "
        "either preserve the signature OR also update every caller "
        "below. Removing or renaming a listed symbol without "
        "updating its callers WILL break the build."
    )
    return header + "\n" + "\n".join(sections)
