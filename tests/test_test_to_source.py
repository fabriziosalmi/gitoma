"""Layer-2 deterministic plan-rewrite tests.

Caught live on rung-3 v8 (2026-04-23am): planner emitted T001 with
priority=1 (Layer-1 prompt rule worked) but pointed file_hints at
``tests/test_db.py`` instead of ``src/db.py``. The fix is to parse
the test file's imports, resolve them to real source files in the
repo, and rewrite the file_hints — deterministically, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitoma.planner.test_to_source import (
    infer_source_files_from_tests,
    is_test_only_targeting,
    rewrite_plan_in_place,
)


# ── is_test_only_targeting ──────────────────────────────────────────────────


def test_test_only_targeting_recognises_tests_dir() -> None:
    assert is_test_only_targeting(["tests/test_db.py"])
    assert is_test_only_targeting(["tests/foo/test_x.py"])
    assert is_test_only_targeting(["__tests__/render.test.js"])
    assert is_test_only_targeting(["spec/core_spec.rb"])


def test_test_only_targeting_recognises_test_naming() -> None:
    """Files outside a tests/ dir but with the test_/_test naming
    convention also count — Go puts ``server_test.go`` next to
    ``server.go`` in the same package directory."""
    assert is_test_only_targeting(["server/server_test.go"])
    assert is_test_only_targeting(["src/foo_test.py"])
    assert is_test_only_targeting(["src/test_bar.py"])


def test_test_only_targeting_rejects_source_paths() -> None:
    assert not is_test_only_targeting(["src/db.py"])
    assert not is_test_only_targeting(["server/server.go"])
    # Mixed → also reject (can't safely rewrite when one is real source)
    assert not is_test_only_targeting(["src/db.py", "tests/test_db.py"])


def test_test_only_targeting_rejects_empty() -> None:
    """No hints to rewrite → no-op signal."""
    assert not is_test_only_targeting([])


# ── infer_source_files_from_tests — Python ──────────────────────────────────


def test_python_from_import_resolves_to_module_file(tmp_path: Path) -> None:
    """``from src.db import x`` → ``src/db.py``."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text("def x(): ...")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text(
        "from src.db import x\n"
        "def test_x(): assert x() is None\n"
    )
    out = infer_source_files_from_tests(["tests/test_db.py"], tmp_path)
    assert "src/db.py" in out


def test_python_resolves_package_init_when_module_file_missing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db").mkdir()
    (tmp_path / "src" / "db" / "__init__.py").write_text("def x(): ...")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text("from src.db import x\n")
    out = infer_source_files_from_tests(["tests/test_db.py"], tmp_path)
    assert "src/db/__init__.py" in out


def test_python_skips_unresolvable_imports(tmp_path: Path) -> None:
    """``import requests`` (third-party) doesn't resolve in the repo;
    must be silently skipped, not fabricated."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import requests\n")
    out = infer_source_files_from_tests(["tests/test_x.py"], tmp_path)
    assert out == []


# ── infer_source_files_from_tests — Go ──────────────────────────────────────


def test_go_resolves_same_package_source(tmp_path: Path) -> None:
    """Go test + source live in the same dir + same package — the
    most common pattern. ``server/server_test.go`` → ``server/server.go``."""
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.go").write_text("package server\n")
    (tmp_path / "server" / "server_test.go").write_text(
        'package server\nimport "testing"\nfunc TestX(t *testing.T) {}\n'
    )
    out = infer_source_files_from_tests(["server/server_test.go"], tmp_path)
    assert "server/server.go" in out
    # And does NOT include the test file itself
    assert "server/server_test.go" not in out


def test_go_resolves_imported_package(tmp_path: Path) -> None:
    """``import "myproject/store"`` → every .go under ``store/``."""
    (tmp_path / "go.mod").write_text("module myproject\n")
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "store.go").write_text("package store\n")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.go").write_text("package server\n")
    (tmp_path / "server" / "server_test.go").write_text(
        'package server\nimport (\n  "testing"\n  "myproject/store"\n)\nfunc TestX(t *testing.T){}\n'
    )
    out = infer_source_files_from_tests(["server/server_test.go"], tmp_path)
    assert "server/server.go" in out
    assert "store/store.go" in out


# ── infer_source_files_from_tests — Rust ────────────────────────────────────


def test_rust_use_statement_resolves_to_crate_file(tmp_path: Path) -> None:
    """``use mylib::foo;`` → ``src/mylib.rs`` (when it exists). Falls
    through several conventional paths."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mylib.rs").write_text("pub fn foo() {}")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "lib_test.rs").write_text(
        "use mylib::foo;\n#[test] fn t() {}\n"
    )
    out = infer_source_files_from_tests(["tests/lib_test.rs"], tmp_path)
    assert "src/mylib.rs" in out


# ── infer_source_files_from_tests — JS ──────────────────────────────────────


def test_js_relative_import_resolves(tmp_path: Path) -> None:
    """``import { foo } from "../src/render.js"`` → ``src/render.js``."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "render.js").write_text("export function f() {}")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "r.test.js").write_text(
        'import { f } from "../src/render.js";\n'
    )
    out = infer_source_files_from_tests(["tests/r.test.js"], tmp_path)
    assert "src/render.js" in out


def test_js_relative_import_without_extension_resolves(tmp_path: Path) -> None:
    """``import x from "../src/render"`` → tries common extensions."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "render.js").write_text("export const x = 1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "r.test.js").write_text(
        'import { x } from "../src/render";\n'
    )
    out = infer_source_files_from_tests(["tests/r.test.js"], tmp_path)
    assert "src/render.js" in out


def test_js_skips_bare_imports(tmp_path: Path) -> None:
    """``import express from "express"`` → external, not in repo."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "r.test.js").write_text('import e from "express";\n')
    out = infer_source_files_from_tests(["tests/r.test.js"], tmp_path)
    assert out == []


# ── rewrite_plan_in_place — orchestrator ────────────────────────────────────


@dataclass
class _FakeMetric:
    name: str
    status: str


@dataclass
class _FakeReport:
    metrics: list[_FakeMetric]


@dataclass
class _FakeSubtask:
    file_hints: list[str] = field(default_factory=list)


@dataclass
class _FakeTask:
    id: str = "T001"
    subtasks: list[_FakeSubtask] = field(default_factory=list)


@dataclass
class _FakePlan:
    tasks: list[_FakeTask] = field(default_factory=list)


def _setup_python_repo(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text("def f(): ...")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text("from src.db import f\n")


def test_rewrite_fires_when_t001_targets_only_tests(tmp_path: Path) -> None:
    """The bug-C-fix happy path: T001 has tests/* as file_hints AND
    Test Results = fail → rewrite to src/* in place."""
    _setup_python_repo(tmp_path)
    report = _FakeReport(metrics=[_FakeMetric("test_results", "fail")])
    plan = _FakePlan(tasks=[_FakeTask(subtasks=[_FakeSubtask(file_hints=["tests/test_db.py"])])])

    out = rewrite_plan_in_place(plan, report, tmp_path)

    assert out is not None
    assert plan.tasks[0].subtasks[0].file_hints == ["src/db.py"]
    assert out["before"] == ["tests/test_db.py"]
    assert out["after"] == ["src/db.py"]


def test_rewrite_skips_when_test_results_passing(tmp_path: Path) -> None:
    _setup_python_repo(tmp_path)
    report = _FakeReport(metrics=[_FakeMetric("test_results", "pass")])
    plan = _FakePlan(tasks=[_FakeTask(subtasks=[_FakeSubtask(file_hints=["tests/test_db.py"])])])
    assert rewrite_plan_in_place(plan, report, tmp_path) is None
    assert plan.tasks[0].subtasks[0].file_hints == ["tests/test_db.py"]  # unchanged


def test_rewrite_skips_when_t001_already_targets_source(tmp_path: Path) -> None:
    """Don't clobber a planner that DID get it right."""
    _setup_python_repo(tmp_path)
    report = _FakeReport(metrics=[_FakeMetric("test_results", "fail")])
    plan = _FakePlan(tasks=[_FakeTask(subtasks=[_FakeSubtask(file_hints=["src/db.py"])])])
    assert rewrite_plan_in_place(plan, report, tmp_path) is None
    assert plan.tasks[0].subtasks[0].file_hints == ["src/db.py"]


def test_rewrite_skips_when_no_imports_resolve(tmp_path: Path) -> None:
    """Test file imports only third-party → no source to rewrite to →
    leave plan alone (better than fabricating a wrong target)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import requests\n")
    report = _FakeReport(metrics=[_FakeMetric("test_results", "fail")])
    plan = _FakePlan(tasks=[_FakeTask(subtasks=[_FakeSubtask(file_hints=["tests/test_x.py"])])])
    assert rewrite_plan_in_place(plan, report, tmp_path) is None
    assert plan.tasks[0].subtasks[0].file_hints == ["tests/test_x.py"]


def test_rewrite_skips_empty_plan(tmp_path: Path) -> None:
    report = _FakeReport(metrics=[_FakeMetric("test_results", "fail")])
    plan = _FakePlan(tasks=[])
    assert rewrite_plan_in_place(plan, report, tmp_path) is None
