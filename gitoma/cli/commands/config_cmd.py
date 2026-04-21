"""gitoma config command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, TYPE_CHECKING

import typer

from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _ok,
    _warn,
)
from gitoma.core.config import load_config, save_config_value
from gitoma.ui.console import console

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command(
    name="config",
    # Allow unknown extra args so KEY=VALUE is never mis-parsed by Click/Typer
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def config_cmd(
    ctx: typer.Context,
    action: Annotated[str, typer.Argument(help="Action: set | show | path")],
) -> None:
    """
    ⚙️  Manage Gitoma configuration.

    [bold]Examples:[/bold]
      gitoma config set GITHUB_TOKEN=ghp_xxx
      gitoma config set LM_STUDIO_MODEL=gemma-4-e2b-it
      gitoma config show
      gitoma config path
    """
    from gitoma.core.config import CONFIG_FILE

    if action == "path":
        console.print(f"[code]{CONFIG_FILE}[/code]")
        return

    if action == "show":
        from gitoma.core.config import resolve_config_source
        cfg = load_config()
        token_display = (
            f"{'*' * 8}{cfg.github.token[-4:]}"
            if len(cfg.github.token) > 8
            else "(not set — run: gitoma config set GITHUB_TOKEN=...)"
        )

        # (env_var_name, toml_section, toml_key, display_row)
        keys = [
            ("GITHUB_TOKEN",           "github",   "token",        ("GitHub", "token", token_display)),
            ("BOT_NAME",               "bot",      "name",         ("Bot Identity", "name", cfg.bot.name)),
            ("BOT_EMAIL",              "bot",      "email",        ("Bot Identity", "email", cfg.bot.email)),
            ("BOT_GITHUB_USER",        "bot",      "github_user",  ("Bot Identity", "github_user", cfg.bot.github_user)),
            ("LM_STUDIO_BASE_URL",     "lmstudio", "base_url",     ("LM Studio", "base_url", cfg.lmstudio.base_url)),
            ("LM_STUDIO_MODEL",        "lmstudio", "model",        ("LM Studio", "model", cfg.lmstudio.model)),
            ("LM_STUDIO_TEMPERATURE",  "lmstudio", "temperature",  ("LM Studio", "temperature", str(cfg.lmstudio.temperature))),
            ("LM_STUDIO_MAX_TOKENS",   "lmstudio", "max_tokens",   ("LM Studio", "max_tokens", str(cfg.lmstudio.max_tokens))),
            ("GITOMA_API_TOKEN",       "api",      "token",        ("Cockpit API", "token",
                f"{'*' * 8}{cfg.api_auth_token[-4:]}" if len(cfg.api_auth_token) > 8 else "(auto-generated)")),
        ]

        def _fmt_source(src: str) -> str:
            home = str(Path.home())
            if src == "env":
                return "[warning]$ENV[/warning]"
            if src == "default":
                return "[muted]default[/muted]"
            # Collapse $HOME for readability.
            shown = src.replace(home, "~")
            return f"[code]{shown}[/code]"

        console.print("\n[heading]Gitoma Configuration[/heading]\n")
        current_section = None
        for env_key, tsec, tkey, (section, field_name, shown) in keys:
            if section != current_section:
                console.print(f"[muted]─ {section} {'─' * (50 - len(section))}[/muted]")
                current_section = section
            _, src = resolve_config_source(env_key, tsec, tkey)
            console.print(
                f"  {field_name:<14}[code]{shown}[/code]   {_fmt_source(src)}"
            )
        console.print(
            f"\n[muted]Precedence: $ENV > ~/.gitoma/.env > <cwd>/.env > {CONFIG_FILE}[/muted]"
        )
        return

    if action == "set":
        # Reconstruct KEY=VALUE from context.args.
        # This handles all shell edge cases:
        #   gitoma config set GITHUB_TOKEN=ghp_xxx      → ctx.args = ['GITHUB_TOKEN=ghp_xxx']
        #   gitoma config set GITHUB_TOKEN ghp_xxx      → ctx.args = ['GITHUB_TOKEN', 'ghp_xxx']
        #   gitoma config set GITHUB_TOKEN = ghp_xxx    → ctx.args = ['GITHUB_TOKEN', '=', 'ghp_xxx']
        raw_args = ctx.args  # list of remaining tokens Click didn't consume

        if not raw_args:
            console.print(
                "[danger]✗ Missing argument.[/danger]\n"
                "[muted]Usage: [primary]gitoma config set KEY=value[/primary]\n"
                "Example: [primary]gitoma config set GITHUB_TOKEN=ghp_xxx[/primary][/muted]"
            )
            raise typer.Exit(1)

        # Rejoin and normalize: handle 'KEY=VAL', 'KEY = VAL', or 'KEY VAL'
        joined = "".join(raw_args)         # remove spaces around '=': KEY=VAL
        if "=" not in joined:
            # Fallback: first token is KEY, rest is VALUE
            key = raw_args[0]
            value = " ".join(raw_args[1:]) if len(raw_args) > 1 else ""
        else:
            key, _, value = joined.partition("=")

        key = key.strip().upper()
        value = value.strip()

        if not key:
            _abort("Empty key. Usage: gitoma config set KEY=value")
        if not value:
            _abort(
                f"Empty value for key '{key}'.",
                hint=f"Usage: gitoma config set {key}=<your-value>",
            )

        # Intelligence: warn BEFORE writing when a higher-priority source
        # would silently override the new value. Root cause of many 'I
        # updated the token but it didn't take effect' incidents.
        from gitoma.core.config import find_overriding_sources
        overriding = find_overriding_sources(key)
        if overriding:
            console.print()
            _warn(
                f"Your new {key} will be overridden at load time by:",
            )
            for src in overriding:
                shown = src if src == "env" else src.replace(str(Path.home()), "~")
                label = "[warning]$ENV[/warning]" if src == "env" else f"[code]{shown}[/code]"
                console.print(f"  → {label}")
            console.print(
                "[muted]  Remove/rename that source first, or edit it "
                "directly. Writing to config.toml anyway.[/muted]\n"
            )

        try:
            save_config_value(key, value)
            _ok(f"Saved {key} → {CONFIG_FILE}")
        except ValueError as e:
            _abort(str(e))
        return

    _abort(f"Unknown action '{action}'", hint="Valid actions: set | show | path")
