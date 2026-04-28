"""Tests for the Layer0 vector-memory client wrapper.

Pure-function tests + silent-fail-open invariant. No live gRPC
needed — the gRPC stub is mocked when present, and the unset-env
path is exercised directly."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gitoma.integrations.layer0 import (
    Layer0Client,
    Layer0Config,
    Layer0Hit,
    namespace_for_repo,
)


# ── Layer0Config.from_env ─────────────────────────────────────────


def test_from_env_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    cfg = Layer0Config.from_env()
    assert cfg.enabled is False
    assert cfg.grpc_url == ""


def test_from_env_enabled_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    monkeypatch.delenv("LAYER0_API_KEY", raising=False)
    cfg = Layer0Config.from_env()
    assert cfg.enabled is True
    assert cfg.grpc_url == "127.0.0.1:50051"
    assert cfg.api_key == ""


def test_from_env_strips_http_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """gRPC takes host:port. URLs with http:// or https:// must be stripped."""
    for prefix in ("http://", "https://"):
        monkeypatch.setenv("LAYER0_GRPC_URL", f"{prefix}layer0.local:50051")
        cfg = Layer0Config.from_env()
        assert cfg.grpc_url == "layer0.local:50051"


def test_from_env_picks_up_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    monkeypatch.setenv("LAYER0_API_KEY", "ghp_secret")
    cfg = Layer0Config.from_env()
    assert cfg.api_key == "ghp_secret"


def test_from_env_invalid_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    monkeypatch.setenv("LAYER0_TIMEOUT_S", "not-a-number")
    cfg = Layer0Config.from_env()
    assert cfg.timeout_s == 2.0


# ── namespace_for_repo ────────────────────────────────────────────


@pytest.mark.parametrize("owner,name,expected", [
    ("fabriziosalmi", "gitoma-bench-blast", "fabriziosalmi__gitoma-bench-blast"),
    ("foo", "bar", "foo__bar"),
    ("user.with.dots", "repo/with/slashes", "user-with-dots__repo-with-slashes"),
    ("Has Spaces", "and!chars", "Has-Spaces__and-chars"),
])
def test_namespace_for_repo(owner: str, name: str, expected: str) -> None:
    assert namespace_for_repo(owner, name) == expected


def test_namespace_for_repo_truncates_to_64() -> None:
    """Layer0 regex caps at 64 chars."""
    out = namespace_for_repo("a" * 50, "b" * 50)
    assert len(out) <= 64
    assert all(c.isalnum() or c in "_-" for c in out)


def test_namespace_for_repo_falls_back_to_default_when_all_stripped() -> None:
    """Pathological input that strips to nothing → 'default'."""
    out = namespace_for_repo("...", "!!!")
    assert out == "default"


# ── Silent fail-open contract ─────────────────────────────────────


def test_client_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    client = Layer0Client()
    assert client.enabled is False


def test_disabled_client_ingest_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    client = Layer0Client()
    # Ingest must be a no-op when disabled — no exception, no side effect
    assert client.ingest_one(text="hi", namespace="x") is False


def test_disabled_client_search_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    client = Layer0Client()
    assert client.search_memory(query="anything", namespace="x", k=5) == []


def test_disabled_client_list_namespaces_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    client = Layer0Client()
    assert client.list_namespaces() == []


def test_close_safe_on_unconnected_client() -> None:
    client = Layer0Client(Layer0Config(grpc_url="", enabled=False))
    client.close()  # Must not raise
    client.close()  # Idempotent


# ── Stub-init failure marks client permanently disabled ───────────


def test_stub_init_failure_disables_client_permanently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If gRPC channel construction blows up (proto generation broken,
    grpc not installed, …) the client must record _stub_init_failed
    and refuse all further calls without re-paying the connect cost."""
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    client = Layer0Client()

    # Patch grpc import to raise on first use
    with patch("grpc.insecure_channel", side_effect=OSError("boom")):
        # First call triggers _ensure_stub which catches the exception
        result = client.ingest_one(text="hi", namespace="ns")
        assert result is False
        assert client._stub_init_failed is True

    # Second call must skip the connect attempt entirely
    with patch("grpc.insecure_channel") as mock_ch:
        result = client.ingest_one(text="hi", namespace="ns")
        assert result is False
        mock_ch.assert_not_called()


# ── Empty-arg guards ──────────────────────────────────────────────


def test_ingest_empty_text_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    client = Layer0Client()
    assert client.ingest_one(text="", namespace="ns") is False


def test_ingest_empty_namespace_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    client = Layer0Client()
    assert client.ingest_one(text="hi", namespace="") is False


def test_search_empty_query_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    client = Layer0Client()
    assert client.search_memory(query="", namespace="ns", k=5) == []


def test_search_zero_k_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    client = Layer0Client()
    assert client.search_memory(query="x", namespace="ns", k=0) == []


# ── Hit dataclass shape ───────────────────────────────────────────


def test_layer0_hit_default_tags() -> None:
    h = Layer0Hit(id=1, text="hi", distance=0.5)
    assert h.tags == ()
    assert h.created_at_ms == 0


def test_layer0_hit_tags_immutable() -> None:
    """Layer0Hit is frozen — mutation must raise."""
    h = Layer0Hit(id=1, text="hi", distance=0.5)
    with pytest.raises(Exception):
        h.id = 99   # type: ignore[misc]
