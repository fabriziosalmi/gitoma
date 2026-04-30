"""``LLMClient.chat(max_tokens=...)`` override tests.

Caught live 2026-04-30 EVE on PR #12: PHASE 5 self-review hit the
global ``LM_STUDIO_MAX_TOKENS=4096`` which truncated the full-PR-diff
review response. Fix: chat() takes a ``max_tokens`` keyword override
that callers can source from a phase-specific env knob (e.g.
self_critic reads ``LM_STUDIO_SELFREVIEW_MAX_TOKENS``, default 8192).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gitoma.planner.llm_client import LLMClient, LLMTruncatedError


def _stub_client(
    *, config_max_tokens: int = 4096, finish_reason: str = "stop",
    content: str = '{"ok": true}',
) -> tuple[LLMClient, MagicMock]:
    client = LLMClient.__new__(LLMClient)
    cfg = MagicMock()
    cfg.lmstudio.base_url = "http://stub/v1"
    cfg.lmstudio.api_key = "x"
    cfg.lmstudio.model = "stub-model"
    cfg.lmstudio.temperature = 0.0
    cfg.lmstudio.max_tokens = config_max_tokens
    cfg.lmstudio.worker_base_url = ""
    cfg.lmstudio.worker_model = ""
    client._config = cfg
    client._role = "planner"
    client._last_usage = None
    client._last_g14_fired = False
    fake_choice = MagicMock()
    fake_choice.message.content = content
    fake_choice.finish_reason = finish_reason
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
    fake_completions = MagicMock()
    fake_completions.create = MagicMock(return_value=fake_response)
    fake_chat = MagicMock()
    fake_chat.completions = fake_completions
    client._client = MagicMock()
    client._client.chat = fake_chat
    return client, fake_completions


def test_default_uses_config_max_tokens() -> None:
    client, completions = _stub_client(config_max_tokens=4096)
    client.chat([{"role": "user", "content": "hi"}], retries=1)
    call_kwargs = completions.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 4096


def test_override_takes_precedence() -> None:
    client, completions = _stub_client(config_max_tokens=4096)
    client.chat([{"role": "user", "content": "hi"}], retries=1, max_tokens=8192)
    call_kwargs = completions.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 8192


def test_explicit_smaller_override_honored() -> None:
    """Override is the truth — even when smaller than config default.
    Callers may want to constrain a single call."""
    client, completions = _stub_client(config_max_tokens=4096)
    client.chat([{"role": "user", "content": "hi"}], retries=1, max_tokens=512)
    call_kwargs = completions.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 512


def test_truncation_error_reports_effective_value() -> None:
    """When length-truncated under an override, the error message
    must report the OVERRIDE value, not the config default — otherwise
    the operator chases the wrong env var."""
    client, _ = _stub_client(
        config_max_tokens=4096, finish_reason="length",
    )
    with pytest.raises(LLMTruncatedError, match="8192 tokens"):
        client.chat(
            [{"role": "user", "content": "hi"}], retries=1, max_tokens=8192,
        )


def test_none_override_falls_back_to_config() -> None:
    """Explicit None == omitted (kwarg default). Falls back to config."""
    client, completions = _stub_client(config_max_tokens=2048)
    client.chat([{"role": "user", "content": "hi"}], retries=1, max_tokens=None)
    call_kwargs = completions.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 2048
