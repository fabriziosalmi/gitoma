"""Core configuration — loads from ~/.gitoma/config.toml + .env."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toml
from dotenv import dotenv_values, load_dotenv

GITOMA_DIR = Path.home() / ".gitoma"
CONFIG_FILE = GITOMA_DIR / "config.toml"
ENV_FILE = GITOMA_DIR / ".env"

# Snapshot the shell environment BEFORE any `load_dotenv()` can mutate it, so
# we can later tell "this came from the real shell" vs "this came from a .env
# file that dotenv dumped into os.environ". Captured at import time, which is
# before load_config() is ever called.
_SHELL_ENV_SNAPSHOT: dict[str, str] = dict(os.environ)

# Auto-generated API token is persisted here between runs, so the cockpit's
# localStorage copy stays valid across restarts. Mode 0o600.
RUNTIME_TOKEN_FILE = GITOMA_DIR / "runtime_token"


@dataclass
class LMStudioConfig:
    base_url: str = "http://localhost:1234/v1"
    model: str = "gemma-4-e2b-it"
    critic_model: str = ""
    api_key: str = "lm-studio"
    temperature: float = 0.3
    max_tokens: int = 4096


@dataclass
class GitHubConfig:
    token: str = ""


@dataclass
class BotConfig:
    name: str = "FabGPT"
    email: str = "fabgpt.inbox@gmail.com"
    github_user: str = "fabgpt-coder"


@dataclass
class Config:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    lmstudio: LMStudioConfig = field(default_factory=LMStudioConfig)
    api_auth_token: str = ""

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = ok)."""
        errors: list[str] = []
        if not self.github.token:
            errors.append("GITHUB_TOKEN is not set. Run: gitoma config set GITHUB_TOKEN=<token>")
        return errors


def load_config() -> Config:
    """Load configuration from ~/.gitoma/.env and ~/.gitoma/config.toml."""
    GITOMA_DIR.mkdir(parents=True, exist_ok=True)

    # Load .env from gitoma dir + local project .env
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    load_dotenv()  # local .env if present

    # Load TOML config
    raw: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        raw = toml.load(CONFIG_FILE)

    # Build config — TOML < ENV (env overrides TOML)
    github = GitHubConfig(
        token=os.getenv("GITHUB_TOKEN", raw.get("github", {}).get("token", "")),
    )

    bot_raw = raw.get("bot", {})
    bot = BotConfig(
        name=os.getenv("BOT_NAME", bot_raw.get("name", "FabGPT")),
        email=os.getenv("BOT_EMAIL", bot_raw.get("email", "fabgpt.inbox@gmail.com")),
        github_user=os.getenv("BOT_GITHUB_USER", bot_raw.get("github_user", "fabgpt-coder")),
    )

    lm_raw = raw.get("lmstudio", {})
    lmstudio = LMStudioConfig(
        base_url=os.getenv("LM_STUDIO_BASE_URL", lm_raw.get("base_url", "http://localhost:1234/v1")),
        model=os.getenv("LM_STUDIO_MODEL", lm_raw.get("model", "gemma-4-e2b-it")),
        critic_model=os.getenv("CRITIC_MODEL", lm_raw.get("critic_model", "")),
        api_key=os.getenv("LM_STUDIO_API_KEY", lm_raw.get("api_key", "lm-studio")),
        temperature=float(os.getenv("LM_STUDIO_TEMPERATURE", lm_raw.get("temperature", 0.3))),
        max_tokens=int(os.getenv("LM_STUDIO_MAX_TOKENS", lm_raw.get("max_tokens", 4096))),
    )

    return Config(
        github=github, 
        bot=bot, 
        lmstudio=lmstudio,
        api_auth_token=os.getenv("GITOMA_API_TOKEN", raw.get("api_auth_token", ""))
    )


def save_config_value(key: str, value: str) -> None:
    """Persist a key=value to ~/.gitoma/config.toml."""
    GITOMA_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        raw = toml.load(CONFIG_FILE)

    # Map flat key to nested TOML
    mapping: dict[str, tuple[str, str]] = {
        "GITHUB_TOKEN": ("github", "token"),
        "BOT_NAME": ("bot", "name"),
        "BOT_EMAIL": ("bot", "email"),
        "BOT_GITHUB_USER": ("bot", "github_user"),
        "LM_STUDIO_BASE_URL": ("lmstudio", "base_url"),
        "LM_STUDIO_MODEL": ("lmstudio", "model"),
        "CRITIC_MODEL": ("lmstudio", "critic_model"),
        "LM_STUDIO_API_KEY": ("lmstudio", "api_key"),
        "LM_STUDIO_TEMPERATURE": ("lmstudio", "temperature"),
        "LM_STUDIO_MAX_TOKENS": ("lmstudio", "max_tokens"),
        "GITOMA_API_TOKEN": ("api", "token"),
    }

    if key not in mapping:
        raise ValueError(f"Unknown config key: {key}. Valid keys: {list(mapping)}")

    section, field_name = mapping[key]
    raw.setdefault(section, {})[field_name] = value

    with CONFIG_FILE.open("w") as f:
        toml.dump(raw, f)


def resolve_config_source(
    env_key: str,
    toml_section: str | None = None,
    toml_key: str | None = None,
) -> tuple[str, str]:
    """Return ``(value, source_label)`` for a config key.

    Source priority (matches ``load_config``'s actual behaviour):

      1. Real shell env var (present in the snapshot taken at import time,
         before any ``load_dotenv``).
      2. ``~/.gitoma/.env``  — dotenv tries first inside load_config.
      3. ``<cwd>/.env``      — dotenv tries second.
      4. ``~/.gitoma/config.toml`` under ``[toml_section] toml_key``.
      5. ``default`` — nothing configured.

    The returned label is an absolute path for file sources, or the string
    ``"env"`` for the shell, or ``"default"``. This is what lets the CLI
    tell the user "your new GITHUB_TOKEN won't win, ``<cwd>/.env`` already
    has one".
    """
    shell_val = _SHELL_ENV_SNAPSHOT.get(env_key)
    if shell_val:
        return shell_val, "env"

    home_env_path = ENV_FILE
    if home_env_path.exists():
        v = (dotenv_values(home_env_path) or {}).get(env_key)
        if v:
            return v, str(home_env_path)

    cwd_env_path = Path.cwd() / ".env"
    if cwd_env_path.exists():
        v = (dotenv_values(cwd_env_path) or {}).get(env_key)
        if v:
            return v, str(cwd_env_path)

    if toml_section and toml_key and CONFIG_FILE.exists():
        try:
            raw = toml.load(CONFIG_FILE)
            v = raw.get(toml_section, {}).get(toml_key, "")
        except Exception:
            v = ""
        if v:
            return str(v), str(CONFIG_FILE)

    return "", "default"


def find_overriding_sources(env_key: str) -> list[str]:
    """Return every source (in winning order) that already sets `env_key`
    with priority higher than config.toml.

    Used by ``gitoma config set`` to warn users that their change will be
    silently overridden — the exact foot-gun that caused this helper to
    exist in the first place.
    """
    hits: list[str] = []
    if _SHELL_ENV_SNAPSHOT.get(env_key):
        hits.append("env")
    if ENV_FILE.exists() and (dotenv_values(ENV_FILE) or {}).get(env_key):
        hits.append(str(ENV_FILE))
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists() and (dotenv_values(cwd_env) or {}).get(env_key):
        hits.append(str(cwd_env))
    return hits


def ensure_runtime_api_token() -> tuple[str, bool]:
    """Return an API token, generating + persisting one if none is configured.

    Priority:
      1. Explicit `GITOMA_API_TOKEN` from env / config.toml — returned as-is.
      2. Previously-generated token in `~/.gitoma/runtime_token` — reused so
         the cockpit's localStorage survives restarts.
      3. Freshly-generated `secrets.token_urlsafe(32)`, written to the file
         with mode 0o600.

    Returns `(token, was_generated_now)`. The caller (typically
    `gitoma serve`) is expected to publish the token via the process env so
    `load_config()` picks it up in every `verify_token` call.
    """
    cfg = load_config()
    if cfg.api_auth_token:
        return cfg.api_auth_token, False

    GITOMA_DIR.mkdir(parents=True, exist_ok=True)
    if RUNTIME_TOKEN_FILE.exists():
        persisted = RUNTIME_TOKEN_FILE.read_text().strip()
        if persisted:
            return persisted, False

    token = secrets.token_urlsafe(32)
    # Create with 0o600 at the syscall boundary — never let the token exist
    # on disk world-readable, not even for the microsecond between write()
    # and chmod(). On filesystems that can't honor POSIX perms (FAT, some
    # network mounts), fail closed: delete the file and raise, so we don't
    # silently leave a valid API token readable by every local user.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(RUNTIME_TOKEN_FILE, flags, 0o600)
    except OSError as e:
        raise RuntimeError(f"Cannot create runtime token file: {e}") from e
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            # fchmod not supported on this FS — we'll still verify st_mode
            # below and fail closed if the mode didn't stick.
            pass
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)

    try:
        mode = RUNTIME_TOKEN_FILE.stat().st_mode & 0o777
    except OSError as e:  # pragma: no cover — stat() almost never fails here
        raise RuntimeError(f"Cannot stat runtime token file: {e}") from e
    if mode & 0o077:
        try:
            RUNTIME_TOKEN_FILE.unlink()
        except OSError:
            pass
        raise RuntimeError(
            "Cannot restrict runtime-token file permissions on this "
            f"filesystem (mode=0o{mode:o}). Set GITOMA_API_TOKEN explicitly "
            "instead so the token isn't persisted world-readable."
        )
    return token, True
