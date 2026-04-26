"""Tests for Test Gen v1 — the 5th critic.

LLM is mocked; we verify orchestration logic (changed-symbol
detection, framework dispatch, test-file path composition,
defensive degradation)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitoma.critic.test_gen import (
    MAX_SYMBOLS_PER_FILE,
    TestGenAgent,
    _compose_test_path,
    _is_test_path,
    _strip_fences,
    is_test_gen_enabled,
)
from gitoma.critic.test_gen_prompts import LANG_SPECS


# ── Env opt-in ────────────────────────────────────────────────────


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_TEST_GEN", raising=False)
    assert is_test_gen_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", "True"])
def test_enabled_via_env(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("GITOMA_TEST_GEN", value)
    assert is_test_gen_enabled() is True


def test_enabled_off_when_set_to_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_TEST_GEN", "off")
    assert is_test_gen_enabled() is False


# ── _is_test_path ─────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("tests/test_x.py",          True),
    ("tests/cpg/test_storage.py", True),
    ("test/test_x.py",           True),
    ("foo_test.go",              True),
    ("api.test.ts",              True),
    ("api.spec.js",              True),
    ("test_foo.py",              True),
    ("src/lib.py",               False),
    ("src/handler.go",           False),
    ("src/api.ts",               False),
    ("README.md",                False),
])
def test_is_test_path(path: str, expected: bool) -> None:
    assert _is_test_path(path) is expected


# ── _compose_test_path ────────────────────────────────────────────


def test_compose_python_under_tests_dir() -> None:
    spec = LANG_SPECS["python"]
    assert _compose_test_path("src/lib.py", spec) == "tests/test_lib.py"


def test_compose_python_root_source() -> None:
    spec = LANG_SPECS["python"]
    assert _compose_test_path("main.py", spec) == "tests/test_main.py"


def test_compose_typescript_colocated() -> None:
    spec = LANG_SPECS["typescript"]
    assert _compose_test_path("src/api.ts", spec) == "src/api.test.ts"


def test_compose_javascript_colocated() -> None:
    spec = LANG_SPECS["javascript"]
    assert _compose_test_path("util.js", spec) == "util.test.js"


def test_compose_rust_under_tests_dir() -> None:
    spec = LANG_SPECS["rust"]
    assert _compose_test_path("src/lib.rs", spec) == "tests/lib_tests.rs"


def test_compose_go_colocated() -> None:
    spec = LANG_SPECS["go"]
    assert _compose_test_path("internal/handler.go", spec) == \
        "internal/handler_test.go"


# ── _strip_fences (defensive) ─────────────────────────────────────


def test_strip_fences_removes_lang_fence() -> None:
    raw = "```python\nimport pytest\n\ndef test_x(): pass\n```"
    assert _strip_fences(raw) == "import pytest\n\ndef test_x(): pass"


def test_strip_fences_no_op_on_clean_content() -> None:
    raw = "import pytest\n\ndef test_x(): pass\n"
    assert _strip_fences(raw).strip() == raw.strip()


def test_strip_fences_handles_empty() -> None:
    assert _strip_fences("") == ""


# ── TestGenAgent integration with mocked LLM ─────────────────────


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


def _mock_llm(canned: str) -> MagicMock:
    llm = MagicMock()
    llm.chat = MagicMock(return_value=canned)
    return llm


def test_no_touched_files_returns_none(tmp_path: Path) -> None:
    agent = TestGenAgent(_mock_llm(""))
    assert agent.generate_for_patch([], originals={}, repo_root=tmp_path) is None


def test_only_test_files_touched_returns_none(tmp_path: Path) -> None:
    """If the patch only touches existing test files, no new tests
    to generate."""
    _populate(tmp_path, {"tests/test_x.py": "def test_old(): pass\n"})
    agent = TestGenAgent(_mock_llm("garbage"))
    out = agent.generate_for_patch(
        ["tests/test_x.py"], originals={}, repo_root=tmp_path,
    )
    assert out is None


def test_no_changed_symbols_returns_none(tmp_path: Path) -> None:
    """File modified but signatures unchanged (body-only edit) →
    nothing for test gen to do."""
    _populate(tmp_path, {
        "src/lib.py": "def helper(x): return x * 2\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    agent = TestGenAgent(_mock_llm("garbage"))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": "def helper(x): return x + 1\n"},
        repo_root=tmp_path,
    )
    assert out is None


def test_python_new_function_calls_llm_and_returns_test(
    tmp_path: Path,
) -> None:
    _populate(tmp_path, {
        "src/lib.py": (
            "def existing(): pass\n"
            "def added(x: int) -> str:\n"
            "    return str(x)\n"
        ),
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    canned = (
        "import pytest\n"
        "from src.lib import added\n"
        "\n"
        "def test_added_returns_str():\n"
        "    assert added(42) == '42'\n"
    )
    llm = _mock_llm(canned)
    agent = TestGenAgent(llm)
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": "def existing(): pass\n"},
        repo_root=tmp_path,
    )
    assert out is not None
    assert "tests/test_lib.py" in out
    assert "test_added_returns_str" in out["tests/test_lib.py"]
    # LLM was called exactly once (one source file per language)
    assert llm.chat.call_count == 1


def test_framework_manifest_missing_skips_language(
    tmp_path: Path,
) -> None:
    """Python source modified but no pyproject.toml → no framework
    manifest → silent skip (caller's tests wouldn't run anyway)."""
    _populate(tmp_path, {
        "src/lib.py": "def added(): pass\n",
    })
    agent = TestGenAgent(_mock_llm("would-not-run"))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    assert out is None


def test_llm_error_returns_none_silently(tmp_path: Path) -> None:
    """LLM failure must NEVER block the patch — Test Gen returns
    None and the worker proceeds with the original patch."""
    _populate(tmp_path, {
        "src/lib.py": "def added(): pass\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    llm = MagicMock()
    llm.chat = MagicMock(side_effect=RuntimeError("upstream LLM down"))
    agent = TestGenAgent(llm)
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    assert out is None


def test_empty_llm_response_returns_none(tmp_path: Path) -> None:
    _populate(tmp_path, {
        "src/lib.py": "def added(): pass\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    agent = TestGenAgent(_mock_llm(""))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    assert out is None


def test_one_line_llm_response_rejected(tmp_path: Path) -> None:
    """Single-line outputs are almost always garbage (the LLM
    forgot what it was supposed to do). Reject silently."""
    _populate(tmp_path, {
        "src/lib.py": "def added(): pass\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    agent = TestGenAgent(_mock_llm("not enough\n"))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    assert out is None


def test_max_symbols_cap_enforced(tmp_path: Path) -> None:
    """A file adding more than MAX_SYMBOLS_PER_FILE public symbols
    sees only the first N reach the LLM prompt — verified by
    inspecting the captured user prompt."""
    big_src = "\n".join(
        f"def fn_{i}(x): return x" for i in range(MAX_SYMBOLS_PER_FILE + 5)
    ) + "\n"
    _populate(tmp_path, {
        "src/lib.py": big_src,
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    canned = (
        "import pytest\n"
        "from src.lib import fn_0\n"
        "\n"
        "def test_fn_0(): assert fn_0(1) == 1\n"
    )
    llm = _mock_llm(canned)
    agent = TestGenAgent(llm)
    agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    user_prompt = llm.chat.call_args[0][0][1]["content"]
    # The cap is N=MAX_SYMBOLS_PER_FILE; count `fn_*` mentions in
    # the symbols-to-test section (signature line per symbol).
    listed = sum(1 for line in user_prompt.split("\n")
                 if "function `fn_" in line)
    assert listed == MAX_SYMBOLS_PER_FILE


def test_strips_markdown_fences_from_llm_output(tmp_path: Path) -> None:
    """Even though the system prompt forbids fences, LLMs sometimes
    emit them. The orchestrator strips defensively."""
    _populate(tmp_path, {
        "src/lib.py": "def added(): pass\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    canned = (
        "```python\n"
        "import pytest\n"
        "from src.lib import added\n"
        "\n"
        "def test_added(): added()\n"
        "```\n"
    )
    agent = TestGenAgent(_mock_llm(canned))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": ""},
        repo_root=tmp_path,
    )
    assert out is not None
    content = out["tests/test_lib.py"]
    assert "```" not in content
    assert "import pytest" in content


def test_signature_change_triggers_test_gen(tmp_path: Path) -> None:
    """Body-only changes don't trigger; signature changes DO."""
    _populate(tmp_path, {
        "src/lib.py": "def helper(x: int, y: int = 5) -> int:\n    return x + y\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    })
    agent = TestGenAgent(_mock_llm(
        "import pytest\n"
        "from src.lib import helper\n"
        "\n"
        "def test_helper(): assert helper(1) == 6\n"
    ))
    out = agent.generate_for_patch(
        ["src/lib.py"],
        originals={"src/lib.py": "def helper(x): return x + 1\n"},
        repo_root=tmp_path,
    )
    assert out is not None
    assert "tests/test_lib.py" in out


def test_multi_language_each_gets_its_own_test_file(tmp_path: Path) -> None:
    """A patch touching .py + .ts + .go produces three test files,
    one per language whose framework manifest exists."""
    _populate(tmp_path, {
        "src/lib.py":   "def py_added(): pass\n",
        "frontend/api.ts": "export function tsAdded(): void {}\n",
        "service/handler.go": "package main\nfunc GoAdded() {}\n",
        "pyproject.toml":  "[tool.pytest.ini_options]\n",
        "package.json":    '{"name": "x"}\n',
        "go.mod":          "module x\ngo 1.21\n",
    })
    canned = (
        "// generated test\n"
        "// second line so length check passes\n"
    )
    llm = _mock_llm(canned)
    agent = TestGenAgent(llm)
    out = agent.generate_for_patch(
        [
            "src/lib.py",
            "frontend/api.ts",
            "service/handler.go",
        ],
        originals={
            "src/lib.py": "",
            "frontend/api.ts": "",
            "service/handler.go": "",
        },
        repo_root=tmp_path,
    )
    assert out is not None
    paths = set(out.keys())
    # Each language → its own conventionally-located test file
    assert "tests/test_lib.py" in paths
    assert "frontend/api.test.ts" in paths
    assert "service/handler_test.go" in paths
    # And the LLM was called once per language
    assert llm.chat.call_count == 3
