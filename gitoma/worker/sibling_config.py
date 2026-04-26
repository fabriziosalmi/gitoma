"""G15 sibling-config reconciliation guard.

Closes the b2v PR #32 failure mode: ``gitoma quality`` shipped a
``.prettierrc`` (``tabWidth: 2``, ``semi: false``, ``singleQuote:
true``) without checking that the repo's existing ``.editorconfig``
said ``indent_size = 4`` and the ``.eslintrc.json`` enforced
``semi: ["error", "always"]``. The patch was syntactically valid
but introduced contradictions with the installed tooling.

This guard runs in the worker apply pipeline (between G14 URL
grounding and Ψ-full). It:

  1. Filters touched files to the JS/TS quality-config family
     (``.editorconfig``, ``.prettierrc*``, ``.eslintrc*``,
     ``package.json`` with ``prettier``/``eslintConfig`` keys).
  2. Parses each touched config to extract a small set of
     reconciliation-relevant values (indent, line endings, semi,
     quotes).
  3. Walks the same dir tree for SIBLING quality configs in the
     family.
  4. Runs the reconciliation matrix — for each
     (touched, sibling, key) triple, applies a comparator function
     that returns ``None`` (compatible) or a string describing
     the conflict.
  5. Returns a result carrying the conflicts so the worker can
     revert + retry with a feedback prompt.

Pure / deterministic / no LLM. v1 covers JS/TS only; Python
family deferred to v1+.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "check_sibling_config",
    "SiblingConfigConflict",
    "SiblingConfigResult",
]


# ── Result types ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SiblingConfigConflict:
    """One reconciliation failure between a touched config and a
    sibling. Self-describing for the LLM retry feedback."""

    touched_file: str
    sibling_file: str
    key: str
    touched_value: str
    sibling_value: str
    message: str


@dataclass(frozen=True)
class SiblingConfigResult:
    """Returned only when at least one conflict is detected.
    Carries every conflict so the LLM gets the full picture in
    one feedback round (cheaper than per-conflict iterations)."""

    conflicts: tuple[SiblingConfigConflict, ...]

    def render_for_llm(self) -> str:
        lines = [
            "Your patch creates / modifies a quality-tooling config "
            "file that DISAGREES with sibling configs already present "
            "in this repo. The codebase would have two configs "
            "fighting each other. Reconcile or do not emit the change."
            "",
        ]
        for c in self.conflicts:
            lines.append(
                f"  * {c.touched_file} sets `{c.key}` = `{c.touched_value}`, "
                f"but {c.sibling_file} has `{c.key}` = `{c.sibling_value}`."
            )
            lines.append(f"    {c.message}")
        return "\n".join(lines)


# ── Family detection ───────────────────────────────────────────────


# JS/TS quality-config family. Filenames matched case-INsensitively
# on the basename; full path doesn't matter (configs can live at
# repo root or in subprojects in monorepos).
_QUALITY_BASENAMES: frozenset[str] = frozenset({
    ".editorconfig",
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".eslintrc.js",
    ".eslintrc.cjs",
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.mjs",
    # package.json may carry prettier / eslintConfig keys; checked
    # specially since most other top-level keys are out of scope.
    "package.json",
})


def _is_quality_config(rel_path: str) -> bool:
    base = Path(rel_path).name
    return base in _QUALITY_BASENAMES


def _config_family(rel_path: str) -> str:
    """Map a quality-config file to its family identifier used in
    the reconciliation matrix."""
    base = Path(rel_path).name
    if base == ".editorconfig":
        return "editorconfig"
    if base.startswith(".prettierrc") or base in (
        # Standalone prettier config files (v1 covers the most
        # common shapes).
    ):
        return "prettier"
    if base == "package.json":
        # package.json is a multi-family carrier; the embedded
        # prettier/eslintConfig keys decide which family it
        # contributes to. Caller routes via a richer accessor.
        return "package_json"
    if base.startswith(".eslintrc") or base.startswith("eslint.config"):
        return "eslint"
    return "unknown"


# ── Parsers — extract only the reconciliation-relevant keys ───────


def _parse_editorconfig(content: str) -> dict[str, dict[str, Any]]:
    """Return ``{section: {key: value}}`` for the keys we care about.
    Section ``"*"`` is the global default; ``"*.{js,ts}"`` style
    sections are kept verbatim. Unknown sections / keys ignored."""
    result: dict[str, dict[str, Any]] = {}
    current = "_root"
    relevant_keys = {"indent_size", "indent_style", "end_of_line"}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k not in relevant_keys:
            continue
        result.setdefault(current, {})[k] = _coerce_editorconfig_value(k, v)
    return result


def _coerce_editorconfig_value(key: str, raw: str) -> Any:
    if key == "indent_size":
        try:
            return int(raw)
        except ValueError:
            return raw  # could be "tab"
    return raw.strip().strip("'\"")


def _parse_prettier_json(content: str) -> dict[str, Any]:
    """Extract reconciliation-relevant Prettier keys from a JSON-ish
    Prettier config (.prettierrc / .prettierrc.json). Returns ``{}``
    on parse failure (defensive: malformed = skip, not crash)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    relevant = {"tabWidth", "useTabs", "semi", "singleQuote", "endOfLine"}
    return {k: v for k, v in data.items() if k in relevant}


def _parse_eslint_json(content: str) -> dict[str, Any]:
    """Extract the reconciliation-relevant ESLint rules. Returns
    ``{rule: choice_string}`` for ``semi`` and ``quotes`` only. Each
    rule's value normalised to a single string ("always", "never",
    "single", "double") — bail to ``"unknown"`` for shapes we can't
    parse."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    rules = data.get("rules") or {}
    if not isinstance(rules, dict):
        return {}
    out: dict[str, Any] = {}
    for rule_name in ("semi", "quotes"):
        if rule_name not in rules:
            continue
        out[rule_name] = _normalize_eslint_rule(rule_name, rules[rule_name])
    return out


def _normalize_eslint_rule(name: str, raw: Any) -> str:
    """Reduce an ESLint rule value to a single string choice."""
    # Shapes:
    #   "always" / "never" / "single" / "double"
    #   ["error", "always"] → "always"
    #   ["error", "single", {"avoidEscape": true}] → "single"
    #   2 (legacy numeric) → unknown without a choice
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and len(raw) >= 2 and isinstance(raw[1], str):
        return raw[1]
    return "unknown"


def _parse_package_json(content: str) -> dict[str, dict[str, Any]]:
    """Return the embedded ``prettier`` / ``eslintConfig`` sub-objects
    keyed by their family. ``{"prettier": {...}, "eslint": {...}}``
    when present, else ``{}``."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(data.get("prettier"), dict):
        prettier_keys = {"tabWidth", "useTabs", "semi", "singleQuote", "endOfLine"}
        out["prettier"] = {
            k: v for k, v in data["prettier"].items() if k in prettier_keys
        }
    if isinstance(data.get("eslintConfig"), dict):
        rules = data["eslintConfig"].get("rules") or {}
        if isinstance(rules, dict):
            eslint_normalised: dict[str, Any] = {}
            for rule_name in ("semi", "quotes"):
                if rule_name in rules:
                    eslint_normalised[rule_name] = _normalize_eslint_rule(
                        rule_name, rules[rule_name],
                    )
            if eslint_normalised:
                out["eslint"] = eslint_normalised
    return out


def _editorconfig_root(parsed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Pick the section that applies to JS/TS source files. Tries
    ``[*]`` first, then ``[*.{js,ts,jsx,tsx,json}]`` style sections.
    Empty dict when nothing matches."""
    if "*" in parsed:
        return parsed["*"]
    if "_root" in parsed:
        return parsed["_root"]
    js_section_keys = {
        "*.js", "*.ts", "*.jsx", "*.tsx", "*.json",
        "*.{js,ts}", "*.{js,ts,jsx,tsx}", "*.{js,jsx,ts,tsx}",
        "*.{js,ts,jsx,tsx,json}",
    }
    for key, sec in parsed.items():
        if key in js_section_keys:
            return sec
    return {}


# ── Reconciliation matrix ─────────────────────────────────────────


def _check_indent(
    editorconfig_section: dict[str, Any],
    prettier: dict[str, Any],
) -> dict[str, Any] | None:
    """Compare ``.editorconfig.indent_size`` to Prettier ``tabWidth``.
    Returns ``{"key", "values": {family: value}, "message"}`` on
    conflict, else ``None``. Per-family ``values`` lets the caller
    correctly orient the conflict around the touched file."""
    eC_size = editorconfig_section.get("indent_size")
    p_size = prettier.get("tabWidth")
    if eC_size is None or p_size is None:
        return None
    if not isinstance(eC_size, int) or not isinstance(p_size, int):
        return None
    if eC_size != p_size:
        return {
            "key": "indent_size↔tabWidth",
            "values": {"editorconfig": str(eC_size), "prettier": str(p_size)},
            "message": (
                f".editorconfig sets indent_size={eC_size} but Prettier "
                f"would format with tabWidth={p_size}; the two would fight "
                "on every save. Pick one value and align both files."
            ),
        }
    return None


_EOL_MAP = {"lf": "lf", "crlf": "crlf", "cr": "cr"}
_PRETTIER_EOL_MAP = {"lf": "lf", "crlf": "crlf", "cr": "cr", "auto": None}


def _check_end_of_line(
    editorconfig_section: dict[str, Any],
    prettier: dict[str, Any],
) -> dict[str, Any] | None:
    eC_eol = editorconfig_section.get("end_of_line")
    p_eol = prettier.get("endOfLine")
    if eC_eol is None or p_eol is None:
        return None
    eC_norm = _EOL_MAP.get(str(eC_eol).lower())
    p_norm = _PRETTIER_EOL_MAP.get(str(p_eol).lower())
    if eC_norm is None or p_norm is None:
        return None
    if eC_norm != p_norm:
        return {
            "key": "end_of_line↔endOfLine",
            "values": {"editorconfig": str(eC_eol), "prettier": str(p_eol)},
            "message": (
                f".editorconfig sets end_of_line={eC_eol} but Prettier "
                f"endOfLine={p_eol} would normalise to a different shape "
                "on every save."
            ),
        }
    return None


def _check_semi(
    eslint: dict[str, Any],
    prettier: dict[str, Any],
) -> dict[str, Any] | None:
    """ESLint's ``semi`` rule vs Prettier's ``semi`` boolean.
    ESLint ``"always"`` ≡ Prettier ``true``; ESLint ``"never"`` ≡
    Prettier ``false``."""
    e_semi = eslint.get("semi")
    p_semi = prettier.get("semi")
    if e_semi is None or p_semi is None:
        return None
    if e_semi == "unknown":
        return None
    expected = True if e_semi == "always" else (
        False if e_semi == "never" else None
    )
    if expected is None:
        return None
    if expected != p_semi:
        return {
            "key": "semi",
            "values": {"eslint": str(e_semi), "prettier": str(p_semi)},
            "message": (
                f"ESLint enforces `semi: \"{e_semi}\"` but Prettier sets "
                f"`semi: {p_semi}`. ESLint will report violations on every "
                f"file Prettier formats."
            ),
        }
    return None


_QUOTES_MAP = {"single": "single", "double": "double"}


def _check_quotes(
    eslint: dict[str, Any],
    prettier: dict[str, Any],
) -> dict[str, Any] | None:
    """ESLint's ``quotes`` rule vs Prettier's ``singleQuote`` boolean.
    ESLint ``"single"`` ≡ Prettier ``true``; ESLint ``"double"`` ≡
    Prettier ``false`` (default)."""
    e_q = eslint.get("quotes")
    p_single = prettier.get("singleQuote")
    if e_q is None or p_single is None:
        return None
    if e_q == "unknown":
        return None
    expected_single = e_q == "single"
    if expected_single != p_single:
        return {
            "key": "quotes↔singleQuote",
            "values": {"eslint": str(e_q), "prettier": str(p_single)},
            "message": (
                f"ESLint enforces `quotes: \"{e_q}\"` but Prettier sets "
                f"`singleQuote: {p_single}`. The two normalisers disagree."
            ),
        }
    return None


# ── Main check ────────────────────────────────────────────────────


def check_sibling_config(
    repo_root: Path,
    touched: list[str],
    originals: dict[str, str] | None = None,
) -> SiblingConfigResult | None:
    """Inspect every quality-config file in ``touched``. For each,
    parse its current (post-patch) content + every sibling quality
    config in ``repo_root``, and run the reconciliation matrix.

    Returns ``SiblingConfigResult`` carrying every detected conflict,
    or ``None`` when no quality config touched OR no conflicts.
    """
    quality_touched = [t for t in touched if _is_quality_config(t)]
    if not quality_touched:
        return None

    # Build the parsed view of every quality-config in the repo
    # (touched + sibling). Touched configs are read from disk
    # (post-patch state), sibling configs ditto.
    parsed_by_file: dict[str, dict[str, Any]] = {}
    for rel in quality_touched:
        abs_p = repo_root / rel
        if not abs_p.is_file():
            continue
        try:
            content = abs_p.read_text(errors="replace")
        except OSError:
            continue
        parsed_by_file[rel] = _parse_for_family(rel, content)

    siblings = _discover_siblings(repo_root, exclude=set(quality_touched))
    for rel in siblings:
        if rel in parsed_by_file:
            continue
        abs_p = repo_root / rel
        try:
            content = abs_p.read_text(errors="replace")
        except OSError:
            continue
        parsed_by_file[rel] = _parse_for_family(rel, content)

    if not parsed_by_file:
        return None

    conflicts: list[SiblingConfigConflict] = []
    for touched_rel in quality_touched:
        touched_view = parsed_by_file.get(touched_rel)
        if not touched_view:
            continue
        for sibling_rel, sibling_view in parsed_by_file.items():
            if sibling_rel == touched_rel:
                continue
            conflicts.extend(_run_matrix(
                touched_rel, touched_view, sibling_rel, sibling_view,
            ))

    if not conflicts:
        return None
    return SiblingConfigResult(conflicts=tuple(conflicts))


def _parse_for_family(rel_path: str, content: str) -> dict[str, Any]:
    """Return a uniform view: ``{"family": str, "<family-data>": ...}``
    where the family-data shape is family-specific."""
    family = _config_family(rel_path)
    if family == "editorconfig":
        return {"family": "editorconfig",
                "section": _editorconfig_root(_parse_editorconfig(content))}
    if family == "prettier":
        # Some .prettierrc files are JSON; .prettierrc.yml/.yaml are
        # YAML which we don't parse in v1. Treat unparseable as
        # empty (defensive).
        return {"family": "prettier", "data": _parse_prettier_json(content)}
    if family == "eslint":
        return {"family": "eslint", "data": _parse_eslint_json(content)}
    if family == "package_json":
        return {"family": "package_json", "data": _parse_package_json(content)}
    return {"family": family}


def _discover_siblings(
    repo_root: Path, exclude: set[str],
) -> list[str]:
    """Walk the repo tree (bounded by skip-dirs) collecting quality-
    config paths. Cap at 50 files (defensive against monorepo
    explosions)."""
    skip = {
        ".git", ".venv", "venv", "env", "__pycache__",
        "node_modules", "dist", "build", "target", ".tox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "site-packages", "vendor",
    }
    found: list[str] = []
    cap = 50
    # Resolve the root ONCE so all `relative_to` calls compare
    # against the same canonicalised prefix (macOS expands
    # /var/folders → /private/var/folders, breaking naïve relpath).
    root_resolved = repo_root.resolve()

    def _walk(current: Path) -> None:
        if len(found) >= cap:
            return
        try:
            entries = sorted(current.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if len(found) >= cap:
                return
            if entry.is_dir():
                if entry.name in skip or entry.name.startswith("."):
                    continue
                _walk(entry)
            elif entry.is_file() and _is_quality_config(entry.name):
                rel = entry.relative_to(root_resolved).as_posix()
                if rel not in exclude:
                    found.append(rel)

    _walk(root_resolved)
    return found


def _run_matrix(
    touched_rel: str,
    touched_view: dict[str, Any],
    sibling_rel: str,
    sibling_view: dict[str, Any],
) -> list[SiblingConfigConflict]:
    """Apply the reconciliation matrix between one touched config
    and one sibling. Returns 0..N conflicts."""
    conflicts: list[SiblingConfigConflict] = []
    t_family = touched_view.get("family")
    s_family = sibling_view.get("family")
    if not t_family or not s_family:
        return conflicts

    # Resolve each side to a canonical (editorconfig | prettier |
    # eslint) view — package_json carries multiple families.
    t_views = _expand_family_views(t_family, touched_view)
    s_views = _expand_family_views(s_family, sibling_view)

    for t_fam, t_data in t_views.items():
        for s_fam, s_data in s_views.items():
            for check in _check_pairs(t_fam, t_data, s_fam, s_data):
                if check is None:
                    continue
                # Orient the conflict so touched_value reflects the
                # value coming from the touched file's family — not
                # the comparator's canonical (left, right) ordering.
                values = check["values"]
                t_val = values.get(t_fam, "?")
                s_val = values.get(s_fam, "?")
                conflicts.append(SiblingConfigConflict(
                    touched_file=touched_rel,
                    sibling_file=sibling_rel,
                    key=check["key"],
                    touched_value=t_val,
                    sibling_value=s_val,
                    message=check["message"],
                ))
    return conflicts


def _expand_family_views(
    family: str, view: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return ``{family_id: data}`` per family the file contributes
    to. Only ``package_json`` contributes more than one."""
    if family == "package_json":
        return view.get("data", {})
    if family == "editorconfig":
        return {"editorconfig": view.get("section", {})}
    if family in ("prettier", "eslint"):
        return {family: view.get("data", {})}
    return {}


def _check_pairs(
    a_fam: str, a_data: dict[str, Any],
    b_fam: str, b_data: dict[str, Any],
):
    """Yield comparator results for the (a_fam, b_fam) pairing.
    Order-insensitive via symmetric dispatch."""
    pair = frozenset({a_fam, b_fam})
    if pair == frozenset({"editorconfig", "prettier"}):
        eC = a_data if a_fam == "editorconfig" else b_data
        p = a_data if a_fam == "prettier" else b_data
        yield _check_indent(eC, p)
        yield _check_end_of_line(eC, p)
    elif pair == frozenset({"eslint", "prettier"}):
        e = a_data if a_fam == "eslint" else b_data
        p = a_data if a_fam == "prettier" else b_data
        yield _check_semi(e, p)
        yield _check_quotes(e, p)
    # editorconfig↔eslint not paired in v1; ESLint doesn't expose an
    # indent rule we parse simply enough yet.
