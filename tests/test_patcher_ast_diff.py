"""Tests for the AST-diff guard — every top-level def in a "modify"
target's original content must still be present in the new content.

Caught live on rung-3 v17/v18: worker emitted a "modify" patch on
``tests/test_db.py`` that dropped the ``db`` pytest fixture + 3
sibling tests because its rewrite "didn't need them". The new file
parsed cleanly (so the syntax check passed) but test collection
broke at import. The AST-diff guard catches this at write time.
"""

from __future__ import annotations

from pathlib import Path

from gitoma.worker.patcher import (
    _python_top_level_defs,
    read_modify_originals,
    validate_top_level_preservation,
)


def _write(root: Path, rel: str, body: str) -> str:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return rel


# ── _python_top_level_defs ──────────────────────────────────────────────


def test_top_level_defs_collects_functions_and_classes() -> None:
    src = (
        "def alpha():\n    pass\n\n"
        "class Beta:\n    def method(self):\n        pass\n\n"
        "async def gamma():\n    pass\n"
    )
    assert _python_top_level_defs(src) == {"alpha", "Beta", "gamma"}


def test_top_level_defs_ignores_class_methods() -> None:
    """Methods inside a class body are NOT top-level — refactoring
    methods is normal, only class deletion is the failure shape we
    care about."""
    src = (
        "class A:\n"
        "    def m1(self): pass\n"
        "    def m2(self): pass\n"
    )
    assert _python_top_level_defs(src) == {"A"}


def test_top_level_defs_ignores_assignments() -> None:
    """Assignments at top level (``CONST = 5``) are not in the v1
    scope — too noisy to compare across legitimate edits."""
    src = "CONSTANT = 42\nOTHER = 'x'\ndef func(): pass\n"
    assert _python_top_level_defs(src) == {"func"}


def test_top_level_defs_handles_decorated() -> None:
    """``@pytest.fixture`` decorated functions are still
    ``ast.FunctionDef`` — the decorator wraps the def. Critical
    for the test-fixture-deletion case."""
    src = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def db():\n    return None\n"
    )
    assert _python_top_level_defs(src) == {"db"}


def test_top_level_defs_returns_empty_on_syntax_error() -> None:
    """Caller has the syntax check; this helper just can't compare
    apples to apples on broken source. Empty set means "unknown",
    not "nothing was there"."""
    assert _python_top_level_defs("def broken(:\n    pass\n") == set()


# ── read_modify_originals ──────────────────────────────────────────────


def test_read_originals_captures_modify_targets(tmp_path: Path) -> None:
    _write(tmp_path, "src/x.py", "def existing():\n    pass\n")
    patches = [
        {"action": "modify", "path": "src/x.py", "content": "..."},
    ]
    originals = read_modify_originals(tmp_path, patches)
    assert originals == {"src/x.py": "def existing():\n    pass\n"}


def test_read_originals_skips_create_actions(tmp_path: Path) -> None:
    """``create`` has no original to preserve — skip."""
    patches = [
        {"action": "create", "path": "src/new.py", "content": "..."},
    ]
    assert read_modify_originals(tmp_path, patches) == {}


def test_read_originals_skips_missing_files(tmp_path: Path) -> None:
    """``modify`` of a file that doesn't exist on disk is the
    worker's bug. Don't crash here — let the patcher's normal flow
    report the write outcome."""
    patches = [
        {"action": "modify", "path": "src/ghost.py", "content": "..."},
    ]
    assert read_modify_originals(tmp_path, patches) == {}


def test_read_originals_skips_binary_files(tmp_path: Path) -> None:
    """Unreadable / binary files: skip rather than crash. The
    preservation check is then no-op for them, which is correct
    (we don't AST-parse binaries)."""
    full = tmp_path / "img.png"
    full.write_bytes(b"\x89PNG\x00\x01\x02non-utf8")
    patches = [{"action": "modify", "path": "img.png", "content": "..."}]
    # No exception, no entry in dict.
    assert "img.png" not in read_modify_originals(tmp_path, patches)


# ── validate_top_level_preservation ─────────────────────────────────────


def test_preservation_clean_pass(tmp_path: Path) -> None:
    """Original had get_conn + find. New has both, plus a body
    change. Clean."""
    original = (
        "def get_conn():\n    return None\n\n"
        "def find(name):\n    return name\n"
    )
    new_body = (
        "def get_conn():\n    return None\n\n"
        "def find(name):\n    return name.upper()\n"  # body change
    )
    rel = _write(tmp_path, "src/db.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is None


def test_preservation_catches_v17_test_fixture_deletion(tmp_path: Path) -> None:
    """The exact rung-3 v17/v18 corruption: original tests/test_db.py
    has db fixture + 4 tests; worker emits a "modify" patch with
    only ONE test and no fixture."""
    original = (
        "import pytest\n"
        "from src.db import find_user_by_name\n\n"
        "@pytest.fixture\n"
        "def db():\n    return None\n\n"
        "def test_a(db): pass\n"
        "def test_b(db): pass\n"
        "def test_c(db): pass\n"
        "def test_d(db): pass\n"
    )
    new_body = (
        "import pytest\n"
        "from src.db import find_user_by_name\n\n"
        "def test_d(db): pass\n"  # only one test left, no fixture
    )
    rel = _write(tmp_path, "tests/test_db.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is not None
    bad_path, missing = result
    assert bad_path == "tests/test_db.py"
    assert missing == {"db", "test_a", "test_b", "test_c"}


def test_preservation_allows_adding_new_functions(tmp_path: Path) -> None:
    """Adding a new function (``def helper():``) is fine — the rule
    is "must not REMOVE existing", not "must not ADD new"."""
    original = "def keep():\n    pass\n"
    new_body = (
        "def keep():\n    pass\n\n"
        "def helper():\n    pass\n"
    )
    rel = _write(tmp_path, "x.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is None


def test_preservation_allows_class_method_deletion(tmp_path: Path) -> None:
    """Methods inside class bodies are NOT in scope — only top-level
    classes themselves matter. Removing a method during a refactor
    must NOT trip the guard."""
    original = "class A:\n    def m1(self): pass\n    def m2(self): pass\n"
    new_body = "class A:\n    def m1(self): pass\n"  # m2 removed
    rel = _write(tmp_path, "a.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is None


def test_preservation_skips_non_python_files(tmp_path: Path) -> None:
    """The AST-diff is Python-only; ``.toml``/``.json``/``.md`` go
    through the syntax check (or no check at all)."""
    original = "x = 1\n"
    new_body = ""
    rel = _write(tmp_path, "config.toml", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is None


def test_preservation_skips_files_without_originals(tmp_path: Path) -> None:
    """A "create" target wasn't in originals — nothing to compare,
    no failure."""
    rel = _write(tmp_path, "src/new.py", "def x(): pass\n")
    result = validate_top_level_preservation(
        tmp_path, [rel], {}  # no originals
    )
    assert result is None


def test_preservation_first_failure_short_circuits(tmp_path: Path) -> None:
    """When multiple files violate, return the first one. Important
    so the retry feedback names a single concrete file."""
    rel1 = _write(tmp_path, "a.py", "def kept(): pass\n")  # OK
    rel2 = _write(tmp_path, "b.py", "")  # everything dropped
    originals = {
        "a.py": "def kept(): pass\n",
        "b.py": "def lost(): pass\n",
    }
    result = validate_top_level_preservation(
        tmp_path, [rel1, rel2], originals
    )
    assert result is not None
    assert result[0] == "b.py"
    assert result[1] == {"lost"}


def test_preservation_handles_async_functions(tmp_path: Path) -> None:
    """``async def`` is also a top-level def — must be tracked."""
    original = "async def fetch(): pass\n"
    new_body = ""  # dropped
    rel = _write(tmp_path, "async_x.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    assert result is not None
    assert "fetch" in result[1]


def test_preservation_no_op_when_new_file_unparseable(tmp_path: Path) -> None:
    """If the new content has SyntaxError, the AST-diff helper
    returns empty for the new set → every original def appears
    "missing". That's noise; the syntax check will catch it
    separately. Document this contract: the AST-diff DOES fire here,
    but the syntax check fires FIRST in the wired flow so the
    operator sees the cleaner SyntaxError message first."""
    original = "def kept(): pass\n"
    new_body = "def broken(:\n    pass\n"  # syntax error
    rel = _write(tmp_path, "x.py", new_body)
    result = validate_top_level_preservation(
        tmp_path, [rel], {rel: original}
    )
    # Will fire — but this is fine, syntax check catches it earlier.
    assert result is not None
    assert "kept" in result[1]
