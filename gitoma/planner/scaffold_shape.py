"""PHASE 1.7 — stack-shape context for the LLM planner.

Pure-function module: given a RepoBrief + occam-trees catalog +
the repo's file_tree, infer (stack_id, level), pull the canonical
scaffold from occam-trees, diff against the repo, and render a
compact "missing canonical paths" context block to be injected
into the planner prompt.

Composition of three legs:
  - RepoBrief (deterministic stack signals from manifests)
  - occam-trees client (canonical 100×10 scaffold catalog)
  - planner prompt (consumer)

Silent fail-open contract: any inference failure (no signals,
no match, server unreachable) returns ``None`` and the caller
must treat it as "no shape context — skip the block".

Why additive-only delta: gitoma is a polish-agent (memory file
``project_bench_generation_2026-04-28_planner_blind.md``); the
planner cannot be trusted to remove files. ``gitoma scaffold``
already enforces additive-only at the materialisation layer;
this module mirrors that contract at the planning layer so the
planner sees missing canonical files as actionable hints, never
"delete this file" suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gitoma.context import RepoBrief

# ── Stack inference ───────────────────────────────────────────────


@dataclass(frozen=True)
class StackInferenceResult:
    """Outcome of matching RepoBrief.stack against occam-trees catalog."""

    stack_id: str
    stack_name: str
    matched_components: tuple[str, ...]
    match_count: int
    candidates: tuple[tuple[str, int], ...] = ()  # top-3 (id, match_count)


def _normalise(s: str) -> str:
    """Lowercase + strip non-alnum for fuzzy component matching."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def infer_stack(
    brief: RepoBrief,
    available_stacks: list[dict[str, Any]],
    min_matches: int = 2,
) -> StackInferenceResult | None:
    """Score each stack by component-set intersection with brief.stack.

    Returns the highest-scoring stack with at least ``min_matches``
    components in common, or ``None`` when nothing reaches the
    threshold. Tie-breaks: more matches first, then lower rank
    (= more popular). The fallback path (1 match + perfect
    language coverage) is intentionally NOT included — too many
    false positives on broad signals like "Python".
    """
    if not brief.stack or not available_stacks:
        return None

    brief_norm = {_normalise(s) for s in brief.stack if s}
    if not brief_norm:
        return None

    scored: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    # (match_count, -rank_tiebreaker, id, name, matched_components)

    for stack in available_stacks:
        components = stack.get("components") or []
        if not isinstance(components, list):
            continue
        matched = tuple(
            c for c in components
            if isinstance(c, str) and _normalise(c) in brief_norm
        )
        if not matched:
            continue
        rank = stack.get("rank")
        if not isinstance(rank, int):
            rank = 9999
        scored.append((
            len(matched), -rank, str(stack.get("id", "")),
            str(stack.get("name", "")), matched,
        ))

    if not scored:
        return None

    scored.sort(reverse=True)
    best = scored[0]
    if best[0] < min_matches:
        return None

    candidates = tuple(
        (s[2], s[0]) for s in scored[:3]
    )
    return StackInferenceResult(
        stack_id=best[2],
        stack_name=best[3],
        matched_components=best[4],
        match_count=best[0],
        candidates=candidates,
    )


# ── Level inference ───────────────────────────────────────────────


# Paths matching ANY of these are excluded from the "source file"
# count used to pick a level. Conservative on purpose — a small
# slice of common noise. Tests are excluded because they shouldn't
# inflate a project's perceived size (a 5-file project with 50 test
# files is still L2-ish).
_LEVEL_EXCLUDE_PARTS = (
    "node_modules", ".git", "dist", "build", "target", ".venv",
    "venv", "__pycache__", ".next", ".nuxt", "vendor", "out",
    "coverage", ".cache", ".gradle", ".idea", ".vscode",
)
_LEVEL_EXCLUDE_TEST_DIRS = ("test", "tests", "spec", "specs", "__tests__")


def _is_source_file(path: str) -> bool:
    """Heuristic: is ``path`` likely a source file we should count
    toward project-size level inference?"""
    if not path or path.endswith("/"):
        return False
    # Skip dotfiles at the root (.gitignore, .env etc)
    parts = path.split("/")
    if any(p in _LEVEL_EXCLUDE_PARTS for p in parts):
        return False
    if any(p.lower() in _LEVEL_EXCLUDE_TEST_DIRS for p in parts):
        return False
    # Skip docs / images / pure-data files
    leaf = parts[-1].lower()
    if leaf.endswith((
        ".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".lock", ".log",
    )):
        return False
    return True


# Thresholds tuned to occam-trees archetype levels (1 = skeleton,
# 10 = enterprise). Lower bound inclusive; first matching tier wins.
_LEVEL_TIERS: tuple[tuple[int, int], ...] = (
    (1, 5),
    (2, 15),
    (3, 40),
    (4, 120),
    (5, 300),
    (6, 700),
    (7, 1500),
    (8, 3500),
    (9, 8000),
    # Anything bigger → L10
)


def infer_level(file_tree: list[str]) -> int:
    """Return an integer 1-10 inferred from ``file_tree`` size."""
    n = sum(1 for p in file_tree if _is_source_file(p))
    for level, ceil in _LEVEL_TIERS:
        if n < ceil:
            return level
    return 10


# ── Delta computation ─────────────────────────────────────────────


def _norm_path(p: str) -> str:
    """Normalise a path for cross-platform compare. Trailing slash
    and leading ./ stripped; backslashes → forward slashes."""
    if not p:
        return ""
    p = p.replace("\\", "/").lstrip("./")
    return p.rstrip("/")


def compute_delta(
    canonical: list[tuple[str, str]],
    current: list[str],
) -> list[tuple[str, str]]:
    """Return ``(path, role)`` tuples present in ``canonical`` but
    NOT in ``current``. Additive only — never recommend removals."""
    if not canonical:
        return []
    current_set = {_norm_path(p) for p in current if p}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, role in canonical:
        norm = _norm_path(path)
        if not norm or norm in current_set or norm in seen:
            continue
        seen.add(norm)
        out.append((path, role))
    return out


# ── Render ────────────────────────────────────────────────────────


# Hard ceiling on the prompt block. Roughly aligned with the other
# context blocks (skeleton, fingerprint) so PHASE 1.7 can't blow
# the budget on its own.
DEFAULT_MAX_CHARS = 1200


def render_shape_context(
    *,
    stack_id: str,
    stack_name: str,
    level: int,
    matched_components: tuple[str, ...],
    delta: list[tuple[str, str]],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Render the PHASE 1.7 block. Empty string when ``delta`` is
    empty (the repo already matches the canonical scaffold)."""
    if not delta:
        return ""

    header = (
        f"Inferred stack: {stack_name} ({stack_id}) · level {level} "
        f"· matched components: {', '.join(matched_components)}"
    )

    # Group delta paths by their top-level directory for readability
    by_top: dict[str, list[tuple[str, str]]] = {}
    for path, role in delta:
        top = path.split("/", 1)[0] if "/" in path else "(root)"
        by_top.setdefault(top, []).append((path, role))

    lines = [header, "", "Canonical paths missing from the repo:"]
    for top in sorted(by_top.keys()):
        lines.append(f"  {top}/")
        for path, role in by_top[top]:
            role_str = f" [{role}]" if role else ""
            lines.append(f"    - {path}{role_str}")

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered

    # Truncation: keep header + as many lines as fit + "…(N more)"
    truncated_lines = [header, "", "Canonical paths missing from the repo:"]
    used = sum(len(line) + 1 for line in truncated_lines)
    shown = 0
    total = len(delta)
    for top in sorted(by_top.keys()):
        section = [f"  {top}/"]
        for path, role in by_top[top]:
            role_str = f" [{role}]" if role else ""
            section.append(f"    - {path}{role_str}")
        section_size = sum(len(line) + 1 for line in section)
        # Leave ~30 chars for the trailing "…(N more)" notice
        if used + section_size + 30 > max_chars:
            break
        truncated_lines.extend(section)
        used += section_size
        shown += len(section) - 1  # subtract the dir header line
    if shown < total:
        truncated_lines.append(f"  …({total - shown} more)")
    return "\n".join(truncated_lines)
