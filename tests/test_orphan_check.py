"""Tests for G16 (dead-code-introduction) + G18 (abandoned-helper)
+ G19 (echo-chamber) orphan detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.cpg import build_index
from gitoma.worker.orphan_check import (
    G16Conflict,
    G16Result,
    G18Conflict,
    G18Result,
    G19Conflict,
    G19Result,
    _is_test_file,
    check_g16_dead_code,
    check_g18_abandoned_helpers,
    check_g19_echo_chamber,
    is_g16_enabled,
    is_g18_enabled,
    is_g19_enabled,
)


def _populate(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


# ── Env opt-in ────────────────────────────────────────────────────


def test_g18_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G18_ABANDONED", raising=False)
    assert is_g18_enabled() is False


def test_g19_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G19_ECHO_CHAMBER", raising=False)
    assert is_g19_enabled() is False


@pytest.mark.parametrize("env_value", ["on", "1", "true", "yes", "ON"])
def test_g18_enabled_via_env(
    monkeypatch: pytest.MonkeyPatch, env_value: str,
) -> None:
    monkeypatch.setenv("GITOMA_G18_ABANDONED", env_value)
    assert is_g18_enabled() is True


@pytest.mark.parametrize("env_value", ["on", "1", "true", "yes", "ON"])
def test_g19_enabled_via_env(
    monkeypatch: pytest.MonkeyPatch, env_value: str,
) -> None:
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", env_value)
    assert is_g19_enabled() is True


# ── G18 — disabled returns None ───────────────────────────────────


def test_g18_disabled_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env opt-in is off, the check is a no-op regardless of
    inputs."""
    monkeypatch.delenv("GITOMA_G18_ABANDONED", raising=False)
    _populate(tmp_path, {"lib.py": "def helper(): pass\n"})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
    ) is None


def test_g18_no_originals_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    _populate(tmp_path, {"lib.py": "def helper(): pass\n"})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals=None,
    ) is None


def test_g18_create_action_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File didn't exist before — `create` action; G18 has nothing
    to compare against (no abandoned helpers possible)."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    _populate(tmp_path, {"lib.py": "def helper(): pass\n"})
    # File not in originals = create
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={},
    ) is None


# ── G18 — abandoned detection ─────────────────────────────────────


def test_g18_replay_abandoned_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classic G18 case: patch deletes the only caller of
    helper(); helper stays defined → flagged."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "def helper():\n    return 1\n"
        "def caller():\n    return helper()\n"
    )
    after = "def helper():\n    return 1\n"  # caller deleted, helper kept
    _populate(tmp_path, {"lib.py": after})
    result = check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": before},
    )
    assert result is not None
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.symbol_name == "helper"
    assert c.refs_before == 1
    assert c.refs_after == 0
    assert "helper" in result.render_for_llm()


def test_g18_no_flag_when_both_helper_and_caller_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch deletes BOTH the helper and the caller — clean removal,
    not abandoned."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "def helper():\n    return 1\n"
        "def caller():\n    return helper()\n"
    )
    after = "def keep():\n    return 0\n"
    _populate(tmp_path, {"lib.py": after})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": before},
    ) is None


def test_g18_no_flag_when_caller_replaced_with_new_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch deletes one caller but adds another in same file —
    helper still has a ref, not abandoned."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "def helper():\n    return 1\n"
        "def caller():\n    return helper()\n"
    )
    after = (
        "def helper():\n    return 1\n"
        "def new_caller():\n    return helper() + 1\n"
    )
    _populate(tmp_path, {"lib.py": after})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": before},
    ) is None


def test_g18_no_flag_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No change → no flag."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    src = "def helper(): pass\ndef caller(): return helper()\n"
    _populate(tmp_path, {"lib.py": src})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": src},
    ) is None


def test_g18_skips_private_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private (underscore) symbols are not tracked — operator may
    legitimately abandon internals during refactor."""
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "def _internal(): return 1\n"
        "def caller(): return _internal()\n"
    )
    after = "def _internal(): return 1\n"
    _populate(tmp_path, {"lib.py": after})
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": before},
    ) is None


def test_g18_skips_non_indexable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    _populate(tmp_path, {"config.toml": "[x]\nkey = 'a'\n"})
    assert check_g18_abandoned_helpers(
        tmp_path, ["config.toml"], originals={"config.toml": ""},
    ) is None


# ── G18 — multi-language coverage ─────────────────────────────────


def test_g18_works_on_typescript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "export function helper(): number { return 1; }\n"
        "export function caller(): number { return helper(); }\n"
    )
    after = "export function helper(): number { return 1; }\n"
    _populate(tmp_path, {"lib.ts": after})
    result = check_g18_abandoned_helpers(
        tmp_path, ["lib.ts"], originals={"lib.ts": before},
    )
    assert result is not None
    assert any(c.symbol_name == "helper" for c in result.conflicts)


def test_g18_works_on_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G18_ABANDONED", "on")
    before = (
        "package x\n"
        "func Helper() int { return 1 }\n"
        "func Caller() int { return Helper() }\n"
    )
    after = (
        "package x\n"
        "func Helper() int { return 1 }\n"
    )
    _populate(tmp_path, {"lib.go": after})
    result = check_g18_abandoned_helpers(
        tmp_path, ["lib.go"], originals={"lib.go": before},
    )
    assert result is not None
    assert any(c.symbol_name == "Helper" for c in result.conflicts)


# ── G19 — disabled / inputs ───────────────────────────────────────


def test_g19_disabled_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITOMA_G19_ECHO_CHAMBER", raising=False)
    _populate(tmp_path, {"lib.py": "def x(): pass\n"})
    idx = build_index(tmp_path)
    assert check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    ) is None


def test_g19_no_cpg_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {"lib.py": "def x(): pass\n"})
    assert check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=None,
    ) is None


# ── G19 — echo detection ──────────────────────────────────────────


def test_g19_replay_echo_chamber_in_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic echo: new file with two functions calling each other,
    nothing existing calls them."""
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {
        "lib.py": (
            "def new_x():\n    return new_y()\n"
            "def new_y():\n    return 42\n"
        ),
    })
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is not None
    assert len(result.conflicts) >= 1
    # new_y should be flagged — only caller is new_x in the same
    # newly-populated file.
    names = {c.symbol_name for c in result.conflicts}
    assert "new_y" in names


def test_g19_no_flag_with_external_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch creates new_lib with new_x; existing.py (NOT touched)
    calls new_x. External caller → not echo."""
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {
        "existing.py": (
            "from new_lib import new_x\n"
            "def existing_caller():\n    return new_x()\n"
        ),
        "new_lib.py": "def new_x():\n    return 1\n",
    })
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["new_lib.py"], originals={},
        cpg_index=idx,
    )
    assert result is None


def test_g19_no_flag_when_no_callers_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 callers = G16 territory (truly dead code), NOT G19. The
    intent is to keep the two critics separated by failure shape."""
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {
        "lib.py": "def lonely():\n    return 1\n",  # no callers
    })
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is None


def test_g19_no_flag_when_no_new_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body-only changes (no new public symbols) → nothing for
    G19 to consider."""
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    src = "def existing(): return 1\n"
    _populate(tmp_path, {"lib.py": src})
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["lib.py"],
        originals={"lib.py": "def existing(): return 0\n"},
        cpg_index=idx,
    )
    assert result is None


def test_g19_skips_private_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private symbols not tracked. New `_helper()` calling
    `_other()` doesn't trigger G19 — internals can be private and
    that's fine."""
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {
        "lib.py": (
            "def _helper():\n    return _other()\n"
            "def _other():\n    return 1\n"
        ),
    })
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is None


def test_g19_render_for_llm_includes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G19_ECHO_CHAMBER", "on")
    _populate(tmp_path, {
        "lib.py": (
            "def new_x():\n    return new_y()\n"
            "def new_y():\n    return 42\n"
        ),
    })
    idx = build_index(tmp_path)
    result = check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is not None
    rendered = result.render_for_llm()
    assert "ECHO-CHAMBER" in rendered
    assert "lib.py" in rendered
    assert "new_y" in rendered


# ── Combined scenarios ────────────────────────────────────────────


def test_both_critics_silent_when_disabled_by_default(tmp_path: Path) -> None:
    """Default-off: no env vars set → both return None for any input
    (no surprise side-effects on existing benches / runs)."""
    _populate(tmp_path, {
        "lib.py": (
            "def x():\n    return y()\n"
            "def y():\n    return 1\n"
        ),
    })
    idx = build_index(tmp_path)
    assert check_g18_abandoned_helpers(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
    ) is None
    assert check_g19_echo_chamber(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    ) is None


# ── G16 — env opt-in ──────────────────────────────────────────────


def test_g16_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_G16_DEAD_CODE", raising=False)
    assert is_g16_enabled() is False


@pytest.mark.parametrize("env_value", ["on", "1", "true", "yes", "ON"])
def test_g16_enabled_via_env(
    monkeypatch: pytest.MonkeyPatch, env_value: str,
) -> None:
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", env_value)
    assert is_g16_enabled() is True


# ── G16 — disabled / inputs ───────────────────────────────────────


def test_g16_disabled_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITOMA_G16_DEAD_CODE", raising=False)
    _populate(tmp_path, {"lib.py": "def lonely(): pass\n"})
    idx = build_index(tmp_path)
    assert check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    ) is None


def test_g16_no_cpg_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def lonely(): pass\n"})
    assert check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=None,
    ) is None


def test_g16_no_originals_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def lonely(): pass\n"})
    idx = build_index(tmp_path)
    assert check_g16_dead_code(
        tmp_path, ["lib.py"], originals=None, cpg_index=idx,
    ) is None


# ── G16 — dead-code detection ─────────────────────────────────────


def test_g16_replay_dead_function_in_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical case: patch creates a new file with a public
    function that NOTHING in the codebase calls. Pure dead code."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def lonely():\n    return 42\n"})
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is not None
    assert len(result.conflicts) == 1
    assert result.conflicts[0].symbol_name == "lonely"
    assert result.conflicts[0].file == "lib.py"


def test_g16_no_flag_when_called_externally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch adds new public symbol that an existing file calls →
    not dead, G16 silent."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {
        "existing.py": (
            "from new_lib import shipped\n"
            "def consumer(): return shipped()\n"
        ),
        "new_lib.py": "def shipped():\n    return 1\n",
    })
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["new_lib.py"], originals={},
        cpg_index=idx,
    )
    assert result is None


def test_g16_no_flag_when_self_calling_clique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two new functions calling each other: total_callers > 0 for
    each → G16 silent. This is G19 territory (echo-chamber)."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {
        "lib.py": (
            "def new_x():\n    return new_y()\n"
            "def new_y():\n    return new_x()\n"
        ),
    })
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is None


def test_g16_skips_private_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_private` not tracked as public → G16 ignores even if dead."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def _private():\n    return 1\n"})
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is None


def test_g16_skips_test_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest discovers `test_*` by reflection — must NOT flag as
    dead. The whole exemption hinges on the path heuristic."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {
        "tests/test_foo.py": (
            "def test_obvious():\n    assert True\n"
        ),
    })
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["tests/test_foo.py"], originals={"tests/test_foo.py": ""},
        cpg_index=idx,
    )
    assert result is None


def test_g16_no_flag_when_no_new_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body-only patch → no new public symbols → nothing for G16."""
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def existing(): return 1\n"})
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["lib.py"],
        originals={"lib.py": "def existing(): return 0\n"},
        cpg_index=idx,
    )
    assert result is None


def test_g16_render_for_llm_includes_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITOMA_G16_DEAD_CODE", "on")
    _populate(tmp_path, {"lib.py": "def lonely():\n    return 1\n"})
    idx = build_index(tmp_path)
    result = check_g16_dead_code(
        tmp_path, ["lib.py"], originals={"lib.py": ""},
        cpg_index=idx,
    )
    assert result is not None
    rendered = result.render_for_llm()
    assert "DEAD CODE" in rendered
    assert "lonely" in rendered
    assert "lib.py" in rendered


# ── _is_test_file heuristic ───────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    # Path fragments
    ("tests/test_foo.py", True),
    ("test/foo.py", True),
    ("src/__tests__/foo.ts", True),
    ("packages/spec/foo.js", True),
    # Name patterns
    ("foo_test.py", True),
    ("foo.test.ts", True),
    ("foo.test.tsx", True),
    ("foo.spec.js", True),
    ("foo_test.go", True),
    ("foo_test.rs", True),
    ("test_foo.py", True),
    # NOT test files
    ("src/lib.py", False),
    ("gitoma/worker/foo.py", False),
    ("test.py", False),  # plain "test.py" — not the prefix pattern
    ("contesto/foo.py", False),  # substring "test" inside name not enough
    ("manifest.json", False),
])
def test_is_test_file(path: str, expected: bool) -> None:
    assert _is_test_file(path) is expected
