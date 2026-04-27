"""G20 — TOML / INI syntax validator for quality config files.

Closes the gap revealed by bench-blast PR #1 (closed 2026-04-26):
the full G1-G15 stack + the LLM self-critic missed 3 syntax errors
in shipped config files (`pyproject.toml [tool.mypy]` indentation,
`.ruff.toml` wrong table format, `[coverage]` malformed table). The
LLM critic caught 1 semantic issue (mypy strictness) but didn't
parse the TOML at all.

This guard fills the gap deterministically — no LLM, no heuristics:
just feed the touched config files through stdlib `tomllib` /
`configparser` and report parse errors with line/column for the
LLM retry feedback.

Out of scope (deferred):
- Per-tool key vocabularies (e.g. "is `tabWidth` a real Prettier
  key?"). G10 already covers JSON-schema'd configs (Prettier,
  ESLint, etc.); G20 only catches SYNTAX errors, not semantic
  unknowns. v2 could add per-tool vocabularies for ruff/mypy/etc.
- Pluggable validators for non-stdlib formats (YAML, HCL, …).
  Add when the bench surfaces such failures.
"""

from __future__ import annotations

import configparser
import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ConfigSyntaxError",
    "ConfigSyntaxResult",
    "check_config_syntax",
    "TOML_BASENAMES",
    "INI_BASENAMES",
]


# Files we check. Basenames matched case-INsensitively; full path
# doesn't matter (these can live at root or in subprojects). Bench
# evidence (bench-blast PR #1) pointed at exactly these targets.
TOML_BASENAMES: frozenset[str] = frozenset({
    "pyproject.toml",
    ".ruff.toml",
    "ruff.toml",
    "uv.toml",
    "mypy.toml",
    "rustfmt.toml",
    ".rustfmt.toml",
    "clippy.toml",
    ".clippy.toml",
    "Cargo.toml",  # already protected by G3 manifest denylist + G10
                   # JSON-schema, but if a patch slips through the
                   # syntax check is still useful belt-and-suspenders
})

INI_BASENAMES: frozenset[str] = frozenset({
    "setup.cfg",
    "tox.ini",
    ".flake8",
    ".pylintrc",
    "pylintrc",
    ".coveragerc",
    "mypy.ini",
    ".mypy.ini",
    ".isort.cfg",
})


@dataclass(frozen=True)
class ConfigSyntaxError:
    """One syntax error in a touched config. Self-describing for the
    LLM retry feedback."""

    file: str
    format: str  # "toml" | "ini"
    line: int | None  # 1-based; None when the parser doesn't expose location
    column: int | None  # 1-based; None when not available
    message: str  # the parser's own error string (verbatim)


@dataclass(frozen=True)
class ConfigSyntaxResult:
    """Returned only when at least one syntax error is detected."""

    errors: tuple[ConfigSyntaxError, ...]

    def render_for_llm(self) -> str:
        lines = [
            "Your patch contains config files with INVALID SYNTAX. "
            "These would break every tool that loads them (mypy, ruff, "
            "coverage, etc.) before any semantic check could even run. "
            "Fix the syntax and re-emit:",
            "",
        ]
        for e in self.errors:
            loc = ""
            if e.line is not None:
                loc = f" at line {e.line}"
                if e.column is not None:
                    loc += f", col {e.column}"
            lines.append(
                f"  * {e.file} ({e.format.upper()}){loc}: {e.message}"
            )
        return "\n".join(lines)


# ── Family detection ──────────────────────────────────────────────


def _classify(rel_path: str) -> str | None:
    """Return ``"toml"`` / ``"ini"`` / None for the path's family.
    Case-insensitive on the basename; full path doesn't matter."""
    base = Path(rel_path).name
    if base in TOML_BASENAMES:
        return "toml"
    if base in INI_BASENAMES:
        return "ini"
    # Fallback by extension — catches new config files we haven't
    # explicitly listed but that follow the convention.
    if base.endswith(".toml"):
        return "toml"
    if base.endswith((".ini", ".cfg")):
        return "ini"
    return None


# ── Parsers ──────────────────────────────────────────────────────


def _check_toml(rel_path: str, content: str) -> ConfigSyntaxError | None:
    """Validate TOML via stdlib ``tomllib``. Returns None on parse
    success, else a ConfigSyntaxError with the parser's location."""
    try:
        tomllib.loads(content)
        return None
    except tomllib.TOMLDecodeError as exc:
        # tomllib's exceptions include line/col when present in the
        # message string but not as structured fields. Best-effort
        # parse via the message format "line N, column M".
        line, col = _extract_location(str(exc))
        return ConfigSyntaxError(
            file=rel_path,
            format="toml",
            line=line,
            column=col,
            message=str(exc),
        )


def _check_ini(rel_path: str, content: str) -> ConfigSyntaxError | None:
    """Validate INI via stdlib ``configparser``. Catches
    `MissingSectionHeaderError`, `DuplicateSectionError`,
    `DuplicateOptionError`, `ParsingError`."""
    parser = configparser.ConfigParser(strict=True, interpolation=None)
    try:
        parser.read_string(content, source=rel_path)
        return None
    except configparser.Error as exc:
        line, col = _extract_location(str(exc))
        return ConfigSyntaxError(
            file=rel_path,
            format="ini",
            line=line,
            column=col,
            message=str(exc),
        )


def _extract_location(msg: str) -> tuple[int | None, int | None]:
    """Best-effort line/col extraction from parser error strings.
    Both stdlib parsers embed location info in the message; this
    helper normalises across them. Returns ``(None, None)`` when
    nothing matches — the LLM still gets the message verbatim."""
    import re
    # tomllib: "Invalid statement (at line 5, column 2)"
    m = re.search(r"line (\d+), column (\d+)", msg)
    if m:
        return int(m.group(1)), int(m.group(2))
    # configparser: "[line  5]: ..."
    m = re.search(r"\[line\s+(\d+)\]", msg)
    if m:
        return int(m.group(1)), None
    # tomllib older: "at line 5"
    m = re.search(r"\bline (\d+)", msg)
    if m:
        return int(m.group(1)), None
    return None, None


# ── Main check ───────────────────────────────────────────────────


def check_config_syntax(
    repo_root: Path,
    touched: list[str],
) -> ConfigSyntaxResult | None:
    """Validate every touched TOML/INI config. Returns None when no
    config touched OR all configs parse cleanly. Returns a result
    carrying every error otherwise — the caller composes a single
    LLM-feedback string and triggers revert+retry."""
    errors: list[ConfigSyntaxError] = []
    for rel in touched:
        family = _classify(rel)
        if family is None:
            continue
        abs_path = repo_root / rel
        if not abs_path.is_file():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if family == "toml":
            err = _check_toml(rel, content)
        else:
            err = _check_ini(rel, content)
        if err is not None:
            errors.append(err)
    if not errors:
        return None
    return ConfigSyntaxResult(errors=tuple(errors))
