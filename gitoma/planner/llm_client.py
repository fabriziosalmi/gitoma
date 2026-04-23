"""LM Studio health checks and LLM client — robust, multi-level diagnostics."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

from gitoma.core.config import Config

if TYPE_CHECKING:
    from openai import OpenAI


# ─────────────────────────────────────────────────────────────────────────────
# Health check result types
# ─────────────────────────────────────────────────────────────────────────────

class HealthLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass
class HealthCheckResult:
    level: HealthLevel
    message: str
    detail: str = ""
    available_models: list[str] = field(default_factory=list)
    target_model_loaded: bool = False

    @property
    def ok(self) -> bool:
        return self.level == HealthLevel.OK

    @property
    def failed(self) -> bool:
        return self.level == HealthLevel.ERROR


# ─────────────────────────────────────────────────────────────────────────────
# LM Studio health checker (3 levels: connection → models → target model)
# ─────────────────────────────────────────────────────────────────────────────

def check_lmstudio(config: Config, timeout: float = 10.0) -> HealthCheckResult:
    """
    Perform a 3-level health check on LM Studio:

    Level 1 — TCP connectivity to base_url
    Level 2 — /v1/models endpoint returns a list
    Level 3 — the configured model name is in the list

    Returns a HealthCheckResult with full diagnostic info.
    Never raises; all exceptions are caught and surfaced in the result.
    """
    import httpx

    base_url = config.lmstudio.base_url.rstrip("/")
    model_name = config.lmstudio.model

    # ── Level 1: HTTP connection ──────────────────────────────────────────
    try:
        resp = httpx.get(f"{base_url}/models", timeout=timeout)
    except httpx.ConnectError as e:
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message="LM Studio is not reachable",
            detail=(
                f"Connection refused at {base_url}.\n"
                f"  → Start LM Studio and ensure the local server is ON "
                f"(Server tab → Start Server).\n"
                f"  → Technical: {type(e).__name__}: {str(e)[:120]}"
            ),
        )
    except httpx.TimeoutException:
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message="LM Studio connection timed out",
            detail=(
                f"No response from {base_url} within {timeout}s.\n"
                f"  → The server may be starting up. Try again in a few seconds."
            ),
        )
    except Exception as e:
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message=f"Unexpected connection error: {type(e).__name__}",
            detail=str(e)[:200],
        )

    # ── Level 2: Parse model list ─────────────────────────────────────────
    if resp.status_code not in (200, 201):
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message=f"LM Studio returned HTTP {resp.status_code}",
            detail=(
                f"GET {base_url}/models → {resp.status_code}\n"
                f"  → Body: {resp.text[:200]}"
            ),
        )

    try:
        data = resp.json()
        raw_models = data.get("data", [])
        available_models = [
            m.get("id", "") for m in raw_models if isinstance(m, dict)
        ]
    except Exception as e:
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message="Could not parse /v1/models response",
            detail=(
                f"Response body was not valid JSON or unexpected shape.\n"
                f"  → {type(e).__name__}: {str(e)[:120]}\n"
                f"  → Raw: {resp.text[:200]}"
            ),
        )

    if not available_models:
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message="LM Studio is running but no models are loaded",
            detail=(
                "The server responds but the model list is empty.\n"
                f"  → Load a model in LM Studio (target: {model_name})."
            ),
            available_models=[],
        )

    # ── Level 3: Check target model ───────────────────────────────────────
    # Fuzzy match: LM Studio IDs can be "publisher/model-name" or just "model-name"
    target_loaded = _model_matches(model_name, available_models)

    if not target_loaded:
        # Build a helpful suggestion
        suggestions = _suggest_similar(model_name, available_models)
        detail_lines = [
            f"Configured model [bold]{model_name}[/bold] is not loaded in LM Studio.",
            f"  → Available model(s): {', '.join(available_models[:5])}",
        ]
        if suggestions:
            detail_lines.append(
                f"  → Did you mean: {suggestions[0]}?"
            )
        detail_lines.append(
            f"  → Load the model in LM Studio, or update config:\n"
            f"     gitoma config set LM_STUDIO_MODEL={available_models[0]}"
        )
        return HealthCheckResult(
            level=HealthLevel.ERROR,
            message=f"Model '{model_name}' is not loaded in LM Studio",
            detail="\n".join(detail_lines),
            available_models=available_models,
            target_model_loaded=False,
        )

    return HealthCheckResult(
        level=HealthLevel.OK,
        message=f"LM Studio ready — model '{model_name}' is loaded",
        detail=f"Available models: {', '.join(available_models[:5])}",
        available_models=available_models,
        target_model_loaded=True,
    )


def _model_matches(target: str, available: list[str]) -> bool:
    """
    Fuzzy model ID match.
    LM Studio may use 'google/gemma-4-e2b-it' or 'gemma-4-e2b-it'.
    Matches if target is a suffix of or equal to any available ID.
    """
    target_lower = target.lower()
    for m in available:
        m_lower = m.lower()
        if m_lower == target_lower:
            return True
        # e.g. target="gemma-4-e2b-it", available="google/gemma-4-e2b-it"
        if m_lower.endswith("/" + target_lower):
            return True
        # e.g. target includes publisher prefix
        if target_lower.endswith("/" + m_lower):
            return True
    return False


def _suggest_similar(target: str, available: list[str]) -> list[str]:
    """Find models whose name contains parts of the target (keyword overlap)."""
    target_parts = set(re.split(r"[/-]", target.lower()))
    scored: list[tuple[int, str]] = []
    for m in available:
        m_parts = set(re.split(r"[/-]", m.lower()))
        overlap = len(target_parts & m_parts)
        if overlap:
            scored.append((overlap, m))
    scored.sort(reverse=True)
    return [m for _, m in scored[:3]]


# ─────────────────────────────────────────────────────────────────────────────
# LLM Client
# ─────────────────────────────────────────────────────────────────────────────

class LLMError(Exception):
    """Raised for unrecoverable LLM errors."""


class LLMTruncatedError(LLMError):
    """Raised when the LLM response was cut off by the token budget.

    A response cut off mid-JSON parses cleanly via best-effort fallback
    (the brace-matching extractor closes the dangling structure with the
    last seen ``}``), so the caller would silently accept a partial plan
    or partial patch. We instead raise an explicit error so the worker /
    planner can decide to retry with a higher ``max_tokens`` or fail
    loudly rather than commit a half-baked result.
    """


class LLMClient:
    """OpenAI-compatible client pointing at LM Studio with robust error handling."""

    def __init__(self, config: Config) -> None:
        self._config = config
        # Build the client lazily to avoid import-time failures
        self._client = self._build_client()
        # Last call's token usage, populated by chat() on success.
        # First step of M2 cost telemetry — callers (critic panel for
        # now, eventually trace events for every call) read this
        # immediately after chat() returns. Tuple of
        # (prompt_tokens, completion_tokens), or None when the
        # backend did not report usage on the last call.
        self._last_usage: tuple[int, int] | None = None

    def _build_client(self) -> "OpenAI":
        try:
            from openai import OpenAI
            import os
            # 120s default is OK for medium-context worker calls; some
            # phases (Q&A on 4B model with multi-file context) need more.
            # Operator-tunable via env, capped at 600s to surface stuck
            # calls instead of pretending forever.
            try:
                _t = float(os.environ.get("LM_STUDIO_TIMEOUT") or "120")
            except ValueError:
                _t = 120.0
            _t = max(10.0, min(600.0, _t))
            return OpenAI(
                base_url=self._config.lmstudio.base_url,
                api_key=self._config.lmstudio.api_key,
                timeout=_t,
            )
        except Exception as e:
            raise LLMError(f"Failed to initialize OpenAI client: {e}") from e

    @property
    def model(self) -> str:
        return self._config.lmstudio.model

    # ── Public API ──────────────────────────────────────────────────────────

    def health_check(self) -> HealthCheckResult:
        """Run a full 3-level health check. Never raises."""
        return check_lmstudio(self._config)

    def chat(
        self,
        messages: list[dict[str, str]],
        retries: int = 3,
        retry_delay: float = 2.0,
        *,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """
        Send a chat completion request. Returns the raw text response.

        ``temperature`` is keyword-only — when provided overrides the
        config default for this single call (e.g. critic panel wants
        deterministic-ish reviews without changing global config).

        ``model`` is keyword-only — when provided routes this single
        call to a different model loaded on the same LM Studio server
        (e.g. critic panel uses gemma for personas + a bigger model for
        the devil's advocate). Falls back to ``self.model`` (which reads
        from config) when omitted, preserving backwards compat.

        Retries on transient connection/timeout errors with exponential backoff.
        Raises LLMError on unrecoverable failures.

        After every successful call, ``self._last_usage`` is set to a
        ``(prompt_tokens, completion_tokens)`` tuple if the backend
        reported usage, else ``None``. The tuple is the first step
        toward project-wide cost telemetry (M2) — the critic panel
        already reads it; the rest of the codebase will follow.
        """
        from openai import (
            APIConnectionError,
            APITimeoutError,
            APIStatusError,
            RateLimitError,
        )

        last_error: Exception | None = None

        # Optional: append the Qwen3 ``/no_think`` soft-switch to the LAST
        # user message when ``LM_STUDIO_DISABLE_THINKING=true``. Verified
        # 2026-04-23 against ``qwen/qwen3-8b`` on Mac: reasoning_tokens
        # dropped from 297 → 1, content unchanged. NO-OP on models that
        # don't recognise the suffix (DeepSeek-R1, Qwen3.5) — they treat
        # the trailing ``/no_think`` as harmless prose. Per-call: makes a
        # COPY of the messages list so the caller's data is untouched.
        import os as _os
        if (_os.environ.get("LM_STUDIO_DISABLE_THINKING") or "").lower() in ("1", "true", "yes"):
            messages = _append_no_think(messages)

        for attempt in range(retries):
            try:
                response = self._client.chat.completions.create(
                    model=model if model else self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=(
                        temperature
                        if temperature is not None
                        else self._config.lmstudio.temperature
                    ),
                    max_tokens=self._config.lmstudio.max_tokens,
                )
                choice = response.choices[0]
                content = choice.message.content
                # Capture token usage for the caller (cost telemetry).
                # Some self-hosted OpenAI-compat backends omit ``.usage``;
                # we tolerate that with None.
                usage = getattr(response, "usage", None)
                if usage is not None:
                    pt = getattr(usage, "prompt_tokens", None)
                    ct = getattr(usage, "completion_tokens", None)
                    if pt is not None and ct is not None:
                        self._last_usage = (int(pt), int(ct))
                    else:
                        self._last_usage = None
                else:
                    self._last_usage = None
                if content is None:
                    raise LLMError("LLM returned an empty response (content=None)")
                # OpenAI-compatible APIs (LM Studio included) report why the
                # generation stopped via ``finish_reason``. ``"length"``
                # means we hit ``max_tokens`` mid-output — the trailing
                # tokens are silently dropped, so any JSON inside is now
                # syntactically valid-looking but semantically truncated.
                # We surface this so the caller can retry / bail instead
                # of accepting a half-formed response.
                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason == "length":
                    raise LLMTruncatedError(
                        f"LLM response was truncated by max_tokens "
                        f"({self._config.lmstudio.max_tokens} tokens). "
                        "Increase LM_STUDIO_MAX_TOKENS or shrink the prompt."
                    )
                return str(content)

            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    time.sleep(wait)

            except RateLimitError as e:
                # LM Studio shouldn't rate-limit but handle gracefully
                raise LLMError(
                    f"LM Studio rate limit hit (unexpected): {e}"
                ) from e

            except APIStatusError as e:
                # e.g. model not found, bad request
                raise LLMError(
                    f"LM Studio API error {e.status_code}: {e.message}"
                ) from e

            except LLMError:
                raise

            except Exception as e:
                # Unexpected: don't retry, surface immediately
                raise LLMError(
                    f"Unexpected LLM error ({type(e).__name__}): {e}"
                ) from e

        raise LLMError(
            f"LM Studio connection failed after {retries} attempt(s). "
            f"Last error: {type(last_error).__name__}: {last_error}"
        )

    def chat_json(
        self,
        messages: list[dict[str, str]],
        retries: int = 3,
    ) -> dict[str, Any]:
        """
        Like chat() but parses and returns the JSON response.

        Strips markdown code fences. On parse failure, adds a correction turn
        and retries. Raises LLMError if JSON cannot be obtained.
        """
        current_messages = list(messages)

        for attempt in range(retries):
            # chat() already retries on connection errors internally
            try:
                raw = self.chat(current_messages, retries=1)
            except LLMError:
                raise  # don't swallow — let caller handle

            cleaned = _extract_json(raw)

            try:
                parsed = json.loads(cleaned)
                if not isinstance(parsed, dict):
                    raise json.JSONDecodeError("Expected a JSON object", cleaned, 0)
                return parsed
            except json.JSONDecodeError:
                if attempt < retries - 1:
                    # Append a correction turn and retry (in-context self-correction)
                    current_messages = current_messages + [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON. "
                                "Respond with ONLY a valid JSON object — "
                                "no markdown fences, no explanation, no extra text. "
                                "Start your response with { and end with }."
                            ),
                        },
                    ]

        raise LLMError(
            f"Could not obtain valid JSON from LLM after {retries} attempt(s). "
            f"Last raw response (truncated): {raw[:300]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# JSON extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _append_no_think(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Append ``/no_think`` to the last user message's content.

    Qwen3-family soft-switch: appending ``/no_think`` to the prompt
    short-circuits the chain-of-thought stage. Verified 2026-04-23 on
    ``qwen/qwen3-8b`` — reasoning tokens 297 → 1. NO-OP on models that
    don't implement the switch (the suffix is harmless prose).

    Returns a COPY so the caller's messages list isn't mutated.
    """
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    # Find the LAST user message and append the marker.
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            content = out[i].get("content") or ""
            if "/no_think" not in content:
                # Use newline rather than trailing space so the prompt
                # stays visually clean if a model echoes it back.
                out[i]["content"] = content + "\n/no_think"
            break
    return out


def _extract_json(text: str) -> str:
    """
    Extract a JSON object from LLM output.

    Handles:
    - ```json ... ``` and ``` ... ``` code fences
    - Leading/trailing prose around a JSON block
    - Partial JSON (best-effort)
    """
    # 1. Try ```json ... ``` fence
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 2. Find outermost { ... } pair
    start = text.find("{")
    if start == -1:
        return text.strip()

    # Walk forward tracking brace depth to find the matching }
    depth = 0
    in_string = False
    escape_next = False
    end = -1

    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end != -1:
        return text[start : end + 1]

    # Fallback: take from first { to last }
    last_brace = text.rfind("}")
    if last_brace > start:
        return text[start : last_brace + 1]

    return text.strip()
