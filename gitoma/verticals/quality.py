"""QUALITY_VERTICAL — narrows the pipeline to lint / format config.

Second vertical, registered to prove the Castelletto Taglio A
architecture: adding a new vertical = one file (this one) + one line
in :mod:`gitoma.verticals.__init__`. No edits required to
``run.py``, ``scope_filter.py``, ``prompts.py``, or
``cli/commands/_vertical.py``.

Scope: top-level lint / format / type-check config files only. Does
NOT touch source code, docs, tests, CI, or build manifests. Does
NOT extend into ``pyproject.toml [tool.*]`` sub-sections — that
needs deeper sub-section logic and belongs to a future Taglio.

Motivated by the recurring "the linter has 200 warnings" problem
that doesn't deserve a full-pass run but where the operator wants
gitoma to triage / configure / suppress without wandering into the
source tree.
"""

from __future__ import annotations

from gitoma.verticals._base import Vertical, VerticalFileScope

__all__ = ["QUALITY_VERTICAL"]


QUALITY_VERTICAL = Vertical(
    name="quality",
    summary="Run gitoma narrowed to lint/format/type-check config files only.",
    file_allow_list=VerticalFileScope(
        # No generic extensions — quality config files are matched
        # exclusively by basename (so `src/.eslintrc` AND root-level
        # `.eslintrc` both hit, but `src/main.py` does not).
        extensions=frozenset(),
        path_prefixes=(),
        root_names=frozenset({
            # JavaScript / TypeScript ecosystem
            ".prettierrc", ".prettierrc.json", ".prettierrc.yml",
            ".prettierrc.yaml", ".prettierrc.js", ".prettierrc.cjs",
            ".prettierignore",
            ".eslintrc", ".eslintrc.json", ".eslintrc.yml",
            ".eslintrc.yaml", ".eslintrc.js", ".eslintrc.cjs",
            ".eslintignore",
            "eslint.config.js", "eslint.config.cjs", "eslint.config.mjs",
            "eslint.config.ts",
            "biome.json", "biome.jsonc",
            "tsconfig.json",
            # Python ecosystem
            ".ruff.toml", "ruff.toml",
            ".flake8", "setup.cfg",
            "mypy.ini", ".mypy.ini",
            ".pylintrc", "pylintrc",
            ".isort.cfg",
            ".black.toml",
            "tox.ini",
            # Editor / shared
            ".editorconfig",
            # Pre-commit (the workflow path itself stays denylisted at
            # the patcher level — this is just the config; G3 protects
            # the .github/workflows directory globally).
            ".pre-commit-config.yaml", ".pre-commit-config.yml",
            # Go
            ".golangci.yml", ".golangci.yaml",
            # Rust
            "rustfmt.toml", ".rustfmt.toml", "clippy.toml", ".clippy.toml",
        }),
    ),
    metric_allow_list=frozenset({
        "code_quality",
    }),
    prompt_addendum=(
        "VERTICAL=quality ACTIVE. You may ONLY emit subtasks whose "
        "file_hints are top-level lint, format, or type-check config "
        "files (.prettierrc*, .eslintrc*, biome.json, tsconfig.json, "
        ".ruff.toml, setup.cfg, mypy.ini, .pylintrc, .editorconfig, "
        ".pre-commit-config.yaml, .golangci.yml, rustfmt.toml, "
        "clippy.toml, etc.). Do NOT propose source-code edits, doc "
        "edits, test changes, CI workflow edits, or build-manifest "
        "rewrites. Do NOT propose creating pyproject.toml — its "
        "[tool.*] sub-sections are out of scope for this vertical "
        "(needs deeper sub-section logic). Stay surgical: tighten / "
        "loosen / add suppressions in EXISTING config files when "
        "possible; new config files only when no existing one covers "
        "the rule."
    ),
    no_auto_fix_ci=True,
)
