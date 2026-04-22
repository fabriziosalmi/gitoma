"""Build-integrity preflight analyzer.

Runs the language's canonical compile/syntax-check command before the
planner sees the repo. Its output enters the metric report like any
other analyzer's — but with a deliberately-high weight, so when the
project does not compile the planner prioritises that over cosmetic
scaffolding (LICENSE, CONTRIBUTING, etc.). Zero LLM, zero network.

Caught live on 2026-04-22pm rung-1: without this analyzer, gitoma
would plan 8 "improve a Go project" scaffolding tasks on a repo where
``go build ./...`` fails at line 27 — wasting ~5 min of worker cycles
on noise instead of the actual bug.

On top of the raw stderr, the analyzer also attaches DETERMINISTIC
enrichment (no LLM, no network):
  * ``path:line[:col]: msg`` parsed into a structured error list
  * Source snippet (± 3 lines) around each error location
  * Error taxonomy tag (signature_mismatch | undefined_symbol |
    syntax_error | type_mismatch | missing_import | other)
  * Cross-file pointer when the error mentions a type / method whose
    definition lives in another file of the repo (best-effort grep)

Every bit of context we pre-compute here = fewer LLM roundtrips (and
fewer chances to hallucinate) downstream.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from gitoma.analyzers.base import BaseAnalyzer, MetricResult

# lang → (presence marker, build/check command). Commands must be:
#   * non-mutating (read-only / dry-check style)
#   * bounded-latency (< ~60s on tiny repos, hard-capped by ``timeout``)
#   * available in PATH where gitoma runs (detected by ``FileNotFoundError``)
# When the toolchain is missing, we gracefully skip (score=1.0) rather
# than failing the build check — the analyzer should never become
# another reason to fail a run.
_LANG_COMMANDS: dict[str, tuple[list[str], list[str]]] = {
    "Go": (["go.mod"], ["go", "build", "./..."]),
    "Rust": (["Cargo.toml"], ["cargo", "check", "--quiet"]),
    # TypeScript compile check only (no JS fallback because any random
    # JS file may legitimately fail node --check without being broken).
    "TypeScript": (["tsconfig.json"], ["npx", "--no-install", "tsc", "--noEmit"]),
}


class BuildAnalyzer(BaseAnalyzer):
    name = "build"
    display_name = "Build Integrity"
    # Weight = 5 so a failing build dominates the weighted overall score —
    # a non-compiling project can't benefit from any other "improvement".
    weight = 5.0

    def analyze(self) -> MetricResult:
        # Try language-specific toolchains first
        for lang in self.languages:
            spec = _LANG_COMMANDS.get(lang)
            if spec is not None:
                markers, cmd = spec
                if any((self.root / m).exists() for m in markers):
                    return self._run_cmd(lang, cmd)
            if lang == "Python":
                py_result = self._check_python()
                if py_result is not None:
                    return py_result

        # No recognised toolchain → pass silently. (The alternative would
        # be a false positive on exotic-language repos where we simply
        # can't know whether they compile.)
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=1.0,
            details=(
                "Build check skipped (no matching toolchain for "
                f"{', '.join(self.languages) or 'Unknown'})"
            ),
            weight=self.weight,
        )

    def _run_cmd(self, lang: str, cmd: list[str]) -> MetricResult:
        try:
            r = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} build check skipped (toolchain not in PATH: {cmd[0]})",
                weight=self.weight,
            )
        except subprocess.TimeoutExpired:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} build check timed out after 60s — treated as soft-pass",
                weight=self.weight,
            )

        if r.returncode == 0:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} builds clean ({' '.join(cmd)})",
                weight=self.weight,
            )

        raw = (r.stderr or r.stdout)
        enriched = _enrich_build_errors(raw, self.root)
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=0.0,
            details=(
                f"{lang} BUILD FAILED — project does not compile. "
                "Fix these errors BEFORE anything else (no feature work, no docs, "
                "no CI, no license — a non-compiling project is worth nothing):\n\n"
                + enriched
            ),
            suggestions=[f"Fix compile errors surfaced by `{' '.join(cmd)}`"],
            weight=self.weight,
        )

    def _check_python(self) -> MetricResult | None:
        """Syntax-check every .py file via ``py_compile`` (stdlib, zero deps).

        Returns ``None`` when there are no .py files to check, so the
        caller continues trying other toolchains (a mixed Python+Go repo
        should use both the Go and Python checks).
        """
        pyfiles = [p for p in self.root.rglob("*.py") if ".git" not in p.parts and ".venv" not in p.parts]
        if not pyfiles:
            return None

        errors: list[str] = []
        for pf in pyfiles[:80]:  # cap to bound latency on huge repos
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", str(pf)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if r.returncode != 0:
                msg = (r.stderr or r.stdout).strip().replace("\n", " ")
                errors.append(f"{pf.relative_to(self.root)}: {msg[:200]}")

        if errors:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=0.0,
                details=(
                    f"Python BUILD FAILED — {len(errors)} file(s) do not compile. "
                    "Fix these errors BEFORE anything else:\n" + "\n".join(errors[:10])
                ),
                suggestions=["Fix syntax errors surfaced by `python -m py_compile`"],
                weight=self.weight,
            )
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=1.0,
            details=f"Python syntax check clean ({len(pyfiles)} file(s))",
            weight=self.weight,
        )


def _truncate_errors(raw: str, max_lines: int = 10, max_chars: int = 1500) -> str:
    lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
    head = "\n".join(lines[:max_lines])
    if len(head) > max_chars:
        head = head[:max_chars] + " …(truncated)"
    if len(lines) > max_lines:
        head += f"\n… (+{len(lines) - max_lines} more lines omitted)"
    return head


# ── Deterministic enrichment ────────────────────────────────────────────────
#
# The raw stderr is already useful; the planner / worker prompts eat it
# directly. But we can do better without any LLM: parse the error
# locations, read the offending source lines, attach a taxonomy tag,
# and try to point at the cross-file definition when the error
# references a symbol. Every byte of context we fix here is a byte the
# LLM doesn't need to invent.

_LOC_RE = re.compile(
    r"""
    (?P<path>[\w./\\-]+\.(?:go|rs|ts|tsx|py|js|jsx|java|c|h|cpp|hpp))  # file
    :
    (?P<line>\d+)
    (?::(?P<col>\d+))?
    :\s*
    (?P<msg>.+?)$
    """,
    re.VERBOSE | re.MULTILINE,
)


_TAXONOMY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Order matters: more specific first.
    ("signature_mismatch", re.compile(r"assignment mismatch|too many (return )?values|not enough (return )?values|wrong number of arguments", re.IGNORECASE)),
    ("type_mismatch",      re.compile(r"cannot use .* as .* in|mismatched types|expected .*, found|incompatible types", re.IGNORECASE)),
    ("undefined_symbol",   re.compile(r"\bundefined\b|\bnot declared\b|\bunresolved\b|\bcannot find\b|name .* is not defined|NameError", re.IGNORECASE)),
    ("missing_import",     re.compile(r"\bimport\b.*\bnot found\b|\bno module named\b|\bModuleNotFoundError\b|unused import", re.IGNORECASE)),
    ("syntax_error",       re.compile(r"\bsyntax error\b|expected .* found|SyntaxError|parse error|unexpected token", re.IGNORECASE)),
)


def _classify(msg: str) -> str:
    for tag, pat in _TAXONOMY_PATTERNS:
        if pat.search(msg):
            return tag
    return "other"


def _read_snippet(root: Path, relpath: str, line: int, radius: int = 3) -> str | None:
    """Read ± radius lines around ``line`` (1-indexed) from ``root/relpath``.

    Output includes line numbers and a ``>`` marker on the error line.
    Returns None if the file is unreadable or the line is out of range.
    """
    fp = (root / relpath).resolve()
    try:
        fp.relative_to(root.resolve())  # guard against path traversal
    except ValueError:
        return None
    if not fp.is_file():
        return None
    try:
        src_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if line < 1 or line > len(src_lines):
        return None
    start = max(1, line - radius)
    end = min(len(src_lines), line + radius)
    width = len(str(end))
    out = []
    for i in range(start, end + 1):
        prefix = ">" if i == line else " "
        out.append(f"  {prefix} {i:>{width}}| {src_lines[i - 1]}")
    return "\n".join(out)


def _find_cross_file_hint(root: Path, msg: str, current_path: str) -> str | None:
    """When the error message mentions a method/type (e.g., ``s.users.Get``),
    try to locate its DEFINITION elsewhere in the repo via literal grep on
    ``func.*Get`` / ``def Get`` / ``class Get``. Best-effort, deterministic;
    returns None when nothing is found.
    """
    # Extract identifier-looking tokens from the message
    toks = re.findall(r"[A-Z][A-Za-z_]{2,}|\b[a-z][a-zA-Z_]{3,}\b", msg)
    # Drop very common English words to avoid greps on "returns" / "values"
    stopwords = {"returns", "values", "variable", "assignment", "mismatch", "expected",
                 "found", "cannot", "declared", "undefined", "defined", "needed",
                 "imported", "module", "syntax"}
    candidates = [t for t in toks if t.lower() not in stopwords][:3]
    if not candidates:
        return None

    hits: list[str] = []
    for path in root.rglob("*"):
        if ".git" in path.parts or "node_modules" in path.parts or "target" in path.parts or ".venv" in path.parts:
            continue
        if not path.is_file() or path.suffix not in {".go", ".py", ".rs", ".ts", ".tsx", ".js"}:
            continue
        try:
            relp = str(path.relative_to(root))
        except ValueError:
            continue
        if relp == current_path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tok in candidates:
            # Language-agnostic "definition-like" patterns
            pat = rf"\b(?:func|def|class|fn|type)\b[^(]*\b{re.escape(tok)}\b"
            m = re.search(pat, text)
            if m:
                line_no = text[: m.start()].count("\n") + 1
                hits.append(f"{relp}:{line_no} (defines {tok!r})")
                break
        if len(hits) >= 3:
            break
    return "; ".join(hits) if hits else None


def _enrich_build_errors(raw: str, root: Path) -> str:
    """Turn raw stderr into a structured block for the planner prompt.

    The raw text is preserved at the top (model may prefer the
    original formatting its language's compiler emits). Below it we
    append per-error structured blocks with source snippets + taxonomy
    + cross-file hints.
    """
    raw_trim = _truncate_errors(raw)
    matches = list(_LOC_RE.finditer(raw))
    if not matches:
        return raw_trim

    # De-duplicate on (path, line)
    seen: set[tuple[str, int]] = set()
    blocks: list[str] = []
    for m in matches[:6]:  # cap for prompt budget
        path = m.group("path").lstrip("./")
        line = int(m.group("line"))
        if (path, line) in seen:
            continue
        seen.add((path, line))
        col = m.group("col")
        msg = m.group("msg").strip()
        tag = _classify(msg)
        snippet = _read_snippet(root, path, line)
        hint = _find_cross_file_hint(root, msg, path)

        chunk = [f"• {path}:{line}" + (f":{col}" if col else "") + f"  [{tag}]", f"  {msg}"]
        if snippet:
            chunk.append(snippet)
        if hint:
            chunk.append(f"  related: {hint}")
        blocks.append("\n".join(chunk))

    return raw_trim + "\n\n── structured errors ──\n\n" + "\n\n".join(blocks)
