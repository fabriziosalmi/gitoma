"""CPG-lite v0.5-expansion — bench artifact generator.

Indexes b2v with the full v0.5-expansion stack (Python + TS + JS +
Rust). Compared to v0.5-slim (TS-only on b2v: 3 files / 14
symbols), the expansion picks up Rust source as well — a much
richer artifact for a Rust+JS repo.

Run from repo root::

    BENCH_REPO=/tmp/b2v_bench \\
    python tests/bench/cpg_lite_v05_expansion/index_demo.py
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

from gitoma.cpg import build_index
from gitoma.cpg.blast_radius import render_blast_radius_block


REPO_ROOT = Path(os.environ.get("BENCH_REPO", "/tmp/b2v_bench"))
OUTPUT_PATH = Path(__file__).parent / "index_demo_output.txt"


def main() -> None:
    if not REPO_ROOT.exists():
        raise SystemExit(
            f"Repo not found: {REPO_ROOT}. "
            "Clone b2v first or set BENCH_REPO."
        )

    print(f"Building CPG-lite (v0.5-expansion) from {REPO_ROOT}…")
    t0 = time.perf_counter()
    idx = build_index(REPO_ROOT, max_files=500)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Count symbols per language
    storage = idx._storage  # noqa: SLF001
    rows = storage._conn.execute(  # noqa: SLF001
        "SELECT language, COUNT(*) AS n FROM symbols GROUP BY language",
    ).fetchall()
    by_lang = {r["language"]: int(r["n"]) for r in rows}

    # Discover indexed files per language
    file_rows = storage._conn.execute(  # noqa: SLF001
        "SELECT DISTINCT file, language FROM symbols ORDER BY file",
    ).fetchall()
    files_by_lang: dict[str, list[str]] = {}
    for r in file_rows:
        files_by_lang.setdefault(str(r["language"]), []).append(str(r["file"]))

    lines: list[str] = []
    lines.append("CPG-lite v0.5-expansion — bench artifact (b2v multi-language)")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Repo root:        {REPO_ROOT}")
    lines.append(f"Files indexed:    {idx.file_count()}")
    lines.append(f"Symbols emitted:  {idx.symbol_count()}")
    lines.append(f"References:       {idx.reference_count()}")
    lines.append(f"Build time:       {elapsed_ms:.0f}ms")
    lines.append("")
    lines.append("Symbols per language:")
    for lang, n in sorted(by_lang.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {lang:14s} {n:5d}")
    lines.append("")
    lines.append("Files per language:")
    for lang, fs in sorted(files_by_lang.items()):
        lines.append(f"  {lang:14s} ({len(fs)} files)")
        for f in fs[:5]:
            lines.append(f"    - {f}")
        if len(fs) > 5:
            lines.append(f"    … (+{len(fs) - 5} more)")
    lines.append("")
    lines.append("=" * 64)
    lines.append("Sample BLAST RADIUS for a Rust source file (first 1):")
    lines.append("-" * 64)
    rust_files = [f for f, lang in [(r["file"], r["language"])
                                     for r in file_rows]
                  if lang == "rust"]
    if rust_files:
        sample = rust_files[0]
        lines.append("")
        block = render_blast_radius_block([sample], idx)
        lines.append(block if block else "(no blast radius — file has no public defining symbols)")
    else:
        lines.append("(no Rust files indexed in this repo)")
    lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} lines to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
