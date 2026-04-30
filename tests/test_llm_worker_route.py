"""Worker-route override tests — `LM_STUDIO_WORKER_BASE_URL` /
`LM_STUDIO_WORKER_MODEL` + role-aware anti-thinking env precedence.

Caught live 2026-04-30 EVE: PR #12 validated qwen3.5-9b as worker-grade
on mm2 (192.168.0.117:1234) but the planner stack still needed
qwen3-8b on mm1 (no PRELUDE). Old single-`LM_STUDIO_*` config couldn't
express "different model, different endpoint, different anti-think
recipe per role". This module pins the split.
"""

from __future__ import annotations

import os

import pytest

from gitoma.core.config import Config, LMStudioConfig
from gitoma.planner.llm_client import LLMClient


@pytest.fixture
def base_config() -> Config:
    """Config with planner endpoint set + worker overrides empty."""
    cfg = Config()
    cfg.lmstudio = LMStudioConfig(
        base_url="http://planner:1234/v1",
        model="qwen/qwen3-8b",
        api_key="lm-studio",
    )
    return cfg


def test_default_role_is_planner(base_config: Config) -> None:
    client = LLMClient(base_config)
    assert client.role == "planner"


def test_invalid_role_rejected(base_config: Config) -> None:
    with pytest.raises(ValueError, match="role must be"):
        LLMClient(base_config, role="critic")


def test_planner_uses_base_endpoint(base_config: Config) -> None:
    client = LLMClient(base_config, role="planner")
    assert client.model == "qwen/qwen3-8b"
    assert client._resolve_base_url() == "http://planner:1234/v1"


def test_worker_falls_back_to_planner_when_overrides_empty(base_config: Config) -> None:
    """Backwards-compat: role=worker with no worker_* overrides reuses
    the planner endpoint + model. Lets pre-existing setups upgrade
    to LLMClient(role='worker') without changing config."""
    client = LLMClient(base_config, role="worker")
    assert client.model == "qwen/qwen3-8b"
    assert client._resolve_base_url() == "http://planner:1234/v1"


def test_worker_uses_overrides_when_set(base_config: Config) -> None:
    base_config.lmstudio.worker_base_url = "http://worker:1234/v1"
    base_config.lmstudio.worker_model = "qwen/qwen3.5-9b"
    client = LLMClient(base_config, role="worker")
    assert client.model == "qwen/qwen3.5-9b"
    assert client._resolve_base_url() == "http://worker:1234/v1"


def test_worker_partial_override_model_only(base_config: Config) -> None:
    """Only `worker_model` set: worker uses planner endpoint with worker model."""
    base_config.lmstudio.worker_model = "qwen/qwen3.5-9b"
    client = LLMClient(base_config, role="worker")
    assert client.model == "qwen/qwen3.5-9b"
    assert client._resolve_base_url() == "http://planner:1234/v1"


def test_worker_partial_override_base_url_only(base_config: Config) -> None:
    """Only `worker_base_url` set: worker uses worker endpoint with planner model."""
    base_config.lmstudio.worker_base_url = "http://worker:1234/v1"
    client = LLMClient(base_config, role="worker")
    assert client.model == "qwen/qwen3-8b"
    assert client._resolve_base_url() == "http://worker:1234/v1"


def test_planner_ignores_worker_overrides(base_config: Config) -> None:
    """Even when worker_* are set, role=planner stays on the base endpoint."""
    base_config.lmstudio.worker_base_url = "http://worker:1234/v1"
    base_config.lmstudio.worker_model = "qwen/qwen3.5-9b"
    client = LLMClient(base_config, role="planner")
    assert client.model == "qwen/qwen3-8b"
    assert client._resolve_base_url() == "http://planner:1234/v1"


def test_for_worker_factory(base_config: Config) -> None:
    base_config.lmstudio.worker_model = "qwen/qwen3.5-9b"
    client = LLMClient.for_worker(base_config)
    assert client.role == "worker"
    assert client.model == "qwen/qwen3.5-9b"


# ── worker_max_tokens override (2026-05-01) ─────────────────────────────────

def _stub_worker_client(*, base_max: int, worker_max: int = 0):
    """Stub for testing chat() max_tokens precedence without OpenAI."""
    from unittest.mock import MagicMock
    client = LLMClient.__new__(LLMClient)
    cfg = MagicMock()
    cfg.lmstudio.base_url = "http://stub/v1"
    cfg.lmstudio.api_key = "x"
    cfg.lmstudio.model = "stub"
    cfg.lmstudio.temperature = 0.0
    cfg.lmstudio.max_tokens = base_max
    cfg.lmstudio.worker_base_url = ""
    cfg.lmstudio.worker_model = ""
    cfg.lmstudio.worker_max_tokens = worker_max
    client._config = cfg
    client._role = "worker"
    client._last_usage = None
    client._last_g14_fired = False
    fake_choice = MagicMock()
    fake_choice.message.content = '{"ok":true}'
    fake_choice.finish_reason = "stop"
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    fake_completions = MagicMock()
    fake_completions.create = MagicMock(return_value=fake_response)
    client._client = MagicMock()
    client._client.chat = MagicMock(completions=fake_completions)
    return client, fake_completions


def test_worker_uses_worker_max_tokens_when_set() -> None:
    """role=worker + worker_max_tokens=8192 → effective=8192 (vs base 4096)."""
    client, completions = _stub_worker_client(base_max=4096, worker_max=8192)
    client.chat([{"role": "user", "content": "hi"}], retries=1)
    assert completions.create.call_args.kwargs["max_tokens"] == 8192


def test_worker_falls_back_when_worker_max_unset() -> None:
    """worker_max_tokens=0 → still uses base 4096."""
    client, completions = _stub_worker_client(base_max=4096, worker_max=0)
    client.chat([{"role": "user", "content": "hi"}], retries=1)
    assert completions.create.call_args.kwargs["max_tokens"] == 4096


def test_explicit_kwarg_beats_worker_max_tokens() -> None:
    """Explicit max_tokens=512 trumps worker_max_tokens=8192 — caller has
    the strongest say (e.g. self_critic with phase-specific budget)."""
    client, completions = _stub_worker_client(base_max=4096, worker_max=8192)
    client.chat(
        [{"role": "user", "content": "hi"}], retries=1, max_tokens=512,
    )
    assert completions.create.call_args.kwargs["max_tokens"] == 512


def test_planner_role_ignores_worker_max_tokens() -> None:
    """role=planner stays on base max_tokens even when worker_max_tokens is set."""
    client, completions = _stub_worker_client(base_max=2048, worker_max=8192)
    client._role = "planner"
    client.chat([{"role": "user", "content": "hi"}], retries=1)
    assert completions.create.call_args.kwargs["max_tokens"] == 2048


# ── Role-aware anti-thinking env precedence ─────────────────────────────────

class _StubConfig:
    """Minimal Config-shaped object for env-only tests (no real OpenAI client)."""
    class _LM:
        base_url = "http://stub/v1"
        model = "stub-model"
        api_key = "stub"
        worker_base_url = ""
        worker_model = ""
    lmstudio = _LM()


def _make_flag_fn(role: str):
    """Replicate the inline `_flag` from chat() against a stubbed env."""
    def _flag(name: str) -> bool:
        v = ""
        if role == "worker":
            v = os.environ.get(f"LM_STUDIO_WORKER_{name}") or ""
        if not v:
            v = os.environ.get(f"LM_STUDIO_{name}") or ""
        return v.lower() in ("1", "true", "yes")
    return _flag


def test_planner_reads_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LM_STUDIO_DISABLE_THINKING_PRELUDE", "1")
    flag = _make_flag_fn("planner")
    assert flag("DISABLE_THINKING_PRELUDE") is True


def test_planner_ignores_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner never reads WORKER_-prefixed flags — they're worker-scoped."""
    monkeypatch.delenv("LM_STUDIO_DISABLE_THINKING_PRELUDE", raising=False)
    monkeypatch.setenv("LM_STUDIO_WORKER_DISABLE_THINKING_PRELUDE", "1")
    flag = _make_flag_fn("planner")
    assert flag("DISABLE_THINKING_PRELUDE") is False


def test_worker_prefers_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker reads `LM_STUDIO_WORKER_*` first — that's the whole point."""
    monkeypatch.setenv("LM_STUDIO_DISABLE_THINKING_PRELUDE", "0")
    monkeypatch.setenv("LM_STUDIO_WORKER_DISABLE_THINKING_PRELUDE", "1")
    flag = _make_flag_fn("worker")
    assert flag("DISABLE_THINKING_PRELUDE") is True


def test_worker_falls_back_to_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker without WORKER_ override inherits global LM_STUDIO_ flag."""
    monkeypatch.setenv("LM_STUDIO_DISABLE_THINKING_PRELUDE", "1")
    monkeypatch.delenv("LM_STUDIO_WORKER_DISABLE_THINKING_PRELUDE", raising=False)
    flag = _make_flag_fn("worker")
    assert flag("DISABLE_THINKING_PRELUDE") is True


def test_both_unset_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LM_STUDIO_DISABLE_THINKING_PRELUDE", raising=False)
    monkeypatch.delenv("LM_STUDIO_WORKER_DISABLE_THINKING_PRELUDE", raising=False)
    assert _make_flag_fn("worker")("DISABLE_THINKING_PRELUDE") is False
    assert _make_flag_fn("planner")("DISABLE_THINKING_PRELUDE") is False
