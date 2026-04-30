"""``LM_STUDIO_DISABLE_THINKING_PRELUDE=true`` system-prompt prelude tests.

Caught live 2026-04-30 on ``qwen/qwen3.5-9b``: the model ignores both
the ``/no_think`` suffix AND ``chat_template_kwargs={"enable_thinking":
false}``, emitting 3425 chars of reasoning_content per call (turnaround
182s for a 1-line diff). A system-message prelude that explicitly
forbids thinking collapsed reasoning to 391 chars and turnaround to
14.8s. This module pins the helper that injects the prelude.
"""

from __future__ import annotations

from gitoma.planner.llm_client import (
    _NO_THINK_PRELUDE,
    _prepend_no_think_prelude,
)


def test_inserts_system_when_absent() -> None:
    messages = [{"role": "user", "content": "What is 2+2?"}]
    out = _prepend_no_think_prelude(messages)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[0]["content"] == _NO_THINK_PRELUDE
    assert out[1] == {"role": "user", "content": "What is 2+2?"}


def test_prepends_to_existing_system() -> None:
    messages = [
        {"role": "system", "content": "You are a tester"},
        {"role": "user", "content": "What is 2+2?"},
    ]
    out = _prepend_no_think_prelude(messages)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[0]["content"].startswith(_NO_THINK_PRELUDE)
    assert out[0]["content"].endswith("You are a tester")
    assert "\n\n" in out[0]["content"]


def test_idempotent_when_prelude_already_present() -> None:
    """Already-marked input → no-op (no double prelude)."""
    messages = [
        {"role": "system", "content": _NO_THINK_PRELUDE + "\n\nYou are a tester"},
        {"role": "user", "content": "hi"},
    ]
    out = _prepend_no_think_prelude(messages)
    assert out[0]["content"].count(_NO_THINK_PRELUDE) == 1


def test_idempotent_across_multiple_system_messages() -> None:
    """Some chat templates split into multiple system blocks; the
    sentinel anywhere in the system layer is enough to short-circuit."""
    messages = [
        {"role": "system", "content": "header"},
        {"role": "system", "content": _NO_THINK_PRELUDE},
        {"role": "user", "content": "hi"},
    ]
    out = _prepend_no_think_prelude(messages)
    # Every system message preserved as-is, no extra prelude added.
    assert len(out) == 3
    assert out[0]["content"] == "header"
    assert out[1]["content"] == _NO_THINK_PRELUDE


def test_no_op_on_empty_list() -> None:
    assert _prepend_no_think_prelude([]) == []


def test_inserts_when_only_assistant_messages() -> None:
    """Assistant-only seeds (rare but valid) — still get the prelude."""
    messages = [{"role": "assistant", "content": "hello"}]
    out = _prepend_no_think_prelude(messages)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[0]["content"] == _NO_THINK_PRELUDE


def test_does_not_mutate_input() -> None:
    """Caller's original list + dicts are preserved."""
    messages = [
        {"role": "system", "content": "orig"},
        {"role": "user", "content": "do thing"},
    ]
    original_system = messages[0]["content"]
    original_user = messages[1]["content"]
    out = _prepend_no_think_prelude(messages)
    assert messages[0]["content"] == original_system
    assert messages[1]["content"] == original_user
    assert out[0]["content"] != original_system  # copy modified


def test_handles_empty_existing_system_content() -> None:
    """Edge: an existing system message with empty content — we don't
    want to leave a trailing ``\\n\\n`` on the merged content."""
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "hi"},
    ]
    out = _prepend_no_think_prelude(messages)
    assert out[0]["content"] == _NO_THINK_PRELUDE
