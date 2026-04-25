"""Tests for G14 — ``validate_url_grounding``.

Headline test replays the exact b2v PR #24 + #27 fabricated-link
patterns: invented `b2v.github.io` hostname (DNS resolves via
GitHub Pages wildcard but HEAD returns 404) and invented
`docs/guide/code/encoder.md` relative paths.

Network-dependent paths are mocked to keep the suite hermetic
(monkeypatch ``_url_resolves``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitoma.worker.url_grounding import (
    DOC_EXTENSIONS,
    _extract_external_urls,
    _extract_link_targets,
    _is_private_ipv4,
    _is_skippable_link,
    _resolve_doc_link,
    validate_url_grounding,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


# ── Headline replay: b2v PR #24 + #27 ────────────────────────────────


def test_b2v_pr24_invented_github_io_hostname_caught(tmp_path: Path) -> None:
    """The exact PR #24 regression: a Markdown link to
    ``https://b2v.github.io/docs/...`` that DNS-resolves (wildcard)
    but HEAD-returns 404. Mock ``_url_resolves`` to simulate the
    HEAD-404 finding deterministically (no real network)."""
    orig = "# b2v\nA tool for backups.\n"
    new = (
        "# b2v\nA tool for backups.\n\n## Documentation\n"
        "- [Architecture](https://b2v.github.io/docs/architecture.md)\n"
    )
    rel = _write(tmp_path, "README.md", new)
    with patch(
        "gitoma.worker.url_grounding._url_resolves", return_value=False
    ):
        result = validate_url_grounding(tmp_path, [rel], {rel: orig})
    assert result is not None
    path, msg = result
    assert path == "README.md"
    assert "b2v.github.io" in msg
    assert "404" in msg or "does not resolve" in msg


def test_b2v_pr27_invented_relative_paths_caught(tmp_path: Path) -> None:
    """The exact PR #27 regression: a doc adds links to
    ``docs/guide/code/encoder.md`` etc. — files that don't exist
    in the repo. No network mock needed; pure filesystem."""
    orig = "# b2v\nIntro.\n"
    new = (
        "# b2v\nIntro.\n\n## Documentation\n"
        "- [Encoder](docs/guide/code/encoder.md)\n"
        "- [Decoder](docs/guide/code/decoder.md)\n"
    )
    rel = _write(tmp_path, "README.md", new)
    result = validate_url_grounding(tmp_path, [rel], {rel: orig})
    assert result is not None
    assert result[0] == "README.md"
    # Sorted iteration → decoder.md flags first (alphabetical before encoder)
    assert (
        "docs/guide/code/encoder.md" in result[1]
        or "docs/guide/code/decoder.md" in result[1]
    )


def test_pr27_path_when_file_exists_passes(tmp_path: Path) -> None:
    """If the target file actually exists in the repo (a previous
    subtask created it, or it was already there), the link is
    grounded — pass."""
    orig = "# b2v\nIntro.\n"
    new = (
        "# b2v\nIntro.\n\n## Documentation\n"
        "- [Architecture](docs/guide/architecture.md)\n"
    )
    # Create the file so the link grounds
    _write(tmp_path, "docs/guide/architecture.md", "# Arch\n")
    rel = _write(tmp_path, "README.md", new)
    assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


# ── Carry-over (already in original) is exempt ──────────────────────


def test_carry_over_url_exempt(tmp_path: Path) -> None:
    """A URL that was ALREADY in the original is not the worker's
    invention — must not flag, even if the URL itself is bad."""
    orig = "[bad](https://invented.example.does-not-exist)\n"
    new = "## Updated\n[bad](https://invented.example.does-not-exist)\n"
    rel = _write(tmp_path, "README.md", new)
    # Even with a mock that says "all URLs are bad", carry-over passes
    with patch(
        "gitoma.worker.url_grounding._url_resolves", return_value=False
    ):
        assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


def test_carry_over_path_exempt(tmp_path: Path) -> None:
    """Same exemption for relative-path links — if it was in the
    original (presumably broken pre-existing link), it's not a
    new regression."""
    orig = "[old](docs/missing.md)\n"
    new = "## Improved\n[old](docs/missing.md)\nNew text.\n"
    rel = _write(tmp_path, "README.md", new)
    assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


# ── Skippable link types ────────────────────────────────────────────


@pytest.mark.parametrize("target", [
    "#section",
    "#install",
    "mailto:foo@bar.com",
    "tel:+15551234567",
    "https://github.com",
    "http://example.com/page",
    "<https://wrapped.example>",
])
def test_skippable_links_pass(target: str) -> None:
    assert _is_skippable_link(target) is True


@pytest.mark.parametrize("target", [
    "docs/guide.md",
    "./relative.md",
    "../parent/file.md",
    "/abs/path.md",
])
def test_filesystem_links_not_skipped(target: str) -> None:
    assert _is_skippable_link(target) is False


def test_anchor_only_link_passes(tmp_path: Path) -> None:
    """In-page anchors don't need filesystem checks."""
    orig = "# Doc\nold body\n"
    new = "# Doc\nold body\n\nSee [intro](#intro) for details.\n"
    rel = _write(tmp_path, "README.md", new)
    assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


def test_mailto_link_passes(tmp_path: Path) -> None:
    orig = "# Doc\nold body\n"
    new = "# Doc\nContact [us](mailto:hello@example.com).\n"
    rel = _write(tmp_path, "README.md", new)
    assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


# ── Path resolution variants ────────────────────────────────────────


def test_resolve_doc_link_relative_to_doc(tmp_path: Path) -> None:
    """Markdown convention: ``[link](sibling.md)`` is relative to
    the doc file's directory."""
    _write(tmp_path, "docs/guide/sibling.md", "# sibling")
    doc = tmp_path / "docs" / "guide" / "main.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# main")
    assert _resolve_doc_link(tmp_path, doc, "sibling.md") is True


def test_resolve_doc_link_relative_to_root(tmp_path: Path) -> None:
    """VitePress / GitBook convention: ``[link](docs/x.md)`` is
    relative to repo root."""
    _write(tmp_path, "docs/x.md", "# x")
    doc = tmp_path / "README.md"
    doc.write_text("# README")
    assert _resolve_doc_link(tmp_path, doc, "docs/x.md") is True


def test_resolve_doc_link_strips_anchor(tmp_path: Path) -> None:
    """``foo.md#section`` should check existence of ``foo.md`` only."""
    _write(tmp_path, "docs/x.md", "# x\n## section\n")
    doc = tmp_path / "README.md"
    doc.write_text("# README")
    assert _resolve_doc_link(tmp_path, doc, "docs/x.md#section") is True


def test_resolve_doc_link_missing_returns_false(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# README")
    assert _resolve_doc_link(tmp_path, doc, "nope/missing.md") is False


# ── Extractors ──────────────────────────────────────────────────────


def test_extract_external_urls_basic() -> None:
    text = "See https://github.com and http://example.com/path?q=1."
    urls = _extract_external_urls(text)
    assert "https://github.com" in urls
    assert "http://example.com/path?q=1" in urls


def test_extract_external_urls_in_markdown_link() -> None:
    text = "Visit [GitHub](https://github.com)."
    urls = _extract_external_urls(text)
    assert "https://github.com" in urls


def test_extract_external_urls_strips_trailing_punct() -> None:
    """A URL at the end of a sentence should NOT include the period."""
    text = "See https://github.com."
    urls = _extract_external_urls(text)
    assert "https://github.com" in urls


def test_extract_link_targets() -> None:
    text = "[A](./a.md) [B](https://x.example) [C](#anchor) [D](docs/d.md)"
    targets = _extract_link_targets(text)
    assert "./a.md" in targets
    assert "https://x.example" in targets
    assert "#anchor" in targets
    assert "docs/d.md" in targets


# ── Silent-pass paths ───────────────────────────────────────────────


def test_no_doc_files_silent_pass(tmp_path: Path) -> None:
    rel = _write(tmp_path, "src/main.py", "x = 1")
    assert validate_url_grounding(
        tmp_path, [rel], {rel: "old content"},
    ) is None


def test_no_original_silent_pass(tmp_path: Path) -> None:
    """CREATE / not-captured docs aren't checked — only MODIFY."""
    rel = _write(tmp_path, "docs/new.md", "# brand new with [bad](https://nope.invalid)")
    assert validate_url_grounding(tmp_path, [rel], {}) is None


def test_missing_file_silent_pass(tmp_path: Path) -> None:
    assert validate_url_grounding(
        tmp_path, ["docs/gone.md"], {"docs/gone.md": "old"},
    ) is None


def test_empty_touched_is_noop(tmp_path: Path) -> None:
    assert validate_url_grounding(tmp_path, [], {}) is None


def test_offline_env_var_disables_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting GITOMA_URL_GROUNDING_OFFLINE skips ALL checks — for
    CI sandboxes / offline test envs."""
    monkeypatch.setenv("GITOMA_URL_GROUNDING_OFFLINE", "true")
    orig = "# old\n"
    new = "# old\n[broken](https://nope.invalid) [path](missing.md)\n"
    rel = _write(tmp_path, "README.md", new)
    assert validate_url_grounding(tmp_path, [rel], {rel: orig}) is None


# ── First-violation short-circuit ───────────────────────────────────


def test_first_violation_short_circuits(tmp_path: Path) -> None:
    """Two bad doc files → return the FIRST listed."""
    orig = "# orig\n"
    new = "[broken](missing.md)\n"
    a = _write(tmp_path, "README.md", new)
    b = _write(tmp_path, "docs/g.md", new)
    result = validate_url_grounding(
        tmp_path, [a, b], {a: orig, b: orig},
    )
    assert result is not None
    assert result[0] == "README.md"


# ── Helper unit tests ───────────────────────────────────────────────


@pytest.mark.parametrize("host,expected", [
    ("10.0.0.1", True),
    ("10.255.255.255", True),
    ("172.16.0.1", True),
    ("172.31.255.255", True),
    ("172.32.0.1", False),
    ("192.168.1.1", True),
    ("8.8.8.8", False),
    ("github.com", False),
    ("172.15.0.1", False),
])
def test_is_private_ipv4(host: str, expected: bool) -> None:
    assert _is_private_ipv4(host) == expected


def test_doc_extensions_includes_standard_extensions() -> None:
    for ext in (".md", ".rst", ".mdx", ".txt"):
        assert ext in DOC_EXTENSIONS
