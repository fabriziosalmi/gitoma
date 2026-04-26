"""CPG-lite v0 — bench artifact generator.

Run from repo root::

    python tests/bench/cpg_lite_v0/index_demo.py

Builds the index against gitoma's own source, dumps build stats and
a BLAST RADIUS rendering for 4 hand-picked load-bearing modules into
``index_demo_output.txt``. The output file is committed alongside
this script so future maintainers can diff it after edits to the
indexer or resolver.

This is NOT an A/B bench against an LLM — that requires sample size
+ budget + cache (deferred to v0.1). What this IS: a deterministic
"signal-density review" — the LLM-side question answered by the
output is "would this block be useful in a worker prompt?".
"""

from __future__ import annotations

import time
from pathlib import Path

from gitoma.cpg import build_index
from gitoma.cpg.blast_radius import render_blast_radius_block


REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_PATH = Path(__file__).parent / "index_demo_output.txt"

TARGETS = [
    "gitoma/planner/llm_client.py",
    "gitoma/cpg/storage.py",
    "gitoma/verticals/_base.py",
    "gitoma/cli/commands/run.py",
]


def main() -> None:
    print(f"Building CPG-lite index from {REPO_ROOT}…")
    t0 = time.perf_counter()
    idx = build_index(REPO_ROOT, max_files=500)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    lines: list[str] = []
    lines.append("CPG-lite v0 — bench artifact (auto-applicazione su gitoma)")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Repo root:        {REPO_ROOT}")
    lines.append(f"Files indexed:    {idx.file_count()}")
    lines.append(f"Symbols emitted:  {idx.symbol_count()}")
    lines.append(f"References:       {idx.reference_count()}")
    lines.append(f"Build time:       {elapsed_ms:.0f}ms")
    lines.append("")
    lines.append("Targets sampled below were chosen as load-bearing modules:")
    lines.append("planner LLM client, CPG storage, vertical config, run CLI.")
    lines.append("Each section shows what BLAST RADIUS the worker would see")
    lines.append("if the planner sent it a subtask touching that file.")
    lines.append("")
    lines.append("=" * 64)

    for target in TARGETS:
        lines.append("")
        lines.append(f"### TARGET: {target}")
        lines.append("")
        block = render_blast_radius_block([target], idx)
        if not block:
            lines.append("(no BLAST RADIUS — file empty / private-only / "
                         "not indexed)")
        else:
            lines.append(block)
        lines.append("")
        lines.append("-" * 64)

    OUTPUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} lines to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
