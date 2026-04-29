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


# ── New API surface (server v0.0.1+ post 2026-04-29) ──────────────


def test_layer0_group_default_empty_hits() -> None:
    from gitoma.integrations.layer0 import Layer0Group
    g = Layer0Group(tag="x")
    assert g.tag == "x"
    assert g.hits == ()


def test_disabled_client_search_grouped_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    c = Layer0Client()
    assert c.search_grouped(
        query="x", namespace="ns", group_tags=["a", "b"], k_per_group=3,
    ) == []


def test_search_grouped_empty_args_return_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    c = Layer0Client()
    assert c.search_grouped(
        query="", namespace="ns", group_tags=["a"], k_per_group=3,
    ) == []
    assert c.search_grouped(
        query="x", namespace="", group_tags=["a"], k_per_group=3,
    ) == []
    assert c.search_grouped(
        query="x", namespace="ns", group_tags=[], k_per_group=3,
    ) == []
    assert c.search_grouped(
        query="x", namespace="ns", group_tags=["a"], k_per_group=0,
    ) == []


def test_ingest_one_accepts_pinned_and_ttl_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke: passing the new kwargs must not crash even when the
    transport layer fails — silent-fail-open is preserved."""
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    c = Layer0Client()
    # pinned
    assert c.ingest_one(
        text="arch fact", namespace="ns", tags=["arch"], pinned=True,
    ) is False
    # ttl_ms
    assert c.ingest_one(
        text="ephemeral", namespace="ns", tags=["e"], ttl_ms=86400000,
    ) is False
    # both (pinned wins on the server side; client just passes both)
    assert c.ingest_one(
        text="both", namespace="ns", pinned=True, ttl_ms=86400000,
    ) is False


def test_search_memory_accepts_tag_all_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled client returns [] regardless — but the new kwarg
    must be accepted without TypeError."""
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    c = Layer0Client()
    assert c.search_memory(
        query="x", namespace="ns", k=5,
        tag_all_of=["guard-fail", "G18"],
    ) == []
    assert c.search_memory(
        query="x", namespace="ns", k=5,
        tag_any_of=["a"], tag_all_of=["b"],
    ) == []


# ── get_by_id (new in 2026-04-29 follow-up) ───────────────────────


def test_disabled_client_get_by_id_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAYER0_GRPC_URL", raising=False)
    c = Layer0Client()
    assert c.get_by_id(id=42, namespace="ns") is None


def test_get_by_id_empty_namespace_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    c = Layer0Client()
    assert c.get_by_id(id=42, namespace="") is None


def test_get_by_id_negative_id_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAYER0_GRPC_URL", "127.0.0.1:50051")
    c = Layer0Client()
    assert c.get_by_id(id=-1, namespace="ns") is None


# ── dedupe_hits (pure module function) ────────────────────────────


def test_dedupe_hits_empty_list() -> None:
    from gitoma.integrations.layer0 import dedupe_hits
    assert dedupe_hits([]) == []


def test_dedupe_hits_unique_pass_through() -> None:
    """All hits with distinct prefixes are preserved in order."""
    from gitoma.integrations.layer0 import dedupe_hits
    hits = [
        Layer0Hit(id=1, text="alpha task", distance=0.1),
        Layer0Hit(id=2, text="beta task", distance=0.2),
        Layer0Hit(id=3, text="gamma task", distance=0.3),
    ]
    out = dedupe_hits(hits)
    assert [h.id for h in out] == [1, 2, 3]


def test_dedupe_hits_collapses_same_prefix_keeps_closer() -> None:
    """Two hits with identical first 80 chars: lower distance wins."""
    from gitoma.integrations.layer0 import dedupe_hits
    base = "PR shipped #" + "X" * 100  # 100+ identical chars
    hits = [
        Layer0Hit(id=1, text=base + " a", distance=0.5),
        Layer0Hit(id=2, text=base + " b", distance=0.2),
        Layer0Hit(id=3, text=base + " c", distance=0.7),
    ]
    out = dedupe_hits(hits)
    assert len(out) == 1
    assert out[0].id == 2  # closest match wins
    assert out[0].distance == 0.2


def test_dedupe_hits_short_text_uses_full_string() -> None:
    """Hits shorter than prefix_len are folded by their full text."""
    from gitoma.integrations.layer0 import dedupe_hits
    hits = [
        Layer0Hit(id=1, text="short", distance=0.5),
        Layer0Hit(id=2, text="short", distance=0.1),  # closer, same text
        Layer0Hit(id=3, text="other", distance=0.3),
    ]
    out = dedupe_hits(hits)
    assert len(out) == 2
    # The "short" survivor should be id=2 (closer)
    short_hit = next(h for h in out if h.text == "short")
    assert short_hit.id == 2


def test_dedupe_hits_preserves_order_of_first_occurrence() -> None:
    """When dedup keeps a different id at index N, the position of
    the deduped slot stays where the first occurrence was."""
    from gitoma.integrations.layer0 import dedupe_hits
    base = "X" * 200
    hits = [
        Layer0Hit(id=1, text=base + " a", distance=0.5),
        Layer0Hit(id=2, text="unique", distance=0.4),
        Layer0Hit(id=3, text=base + " b", distance=0.1),  # collapses with id=1
    ]
    out = dedupe_hits(hits)
    assert len(out) == 2
    # Position 0 = the deduped slot (id=3 wins on distance)
    # Position 1 = the unique hit
    assert out[0].id == 3
    assert out[1].id == 2


def test_dedupe_hits_zero_prefix_len_no_op() -> None:
    """prefix_len=0 disables dedup entirely."""
    from gitoma.integrations.layer0 import dedupe_hits
    hits = [
        Layer0Hit(id=1, text="same", distance=0.1),
        Layer0Hit(id=2, text="same", distance=0.2),
    ]
    out = dedupe_hits(hits, prefix_len=0)
    assert len(out) == 2


def test_dedupe_hits_custom_prefix_len() -> None:
    """A short prefix_len makes more aggressive dedup."""
    from gitoma.integrations.layer0 import dedupe_hits
    hits = [
        Layer0Hit(id=1, text="ABCDEF then alpha", distance=0.5),
        Layer0Hit(id=2, text="ABCDEF then beta", distance=0.1),
    ]
    # Default prefix_len=80 → both texts share <80 chars but diverge,
    # actually both ABCDEF-prefix is only 6 chars long so they share the
    # whole 17-char strings differ, with default they're different prefixes
    out_default = dedupe_hits(hits)
    assert len(out_default) == 2  # different at position 12
    # With prefix_len=6, both share "ABCDEF" → collapse
    out_short = dedupe_hits(hits, prefix_len=6)
    assert len(out_short) == 1
    assert out_short[0].id == 2  # closer wins
