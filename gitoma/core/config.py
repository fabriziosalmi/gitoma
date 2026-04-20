"""Core configuration — loads from ~/.gitoma/config.toml + .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toml
from dotenv import load_dotenv

GITOMA_DIR = Path.home() / ".gitoma"
CONFIG_FILE = GITOMA_DIR / "config.toml"
ENV_FILE = GITOMA_DIR / ".env"


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
