"""Tests for the DocsAnalyzer doc-tool detection — expanded
2026-04-26 to cover Jekyll/VitePress/Hugo/GitBook etc. so the
planner doesn't propose parallel doc systems on top of existing
ones (the lws MkDocs-on-Jekyll hallucination)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.analyzers.docs import DocsAnalyzer


def _run_on(tmp_path: Path, languages: list[str] | None = None) -> tuple[float, str, list[str]]:
    """Instantiate the analyzer on a tmp_path and return
    (score, details, suggestions) for inspection."""
    a = DocsAnalyzer(root=tmp_path, languages=languages or [])
    r = a.analyze()
    return r.score, r.details, list(r.suggestions)


def _touch(root: Path, rel: str, body: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ── Detection coverage ──────────────────────────────────────────────


@pytest.mark.parametrize("filename,tool", [
    ("mkdocs.yml",                   "MkDocs"),
    ("mkdocs.yaml",                  "MkDocs"),
    ("docs/conf.py",                 "Sphinx"),
    ("conf.py",                      "Sphinx"),
    ("docusaurus.config.js",         "Docusaurus"),
    ("docusaurus.config.ts",         "Docusaurus"),
    ("docs/.vitepress/config.ts",    "VitePress"),
    ("docs/.vitepress/config.mjs",   "VitePress"),
    (".vitepress/config.js",         "VitePress"),
    ("docs/_config.yml",             "Jekyll"),
    ("_config.yml",                  "Jekyll"),
    ("hugo.toml",                    "Hugo"),
    ("hugo.yaml",                    "Hugo"),
    ("book.json",                    "GitBook"),
    (".gitbook.yaml",                "GitBook"),
    ("astro.config.mjs",             "Astro"),
    ("theme.config.tsx",             "Nextra"),
])
def test_doc_tool_detected(tmp_path: Path, filename: str, tool: str) -> None:
    """Each known doc-tool config gets correctly identified by name
    in the analyzer details + a dedicated suggestion warns the
    planner against parallel installs."""
    _touch(tmp_path, filename, "config")
    score, details, suggestions = _run_on(tmp_path)
    assert tool in details, f"Expected '{tool}' in details: {details!r}"
    # And the warning suggestion explicitly cites the tool
    sug_text = " ".join(suggestions)
    assert tool in sug_text
    assert "do NOT propose adding a parallel doc system" in sug_text


def test_lws_jekyll_scenario(tmp_path: Path) -> None:
    """Replay the lws case: docs/_config.yml exists (Jekyll on
    GitHub Pages). The analyzer must mention Jekyll and warn
    against MkDocs/Sphinx parallel installs. This is what closes
    the planner hallucination from 2026-04-26."""
    _touch(tmp_path, "docs/_config.yml", "theme: minima\n")
    _touch(tmp_path, "docs/index.md", "# Hello\n")
    score, details, suggestions = _run_on(tmp_path)
    assert "Jekyll" in details
    sug = " ".join(suggestions)
    assert "Jekyll" in sug
    assert "parallel" in sug.lower()


def test_no_doc_tool_falls_back_to_suggestion(tmp_path: Path) -> None:
    """Repo with NO doc tool gets the standard 'consider adding
    one' suggestion — but it lists multiple tools (not just
    MkDocs) so the planner has options."""
    _touch(tmp_path, "src/main.py", "x = 1")
    _, details, suggestions = _run_on(tmp_path)
    # No tool detected — details should NOT mention any
    for tool in ("MkDocs", "VitePress", "Jekyll", "Hugo", "GitBook"):
        assert tool not in details
    # Suggestion mentions at least the main contenders
    sug = " ".join(suggestions)
    assert "MkDocs" in sug or "Sphinx" in sug or "VitePress" in sug


def test_multiple_tools_detected_listed_together(tmp_path: Path) -> None:
    """Edge case: a repo migrated from Jekyll to VitePress might
    still have the legacy _config.yml. List both — the planner /
    operator can decide which to keep."""
    _touch(tmp_path, "docs/_config.yml", "")           # Jekyll legacy
    _touch(tmp_path, "docs/.vitepress/config.ts", "")  # VitePress current
    _, details, suggestions = _run_on(tmp_path)
    assert "Jekyll" in details
    assert "VitePress" in details


def test_repo_with_only_docs_dir_no_tool(tmp_path: Path) -> None:
    """Plain Markdown in docs/ without any builder config — gets
    the docs/ directory bonus but the doc-tool suggestion still
    fires (because no builder = no published site)."""
    _touch(tmp_path, "docs/intro.md", "# intro")
    _, details, suggestions = _run_on(tmp_path)
    assert "docs/" in details
    sug = " ".join(suggestions)
    assert "MkDocs" in sug or "Sphinx" in sug or "VitePress" in sug
