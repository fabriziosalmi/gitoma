"""G14 — fenced-JSON guard tests.

Coder/instruct fine-tunes (gemma-4-e4b on llmproxy 2026-04-24,
qwen3.5-9b on LM Studio 2026-04-30) routinely wrap JSON responses in
``` ```json ... ``` ``` despite the prompt's "no fences, no
explanation" instruction. The silent repair in
``_attempt_json_repair`` recovers the JSON, but without telemetry
we never know WHICH models do this and HOW often. G14 adds detection
+ trace event + opt-in fail-fast (`GITOMA_G14_REJECT_FENCED_JSON=1`).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gitoma.planner.llm_client import (
    LLMClient,
    LLMError,
    _detect_fenced_json,
)


# ── _detect_fenced_json ─────────────────────────────────────────────────────

def test_detects_json_fence_with_lang_tag() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _detect_fenced_json(raw) is True


def test_detects_json_fence_without_lang_tag() -> None:
    raw = '```\n{"a": 1}\n```'
    assert _detect_fenced_json(raw) is True


def test_detects_array_fence() -> None:
    """Some models wrap top-level arrays — still a contract violation."""
    raw = '```json\n[1, 2, 3]\n```'
    assert _detect_fenced_json(raw) is True


def test_detects_with_leading_whitespace() -> None:
    raw = '   \n```json\n{"a": 1}\n```'
    assert _detect_fenced_json(raw) is True


def test_clean_json_passes() -> None:
    """Direct JSON output (the contract) — no G14 fire."""
    assert _detect_fenced_json('{"a": 1}') is False


def test_prose_then_json_no_fire() -> None:
    """Prose followed by JSON is a separate slop pattern (G14 doesn't
    cover it — handled by ``_extract_json`` brace-walker)."""
    assert _detect_fenced_json('Sure! Here you go:\n{"a": 1}') is False


def test_non_json_fence_no_fire() -> None:
    """A code fence wrapping Python/diff/whatever — not our concern."""
    assert _detect_fenced_json('```python\ndef foo(): pass\n```') is False


def test_empty_string_no_fire() -> None:
    assert _detect_fenced_json("") is False


def test_single_line_fence_no_fire() -> None:
    """Pathological one-liner — too unusual to flag confidently."""
    assert _detect_fenced_json("```{\"a\":1}```") is False


# ── chat_json telemetry + strict mode ───────────────────────────────────────

def _make_stub_client(raw_response: str) -> LLMClient:
    """Build a client with chat() stubbed to return a fixed string."""
    client = LLMClient.__new__(LLMClient)
    cfg = MagicMock()
    cfg.lmstudio.base_url = "http://stub/v1"
    cfg.lmstudio.api_key = "x"
    cfg.lmstudio.model = "stub-model"
    cfg.lmstudio.temperature = 0.0
    cfg.lmstudio.max_tokens = 1024
    cfg.lmstudio.worker_base_url = ""
    cfg.lmstudio.worker_model = ""
    client._config = cfg
    client._role = "planner"
    client._last_usage = None
    client._last_g14_fired = False
    client._client = MagicMock()
    client.chat = MagicMock(return_value=raw_response)  # type: ignore[method-assign]
    return client


def test_chat_json_silent_repair_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the strict flag, fenced JSON parses silently AND
    ``_last_g14_fired`` flips True so callers can observe the issue."""
    monkeypatch.delenv("GITOMA_G14_REJECT_FENCED_JSON", raising=False)
    client = _make_stub_client('```json\n{"ok": true}\n```')
    result = client.chat_json([{"role": "user", "content": "go"}], retries=1)
    assert result == {"ok": True}
    assert client._last_g14_fired is True


def test_chat_json_strict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_G14_REJECT_FENCED_JSON", "1")
    client = _make_stub_client('```json\n{"ok": true}\n```')
    with pytest.raises(LLMError, match="G14"):
        client.chat_json([{"role": "user", "content": "go"}], retries=1)


def test_chat_json_strict_passes_clean_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict mode rejects ONLY fenced JSON; clean JSON parses fine."""
    monkeypatch.setenv("GITOMA_G14_REJECT_FENCED_JSON", "1")
    client = _make_stub_client('{"ok": true}')
    result = client.chat_json([{"role": "user", "content": "go"}], retries=1)
    assert result == {"ok": True}
    assert client._last_g14_fired is False


def test_chat_json_resets_g14_flag_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_last_g14_fired`` reflects the LATEST call only — a clean call
    after a fenced one must reset to False."""
    monkeypatch.delenv("GITOMA_G14_REJECT_FENCED_JSON", raising=False)
    client = _make_stub_client('```json\n{"a": 1}\n```')
    client.chat_json([{"role": "user", "content": "go"}], retries=1)
    assert client._last_g14_fired is True
    # Swap stub for clean response.
    client.chat = MagicMock(return_value='{"b": 2}')  # type: ignore[method-assign]
    client.chat_json([{"role": "user", "content": "go"}], retries=1)
    assert client._last_g14_fired is False
