"""G24 — config-section STRUCTURE validity for pyproject.toml.

Closes the bench-blast PR #10 failure mode (qwen3-8b A/B,
2026-04-29 EVE):

    [tool.poetry]
    dependencies = [
        "pytest",
        "mypy",
        "poetry"
    ]

This is structurally wrong on TWO counts:

1. Poetry's ``dependencies`` field MUST be a TABLE, not a list:
   ``[tool.poetry.dependencies]\\npytest = "^7.0"\\n``. The list-of-
   strings form silently parses to valid TOML but Poetry will
   reject it at install time with a confusing error.

2. Listing ``poetry`` as a dep of itself is meta-weird (the build
   tool should not be a runtime dep) — but that's a content-level
   smell, not a structural one. G24 catches the structural error;
   the meta-weirdness is left to PR review.

G23 already catches INVALID KEYS in catalog tool sections. G24
covers the COMPLEMENTARY problem: VALID KEYS in WRONG SHAPE
(table vs list vs scalar). Together they cover the schema-quality
surface that schemastore-based G10 doesn't reach.

What G24 catches
================
For every touched ``pyproject.toml``, parses with tomllib, walks
the closed-set ``_STRUCTURE_RULES`` paths, and rejects any path
where the value is the wrong type for that path.

Closed-set rules (conservative — only the patterns we've actually
seen LLMs emit wrong):

* ``tool.poetry.dependencies`` → table
* ``tool.poetry.dev-dependencies`` → table  (legacy poetry <1.2)
* ``tool.poetry.optional-dependencies`` → table
* ``tool.poetry.extras`` → table
* ``tool.poetry.scripts`` → table
* ``tool.poetry.plugins`` → table
* ``tool.uv.dependency-groups`` → table  (each group's value is a list)
* ``project.dependencies`` → list  (PEP-621 — array of PEP-508 strings)
* ``project.optional-dependencies`` → table  (PEP-621 — group→list)
* ``project.scripts`` → table  (PEP-621)
* ``project.authors`` → list  (PEP-621 — array of {name, email})

We DO NOT enforce shape for `[tool.poetry.group.*.dependencies]`
sub-tables (poetry 1.2+ groups) because the wildcard match is
prone to false positives on operator-defined names. Revisit if
needed.

Why opt-in
==========
Default OFF (``GITOMA_G24_CONFIG_STRUCTURE=1`` to enable). Same
reasoning as G23: closed-set is intentionally narrow; operators
opt in once they verify their stack matches. Keeping the critic
quiet by default protects against false-positives on uncommon
config layouts we haven't catalogued.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "G24Conflict",
    "G24Result",
    "is_g24_enabled",
    "check_g24_config_structure",
]


def is_g24_enabled() -> bool:
    """G24 default = OFF. Operator opt-in via ``GITOMA_G24_CONFIG_STRUCTURE=1``."""
    return (os.environ.get("GITOMA_G24_CONFIG_STRUCTURE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ── Structure rules ──────────────────────────────────────────────


# Each rule maps a dotted-path inside ``pyproject.toml`` to the
# expected value type. ``"table"`` = dict, ``"list"`` = list,
# ``"scalar"`` = str/int/float/bool.
#
# The rule's "intent" string is shown in the LLM feedback as a
# corrective hint — explains how the section SHOULD be authored.
_STRUCTURE_RULES: dict[str, tuple[str, str]] = {
    # path → (expected_type, intent_message)

    # Poetry — `dependencies` and friends must be tables (each dep
    # is `name = "version-spec"`)
    "tool.poetry.dependencies": (
        "table",
        "Poetry deps go in a TABLE: `[tool.poetry.dependencies]` "
        "followed by `name = \"^X.Y\"` lines. The list-of-strings "
        "form (e.g. `dependencies = [\"pytest\"]`) is invalid for "
        "Poetry and will fail at `poetry install` time.",
    ),
    "tool.poetry.dev-dependencies": (
        "table",
        "Poetry dev-deps (legacy <1.2) go in a TABLE under "
        "`[tool.poetry.dev-dependencies]`. For Poetry 1.2+ prefer "
        "`[tool.poetry.group.dev.dependencies]`.",
    ),
    "tool.poetry.optional-dependencies": (
        "table",
        "Poetry optional-deps belong in `[tool.poetry.optional-"
        "dependencies]` as a table.",
    ),
    "tool.poetry.extras": (
        "table",
        "Poetry extras: `[tool.poetry.extras]` table mapping "
        "extra-name → list-of-dep-names.",
    ),
    "tool.poetry.scripts": (
        "table",
        "Poetry scripts: `[tool.poetry.scripts]` table mapping "
        "command-name → entry-point.",
    ),
    "tool.poetry.plugins": (
        "table",
        "Poetry plugins: `[tool.poetry.plugins]` table.",
    ),
    "tool.uv.dependency-groups": (
        "table",
        "uv dependency-groups: `[tool.uv.dependency-groups]` table "
        "mapping group-name → list-of-strings.",
    ),

    # PEP-621 [project] — these are at the OPPOSITE polarity; PEP-621
    # `dependencies` IS a list of PEP-508 strings, and gets confused
    # with poetry conventions
    "project.dependencies": (
        "list",
        "PEP-621 `[project] dependencies` is an ARRAY of PEP-508 "
        "strings (e.g. `dependencies = [\"requests>=2.31\"]`). For "
        "Poetry-style table form, use `[tool.poetry.dependencies]`.",
    ),
    "project.optional-dependencies": (
        "table",
        "PEP-621 `[project.optional-dependencies]` is a TABLE mapping "
        "extra-name → array of PEP-508 strings.",
    ),
    "project.scripts": (
        "table",
        "PEP-621 `[project.scripts]` is a TABLE mapping command-name "
        "→ entry-point string.",
    ),
    "project.authors": (
        "list",
        "PEP-621 `[project] authors` is an ARRAY of `{name, email}` "
        "tables (e.g. `authors = [{ name = \"X\", email = \"y@z\" }]`).",
    ),
}


def _value_type(v: Any) -> str:
    """Map a TOML-parsed value to one of our expected-type tokens."""
    if isinstance(v, dict):
        return "table"
    if isinstance(v, list):
        return "list"
    if isinstance(v, (str, int, float, bool)):
        return "scalar"
    return "other"


def _walk_value_at_path(data: dict, path: str) -> Any | None:
    """Drill into ``data`` along the dotted path. Returns the value
    or None if any component is missing or non-dict."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        if part not in cur:
            return None
        cur = cur[part]
    return cur


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class G24Conflict:
    """One structural-shape conflict."""

    file: str               # path of the offending pyproject.toml
    section_path: str       # e.g. "tool.poetry.dependencies"
    expected_type: str      # "table" / "list" / "scalar"
    actual_type: str        # what we actually got
    intent: str             # corrective hint

    def render(self) -> str:
        return (
            f"  - {self.file} `{self.section_path}` is a "
            f"{self.actual_type.upper()} but must be a "
            f"{self.expected_type.upper()}.\n    {self.intent}"
        )


@dataclass(frozen=True)
class G24Result:
    """Returned only when at least one conflict found."""

    conflicts: tuple[G24Conflict, ...]

    def render_for_llm(self) -> str:
        if not self.conflicts:
            return ""
        lines = [
            f"G24 CONFIG-STRUCTURE INVALID — your patch introduced "
            f"{len(self.conflicts)} pyproject.toml section(s) with the "
            f"wrong shape:",
            "",
        ]
        for c in self.conflicts:
            lines.append(c.render())
        lines.extend([
            "",
            "Each entry above is a section whose VALUE TYPE is wrong "
            "for its semantic purpose (e.g. a list where a table is "
            "required, or vice-versa). The TOML parses as syntactically "
            "valid but the consuming tool will reject it at install / "
            "build time. Re-emit the patch using the shape described "
            "in the intent line.",
        ])
        return "\n".join(lines)


# ── Main check ────────────────────────────────────────────────────


def check_g24_config_structure(
    repo_root: str | Path,
    touched: Iterable[str],
    originals: dict[str, str | None] | None = None,
) -> G24Result | None:
    """Validate the structural shape of well-known pyproject.toml
    sections in any pyproject.toml the patch touched.

    Same diff-aware semantics as G23: when ``originals`` is provided,
    only flag paths whose VALUE TYPE is NEW (or newly wrong) in this
    patch. When ``None``, flag all current violations.

    Returns ``None`` when:
      * G24 not enabled
      * no touched pyproject.toml
      * file doesn't parse as TOML (G20 will catch that separately)
      * no structural conflicts
    """
    if not is_g24_enabled():
        return None

    touched_set = {str(p) for p in touched if p}
    pyproject_paths = [p for p in touched_set if p.endswith("pyproject.toml")]
    if not pyproject_paths:
        return None

    root = Path(repo_root)
    if not root.is_dir():
        return None

    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python <3.11
        return None

    conflicts: list[G24Conflict] = []

    for rel_path in pyproject_paths:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            new_text = full_path.read_text(encoding="utf-8", errors="replace")
            new_data = tomllib.loads(new_text)
        except Exception:  # noqa: BLE001 — G20 handles parse errors
            continue

        old_data: dict | None = None
        if originals is not None and rel_path in originals:
            old_text = originals[rel_path]
            if old_text:
                try:
                    old_data = tomllib.loads(old_text)
                except Exception:  # noqa: BLE001
                    old_data = None

        for section_path, (expected, intent) in _STRUCTURE_RULES.items():
            new_val = _walk_value_at_path(new_data, section_path)
            if new_val is None:
                continue  # section absent — nothing to validate
            actual = _value_type(new_val)
            if actual == expected:
                continue  # correct shape

            # Diff mode: skip if the OLD value was also wrong (pre-
            # existing bug, not introduced by this patch)
            if originals is not None:
                old_val = _walk_value_at_path(old_data or {}, section_path)
                if old_val is not None and _value_type(old_val) == actual:
                    continue  # already broken before patch — leave it

            conflicts.append(G24Conflict(
                file=rel_path,
                section_path=section_path,
                expected_type=expected,
                actual_type=actual,
                intent=intent,
            ))

    if not conflicts:
        return None
    return G24Result(conflicts=tuple(conflicts))
