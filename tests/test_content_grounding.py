"""Tests for G11 — ``validate_content_grounding``.

Headline test: ``test_b2v_pr21_react_in_rust_repo_caught`` replays the
exact hallucination that motivated the guard — a generated
``architecture.md`` claiming React+Redux in a pure-Rust CLI repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.content_grounding import (
    DOC_EXTENSIONS,
    DOC_FRAMEWORK_PATTERNS,
    validate_content_grounding,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


def _rust_cli_fingerprint() -> dict:
    """Fingerprint matching b2v's actual stack (Rust CLI, no JS)."""
    return {
        "languages": [{"name": "Rust", "files": 47}],
        "stack": ["rust/cargo"],
        "declared_frameworks": ["clap", "serde"],
        "declared_deps": {
            "rust": ["clap", "serde", "tokio", "anyhow"],
            "npm": [],
            "python": [],
            "go": [],
        },
        "entrypoints": ["src/main.rs"],
        "manifest_files": ["Cargo.toml"],
    }


# ── The b2v PR #21 headline case ──────────────────────────────────────


def test_b2v_pr21_react_in_rust_repo_caught(tmp_path: Path) -> None:
    """The exact hallucination gitoma shipped in b2v PR #21: a generated
    ``architecture.md`` claimed React+Redux+WebSocket frontend for a
    Rust CLI. Both pass JSON/YAML/TOML/Python parsers (G2 silent),
    structural guards (G7 N/A — not a Python file), and schema check
    (G10 N/A — not a known config). G11 grounds it via fingerprint."""
    bad = """# Architecture

The frontend is built with React and Redux for state management.
WebSocket connections push live updates from the backend.
"""
    rel = _write(tmp_path, "docs/architecture.md", bad)
    result = validate_content_grounding(tmp_path, [rel], _rust_cli_fingerprint())
    assert result is not None
    path, msg = result
    assert path == "docs/architecture.md"
    assert "react" in msg.lower() or "redux" in msg.lower()
    # error message should cite the actually-declared frameworks
    assert "clap" in msg


def test_grounded_doc_passes(tmp_path: Path) -> None:
    """An architecture doc that ONLY mentions frameworks present in
    declared_frameworks/deps must validate clean."""
    good = """# Architecture

The CLI is built with Clap for argument parsing and Serde for
serialization. Tokio handles async runtime where needed.
"""
    rel = _write(tmp_path, "docs/architecture.md", good)
    assert validate_content_grounding(tmp_path, [rel], _rust_cli_fingerprint()) is None


# ── Pattern coverage — high-leverage frameworks individually ───────────


@pytest.mark.parametrize("framework_mention,expected_id", [
    ("React",     "react"),
    ("React.js",  "react"),
    ("Vue 3",     "vue"),
    ("Angular",   "angular"),
    ("SvelteKit", "svelte"),
    ("Next.js",   "next"),
    ("Redux",     "redux"),
    ("Express",   "express"),
    ("Django",    "django"),
    ("FastAPI",   "fastapi"),
    ("Tailwind",  "tailwindcss"),
    ("MUI",       "mui"),
    ("Gin",       "gin"),
    ("Cobra",     "cobra"),
    ("Actix-web", "actix"),
    ("Rocket",    "rocket"),
])
def test_pattern_matches(tmp_path: Path, framework_mention: str, expected_id: str) -> None:
    """Each framework keyword in a doc, against a Rust-CLI fingerprint
    that doesn't declare it, must trigger the guard."""
    body = f"This project uses {framework_mention} for production."
    rel = _write(tmp_path, "README.md", body)
    result = validate_content_grounding(tmp_path, [rel], _rust_cli_fingerprint())
    assert result is not None, f"failed to flag mention of {framework_mention!r}"
    assert expected_id in result[1]


# ── Silent-pass paths ─────────────────────────────────────────────────


def test_no_fingerprint_silent_pass(tmp_path: Path) -> None:
    """Occam disabled / unreachable → fingerprint is None → guard
    does nothing. No false positives in offline mode."""
    rel = _write(tmp_path, "docs/x.md", "We use React + Redux + Vue.")
    assert validate_content_grounding(tmp_path, [rel], None) is None
    assert validate_content_grounding(tmp_path, [rel], {}) is None


def test_no_manifests_silent_pass(tmp_path: Path) -> None:
    """Greenfield repo (no detected manifests) → fingerprint can't
    ground anything → guard does nothing. Avoids punishing brand-new
    repos that don't yet have a Cargo.toml/package.json."""
    rel = _write(tmp_path, "docs/x.md", "Built on React.")
    fp = {"manifest_files": [], "declared_frameworks": [], "declared_deps": {}}
    assert validate_content_grounding(tmp_path, [rel], fp) is None


def test_non_doc_extension_silent_pass(tmp_path: Path) -> None:
    """``.py``, ``.go``, ``.rs`` etc. fall outside G11's scope —
    code grounding is the responsibility of G7/G8/build, not G11."""
    rel = _write(tmp_path, "src/main.py", "# uses React in comments")
    assert validate_content_grounding(tmp_path, [rel], _rust_cli_fingerprint()) is None


def test_missing_file_silent_pass(tmp_path: Path) -> None:
    """Touched-but-deleted paths shouldn't crash the guard."""
    assert validate_content_grounding(tmp_path, ["docs/gone.md"], _rust_cli_fingerprint()) is None


def test_empty_touched_is_noop(tmp_path: Path) -> None:
    assert validate_content_grounding(tmp_path, [], _rust_cli_fingerprint()) is None


def test_doc_with_no_framework_mentions_passes(tmp_path: Path) -> None:
    rel = _write(tmp_path, "docs/install.md", "Run `cargo build` then `./target/release/binary`.")
    assert validate_content_grounding(tmp_path, [rel], _rust_cli_fingerprint()) is None


# ── Substring grounding — covers npm-scope variants ───────────────────


def test_redux_grounded_via_reduxjs_toolkit_dep(tmp_path: Path) -> None:
    """``@reduxjs/toolkit`` is the modern Redux package. A doc that
    mentions ``Redux`` should ground against it via the substring
    fallback (the dep name CONTAINS the framework id)."""
    rel = _write(tmp_path, "README.md", "State management is handled by Redux.")
    fp = {
        "manifest_files": ["package.json"],
        "declared_frameworks": [],   # not in canonical map for this dep
        "declared_deps": {
            "npm": ["@reduxjs/toolkit", "react", "react-dom"],
            "rust": [], "python": [], "go": [],
        },
    }
    assert validate_content_grounding(tmp_path, [rel], fp) is None


def test_framework_in_declared_passes(tmp_path: Path) -> None:
    """Direct framework match in declared_frameworks — the simplest
    grounding path. Fastapi mentioned + fastapi declared → clean."""
    rel = _write(tmp_path, "README.md", "Built on FastAPI and Pydantic.")
    fp = {
        "manifest_files": ["pyproject.toml"],
        "declared_frameworks": ["fastapi", "pydantic"],
        "declared_deps": {"python": ["fastapi", "pydantic"], "rust": [], "npm": [], "go": []},
    }
    assert validate_content_grounding(tmp_path, [rel], fp) is None


# ── First-violation short-circuit ─────────────────────────────────────


def test_first_violation_short_circuits(tmp_path: Path) -> None:
    """When two touched docs both have grounding issues, the FIRST
    one in the touched list is returned — consistent with G2/G7/G10."""
    bad1 = "# Doc 1\nUses React for UI."
    bad2 = "# Doc 2\nUses Vue for UI."
    rel1 = _write(tmp_path, "docs/a.md", bad1)
    rel2 = _write(tmp_path, "docs/b.md", bad2)
    result = validate_content_grounding(tmp_path, [rel1, rel2], _rust_cli_fingerprint())
    assert result is not None
    assert result[0] == "docs/a.md"   # first input → first reported


def test_one_clean_one_dirty_returns_dirty(tmp_path: Path) -> None:
    """A mix of grounded + ungrounded docs returns ONLY the ungrounded
    one — clean docs don't suppress real violations."""
    clean = "# Architecture\nBuilt with Clap."
    dirty = "# Plans\nWill add React frontend later."
    rel_clean = _write(tmp_path, "docs/arch.md", clean)
    rel_dirty = _write(tmp_path, "docs/plans.md", dirty)
    result = validate_content_grounding(
        tmp_path, [rel_clean, rel_dirty], _rust_cli_fingerprint(),
    )
    assert result is not None
    assert result[0] == "docs/plans.md"


# ── Constants sanity ──────────────────────────────────────────────────


def test_doc_extensions_includes_md_rst() -> None:
    """The two extensions we care about most — ``.md`` is what every
    Markdown-flavoured generator outputs, ``.rst`` covers Sphinx
    docs that are still common in older Python repos."""
    assert ".md" in DOC_EXTENSIONS
    assert ".rst" in DOC_EXTENSIONS


def test_doc_framework_patterns_non_empty() -> None:
    """Sanity: the pattern map isn't accidentally empty after a
    refactor. We rely on it for every grounding decision."""
    assert len(DOC_FRAMEWORK_PATTERNS) >= 30
