"""G13 — Doc-preservation guard against fenced-code-block destruction.

The recurring failure mode across b2v PRs #24/#26/#27 (and earlier
PR #22 manual-fix-pre-merge) was the SAME README file getting its
fenced bash examples either deleted, replaced with prose, or
corrupted with literal escape sequences. Every model variant
(gemma-e2b/e4b, qwen3-8b) produced one shape of this regression in
3 of 4 PRs. Self-review caught it 1 of 4 times.

The structural guards (G2/G6 syntax, G7 AST-diff for Python,
G10/G12 schema/config grounding, G11 framework grounding) all skip
plain Markdown / RST / TXT prose. The destruction has to be caught
by something that knows about CODE BLOCK CONTENT inside docs.

G13 is that something. Two deterministic checks:

  1. **Code-block character preservation** — a MODIFY on a doc file
     that loses ≥70% of fenced-code-block content (by character
     count) is almost certainly a regression. Catches the b2v #26
     and #24 cases where bash examples get deleted entirely.

  2. **Literal ``\\n`` corruption** — a fenced code block with a
     line containing ≥2 literal ``\\n`` sequences (backslash-n as
     plain text, NOT newlines) is the qwen3-8b PR #27 bug:
     multi-line bash collapsed onto a single line with the
     line-continuation marker emitted as JSON-escape text rather
     than real newlines.

Architecture mirrors G7 (top-level def preservation):
  * Reads the BEFORE-write content from the ``originals`` dict
    captured by ``read_modify_originals``. CREATE / DELETE paths
    have no original → silent pass.
  * Runs both checks per file, returns first violation as
    ``(rel_path, message)``. Caller reverts + retries with the
    message injected as feedback.
  * No LLM, no parsing libraries, no network. Pure-string +
    re.findall.

Out of scope for v1 (deferred):
  * URL/path reachability (added doc URLs that don't resolve, or
    cite local files that don't exist) — G14 candidate, requires
    DNS or filesystem checks.
  * Prose content drift (paragraphs replaced with vapid summaries)
    — too subjective for a deterministic guard.
  * HTML / Markdown structural validation — outside scope; treat
    docs as "text with code blocks", not as fully-parsed Markdown.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "validate_doc_preservation",
    "DOC_EXTENSIONS",
]


DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})


# Match a fenced code block: ``` optionally with lang tag, newline,
# body (any chars including newlines, lazy), newline, ``` . The
# (.*?) is greedy-minimal so consecutive blocks don't merge.
_FENCE_BLOCK_RE = re.compile(r"```[\w-]*\n(.*?)\n```", re.S)

# Threshold under which ``orig_chars`` is too small to bother
# checking — a 30-char inline example getting replaced is noise,
# not a bash-block deletion.
_MIN_INTERESTING_CHARS = 50

# When new code-block content drops below this fraction of the
# original, flag as a destruction. 0.3 = "kept 30% or more is OK,
# below that is a regression." Picked conservatively: most
# legitimate doc cleanups consolidate examples but don't strip
# them wholesale.
_PRESERVATION_THRESHOLD = 0.3

# Inside a fenced code block, a single line containing ``2+``
# literal ``\n`` text occurrences is the qwen3-8b PR #27
# corruption signature: a multi-line bash command emitted by the
# worker with JSON-escape ``\\n`` (which json.loads decodes to the
# literal 2-char ``\n`` text) instead of actual newlines. Catches
# it without false-positives on legit single-`\n` usage (e.g. a
# regex example showing ``\n`` as a metacharacter).
_LITERAL_NEWLINE_THRESHOLD = 2


def _extract_fenced_blocks(text: str) -> list[str]:
    """Return list of code-block BODIES (between ```...``` fences).
    Lang tag is consumed but not returned. Empty list on no match."""
    return _FENCE_BLOCK_RE.findall(text)


def _check_code_block_preservation(
    rel: str, orig: str, new: str
) -> tuple[str, str] | None:
    """Flag when the new content has < ``_PRESERVATION_THRESHOLD``
    of the original's fenced-code-block content (by char count).
    Silent pass when the original had < ``_MIN_INTERESTING_CHARS``
    in code blocks — too small to judge without false positives."""
    orig_blocks = _extract_fenced_blocks(orig)
    new_blocks = _extract_fenced_blocks(new)
    orig_chars = sum(len(b) for b in orig_blocks)
    new_chars = sum(len(b) for b in new_blocks)
    if orig_chars < _MIN_INTERESTING_CHARS:
        return None
    if new_chars >= orig_chars * _PRESERVATION_THRESHOLD:
        return None
    loss_pct = int((1 - new_chars / orig_chars) * 100)
    return (
        rel,
        f"removed {orig_chars - new_chars} chars of fenced code-block content "
        f"({orig_chars} → {new_chars}, {loss_pct}% loss). Doc modify must "
        f"preserve runnable examples and code samples — re-emit the patch "
        f"keeping every original ``` ```...``` ``` block intact, modifying "
        f"ONLY the surrounding prose if needed."
    )


def _check_literal_newline_corruption(
    rel: str, new: str
) -> tuple[str, str] | None:
    """Flag when ANY fenced block in the new content contains a
    line with ``_LITERAL_NEWLINE_THRESHOLD`` or more literal ``\\n``
    text occurrences. That's the JSON-double-escape corruption
    signature: the worker emitted ``\\\\n`` in JSON, which decodes
    to the 2-char literal ``\\n`` instead of a real newline."""
    blocks = _extract_fenced_blocks(new)
    for i, body in enumerate(blocks):
        for line in body.split("\n"):
            count = line.count("\\n")
            if count >= _LITERAL_NEWLINE_THRESHOLD:
                snippet = line.strip()[:80]
                return (
                    rel,
                    f"fenced code block #{i+1} contains a line with {count} "
                    f"literal '\\n' sequences (snippet: {snippet!r}). This is "
                    f"almost certainly an escape-sequence corruption — multi-"
                    f"line content collapsed onto a single line with '\\n' "
                    f"text instead of real newlines. Re-emit the patch with "
                    f"actual newline characters in the code block."
                )
    return None


def validate_doc_preservation(
    root: Path,
    touched: list[str],
    originals: dict[str, str],
) -> tuple[str, str] | None:
    """Validate every touched doc file against its original (when
    captured) for two destruction patterns. Returns ``(rel_path,
    message)`` on first violation, ``None`` on clean. Silent pass
    when:

      * file extension not in ``DOC_EXTENSIONS``
      * no original content (CREATE / DELETE / not-a-modify)
      * file doesn't exist on disk
      * unreadable
      * original code-block content < ``_MIN_INTERESTING_CHARS``
      * preservation ratio above threshold AND no literal-newline
        corruption found

    Both checks are deterministic — no LLM, no network, no parsing
    libraries beyond stdlib re.
    """
    for rel in touched:
        full = root / rel
        if full.suffix.lower() not in DOC_EXTENSIONS:
            continue
        original_content = originals.get(rel)
        if original_content is None:
            # CREATE / DELETE / not captured — preservation N/A.
            continue
        if not full.is_file():
            continue
        try:
            new_content = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        result = _check_code_block_preservation(rel, original_content, new_content)
        if result is not None:
            return result
        result = _check_literal_newline_corruption(rel, new_content)
        if result is not None:
            return result
    return None
