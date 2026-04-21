"""Regression tests for two fixes pinned together:

1. **WS Origin allow-list port hardcode** — the first cut of the
   Origin guard hardcoded ``:8000`` in its default, so ``gitoma serve
   --port <anything-else>`` silently broke the cockpit: the browser's
   WebSocket upgrade was rejected, the Store stayed empty, and every
   state-dependent button (like Reset) stayed disabled.

2. **Auto-generated API token prefix** — tokens now start with
   ``gitoma_`` so they're recognisable in logs, env dumps, and
   accidental screenshots, matching the ``ghp_`` / ``sk-`` / ``xoxb-``
   convention every other vendor follows.

These tests are here (not in ``test_api_industrial.py``) because the
point of each assertion is *not to regress a bug that already hit
a user* — future contributors should see the scar tissue up front.
"""

from __future__ import annotations


import pytest

from gitoma.api.web import _is_origin_allowed, _LOOPBACK_HOSTS


# ── Origin allow-list: loopback on any port is trusted ──────────────────────


@pytest.mark.parametrize("origin", [
    "http://localhost:8000",
    "http://localhost:8800",      # the port that broke the first cut
    "http://localhost:9999",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8800",
    "http://127.0.0.1:12345",
    "http://0.0.0.0:8000",
    "http://0.0.0.0:8800",
    "https://localhost:8443",     # https loopback is still loopback
])
def test_any_loopback_port_is_accepted(origin):
    """Hard-coding a single port in the default defeats the purpose of
    ``--port`` on ``gitoma serve``. Every loopback host, any port, any
    http(s) scheme must pass the Origin gate out of the box."""
    assert _is_origin_allowed(origin) is True, (
        f"loopback origin {origin!r} must be accepted by default"
    )


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "http://example.com:8000",
    "http://attacker.localhost",            # "localhost" as a subdomain, not the host
    "https://not-localhost.example:8800",
    "ftp://localhost:8000",                 # non-http scheme
    "chrome-extension://abc/index.html",
])
def test_non_loopback_origins_are_rejected_by_default(origin):
    """With no env override, anything that isn't a plain loopback host
    on http(s) must be rejected."""
    # Clear any operator-supplied list so we test the default policy.
    from gitoma.api import web as web_module
    snapshot = web_module._ALLOWED_WS_ORIGINS.copy()
    try:
        web_module._ALLOWED_WS_ORIGINS.clear()
        assert _is_origin_allowed(origin) is False, (
            f"non-loopback origin {origin!r} must be rejected by default"
        )
    finally:
        web_module._ALLOWED_WS_ORIGINS.clear()
        web_module._ALLOWED_WS_ORIGINS.update(snapshot)


def test_explicit_env_list_augments_loopback_default(monkeypatch):
    """An operator wiring the cockpit into a trusted LAN frontend adds
    their origin via env. The env list must be *additive* — it must
    NOT lock them out of localhost by replacing the default."""
    from gitoma.api import web as web_module

    # Mimic the module-load-time parse of GITOMA_WS_ALLOWED_ORIGINS.
    snapshot = web_module._ALLOWED_WS_ORIGINS.copy()
    try:
        web_module._ALLOWED_WS_ORIGINS.clear()
        web_module._ALLOWED_WS_ORIGINS.add("https://cockpit.example.com")

        assert _is_origin_allowed("https://cockpit.example.com") is True
        assert _is_origin_allowed("http://localhost:8800") is True   # still loopback-trusted
        assert _is_origin_allowed("http://evil.example") is False
    finally:
        web_module._ALLOWED_WS_ORIGINS.clear()
        web_module._ALLOWED_WS_ORIGINS.update(snapshot)


def test_loopback_hostname_set_is_sensible():
    """Sanity: the baseline loopback set covers the names a dev-mode
    server will realistically bind to. Keep this test as the
    inventory contract so adding/removing a host is a conscious act."""
    assert _LOOPBACK_HOSTS == frozenset({
        "localhost", "127.0.0.1", "::1", "0.0.0.0",
    })


def test_missing_origin_is_allowed_for_non_browser_clients():
    """CLI WS clients (e.g. ``websockets`` in a script) may omit the
    header. We accept it because ``/ws/state`` is read-only derived
    state and doesn't expose credentials."""
    assert _is_origin_allowed(None) is True


def test_malformed_origin_is_rejected():
    """urlparse can choke on some strings. We must still reject rather
    than raise — a handshake that crashes is a DoS vector."""
    # `urlparse` handles almost anything without raising, but we exercise
    # the branch where the parsed hostname is empty/garbage.
    assert _is_origin_allowed("") is False
    assert _is_origin_allowed("not-a-url") is False


# ── Token prefix: `gitoma_` for recognisability ─────────────────────────────


def test_generated_runtime_token_carries_the_gitoma_prefix(tmp_path, monkeypatch):
    """Auto-generated tokens start with ``gitoma_`` so they're as
    greppable as ``ghp_``/``sk-``/``xoxb-``. Operators who spot a
    ``gitoma_...`` blob in a log or env dump know exactly what it is
    — and can rotate it without guessing."""
    from gitoma.core.config import (
        RUNTIME_TOKEN_PREFIX,
        ensure_runtime_api_token,
    )

    token_file = tmp_path / "runtime_token"
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr(
        "gitoma.core.config.load_config",
        lambda: type("C", (), {"api_auth_token": ""})(),
    )

    token, generated = ensure_runtime_api_token()
    assert generated is True
    assert token.startswith(RUNTIME_TOKEN_PREFIX)
    assert token.startswith("gitoma_")
    # Urlsafe-base64 suffix after the prefix → long enough to be random.
    suffix = token[len("gitoma_"):]
    assert len(suffix) >= 40, (
        f"expected ≥40 chars of entropy after prefix, got {len(suffix)}: {token}"
    )


def test_explicit_token_from_env_is_returned_verbatim_without_prefix(tmp_path, monkeypatch):
    """An operator who sets ``GITOMA_API_TOKEN=my-own-string`` gets
    exactly that string back — we do NOT rewrite or prefix their value.
    The prefix is purely a convenience on the auto-generation path."""
    from gitoma.core.config import ensure_runtime_api_token

    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr(
        "gitoma.core.config.RUNTIME_TOKEN_FILE", tmp_path / "runtime_token",
    )
    monkeypatch.setattr(
        "gitoma.core.config.load_config",
        lambda: type("C", (), {"api_auth_token": "my-own-string-42"})(),
    )

    token, generated = ensure_runtime_api_token()
    assert generated is False
    assert token == "my-own-string-42"
    assert not token.startswith("gitoma_")


def test_previously_persisted_token_is_reused_even_without_prefix(tmp_path, monkeypatch):
    """Forward compatibility: tokens generated by an older Gitoma (no
    prefix) stay valid across an upgrade. We don't forcibly rotate."""
    from gitoma.core.config import ensure_runtime_api_token

    token_file = tmp_path / "runtime_token"
    token_file.write_text("legacy-token-no-prefix-xyz")
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr(
        "gitoma.core.config.load_config",
        lambda: type("C", (), {"api_auth_token": ""})(),
    )

    token, generated = ensure_runtime_api_token()
    assert generated is False
    assert token == "legacy-token-no-prefix-xyz"
