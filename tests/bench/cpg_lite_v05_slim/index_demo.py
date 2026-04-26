"""CPG-lite v0.5-slim — bench artifact generator (TypeScript).

Run from repo root::

    python tests/bench/cpg_lite_v05_slim/index_demo.py

Indexes a sibling clone of b2v (passed via env var BENCH_REPO or
defaulting to /tmp/b2v_bench) and dumps the BLAST RADIUS for each
TS file present. The script is committed; the OUTPUT is committed
alongside so future maintainers can diff after edits to the TS
indexer or resolver.

NOT an LLM A/B — same caveat as the v0 bench. This artifact is a
"signal-density review": the LLM-side question answered by the
output is "would this block be useful in a worker prompt for a TS
file edit?".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from gitoma.cpg import build_index
from gitoma.cpg.blast_radius import render_blast_radius_block


REPO_ROOT = Path(os.environ.get("BENCH_REPO", "/tmp/b2v_bench"))
OUTPUT_PATH = Path(__file__).parent / "index_demo_output.txt"


def main() -> None:
    if not REPO_ROOT.exists():
        raise SystemExit(
            f"Repo not found: {REPO_ROOT}. "
            "Clone b2v first or set BENCH_REPO env var."
        )

    print(f"Building CPG-lite index from {REPO_ROOT}…")
    t0 = time.perf_counter()
    idx = build_index(REPO_ROOT, max_files=500)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Discover the .ts/.tsx files we ended up indexing so the bench
    # is self-describing.
    ts_files: list[str] = []
    for sym in [s for s in idx.get_symbol("") + []]:
        pass  # placeholder — get_symbol("") returns nothing useful
    # Walk symbols table indirectly: we can use file_count, but to
    # list TS files we walk the FS (cheap, < 200 files).
    for entry in REPO_ROOT.rglob("*"):
        if entry.is_file() and entry.suffix in (".ts", ".tsx"):
            rel = entry.relative_to(REPO_ROOT).as_posix()
            if any(part.startswith(".") or part in (
                "node_modules", "target", "dist", "build",
            ) for part in rel.split("/")):
                continue
            ts_files.append(rel)

    lines: list[str] = []
    lines.append("CPG-lite v0.5-slim — bench artifact (b2v multi-language)")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Repo root:        {REPO_ROOT}")
    lines.append(f"Files indexed:    {idx.file_count()}")
    lines.append(f"Symbols emitted:  {idx.symbol_count()}")
    lines.append(f"References:       {idx.reference_count()}")
    lines.append(f"Build time:       {elapsed_ms:.0f}ms")
    lines.append("")
    lines.append(f"TypeScript files discovered: {len(ts_files)}")
    for f in ts_files:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append("Each section below shows the BLAST RADIUS the worker")
    lines.append("would see for a subtask touching that TS file.")
    lines.append("")
    lines.append("=" * 64)

    for target in ts_files:
        lines.append("")
        lines.append(f"### TARGET: {target}")
        lines.append("")
        block = render_blast_radius_block([target], idx)
        if not block:
            lines.append("(no BLAST RADIUS — file empty / private-only / "
                         "no public exports)")
        else:
            lines.append(block)
        lines.append("")
        lines.append("-" * 64)

    OUTPUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} lines to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
