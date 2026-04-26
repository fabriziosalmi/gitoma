"""Skeletal Representation v1 — bench artifact generator.

Run from repo root::

    python tests/bench/skeletal_v1/render_demo.py

Indexes gitoma's own source, renders the skeleton, dumps:
  * total chars (vs the 20000 default budget)
  * fit/truncation status
  * a head + tail sample of the rendered skeleton

The output is committed alongside this script so future maintainers
can diff after edits to the renderer / indexer / signature capture.
"""

from __future__ import annotations

import time
from pathlib import Path

from gitoma.cpg import build_index
from gitoma.cpg.skeletal import DEFAULT_MAX_CHARS, render_skeleton


REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_PATH = Path(__file__).parent / "render_demo_output.txt"


def main() -> None:
    print(f"Building CPG-lite index from {REPO_ROOT}…")
    t0 = time.perf_counter()
    idx = build_index(REPO_ROOT, max_files=500)
    build_ms = (time.perf_counter() - t0) * 1000

    print("Rendering skeleton…")
    t1 = time.perf_counter()
    skeleton = render_skeleton(idx, max_chars=DEFAULT_MAX_CHARS)
    render_ms = (time.perf_counter() - t1) * 1000

    char_count = len(skeleton)
    line_count = skeleton.count("\n") + 1
    truncated = "omitted" in skeleton
    file_count_in_skeleton = skeleton.count("\n## ")

    head = "\n".join(skeleton.split("\n")[:20])
    tail = "\n".join(skeleton.split("\n")[-15:])

    out = [
        "Skeletal Representation v1 — bench artifact",
        "=" * 64,
        "",
        f"Repo root:           {REPO_ROOT}",
        f"Files indexed:       {idx.file_count()}",
        f"Symbols emitted:     {idx.symbol_count()}",
        f"References:          {idx.reference_count()}",
        f"Index build time:    {build_ms:.0f}ms",
        f"Skeleton render time: {render_ms:.0f}ms",
        "",
        f"Skeleton chars:      {char_count} / {DEFAULT_MAX_CHARS} budget",
        f"Skeleton lines:      {line_count}",
        f"Files in skeleton:   {file_count_in_skeleton}",
        f"Truncated:           {truncated}",
        "",
        "=" * 64,
        "HEAD (first 20 lines):",
        "-" * 64,
        head,
        "",
        "=" * 64,
        "TAIL (last 15 lines):",
        "-" * 64,
        tail,
        "",
    ]
    OUTPUT_PATH.write_text("\n".join(out) + "\n")
    print(f"Wrote {len(out)} lines to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
