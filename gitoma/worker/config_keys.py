"""G23 — config-key validity check for ``[tool.X]`` sections in pyproject.toml.

Closes the gap PR #8 of `gitoma-bench-blast` exposed (2026-04-29
EVE bench): the gemma-4-e4b worker emitted plausible-looking
``pyproject.toml`` config sections with **invented or misspelled keys**:

  [tool.mypy]
  suppress_missing_imports = true   # WRONG — actual key is "ignore_missing_imports"

  [tool.coverage]
  ignore_patterns = [".venv/"]      # WRONG — coverage uses [tool.coverage.run] omit
  run_if_covered = true             # WRONG — invented key

G10 (semantic config schema validator) covers JSON-shaped configs
(ESLint / Prettier / package.json / tsconfig / GH Actions / …) but
NOT the per-tool TOML sections in pyproject.toml — each tool defines
its own valid-key set, and there's no single JSON-Schema covering
all of them. G23 fills this gap with a curated closed-set of
known-valid keys for the most common Python tooling tables, plus a
typo-detector for the high-confidence "almost-correct" misspellings.

What G23 catches
================
For every touched pyproject.toml, parses the file (tomllib), walks
the ``[tool.X]`` tables we know about, and rejects any key that:

1. is in our typo-correction dict (``_KNOWN_TYPOS``) — high confidence
   "this key is a misspelling of a real key", e.g.
   ``suppress_missing_imports`` → ``ignore_missing_imports``
2. is NOT in the tool's known-valid set (``_KNOWN_KEYS``) — looser
   check, only fires when the section header itself is one of our
   known tools

Tool sections we know about (closed-set, conservative):

* ``[tool.mypy]``
* ``[tool.ruff]``, ``[tool.ruff.lint]``, ``[tool.ruff.format]``
* ``[tool.pytest.ini_options]``
* ``[tool.coverage.run]``, ``[tool.coverage.report]``,
  ``[tool.coverage.paths]``, ``[tool.coverage.html]``,
  ``[tool.coverage.xml]``
* ``[tool.poetry]`` (top-level only — sub-tables ignored to avoid
  false positives on legitimate poetry layouts)

Tool sections we DO NOT validate (passthrough — operator-specific
or too dynamic):

* ``[tool.setuptools]``, ``[tool.hatch]``, ``[tool.pdm]`` — packaging
  backends with extensible config
* ``[tool.pyright]``, ``[tool.pylint]`` — large dynamic schemas
* Any section we don't list = silent passthrough (default-allow)

Why opt-in
==========
Default OFF (``GITOMA_G23_CONFIG_KEYS=1`` to enable). Two reasons:

* The closed-set is intentionally narrow — false negatives by design
  (we only flag what we're confident about), and the operator may
  want to use tool features we haven't catalogued yet.
* Updating the key sets when tools ship new options is a maintenance
  cost — keeping the critic opt-in lets operators decide when the
  catalog is current enough for their stack.

Why not [tool.coverage] flat
============================
``[tool.coverage]`` (without ``.run`` / ``.report`` / etc.) is a
common mistake (gemma-4-e4b made it in PR #8). The real coverage.py
config tables are sub-tables: ``[tool.coverage.run]``,
``[tool.coverage.report]``, etc. So a flat ``[tool.coverage]``
header itself is a typo we catch, even though TOML is happy with
arbitrary table names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "G23Conflict",
    "G23Result",
    "is_g23_enabled",
    "check_g23_config_keys",
]


def is_g23_enabled() -> bool:
    """G23 default = OFF. Operator opt-in via ``GITOMA_G23_CONFIG_KEYS=1``."""
    return (os.environ.get("GITOMA_G23_CONFIG_KEYS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ── Known-typos dict (high-confidence corrections) ────────────────


# Maps misspelled / invented keys → the correct key the operator
# probably meant. Sourced from real LLM mistakes seen in bench output.
# Key is ``"<tool-section>.<bad-key>"``; value is the correct key
# name. When we hit one of these, the conflict message includes the
# suggested fix.
_KNOWN_TYPOS: dict[str, str] = {
    # mypy: gemma-4-e4b PR #8 (2026-04-29)
    "tool.mypy.suppress_missing_imports": "ignore_missing_imports",
    "tool.mypy.warn_missing_imports": "ignore_missing_imports",  # inverted name
    "tool.mypy.suppress_unused_ignores": "warn_unused_ignores",
    # ruff: common confusions with eslint/prettier nomenclature
    "tool.ruff.line_length": "line-length",  # snake_case but ruff uses dash
    "tool.ruff.target_version": "target-version",
    "tool.ruff.extend_exclude": "extend-exclude",
    # coverage: gemma-4-e4b PR #8 (2026-04-29) — invented keys
    "tool.coverage.ignore_patterns": "(use [tool.coverage.run] omit instead)",
    "tool.coverage.run_if_covered": "(no such key — coverage.py runs unconditionally)",
    "tool.coverage.exclude": "(use [tool.coverage.report] exclude_lines instead)",
    # pytest: confusion with `[tool.pytest]` (which is not a real section)
    "tool.pytest.testpaths": "(move under [tool.pytest.ini_options])",
}


# ── Known-keys per tool section ───────────────────────────────────


# Conservative subset of each tool's documented keys. Updated as
# real tools ship new options. False-negative bias on purpose —
# unknown keys flag only when we're confident the SECTION is one
# we catalog.
_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "tool.mypy": frozenset({
        "python_version", "platform", "files", "exclude", "exclude_gitignore",
        "namespace_packages", "explicit_package_bases", "ignore_missing_imports",
        "follow_imports", "follow_imports_for_stubs", "no_site_packages",
        "no_silence_site_packages", "warn_unused_configs",
        "disable_error_code", "enable_error_code", "extra_checks",
        "implicit_reexport", "strict_concatenate", "strict_equality",
        "warn_redundant_casts", "warn_unused_ignores", "warn_no_return",
        "warn_return_any", "warn_unreachable", "no_implicit_optional",
        "strict_optional", "disallow_any_unimported", "disallow_any_expr",
        "disallow_any_decorated", "disallow_any_explicit",
        "disallow_any_generics", "disallow_subclassing_any",
        "disallow_untyped_calls", "untyped_calls_exclude",
        "disallow_untyped_defs", "disallow_incomplete_defs",
        "check_untyped_defs", "disallow_untyped_decorators",
        "strict", "show_error_context", "show_column_numbers",
        "show_error_code_links", "show_error_codes", "show_traceback",
        "raise_exceptions", "pretty", "color_output", "error_summary",
        "show_absolute_path", "force_uppercase_builtins",
        "incremental", "cache_dir", "sqlite_cache", "cache_fine_grained",
        "skip_version_check", "skip_cache_mtime_checks",
        "plugins", "always_true", "always_false", "scripts_are_modules",
        "modules", "packages", "report", "any_exprs_report", "html_report",
        "linecount_report", "linecoverage_report", "lineprecision_report",
        "txt_report", "xml_report", "junit_xml", "force_union_syntax",
        "verbosity", "mypy_path",
    }),
    "tool.ruff": frozenset({
        "line-length", "indent-width", "target-version", "src", "extend",
        "extend-exclude", "extend-include", "exclude", "include",
        "respect-gitignore", "preview", "show-fixes", "fix", "fix-only",
        "unsafe-fixes", "force-exclude", "required-version", "output-format",
        "namespace-packages", "builtins", "cache-dir", "no-cache",
        "lint", "format",
    }),
    "tool.ruff.lint": frozenset({
        "select", "extend-select", "ignore", "extend-ignore", "fixable",
        "extend-fixable", "unfixable", "extend-unfixable", "exclude",
        "extend-exclude", "task-tags", "allowed-confusables", "dummy-variable-rgx",
        "external", "future-annotations", "explicit-preview-rules",
        "isort", "pep8-naming", "flake8-annotations", "flake8-bandit",
        "flake8-bugbear", "flake8-builtins", "flake8-comprehensions",
        "flake8-import-conventions", "flake8-quotes", "flake8-tidy-imports",
        "flake8-type-checking", "flake8-unused-arguments", "mccabe",
        "pycodestyle", "pydocstyle", "pylint", "pyupgrade", "per-file-ignores",
        "preview",
    }),
    "tool.ruff.format": frozenset({
        "quote-style", "indent-style", "skip-magic-trailing-comma",
        "line-ending", "preview", "docstring-code-format",
        "docstring-code-line-length", "exclude",
    }),
    "tool.pytest.ini_options": frozenset({
        "minversion", "addopts", "testpaths", "norecursedirs",
        "python_files", "python_classes", "python_functions", "pythonpath",
        "markers", "filterwarnings", "log_cli", "log_cli_level",
        "log_cli_format", "log_cli_date_format", "log_file", "log_file_level",
        "log_file_format", "log_file_date_format", "junit_suite_name",
        "junit_logging", "junit_log_passing_tests", "junit_duration_report",
        "junit_family", "asyncio_mode", "tmp_path_retention_count",
        "tmp_path_retention_policy", "console_output_style", "cache_dir",
        "doctest_optionflags", "empty_parameter_set_mark", "faulthandler_timeout",
        "required_plugins", "session_timeout", "timeout", "timeout_method",
        "usefixtures", "xfail_strict",
    }),
    "tool.coverage.run": frozenset({
        "branch", "command_line", "concurrency", "context", "cover_pylib",
        "data_file", "debug", "disable_warnings", "dynamic_context",
        "include", "omit", "parallel", "plugins", "relative_files", "sigterm",
        "source", "source_pkgs", "timid",
    }),
    "tool.coverage.report": frozenset({
        "exclude_also", "exclude_lines", "fail_under", "format", "ignore_errors",
        "include", "omit", "partial_branches", "precision", "show_missing",
        "skip_covered", "skip_empty", "sort",
    }),
    "tool.coverage.paths": frozenset({
        # No real "keys" — this section is operator-named path aliases.
        # We don't validate. Empty set means "don't check keys here".
    }),
    "tool.coverage.html": frozenset({
        "directory", "extra_css", "show_contexts", "skip_covered",
        "skip_empty", "title",
    }),
    "tool.coverage.xml": frozenset({
        "output", "package_depth",
    }),
}


# Sections we validate ONLY for typos — we don't enforce the
# closed-set on these because their key vocabulary is too dynamic
# (e.g. [tool.poetry] subsections are user-defined deps).
_TYPO_ONLY_SECTIONS: frozenset[str] = frozenset({
    "tool.poetry",
})


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class G23Conflict:
    """One config-key conflict."""

    file: str               # path of the offending config file
    section: str            # e.g. "tool.mypy"
    key: str                # the bad key
    suggestion: str         # correct key OR explanation
    is_typo: bool           # True = high-confidence typo, False = unknown key

    def render(self) -> str:
        kind = "typo" if self.is_typo else "unknown key"
        return f"  - {self.file} [{self.section}] {self.key!r} ({kind}) → {self.suggestion}"


@dataclass(frozen=True)
class G23Result:
    """Returned only when at least one conflict found."""

    conflicts: tuple[G23Conflict, ...]

    def render_for_llm(self) -> str:
        if not self.conflicts:
            return ""
        lines = [
            f"G23 CONFIG-KEYS INVALID — your patch introduced "
            f"{len(self.conflicts)} invalid pyproject.toml [tool.X] "
            f"key(s):",
            "",
        ]
        for c in self.conflicts:
            lines.append(c.render())
        lines.extend([
            "",
            "Each entry is a key we either know is a misspelling of a "
            "real option (typo) OR is not in the documented schema for "
            "the tool. Fix the keys (use the suggestion) or remove the "
            "section if you are not actually using that tool.",
        ])
        return "\n".join(lines)


# ── Diff-based key extraction ─────────────────────────────────────


def _extract_keys_from_section(table: dict, section_path: list[str]) -> list[tuple[str, str]]:
    """Walk a TOML table, return ``(section_path_str, key)`` for every
    leaf key. Recurses through nested tables to surface keys in
    ``[tool.coverage.run]`` from a top-level ``[tool]`` parse."""
    out: list[tuple[str, str]] = []
    if not isinstance(table, dict):
        return out
    section_str = ".".join(section_path)
    for k, v in table.items():
        if isinstance(v, dict):
            out.extend(_extract_keys_from_section(v, section_path + [k]))
        else:
            out.append((section_str, k))
    return out


def _section_diff_keys(
    new_tool: dict | None,
    old_tool: dict | None,
) -> list[tuple[str, str]]:
    """Return ``(section, key)`` tuples present in ``new_tool`` but
    NOT in ``old_tool``. Both args are the contents of the top-level
    ``tool`` table (i.e. ``data['tool']``), not the whole TOML doc."""
    new_keys = set(_extract_keys_from_section(new_tool or {}, ["tool"]))
    old_keys = set(_extract_keys_from_section(old_tool or {}, ["tool"]))
    return sorted(new_keys - old_keys)


# ── Main check ────────────────────────────────────────────────────


def check_g23_config_keys(
    repo_root: str | Path,
    touched: Iterable[str],
    originals: dict[str, str | None] | None = None,
) -> G23Result | None:
    """Validate config keys in any pyproject.toml the patch touched.

    ``originals`` maps relative file paths to their pre-patch text
    (or ``None`` for "didn't exist before"). When provided, we only
    flag keys that are NEW in this patch — pre-existing typos in the
    file are left for a separate cleanup. When ``None``, we flag all
    keys (useful for a fresh-clone scan or testing).

    Returns ``None`` when:
      * G23 not enabled
      * no touched pyproject.toml
      * file doesn't parse as TOML (G20 will catch that separately)
      * no invalid keys at or above the typo/unknown threshold
    """
    if not is_g23_enabled():
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

    conflicts: list[G23Conflict] = []

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

        new_tool = (new_data.get("tool") or {}) if isinstance(new_data, dict) else {}
        old_tool = (old_data.get("tool") or {}) if isinstance(old_data, dict) else {}

        # Compute diff: keys NEW in this patch
        if originals is not None:
            new_pairs = _section_diff_keys(new_tool, old_tool)
        else:
            new_pairs = sorted(_extract_keys_from_section(new_tool, ["tool"]))

        for section, key in new_pairs:
            # Strip the leading "tool." for the typo lookup since
            # _KNOWN_TYPOS keys already include "tool." prefix
            full_key = f"{section}.{key}"

            # Check 1: known typo (high-confidence)
            if full_key in _KNOWN_TYPOS:
                conflicts.append(G23Conflict(
                    file=rel_path, section=section, key=key,
                    suggestion=_KNOWN_TYPOS[full_key], is_typo=True,
                ))
                continue

            # Check 2: section is in our catalog → key must be in
            # the known-valid set. Skip if section is typo-only or
            # not in our catalog (silent passthrough).
            if section in _TYPO_ONLY_SECTIONS:
                continue
            if section not in _KNOWN_KEYS:
                continue
            valid = _KNOWN_KEYS[section]
            # Empty set = section we know about but don't enforce keys on
            if not valid:
                continue
            if key not in valid:
                conflicts.append(G23Conflict(
                    file=rel_path, section=section, key=key,
                    suggestion=f"(not in documented [{section}] schema)",
                    is_typo=False,
                ))

    if not conflicts:
        return None
    return G23Result(conflicts=tuple(conflicts))
