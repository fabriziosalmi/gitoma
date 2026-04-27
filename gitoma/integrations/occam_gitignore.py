"""occam-gitignore integration — deterministic .gitignore generation.

Wraps :mod:`occam_gitignore_core` (https://pypi.org/project/occam-gitignore/)
to produce hash-verifiable, byte-deterministic ``.gitignore`` content
for any repo. **No LLM** — the core is a pure function of
``(file_tree, options, templates_version, rules_table_version)``.

Used by the ``gitoma gitignore <repo>`` CLI command (see
:mod:`gitoma.cli.commands.gitignore`) to produce drift-fixing PRs
without involving the worker apply pipeline. **First "deterministic
vertical"** — the pattern future integrations (semgrep, reuse-tool,
license-checker) will follow.

Optional dependency: ``occam-gitignore-core`` is NOT in gitoma's
required deps. When missing, :func:`is_available` returns False and
the CLI command exits with a clean install prompt — never crashes
the worker pipeline (which doesn't touch this integration).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "is_available",
    "version_info",
    "generate_for_repo",
    "OccamGitignoreUnavailable",
    "GenerateResult",
    "DEFAULT_TEMPLATES_DIR",
    "DEFAULT_RULES_TABLE",
]


# Repository-relative paths to the data files shipped by
# occam-gitignore. The package itself doesn't bundle them — they live
# in the source repo's ``data/`` directory. Callers can override via
# the ``OCCAM_GITIGNORE_DATA_DIR`` env var (matching the env var the
# upstream MCP/HTTP servers honor).
_DEFAULT_LOCAL_REPO = "/Users/fab/Documents/git/gitignore/occam-gitignore"
DEFAULT_TEMPLATES_DIR = Path(_DEFAULT_LOCAL_REPO) / "data" / "templates"
DEFAULT_RULES_TABLE = Path(_DEFAULT_LOCAL_REPO) / "data" / "rules_table.json"


# Walker prunes match other CPG-lite skip lists; keep aligned so a
# future user expects the same exclusions across both tools.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    "vendor",
})


class OccamGitignoreUnavailable(Exception):
    """Raised when ``occam-gitignore-core`` cannot be imported OR
    the data files (templates / rules table) aren't reachable. The
    CLI command catches this and prints an install prompt."""


@dataclass(frozen=True)
class GenerateResult:
    """One ``.gitignore`` generation outcome."""

    content: str
    content_hash: str  # ``sha256:<digest>``
    features: tuple[str, ...]  # detected feature names, sorted
    evidence: tuple[tuple[str, str], ...]  # (feature, evidence_path)
    core_version: str
    templates_version: str
    rules_table_version: str


# ── Availability + version probes ─────────────────────────────────


def _resolve_data_dir() -> tuple[Path, Path]:
    """Return ``(templates_dir, rules_table_path)``. Honors
    ``OCCAM_GITIGNORE_DATA_DIR`` env var (same shape as upstream)."""
    env = os.environ.get("OCCAM_GITIGNORE_DATA_DIR")
    if env:
        base = Path(env)
        return base / "templates", base / "rules_table.json"
    return DEFAULT_TEMPLATES_DIR, DEFAULT_RULES_TABLE


def is_available() -> bool:
    """Return True when occam-gitignore-core is importable AND the
    data files exist."""
    try:
        import occam_gitignore_core  # noqa: F401
    except ImportError:
        return False
    templates_dir, rules_path = _resolve_data_dir()
    return templates_dir.is_dir() and rules_path.is_file()


def version_info() -> dict[str, str] | None:
    """Return ``{"core": ..., "templates": ..., "rules_table": ...}``
    or None when unavailable."""
    if not is_available():
        return None
    try:
        from occam_gitignore_core import (
            CORE_VERSION,
            FileSystemTemplateRepository,
            JsonRulesTable,
        )
    except ImportError:
        return None
    templates_dir, rules_path = _resolve_data_dir()
    try:
        templates = FileSystemTemplateRepository(templates_dir)
        rules_table = JsonRulesTable.from_file(rules_path)
    except Exception:  # noqa: BLE001 — defensive
        return None
    return {
        "core": CORE_VERSION,
        "templates": templates.version(),
        "rules_table": rules_table.version(),
    }


# ── Tree walker ───────────────────────────────────────────────────


def _walk_tree(repo_root: Path, max_files: int = 20000) -> list[str]:
    """Build the POSIX-relative file list for a repo, pruning the
    standard skip-dirs. Capped to bound memory on monorepos."""
    root = repo_root.resolve()
    found: list[str] = []

    def _walk(current: Path) -> None:
        if len(found) >= max_files:
            return
        try:
            entries = sorted(current.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if len(found) >= max_files:
                return
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                _walk(entry)
            elif entry.is_file():
                rel = entry.relative_to(root).as_posix()
                found.append(rel)

    _walk(root)
    return found


# ── Main generation entry point ───────────────────────────────────


def generate_for_repo(
    repo_root: Path,
    *,
    extras: list[str] | None = None,
    include_provenance: bool = False,
) -> GenerateResult:
    """Produce a deterministic ``.gitignore`` for the given repo.

    Walks the repo tree (skip-dirs honored), feeds the file list into
    occam-gitignore-core's fingerprinter + generator, and returns a
    :class:`GenerateResult` carrying content + hash + provenance.

    Raises :exc:`OccamGitignoreUnavailable` when the integration
    can't run; the CLI catches this and shows an install prompt.
    """
    if not is_available():
        raise OccamGitignoreUnavailable(
            "occam-gitignore-core is not installed (or its data files "
            "are missing). Install: `pip install occam-gitignore-core` "
            "and ensure templates + rules_table.json are reachable "
            "(see OCCAM_GITIGNORE_DATA_DIR)."
        )
    from occam_gitignore_core import (
        DefaultFingerprinter,
        FileSystemTemplateRepository,
        GenerateOptions,
        JsonRulesTable,
        generate as core_generate,
    )

    tree = _walk_tree(repo_root)
    fingerprinter = DefaultFingerprinter()
    fp = fingerprinter.fingerprint(tuple(tree))

    templates_dir, rules_path = _resolve_data_dir()
    templates = FileSystemTemplateRepository(templates_dir)
    rules_table = JsonRulesTable.from_file(rules_path)

    options = GenerateOptions(
        extras=tuple(extras or ()),
        include_provenance=include_provenance,
    )
    output = core_generate(
        fp, options, templates=templates, rules_table=rules_table,
    )
    return GenerateResult(
        content=output.content,
        content_hash=output.content_hash,
        features=tuple(f.name for f in fp.features),
        evidence=tuple((e[0], e[1]) for e in fp.evidence),
        core_version=output.core_version,
        templates_version=output.templates_version,
        rules_table_version=output.rules_table_version,
    )


def diff_against_existing(
    repo_root: Path, generated_content: str,
) -> str | None:
    """Return None when the existing ``.gitignore`` matches the
    generated content byte-for-byte, else a unified diff string the
    CLI can show to the operator before opening a PR.
    """
    existing_path = repo_root / ".gitignore"
    if existing_path.is_file():
        try:
            existing = existing_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    else:
        existing = ""

    if existing == generated_content:
        return None

    import difflib
    diff = "\n".join(difflib.unified_diff(
        existing.splitlines(),
        generated_content.splitlines(),
        fromfile=".gitignore (current)",
        tofile=".gitignore (occam-generated)",
        lineterm="",
    ))
    return diff or None
