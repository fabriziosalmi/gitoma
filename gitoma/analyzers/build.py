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
"""

from __future__ import annotations

import subprocess

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

        errors = _truncate_errors(r.stderr or r.stdout)
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=0.0,
            details=(
                f"{lang} BUILD FAILED — project does not compile. "
                "Fix these errors BEFORE anything else (no feature work, no docs, "
                "no CI, no license — a non-compiling project is worth nothing):\n"
                + errors
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
