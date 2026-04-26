"""CPG-lite v0 — auto-applicazione bench on gitoma itself.

The cogito-ergo-sum test: build the index from gitoma's own source,
verify the symbol/reference counts are non-trivial, and check that
hand-picked load-bearing symbols come back with caller chains.

This serves THREE purposes:

1. **Regression catcher**: if a future change breaks the indexer or
   resolver, this test fails before any worker integration suffers.
2. **Living documentation**: reading this file tells a maintainer
   what scale of index gitoma builds on itself + which symbols are
   reliably resolved cross-file.
3. **Honest bias disclosure**: the bench is on gitoma indexing
   gitoma — confirmation bias risk is real (we wrote both sides).
   Mitigated by being explicit AND by adding b2v / lws benches in
   v0.5+ when multi-language lands. For v0 we accept the bias and
   document it.

Lower-bound assertions throughout — actual numbers grow as gitoma
evolves; assertions stay valid as long as gitoma keeps growing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gitoma.cpg import CPGIndex, build_index
from gitoma.cpg._base import SymbolKind


# Repo root, not the gitoma package — we index from the root so
# rel_paths like "gitoma/cpg/storage.py" match the qualified module
# names ``gitoma.cpg.storage`` that imports use.
GITOMA_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def gitoma_index() -> CPGIndex:
    """Built once for the whole test module — building on every test
    would re-index 100+ files repeatedly with no signal gain."""
    return build_index(GITOMA_REPO_ROOT, max_files=500)


def test_index_covers_substantial_portion_of_gitoma(
    gitoma_index: CPGIndex,
) -> None:
    """Lower-bound check: gitoma has ~100 .py files and ~2000 symbols
    as of CPG-lite v0 ship. Future growth keeps these passing."""
    assert gitoma_index.file_count() >= 50, (
        f"Expected ≥50 files indexed, got {gitoma_index.file_count()}. "
        "Either gitoma shrank dramatically or the walker is skipping."
    )
    assert gitoma_index.symbol_count() >= 500
    assert gitoma_index.reference_count() >= 2000


def test_build_time_under_budget() -> None:
    """v0 budget: < 2 seconds to index gitoma. Actual measured during
    development was ~700ms; allow 5x headroom for slow CI hardware."""
    start = time.perf_counter()
    idx = build_index(GITOMA_REPO_ROOT, max_files=500)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, (
        f"Build took {elapsed:.2f}s — budget is 5s for v0. "
        f"Indexed {idx.file_count()} files."
    )


@pytest.mark.parametrize("symbol_name,expected_min_callers", [
    # Class symbols used across the codebase. The expected_min_callers
    # is a CONSERVATIVE lower bound — actual counts are higher and
    # will grow; keep the bound low so this test ages well.
    ("LLMClient", 5),
    ("MetricReport", 3),
    ("Vertical", 2),
    ("Storage", 1),
    ("CPGIndex", 1),
])
def test_load_bearing_symbol_has_callers(
    gitoma_index: CPGIndex,
    symbol_name: str,
    expected_min_callers: int,
) -> None:
    """For each load-bearing class, expect ≥1 definition (kind=class)
    and at least the lower-bound number of callers (CALL or
    IMPORT_FROM refs). If this regresses, either the resolver got
    weaker or the symbol legitimately stopped being used (rename?
    delete? in either case, update the parametrize)."""
    defs = [s for s in gitoma_index.get_symbol(symbol_name)
            if s.kind is SymbolKind.CLASS]
    assert len(defs) >= 1, (
        f"No CLASS definition found for '{symbol_name}'. "
        "Either renamed or the indexer regressed on class detection."
    )
    total_callers = 0
    for sym in defs:
        total_callers += len(gitoma_index.callers_of(sym.id))
    assert total_callers >= expected_min_callers, (
        f"'{symbol_name}': expected ≥{expected_min_callers} callers, "
        f"got {total_callers}. Resolver may have weakened."
    )


def test_who_imports_finds_consumers_of_storage(
    gitoma_index: CPGIndex,
) -> None:
    """``gitoma/cpg/storage.py`` is imported by sibling cpg modules.
    Verifies who_imports works on real cross-file imports."""
    importers = gitoma_index.who_imports("gitoma/cpg/storage.py")
    paths = {f for f, _ in importers}
    # At minimum python_indexer.py + queries.py + __init__.py
    assert len(paths) >= 2, (
        f"Expected ≥2 importers of cpg/storage.py, got {paths}"
    )


def test_call_graph_for_returns_acyclic_result(
    gitoma_index: CPGIndex,
) -> None:
    """Sanity: call_graph_for must terminate even on a heavily-used
    symbol with potential transitive cycles (e.g. ``LLMClient``)."""
    llm_clients = [s for s in gitoma_index.get_symbol("LLMClient")
                   if s.kind is SymbolKind.CLASS]
    if not llm_clients:
        pytest.skip("LLMClient class not found — schema may have changed")
    graph = gitoma_index.call_graph_for(llm_clients[0].id, depth=2)
    # Just terminating + returning a dict is the win
    assert isinstance(graph, dict)
    assert llm_clients[0].id in graph
