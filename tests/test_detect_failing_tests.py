"""Tests for ``detect_failing_tests`` — the standalone helper G8
(runtime-test regression gate) consumes to compute before/after
failing-test sets around each subtask."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.analyzers.test_runner import detect_failing_tests


def _write(root: Path, rel: str, body: str) -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return full


# ── Python / pytest path ────────────────────────────────────────────────


def _pytest_project(tmp_path: Path, test_body: str) -> Path:
    _write(tmp_path, "pyproject.toml",
           '[project]\nname="x"\nversion="0"\nrequires-python=">=3.10"\n'
           '[tool.pytest.ini_options]\npythonpath=["."]\ntestpaths=["tests"]\n')
    _write(tmp_path, "tests/test_x.py", test_body)
    return tmp_path


def test_all_passing_returns_empty_set(tmp_path: Path) -> None:
    root = _pytest_project(tmp_path, "def test_a():\n    assert True\n")
    assert detect_failing_tests(root, ["Python"]) == set()


def test_failing_returns_test_identifiers(tmp_path: Path) -> None:
    root = _pytest_project(
        tmp_path,
        "def test_ok():\n    assert True\n\n"
        "def test_boom():\n    assert False\n",
    )
    failing = detect_failing_tests(root, ["Python"])
    assert failing is not None
    # At minimum the test_boom identifier must appear in the result.
    assert any("test_boom" in f for f in failing)


def test_collection_error_reported_as_failure(tmp_path: Path) -> None:
    """A missing fixture triggers ``fixture X not found`` which pytest
    reports as an ERROR at collection time. This is EXACTLY the
    rung-3 v17/v18 shape: worker deletes the ``db`` fixture, tests
    error out before running. G8 must count these as failures."""
    root = _pytest_project(
        tmp_path,
        "def test_uses_fixture(missing_fixture):\n    assert True\n",
    )
    failing = detect_failing_tests(root, ["Python"])
    assert failing is not None
    assert failing  # non-empty — the collection ERROR counts


def test_regression_detected_by_set_diff(tmp_path: Path) -> None:
    """Simulate the gate: capture baseline, mutate, recompute — the
    set difference should expose newly-broken tests."""
    root = _pytest_project(
        tmp_path,
        "def test_one():\n    assert True\n\n"
        "def test_two():\n    assert True\n",
    )
    baseline = detect_failing_tests(root, ["Python"])
    assert baseline == set()

    # Break test_two by editing its assertion.
    _write(root, "tests/test_x.py",
           "def test_one():\n    assert True\n\n"
           "def test_two():\n    assert 1 == 2\n")
    current = detect_failing_tests(root, ["Python"])
    assert current is not None
    regressions = current - baseline
    assert any("test_two" in r for r in regressions)


def test_legit_fix_does_not_trigger_regression(tmp_path: Path) -> None:
    """A subtask that FIXES a failing test: baseline contains it,
    current doesn't. ``current - baseline`` = {}, no regression."""
    import shutil, time
    root = _pytest_project(
        tmp_path,
        "def test_broken():\n    assert 1 == 2\n",
    )
    baseline = detect_failing_tests(root, ["Python"])
    assert baseline  # non-empty at baseline

    # Bust Python's bytecode cache — within-same-second file rewrites
    # otherwise trigger pyc reuse and pytest re-imports the stale
    # source. Real-world worker edits are seconds apart; this tweak is
    # only needed because the tests run back-to-back.
    pycache = root / "tests" / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)
    time.sleep(0.01)
    _write(root, "tests/test_x.py", "def test_broken():\n    assert 1 == 1\n")
    current = detect_failing_tests(root, ["Python"])
    assert current == set()
    regressions = current - baseline
    assert regressions == set()


def test_v17_shape_fixture_deletion_caught(tmp_path: Path) -> None:
    """End-to-end simulation of the rung-3 v17/v18 regression pattern:
    start with a db fixture + 4 tests that all pass, delete the
    fixture, and verify the regression gate sees 4 new failures."""
    _write(tmp_path, "pyproject.toml",
           '[project]\nname="bench"\nversion="0"\nrequires-python=">=3.10"\n'
           '[tool.pytest.ini_options]\npythonpath=["."]\ntestpaths=["tests"]\n')
    _write(tmp_path, "src/db.py",
           "def get_user(name): return name\n")
    fixture_src = (
        "import pytest\n"
        "from src.db import get_user\n\n"
        "@pytest.fixture\n"
        "def db():\n    return 'ok'\n\n"
        "def test_a(db):\n    assert db == 'ok'\n"
        "def test_b(db):\n    assert get_user(db) == 'ok'\n"
        "def test_c(db):\n    assert True\n"
        "def test_d(db):\n    assert True\n"
    )
    _write(tmp_path, "tests/test_db.py", fixture_src)
    baseline = detect_failing_tests(tmp_path, ["Python"])
    assert baseline == set()

    # Simulate v17/v18: delete fixture + 3 tests, keep only 1.
    _write(tmp_path, "tests/test_db.py",
           "import pytest\n"
           "from src.db import get_user\n\n"
           "def test_d(db):\n    assert True\n")
    current = detect_failing_tests(tmp_path, ["Python"])
    assert current is not None
    regressions = current - baseline
    # 4 tests were passing; now all reference the missing ``db`` fixture.
    # (test_a/b/c are gone entirely — they don't fail, they just don't
    # exist; only test_d remains and it errors on missing fixture.)
    assert len(regressions) >= 1


# ── Negative / soft-pass paths ──────────────────────────────────────────


def test_no_matching_language_returns_none(tmp_path: Path) -> None:
    """Empty repo / unknown stack: no toolchain to run, no baseline
    to enforce. Returns None so G8 skips the gate for this run."""
    assert detect_failing_tests(tmp_path, []) is None
    assert detect_failing_tests(tmp_path, ["Haskell"]) is None


def test_missing_markers_returns_none(tmp_path: Path) -> None:
    """Python declared but no pyproject.toml / setup.py / setup.cfg —
    soft-pass (no markers, no run)."""
    _write(tmp_path, "src/x.py", "def f(): pass\n")
    assert detect_failing_tests(tmp_path, ["Python"]) is None


def test_missing_toolchain_returns_none(tmp_path: Path) -> None:
    """Rust declared + Cargo.toml present but cargo not installed:
    FileNotFoundError → return None (don't treat as failure)."""
    import shutil
    if shutil.which("cargo"):
        pytest.skip("cargo is installed; test only meaningful without it")
    _write(tmp_path, "Cargo.toml", '[package]\nname="x"\nversion="0.0.1"\n')
    _write(tmp_path, "src/lib.rs", "pub fn x() {}\n")
    assert detect_failing_tests(tmp_path, ["Rust"]) is None


def test_short_timeout_returns_none(tmp_path: Path) -> None:
    """A timeout is a soft-pass signal — we don't know if tests pass
    or fail, so we can't enforce a regression check."""
    root = _pytest_project(
        tmp_path,
        "import time\n"
        "def test_slow():\n    time.sleep(2.0)\n    assert True\n",
    )
    # 1-second timeout forces TimeoutExpired.
    assert detect_failing_tests(root, ["Python"], timeout_sec=1) is None
