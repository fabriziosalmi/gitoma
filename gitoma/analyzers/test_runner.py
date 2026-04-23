"""Test-runner preflight analyzer — runs the project's tests at audit
time so the planner sees the FAILING test list as direct evidence,
not as a derived "Test Suite present at 35%" score.

Caught live across rung-3 v1..v6 (2026-04-22pm): the existing
TestsAnalyzer counts test FILES (35% = present), so a project with
2/4 failing tests scores the same as one with 4/4 passing. The
planner has no signal that the file ``src/db.py`` is broken right
now, so it generates 12 cosmetic subtasks and never targets the
bug. This analyzer is the "Occam pre-filter" — failing test = T001
priority-1, no LLM judgement required.

Failure modes deliberately handled:
  * Toolchain not in PATH → soft-pass score=1.0 with "skipped (no
    pytest/cargo/go in PATH)" so the analyzer never becomes a
    reason to abort a run on a dev machine.
  * Bounded timeout (default 90s) → soft-pass on slow suites.
  * Output parse failure → score=0 conservative + raw stderr in
    details. We'd rather over-flag than under-flag.

Output is consumed by the planner prompt (see prompts.py) which
enforces a HARD RULE: when test_runner reports fail, T001 must
target one of the listed failing files.
"""

from __future__ import annotations

import re
import subprocess
import sys

from gitoma.analyzers.base import BaseAnalyzer, MetricResult


# Per-language run command + output-parser hook. Entry order matters —
# first matching marker file wins, so a polyglot repo gets exactly one
# run command (the most prominent stack).
#
# JS/TS path: ``npm test`` runs whatever is in ``package.json scripts.test``.
# Many repos wire stdlib ``node --test`` there (rung-4 does), which works
# offline. Repos that wire jest/vitest need their node_modules installed
# already; we can't fix that, but we don't try — soft-pass on the
# eventual subprocess.run failure.
_LANG_RUNNERS: tuple[tuple[str, tuple[str, ...], list[str], str], ...] = (
    # (language, marker files, command, parser key)
    ("Python", ("pyproject.toml", "setup.py", "setup.cfg"),
     [sys.executable, "-m", "pytest", "-q", "--tb=line", "--no-header"], "pytest"),
    ("Rust", ("Cargo.toml",),
     ["cargo", "test", "--quiet", "--no-fail-fast"], "cargo"),
    ("Go", ("go.mod",),
     ["go", "test", "./..."], "go"),
    ("JavaScript", ("package.json",),
     ["npm", "test", "--silent"], "node"),
    ("TypeScript", ("package.json",),
     ["npm", "test", "--silent"], "node"),
)


# Bounded timeout. 90s covers most tiny / med-sized test suites; on a
# huge monorepo we soft-pass rather than block the run for minutes.
_TIMEOUT_SEC = 90


class TestRunnerAnalyzer(BaseAnalyzer):
    # Pytest collects classes named ``Test*`` by default; tell it to
    # skip — this is a production analyzer, not a test class.
    __test__ = False

    name = "test_results"
    display_name = "Test Results"
    # Weight = 4. High enough to dominate cosmetic metrics in the
    # weighted overall_score, slightly below BuildAnalyzer (5) because
    # a non-compiling project blocks tests entirely — Build comes first.
    weight = 4.0

    def analyze(self) -> MetricResult:
        for lang, markers, cmd, parser_key in _LANG_RUNNERS:
            if lang not in self.languages:
                continue
            if not any((self.root / m).exists() for m in markers):
                continue
            return self._run(lang, cmd, parser_key)

        # No matching language → silent pass
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=1.0,
            details=f"Test runner skipped (no recognised stack in {', '.join(self.languages) or 'Unknown'})",
            weight=self.weight,
        )

    def _run(self, lang: str, cmd: list[str], parser_key: str) -> MetricResult:
        try:
            r = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} test run skipped (toolchain not in PATH: {cmd[0]})",
                weight=self.weight,
            )
        except subprocess.TimeoutExpired:
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} test run timed out after {_TIMEOUT_SEC}s — soft-pass",
                weight=self.weight,
            )

        if r.returncode == 0:
            count = _count_passing(parser_key, r.stdout, r.stderr)
            count_note = f" ({count} test(s))" if count else ""
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=1.0,
                details=f"{lang} tests passing{count_note}",
                weight=self.weight,
            )

        failing = _parse_failing(parser_key, r.stdout, r.stderr)
        if not failing:
            # Non-zero exit but we couldn't parse specific failures —
            # still report fail (conservative) with a tail of stderr.
            tail = ((r.stderr or r.stdout) or "")[-600:]
            return MetricResult.from_score(
                name=self.name,
                display_name=self.display_name,
                score=0.0,
                details=(
                    f"{lang} TESTS FAILED (parser couldn't extract specific "
                    f"failures, raw tail follows). Fix these BEFORE adding "
                    f"any new feature, doc, or config:\n{tail}"
                ),
                weight=self.weight,
            )

        # Format the planner-facing payload — every failure with
        # file path so the planner can target the right files.
        bullets = "\n".join(f"  • {f}" for f in failing[:12])
        more = f"\n  … (+{len(failing) - 12} more)" if len(failing) > 12 else ""
        return MetricResult.from_score(
            name=self.name,
            display_name=self.display_name,
            score=0.0,
            details=(
                f"{lang} TESTS FAILING ({len(failing)}). Your T001 MUST target "
                f"one of these failing test paths — not docs, not CI, not "
                f"license. Fix the code that makes them red:\n{bullets}{more}"
            ),
            suggestions=[
                f"Read the failing test(s) and fix the code under test: "
                f"{', '.join(failing[:3])}"
            ],
            weight=self.weight,
        )


# ── Parsers ─────────────────────────────────────────────────────────────────
#
# Every parser returns a list of "file::test" strings (or just file paths
# when the test name isn't recoverable). The planner reads these as
# concrete targets.

# pytest emits failures in two shapes:
#   * Live (one per test as it runs):    ``tests/x.py::test_y FAILED``
#   * Summary (after the run completes): ``FAILED tests/x.py::test_y``
#     — and may have a trailing ``- AssertionError: ...`` reason block,
#     which we tolerate (caught live on rung-3 v7 — the trailing reason
#     made the original ``\s*$``-anchored regex miss the path).
# Match both so the parser is robust regardless of which output snippet
# the analyzer captures.
_PYTEST_FAIL_RE = re.compile(
    r"^(?:FAILED\s+(\S+)(?:\s+-\s+.*)?|(\S+)\s+FAILED(?:\s+\(.*\))?)\s*$",
    re.MULTILINE,
)
_PYTEST_ERROR_RE = re.compile(
    r"^(?:ERROR\s+(\S+)(?:\s+-\s+.*)?|(\S+)\s+ERROR(?:\s+\(.*\))?)\s*$",
    re.MULTILINE,
)
_PYTEST_PASS_COUNT_RE = re.compile(r"(\d+)\s+passed", re.MULTILINE)

_CARGO_FAIL_RE = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", re.MULTILINE)
_CARGO_FAIL_SUMMARY_RE = re.compile(r"^failures:\s*\n((?:\s+.+\n)+)", re.MULTILINE)
_CARGO_PASS_COUNT_RE = re.compile(r"test result:\s+\w+\.\s+(\d+)\s+passed", re.MULTILINE)

_GO_FAIL_RE = re.compile(r"^---\s+FAIL:\s+(\S+)", re.MULTILINE)
_GO_PASS_COUNT_RE = re.compile(r"^ok\s+\S+", re.MULTILINE)

# Node ``node --test`` (and TAP-emitting wrappers) print failing test
# names in two shapes that we both pick up:
#   * ``✖ test name (Xms)`` — TTY-rendered TAP. Requires the timing
#     suffix to filter out section headers like ``✖ failing tests:``
#     which would otherwise be captured as a fake test name.
#   * ``not ok N - test name`` — raw TAP. Numbered, unambiguous.
_NODE_FAIL_RE = re.compile(
    r"(?:^✖\s+(.+?)\s+\(\d+(?:\.\d+)?ms\)\s*$|^not\s+ok\s+\d+\s+-\s+(.+?)$)",
    re.MULTILINE,
)
_NODE_PASS_COUNT_RE = re.compile(r"^(?:ℹ\s+pass\s+|# pass\s+)(\d+)\s*$", re.MULTILINE)


def _parse_failing(parser_key: str, stdout: str, stderr: str) -> list[str]:
    text = (stdout or "") + "\n" + (stderr or "")
    if parser_key == "pytest":
        # findall returns tuples for the two-group alternation; flatten
        # by picking the first non-empty group from each tuple.
        out: list[str] = []
        for groups in _PYTEST_FAIL_RE.findall(text) + _PYTEST_ERROR_RE.findall(text):
            for g in groups:
                if g:
                    out.append(g)
                    break
    elif parser_key == "cargo":
        out = list(_CARGO_FAIL_RE.findall(text))
        if not out:
            for chunk in _CARGO_FAIL_SUMMARY_RE.findall(text):
                out.extend(line.strip() for line in chunk.splitlines() if line.strip())
    elif parser_key == "go":
        out = list(_GO_FAIL_RE.findall(text))
    elif parser_key == "node":
        out = []
        for groups in _NODE_FAIL_RE.findall(text):
            for g in groups:
                if g and g.strip():
                    out.append(g.strip())
                    break
    else:
        out = []
    # Deduplicate, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def detect_failing_tests(
    root, languages: list[str], timeout_sec: int = _TIMEOUT_SEC,
) -> set[str] | None:
    """Run the project's tests and return the set of failing test
    identifiers. Returns ``None`` when the toolchain isn't available
    / times out / the repo has no recognisable stack — callers use
    ``None`` to mean "don't enforce regression check on this run".

    Reuses the per-language runner + parser logic from
    ``TestRunnerAnalyzer``; this helper is the no-scaffolding variant
    G8 (runtime regression gate) consumes to compare
    before-subtask vs after-subtask failing sets.

    Uncollectable tests / import errors are treated as failures (they
    surface via the ERROR parser on pytest, non-zero exit on cargo/go,
    ``not ok`` on node). This is what makes G8 catch the rung-3 v17/v18
    fixture-deletion case: when ``tests/test_db.py`` loses the ``db``
    fixture, pytest raises ``fixture 'db' not found`` as a collection
    ERROR, which parses as a failing test.
    """
    from pathlib import Path as _Path
    root = _Path(root)

    for lang, markers, cmd, parser_key in _LANG_RUNNERS:
        if lang not in languages:
            continue
        if not any((root / m).exists() for m in markers):
            continue
        try:
            r = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0:
            return set()
        failing = _parse_failing(parser_key, r.stdout, r.stderr)
        if failing:
            return set(failing)
        # Non-zero exit but no parseable failures — conservatively
        # report a sentinel "unparsed" entry so the regression set is
        # non-empty and caller treats it as a failure.
        tail = ((r.stderr or r.stdout) or "")[-200:].strip()
        return {f"<unparsed-failure>: {tail[:120]}"}

    return None


def _count_passing(parser_key: str, stdout: str, stderr: str) -> int:
    text = (stdout or "") + "\n" + (stderr or "")
    if parser_key == "pytest":
        m = _PYTEST_PASS_COUNT_RE.search(text)
        return int(m.group(1)) if m else 0
    if parser_key == "cargo":
        m = _CARGO_PASS_COUNT_RE.search(text)
        return int(m.group(1)) if m else 0
    if parser_key == "go":
        return len(_GO_PASS_COUNT_RE.findall(text))
    if parser_key == "node":
        m = _NODE_PASS_COUNT_RE.search(text)
        return int(m.group(1)) if m else 0
    return 0
