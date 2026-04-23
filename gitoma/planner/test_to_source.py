"""Layer-2 deterministic plan post-processor — rewrite a planner-emitted
T001's ``file_hints`` from test files to the source files those tests
exercise.

Caught live on rung-3 v8 (2026-04-23am): the planner's Occam HARD RULE
("T001 MUST target the failing test paths") was honoured — T001
priority=1 was emitted — but a 4B model interpreted "target the
failing tests" as "edit the test files". Worker burned its
build-retry budget breaking ``tests/test_db.py`` syntax twice while
the actual SQLi in ``src/db.py`` stayed untouched.

Layer-1 (the prompt rule) is necessary but not sufficient at 4B
scale. Layer-2 is deterministic: when the plan's T001 file_hints
are all under a tests/ directory AND the TestRunnerAnalyzer reported
failing tests, parse the test files' imports, resolve them to real
source files in the repo, and REWRITE file_hints. The planner LLM
keeps its task title + description; only the file targets change.

Per-language parsers, all stdlib regex (no AST setup):
  * Python: ``from src.db import …`` / ``import src.db`` / ``from .db``
  * Rust:   ``use crate_name::module::…`` / ``use crate_name``
  * Go:     ``import "module/path"`` (and same-package shorthand)
  * JS/TS:  ``import { foo } from "../src/render.js"`` / ``require()``

Each parser returns a list of CANDIDATE source-file paths relative to
the repo root. The orchestrator filters to those that actually exist.
If nothing resolves, the plan is left UNCHANGED — better to defer to
the planner's choice than to clobber it with garbage.
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Per-language import extractors ──────────────────────────────────────────


_PY_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+([a-zA-Z_][\w.]*)\s+import\s+", re.MULTILINE,
)
_PY_BARE_IMPORT_RE = re.compile(
    r"^\s*import\s+([a-zA-Z_][\w.]*)", re.MULTILINE,
)


def _python_imports_to_paths(source: str, repo_root: Path) -> list[str]:
    """Map ``from src.db import x`` → ``src/db.py`` (when it exists).

    Skips standard library and third-party imports by checking which
    candidate actually resolves to a file in ``repo_root``. The check
    is the safety net — never propose a hint that doesn't point at
    something real.
    """
    out: list[str] = []
    for m in _PY_FROM_IMPORT_RE.finditer(source):
        out.append(_py_module_to_path(m.group(1), repo_root))
    for m in _PY_BARE_IMPORT_RE.finditer(source):
        # Skip ``from X import …`` matches that the prior regex already grabbed
        out.append(_py_module_to_path(m.group(1), repo_root))
    return [p for p in out if p]


def _py_module_to_path(module: str, repo_root: Path) -> str | None:
    """``src.db`` → ``src/db.py`` if that path exists, else
    ``src/db/__init__.py``, else None. Stops at the first hit; we
    don't enumerate every possibility."""
    parts = module.split(".")
    candidates = [
        Path(*parts).with_suffix(".py"),
        Path(*parts) / "__init__.py",
    ]
    for cand in candidates:
        if (repo_root / cand).is_file():
            return str(cand).replace("\\", "/")
    return None


_RUST_USE_RE = re.compile(
    r"^\s*use\s+([a-zA-Z_][\w:]*)", re.MULTILINE,
)


def _rust_imports_to_paths(source: str, repo_root: Path) -> list[str]:
    """``use calc::divide;`` → ``src/calc.rs`` or ``src/lib.rs`` (when
    the crate root is named via ``[lib].name`` in Cargo.toml — most
    rungs use a top-level ``src/<crate>.rs``)."""
    out: list[str] = []
    for m in _RUST_USE_RE.finditer(source):
        crate_path = m.group(1).split("::")
        if not crate_path:
            continue
        # Try several conventions — first hit wins
        crate = crate_path[0]
        candidates = [
            Path("src") / f"{crate}.rs",
            Path("src") / crate / "lib.rs",
            Path("src") / "lib.rs",  # crate root
            Path("src") / "main.rs",
        ]
        for cand in candidates:
            if (repo_root / cand).is_file():
                out.append(str(cand).replace("\\", "/"))
                break
    return out


_GO_IMPORT_BLOCK_RE = re.compile(r"import\s+\(([\s\S]*?)\)", re.MULTILINE)
_GO_IMPORT_LINE_RE = re.compile(r'"([^"]+)"')


def _go_imports_to_paths(source: str, repo_root: Path, test_path: str) -> list[str]:
    """Go: imports look like ``"gitoma-bench-rung-1/store"``. Map to
    the actual directory in repo (each .go file under that dir is a
    candidate). Also include the same-package source files (Go puts
    test + source in the same dir + same package)."""
    out: list[str] = []

    # Same-package source files — strongest signal in Go
    test_dir = (Path(test_path).parent if test_path else Path("."))
    for src_file in (repo_root / test_dir).glob("*.go"):
        if src_file.name.endswith("_test.go"):
            continue
        rel = src_file.relative_to(repo_root)
        out.append(str(rel).replace("\\", "/"))

    # Cross-package imports inside import (...) blocks
    for block in _GO_IMPORT_BLOCK_RE.findall(source):
        for path in _GO_IMPORT_LINE_RE.findall(block):
            # Module-prefixed → strip first component (the module name)
            parts = path.split("/")
            if len(parts) >= 2:
                local_dir = Path(*parts[1:])
                pkg_dir = repo_root / local_dir
                if pkg_dir.is_dir():
                    for src_file in pkg_dir.glob("*.go"):
                        if src_file.name.endswith("_test.go"):
                            continue
                        rel = src_file.relative_to(repo_root)
                        out.append(str(rel).replace("\\", "/"))
    return out


_JS_IMPORT_FROM_RE = re.compile(
    r"""(?:import\s+[^;]*?\s+from|require\(\s*)\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _js_imports_to_paths(source: str, repo_root: Path, test_path: str) -> list[str]:
    """``import { foo } from "../src/render.js"`` → resolved relative
    to the test file's directory. Tries the literal path, then with
    common JS/TS extensions appended."""
    out: list[str] = []
    test_dir = Path(test_path).parent if test_path else Path(".")
    for m in _JS_IMPORT_FROM_RE.finditer(source):
        spec = m.group(1)
        if not spec.startswith((".", "/")):
            continue  # Skip bare/external imports (node_modules)
        candidates: list[Path] = []
        base = (test_dir / spec) if not spec.startswith("/") else Path(spec.lstrip("/"))
        # Literal first
        candidates.append(base)
        # Without extension → try common ones
        if base.suffix == "":
            for ext in (".js", ".ts", ".tsx", ".mjs"):
                candidates.append(base.with_suffix(ext))
            # index.* in the directory
            for ext in (".js", ".ts", ".tsx", ".mjs"):
                candidates.append(base / f"index{ext}")
        for cand in candidates:
            try:
                # Resolve relative to repo_root and check existence
                resolved = (repo_root / cand).resolve()
                if resolved.is_file() and resolved.is_relative_to(repo_root.resolve()):
                    rel = resolved.relative_to(repo_root.resolve())
                    out.append(str(rel).replace("\\", "/"))
                    break
            except (OSError, ValueError):
                continue
    return out


# ── Orchestrator ────────────────────────────────────────────────────────────


def infer_source_files_from_tests(
    test_paths: list[str], repo_root: Path,
) -> list[str]:
    """Read each ``test_path`` (relative to ``repo_root``), parse its
    imports, return the set of source files in the repo those tests
    depend on. Empty list when nothing resolves — caller should leave
    the plan unchanged in that case."""
    seen: set[str] = set()
    out: list[str] = []
    for tp in test_paths:
        full = repo_root / tp
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        candidates: list[str] = []
        suffix = full.suffix.lower()
        if suffix == ".py":
            candidates = _python_imports_to_paths(source, repo_root)
        elif suffix == ".rs":
            candidates = _rust_imports_to_paths(source, repo_root)
        elif suffix == ".go":
            candidates = _go_imports_to_paths(source, repo_root, tp)
        elif suffix in {".js", ".mjs", ".ts", ".tsx", ".jsx"}:
            candidates = _js_imports_to_paths(source, repo_root, tp)

        for cand in candidates:
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def rewrite_plan_in_place(plan, report, repo_root: Path) -> dict | None:
    """Layer-2 post-process: when conditions warrant, rewrite ``plan.tasks[0]``'s
    file_hints from test paths to source paths.

    Conditions (ALL must hold):
      * The metric report contains ``test_results`` with status ``fail``.
      * Plan has at least one task.
      * Every subtask of T001 has file_hints that are test-only.
      * The test-import parsing yields at least one source-file path.

    On success, mutates the plan in place AND returns a dict describing
    what changed (for trace logging). On no-op, returns ``None`` so the
    caller can record that the orchestrator ran but didn't fire.
    """
    test_results_failing = any(
        m.name == "test_results" and m.status == "fail" for m in report.metrics
    )
    if not test_results_failing or not plan.tasks:
        return None

    t001 = plan.tasks[0]
    # Collect every file_hint across the task's subtasks
    current_hints: list[str] = []
    for sub in t001.subtasks:
        current_hints.extend(getattr(sub, "file_hints", []) or [])
    if not current_hints or not all(is_test_only_targeting([h]) for h in current_hints):
        return None  # Already targets a source file (or has no hints)

    # Resolve test → source
    inferred = infer_source_files_from_tests(current_hints, repo_root)
    if not inferred:
        return None  # Nothing resolved — leave plan alone

    # Rewrite each subtask's file_hints to the inferred sources
    before = list(current_hints)
    for sub in t001.subtasks:
        sub.file_hints = list(inferred)
    return {
        "before": before,
        "after": list(inferred),
        "task_id": t001.id,
        "subtasks_rewritten": len(t001.subtasks),
    }


def is_test_only_targeting(file_hints: list[str]) -> bool:
    """Cheap predicate: are all of T001's file_hints under a tests/-like
    directory? Used to decide whether to apply the rewrite."""
    if not file_hints:
        return False
    test_markers = ("test_", "_test.")
    for hint in file_hints:
        parts = Path(hint).parts
        # Path lives under a tests/ dir somewhere?
        if not any(p.lower() in {"tests", "test", "__tests__", "spec"} for p in parts):
            # Or has a test naming convention?
            name = Path(hint).name
            if not any(marker in name for marker in test_markers):
                return False
    return True
