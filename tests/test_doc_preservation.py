"""Tests for G13 — ``validate_doc_preservation``.

Headline tests replay the EXACT three README destruction patterns
that have shipped in b2v PRs over 2026-04-23 → 2026-04-25:
  * #24/#26: bash code blocks deleted entirely
  * #27: bash blocks corrupted with literal ``\\n`` text instead
    of real newlines
"""

from __future__ import annotations

from pathlib import Path

from gitoma.worker.doc_preservation import (
    DOC_EXTENSIONS,
    _check_code_block_preservation,
    _check_literal_newline_corruption,
    _extract_fenced_blocks,
    validate_doc_preservation,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


# Sample original README with substantial bash code-block content,
# mirroring b2v's actual README.md structure.
_ORIG_README = """# b2v

A binary-to-video CLI.

## Encode (file to video)
```bash
b2v encode \\
  --input ./backup.iso \\
  --output ./backup_video.mkv \\
  --block-size 4 \\
  --codec ffv1
```

## Decode (video to file)
```bash
b2v decode \\
  --input ./backup_video.mkv \\
  --output ./restored_backup.iso
```
"""


# ── Headline replay: the three b2v destruction patterns ──────────────


def test_b2v_pr26_bash_blocks_deleted_caught(tmp_path: Path) -> None:
    """The exact b2v PR #26 regression (gemma-4-e4b): worker rewrote
    README dropping both bash code blocks entirely. Self-review caught
    it as a major finding but only AFTER the PR was opened. G13 must
    catch it BEFORE commit."""
    new = """# b2v

A binary-to-video CLI. See [docs/](docs/) for usage.
"""
    rel = _write(tmp_path, "README.md", new)
    result = validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    )
    assert result is not None
    path, msg = result
    assert path == "README.md"
    assert "fenced code-block content" in msg
    assert "100% loss" in msg


def test_b2v_pr27_literal_newline_corruption_caught(tmp_path: Path) -> None:
    """The exact b2v PR #27 regression (qwen3-8b): worker collapsed
    multi-line bash commands onto a single line with the literal text
    ``\\n`` between args (likely JSON-double-escape during patch
    generation). The bash block parses as Markdown but the command
    itself is broken. G13 spots the literal-newline pattern."""
    new = """# b2v

## Encode
```bash
b2v encode \\n  --input ./backup.iso \\n  --output ./backup_video.mkv \\n  --block-size 4 \\n  --codec ffv1
```
"""
    rel = _write(tmp_path, "README.md", new)
    result = validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    )
    assert result is not None
    path, msg = result
    assert path == "README.md"
    assert "literal '\\n' sequences" in msg


def test_b2v_pr24_partial_replace_caught(tmp_path: Path) -> None:
    """PR #24 variant: bash blocks replaced with prose + invented
    docs URLs. Loss is total (100%) — caught by the preservation
    check even though the new content adds different prose."""
    new = """# b2v

## Documentation
For details see:
- [Getting Started](https://b2v.github.io/docs/guide/getting-started.md)
- [Architecture](https://b2v.github.io/docs/guide/architecture.md)
"""
    rel = _write(tmp_path, "README.md", new)
    result = validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    )
    assert result is not None
    assert result[0] == "README.md"
    assert "fenced code-block content" in result[1]


# ── Clean cases (must NOT flag) ──────────────────────────────────────


def test_clean_minor_prose_change_passes(tmp_path: Path) -> None:
    """A small wording tweak that preserves all code blocks must
    pass clean. The whole point of the threshold (30% retention) is
    to allow legitimate doc cleanup."""
    new = _ORIG_README.replace("# b2v", "# b2v — Eternal Stream").replace(
        "A binary-to-video CLI.",
        "A binary-to-video CLI for offline backups.",
    )
    rel = _write(tmp_path, "README.md", new)
    assert validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    ) is None


def test_clean_consolidation_below_loss_threshold_passes(tmp_path: Path) -> None:
    """Consolidating two examples into one (50% loss in code-block
    chars) is legitimate cleanup — must pass. Threshold is 70% loss
    minimum to flag."""
    new = """# b2v

## Encode
```bash
b2v encode \\
  --input ./backup.iso \\
  --output ./backup_video.mkv \\
  --block-size 4 \\
  --codec ffv1
```

(Decode example removed — see `b2v decode --help`.)
"""
    rel = _write(tmp_path, "README.md", new)
    # 50% loss is below the 70% threshold → should pass
    assert validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    ) is None


def test_legitimate_single_literal_newline_in_block_passes(tmp_path: Path) -> None:
    """A code block that legitimately contains a SINGLE ``\\n`` (e.g.
    a regex example or a printf format string) must NOT trigger the
    literal-newline corruption check. Threshold is 2+ on the same
    line."""
    new = _ORIG_README + """

## Regex example
```python
import re
re.split(r'\\n', text)
```
"""
    rel = _write(tmp_path, "README.md", new)
    assert validate_doc_preservation(
        tmp_path, [rel], {rel: _ORIG_README},
    ) is None


# ── Silent-pass paths ────────────────────────────────────────────────


def test_no_doc_files_silent_pass(tmp_path: Path) -> None:
    """Touched files outside DOC_EXTENSIONS get skipped — G13 has
    no business looking at .py / .rs / .ts."""
    rel = _write(tmp_path, "src/main.py", "# moved everything elsewhere")
    assert validate_doc_preservation(
        tmp_path, [rel], {rel: "# def x(): pass\n```bash\nbig block\n```\n"},
    ) is None


def test_no_original_silent_pass(tmp_path: Path) -> None:
    """A doc file in touched but absent from originals (i.e. a
    CREATE, not a MODIFY) is out of scope — G13 only validates
    preservation, has nothing to compare against on a brand-new
    file."""
    new = "# brand new doc with no code blocks at all"
    rel = _write(tmp_path, "docs/intro.md", new)
    assert validate_doc_preservation(tmp_path, [rel], {}) is None


def test_missing_file_silent_pass(tmp_path: Path) -> None:
    """File in touched but absent on disk (deleted by a prior subtask)
    — silent pass, nothing to validate."""
    assert validate_doc_preservation(
        tmp_path, ["docs/gone.md"], {"docs/gone.md": "original"},
    ) is None


def test_empty_touched_is_noop(tmp_path: Path) -> None:
    assert validate_doc_preservation(tmp_path, [], {}) is None


def test_below_min_interesting_chars_silent_pass(tmp_path: Path) -> None:
    """A doc whose original code-block content is < 50 chars total
    is too small to judge. Removing 100% of a 10-char inline example
    is noise, not a regression."""
    orig_tiny = "# Doc\n\nUsage:\n```bash\nb2v -h\n```\n"
    new = "# Doc\n\nSee `b2v -h`.\n"
    rel = _write(tmp_path, "README.md", new)
    assert validate_doc_preservation(
        tmp_path, [rel], {rel: orig_tiny},
    ) is None


# ── First-violation short-circuit ────────────────────────────────────


def test_first_violation_short_circuits(tmp_path: Path) -> None:
    """Two bad doc files → return the FIRST listed in touched.
    Mirrors G2/G7/G10/G11/G12 shape."""
    a = _write(tmp_path, "README.md", "# stripped")
    b = _write(tmp_path, "docs/guide.md", "# stripped too")
    result = validate_doc_preservation(
        tmp_path, [a, b], {a: _ORIG_README, b: _ORIG_README},
    )
    assert result is not None
    assert result[0] == "README.md"


def test_one_clean_one_dirty_returns_dirty(tmp_path: Path) -> None:
    """Mix of grounded + ungrounded — only the dirty one flagged."""
    clean_new = _ORIG_README.replace("CLI.", "CLI tool.")
    dirty_new = "# stripped"
    a = _write(tmp_path, "README.md", clean_new)
    b = _write(tmp_path, "docs/guide.md", dirty_new)
    result = validate_doc_preservation(
        tmp_path, [a, b], {a: _ORIG_README, b: _ORIG_README},
    )
    assert result is not None
    assert result[0] == "docs/guide.md"


# ── Helper sanity ───────────────────────────────────────────────────


def test_extract_fenced_blocks_finds_multiple() -> None:
    """The regex captures consecutive blocks correctly without
    merging them (lazy quantifier)."""
    text = "intro\n```bash\nfoo\n```\nmiddle\n```python\nbar\n```\nend"
    blocks = _extract_fenced_blocks(text)
    assert blocks == ["foo", "bar"]


def test_extract_fenced_blocks_no_match_returns_empty() -> None:
    assert _extract_fenced_blocks("just prose") == []


def test_doc_extensions_includes_md_rst_mdx_txt() -> None:
    for ext in (".md", ".rst", ".mdx", ".txt"):
        assert ext in DOC_EXTENSIONS


def test_unit_check_preservation_returns_none_on_clean() -> None:
    """Direct unit on the helper — when retention is exactly at
    threshold (30%), still pass."""
    orig = "```bash\n" + ("x" * 100) + "\n```"
    new = "```bash\n" + ("x" * 30) + "\n```"
    assert _check_code_block_preservation("R", orig, new) is None


def test_unit_check_preservation_flags_below_threshold() -> None:
    """Just under threshold → flag."""
    orig = "```bash\n" + ("x" * 100) + "\n```"
    new = "```bash\n" + ("x" * 29) + "\n```"
    result = _check_code_block_preservation("R", orig, new)
    assert result is not None


def test_unit_literal_newline_check_legit_single_passes() -> None:
    """Single \\n in a code block (e.g. regex meta) must not flag."""
    new = "```python\nre.split(r'\\n', s)\n```"
    assert _check_literal_newline_corruption("R", new) is None


def test_unit_literal_newline_check_two_or_more_flags() -> None:
    """Two literal \\n on the same line → flag."""
    new = "```bash\nfoo \\n bar \\n baz\n```"
    result = _check_literal_newline_corruption("R", new)
    assert result is not None
    assert "literal '\\n'" in result[1]
