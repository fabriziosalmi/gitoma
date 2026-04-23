"""``LM_STUDIO_DISABLE_THINKING=true`` env append-``/no_think`` tests.

Caught live 2026-04-23 on rung-3 v10: deepseek-r1-0528-qwen3-8b
emitted ~3000 chars of reasoning per call, making each subtask take
20-30 minutes. ``qwen/qwen3-8b`` with the ``/no_think`` soft-switch
suffix dropped reasoning tokens 297 → 1 in a controlled probe.
This module pins the helper that injects the suffix.
"""

from __future__ import annotations

from gitoma.planner.llm_client import _append_no_think


def test_appends_to_last_user_message() -> None:
    messages = [
        {"role": "system", "content": "You are a tester"},
        {"role": "user", "content": "What is 2+2?"},
    ]
    out = _append_no_think(messages)
    assert out[-1]["content"].endswith("/no_think")
    # Original input unchanged
    assert messages[-1]["content"] == "What is 2+2?"


def test_skips_when_already_present() -> None:
    """Idempotent — never double-append."""
    messages = [{"role": "user", "content": "hi /no_think"}]
    out = _append_no_think(messages)
    assert out[-1]["content"].count("/no_think") == 1


def test_appends_only_to_last_user_message() -> None:
    """When there are MULTIPLE user messages (multi-turn convo),
    only the LAST one gets the marker — that's the request the
    model is about to act on."""
    messages = [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second turn"},
    ]
    out = _append_no_think(messages)
    assert out[0]["content"] == "first turn"  # untouched
    assert "/no_think" in out[-1]["content"]


def test_no_op_on_empty_list() -> None:
    assert _append_no_think([]) == []


def test_no_op_when_no_user_message() -> None:
    """System-only / assistant-only message lists — there's no user
    request to mark, so leave the list alone."""
    messages = [{"role": "system", "content": "ping"}]
    out = _append_no_think(messages)
    assert out == messages


def test_does_not_mutate_input() -> None:
    """Caller's original list + dicts are preserved. Critical when the
    same messages are passed to multiple chat() calls."""
    messages = [{"role": "user", "content": "do thing"}]
    original_content = messages[0]["content"]
    out = _append_no_think(messages)
    assert messages[0]["content"] == original_content  # input untouched
    assert out[0]["content"] != messages[0]["content"]  # copy modified


def test_uses_newline_separator() -> None:
    """Newline rather than space so the marker stays visually distinct
    if the model echoes the prompt back in its reply."""
    messages = [{"role": "user", "content": "hi"}]
    out = _append_no_think(messages)
    assert out[-1]["content"] == "hi\n/no_think"
