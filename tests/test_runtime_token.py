"""Tests for `ensure_runtime_api_token` — the auto-bootstrap token helper."""

from __future__ import annotations

import stat

import pytest

from gitoma.core import config as config_module


@pytest.fixture(autouse=True)
def _isolated_gitoma_dir(tmp_path, monkeypatch):
    """Redirect every file ensure_runtime_api_token touches into tmp_path."""
    monkeypatch.setattr(config_module, "GITOMA_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config_module, "RUNTIME_TOKEN_FILE", tmp_path / "runtime_token")
    # Neutralize dotenv so the developer's real .env doesn't leak in.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **kw: False)
    for k in ("GITHUB_TOKEN", "GITOMA_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def test_returns_explicit_token_when_configured(monkeypatch):
    monkeypatch.setenv("GITOMA_API_TOKEN", "explicit-token")

    token, generated = config_module.ensure_runtime_api_token()

    assert token == "explicit-token"
    assert generated is False
    # Must NOT write to the runtime file when an explicit token wins.
    assert not config_module.RUNTIME_TOKEN_FILE.exists()


def test_reuses_previously_persisted_token():
    config_module.RUNTIME_TOKEN_FILE.write_text("persisted-token\n")

    token, generated = config_module.ensure_runtime_api_token()

    assert token == "persisted-token"
    assert generated is False


def test_generates_and_persists_new_token_with_600_mode():
    token, generated = config_module.ensure_runtime_api_token()

    assert generated is True
    assert len(token) >= 32
    assert config_module.RUNTIME_TOKEN_FILE.read_text().strip() == token
    # mode 0o600 — readable/writable only by the owner
    mode = stat.S_IMODE(config_module.RUNTIME_TOKEN_FILE.stat().st_mode)
    assert mode == 0o600


def test_regenerates_when_persisted_file_is_empty():
    config_module.RUNTIME_TOKEN_FILE.write_text("   \n")  # whitespace only

    token, generated = config_module.ensure_runtime_api_token()

    assert generated is True
    assert token.strip() == token
    assert config_module.RUNTIME_TOKEN_FILE.read_text().strip() == token


def test_explicit_token_beats_persisted_file(monkeypatch):
    """Operator-configured token takes priority over any previously
    auto-generated one — so `gitoma config set GITOMA_API_TOKEN=…` always
    wins without needing to delete the runtime file."""
    config_module.RUNTIME_TOKEN_FILE.write_text("stale-auto-token")
    monkeypatch.setenv("GITOMA_API_TOKEN", "operator-wins")

    token, generated = config_module.ensure_runtime_api_token()

    assert token == "operator-wins"
    assert generated is False
