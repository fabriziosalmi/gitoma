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
    """OpenAI-compatible client pointing at LM Studio with robust error handling.

    ``role`` (``"planner"`` default, or ``"worker"``) controls two things:

      1. Endpoint + model routing — when role=="worker" AND
         ``config.lmstudio.worker_base_url`` / ``worker_model`` are
         set, the client points at that endpoint with that model
         instead of the planner's. This is the parallel-topology hook
         (mm1 plans on qwen3-8b, mm2 codes on qwen3.5-9b).

      2. Anti-thinking env precedence — when role=="worker", the
         four no-think kill-switches first read
         ``LM_STUDIO_WORKER_<NAME>`` and only fall back to
         ``LM_STUDIO_<NAME>`` if the worker variant is unset.
         Lets the operator give the worker a model-specific recipe
         (e.g. PRELUDE on for qwen3.5-9b worker) without polluting
         the planner's call shape (qwen3-8b doesn't need it).
    """

    def __init__(self, config: Config, *, role: str = "planner") -> None:
        if role not in ("planner", "worker", "reviewer"):
            raise ValueError(
                f"role must be 'planner', 'worker', or 'reviewer', got {role!r}"
            )
        self._config = config
        self._role = role
        # Build the client lazily to avoid import-time failures
        self._client = self._build_client()
        # Last call's token usage, populated by chat() on success.
        # First step of M2 cost telemetry — callers (critic panel for
        # now, eventually trace events for every call) read this
        # immediately after chat() returns. Tuple of
        # (prompt_tokens, completion_tokens), or None when the
        # backend did not report usage on the last call.
        self._last_usage: tuple[int, int] | None = None
        # G14 — fenced-JSON guard fired on last chat_json() call.
        # Reset at the START of every chat_json call so callers can
        # read it AFTER the call to know if the model violated the
        # "no fences" prompt contract on this attempt.
        self._last_g14_fired: bool = False

    @classmethod
    def for_worker(cls, config: Config) -> "LLMClient":
        """Build a worker-routed client.

        Returns a client that points at ``worker_base_url`` /
        ``worker_model`` if either is set in config, falling back to
        the planner endpoint/model otherwise. Always tagged
        ``role="worker"`` so the role-aware anti-thinking lookup
        applies to every chat() call from the worker stack.
        """
        return cls(config, role="worker")

    @classmethod
    def for_reviewer(cls, config: Config) -> "LLMClient":
        """Build a reviewer-routed client.

        Mirrors ``for_worker``: when ``review_base_url`` /
        ``review_model`` are set, PHASE 5 self-critic hits a third
        endpoint/model (e.g. ``google/gemma-4-e2b@localhost`` for an
        out-of-family second opinion). Falls back to the planner
        endpoint when both are empty. Tagged ``role="reviewer"`` so
        operators can opt into review-specific anti-thinking via
        ``LM_STUDIO_REVIEW_DISABLE_THINKING_*`` envs.
        """
        return cls(config, role="reviewer")

    def _resolve_base_url(self) -> str:
        # ``getattr`` fallbacks support test doubles that bypass __init__
        # via ``LLMClient.__new__`` and never set ``_role`` /
        # ``worker_base_url`` on a MagicMock'd config.
        role = getattr(self, "_role", "planner")
        if role == "worker":
            wb = getattr(self._config.lmstudio, "worker_base_url", "") or ""
            if wb:
                return wb
        elif role == "reviewer":
            rb = getattr(self._config.lmstudio, "review_base_url", "") or ""
            if rb:
                return rb
        return self._config.lmstudio.base_url

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
                base_url=self._resolve_base_url(),
                api_key=self._config.lmstudio.api_key,
                timeout=_t,
            )
        except Exception as e:
            raise LLMError(f"Failed to initialize OpenAI client: {e}") from e

    @property
    def model(self) -> str:
        role = getattr(self, "_role", "planner")
        if role == "worker":
            wm = getattr(self._config.lmstudio, "worker_model", "") or ""
            if wm:
                return wm
        elif role == "reviewer":
            rm = getattr(self._config.lmstudio, "review_model", "") or ""
            if rm:
                return rm
        return self._config.lmstudio.model

    @property
    def role(self) -> str:
        return getattr(self, "_role", "planner")

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
        max_tokens: int | None = None,
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

        ``max_tokens`` is keyword-only — when provided overrides the
        config default for this single call. Use case: PHASE 5
        self-review on big PRs needs a bigger budget than the worker's
        4096 default (verified 2026-04-30 on PR #12 — global 4096
        truncated the review). Caller (e.g. ``self_critic``) is
        expected to source the value from a phase-specific env knob.

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

        # Role-aware env lookup: when this client is the worker (built
        # via ``LLMClient.for_worker``), each anti-thinking switch
        # first checks ``LM_STUDIO_WORKER_<NAME>`` then falls back
        # to the planner-side ``LM_STUDIO_<NAME>``. Lets the operator
        # apply a model-specific recipe to the worker (e.g. PRELUDE on
        # for qwen3.5-9b worker) without polluting the planner's call
        # shape (qwen3-8b doesn't need it).
        import os as _os
        # ``__new__``-bypassed test doubles may not set ``_role``;
        # default to planner so legacy fixtures keep working.
        _role = getattr(self, "_role", "planner")
        def _flag(name: str) -> bool:
            v = ""
            if _role == "worker":
                v = _os.environ.get(f"LM_STUDIO_WORKER_{name}") or ""
            elif _role == "reviewer":
                v = _os.environ.get(f"LM_STUDIO_REVIEW_{name}") or ""
            if not v:
                v = _os.environ.get(f"LM_STUDIO_{name}") or ""
            return v.lower() in ("1", "true", "yes")
        # Optional: append the Qwen3 ``/no_think`` soft-switch to the LAST
        # user message when ``LM_STUDIO_DISABLE_THINKING=true``. Verified
        # 2026-04-23 against ``qwen/qwen3-8b`` on Mac: reasoning_tokens
        # dropped from 297 → 1, content unchanged. NO-OP on models that
        # don't recognise the suffix (DeepSeek-R1, Qwen3.5) — they treat
        # the trailing ``/no_think`` as harmless prose. Per-call: makes a
        # COPY of the messages list so the caller's data is untouched.
        _disable_thinking = _flag("DISABLE_THINKING")
        # ``/no_think`` suffix in the message body — the Qwen3 family
        # soft-switch. Harmless prose to other models that don't
        # recognise it. Verified 2026-04-23 against ``qwen/qwen3-8b``:
        # reasoning_tokens 297 → 1, content unchanged.
        if _disable_thinking:
            messages = _append_no_think(messages)
        # Optional second prong: ``chat_template_kwargs={"enable_thinking":
        # false}`` via ``extra_body`` — the Jinja-template kill-switch
        # used by GLM, Gemma, some Qwen variants, and vLLM/Together
        # backends. NOT safe everywhere: LM Studio's OpenAI-compat shim
        # appears to silently choke on the unknown field for medium-
        # sized prompts (~6-8K tokens for the planner), returning 502
        # via llmproxy or hanging the upstream request. Gated behind a
        # SEPARATE env var so the default LM Studio path stays
        # unbroken; opt in when targeting a backend (vLLM, Together,
        # llmproxy with auto-disable plugin) that honors it. Verified
        # 2026-04-24 against ``google/gemma-4-e4b`` on llmproxy SHORT
        # prompts: reasoning_content 684 → 0.
        _extra_body: dict | None = None
        if _flag("DISABLE_THINKING_TEMPLATE_KWARG"):
            _extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
        # Optional third prong: top-level ``enable_thinking: false`` —
        # the only kill-switch exo's OpenAI-compat shim honors (verified
        # 2026-04-27 against ``mlx-community/Qwen3.6-35B-A3B-4bit``:
        # reasoning_content non-null → null, finish_reason length → stop).
        # exo silently ignores both ``/no_think`` AND
        # ``chat_template_kwargs``. Gated behind its own env so the
        # default LM Studio path stays unbroken; opt in when targeting
        # an exo cluster or another backend known to honor the
        # top-level field. Composes additively with the kwarg above —
        # backends that honor neither field (LM Studio) will reject
        # the unknown key, hence the explicit opt-in.
        if _flag("DISABLE_THINKING_TOPLEVEL"):
            if _extra_body is None:
                _extra_body = {}
            _extra_body["enable_thinking"] = False
        # Optional fourth prong: system-prompt prelude that explicitly
        # forbids reasoning. The only kill-switch that works on Qwen3.5
        # — verified 2026-04-30 against ``qwen/qwen3.5-9b`` on LM Studio:
        # ``/no_think`` suffix ignored (3425 ch reasoning), template
        # kwargs returned 502, top-level field unsupported. With this
        # prelude reasoning_content collapsed 3425 → 391 chars (≈10×)
        # and content rendered in 14.8s vs 182s out-of-box. Composes
        # additively with the other 3 prongs — for backends that honor
        # them, having the prelude AS WELL is harmless. Gated behind
        # its own env so the default LM Studio path stays unbroken;
        # opt in for reasoning models that ignore ``/no_think`` and
        # template kwargs (Qwen3.5, some DeepSeek-R1 variants).
        if _flag("DISABLE_THINKING_PRELUDE"):
            messages = _prepend_no_think_prelude(messages)

        for attempt in range(retries):
            try:
                # Effective max_tokens precedence:
                #   1. explicit kwarg (caller wants exact control —
                #      e.g. self_critic with PHASE-5 budget)
                #   2. role-aware worker config (when role=="worker"
                #      and lmstudio.worker_max_tokens > 0) — lets
                #      the worker have a bigger budget than the
                #      planner without bloating planner JSON calls
                #   3. global config max_tokens
                if max_tokens is not None:
                    _effective_max_tokens = max_tokens
                else:
                    _wmt = getattr(
                        self._config.lmstudio, "worker_max_tokens", 0,
                    ) or 0
                    if _role == "worker" and _wmt > 0:
                        _effective_max_tokens = _wmt
                    else:
                        _effective_max_tokens = self._config.lmstudio.max_tokens
                _create_kwargs: dict = dict(
                    model=model if model else self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=(
                        temperature
                        if temperature is not None
                        else self._config.lmstudio.temperature
                    ),
                    max_tokens=_effective_max_tokens,
                )
                if _extra_body is not None:
                    _create_kwargs["extra_body"] = _extra_body
                response = self._client.chat.completions.create(**_create_kwargs)
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
                        f"({_effective_max_tokens} tokens). "
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

        G14 — fenced-JSON guard: when the raw response is wrapped in
        `````json ...````` despite the prompt's
        "no fences" instruction, ``self._last_g14_fired`` flips True
        and a ``g14_fenced_json`` trace event is emitted (model + role).
        Default behaviour: silent repair, same as before. Opt-in
        ``GITOMA_G14_REJECT_FENCED_JSON=1`` flips behaviour to
        fail-fast — useful in benches/CI to surface offending models
        rather than masking the issue.
        """
        import os as _g14_os
        _g14_strict = (
            _g14_os.environ.get("GITOMA_G14_REJECT_FENCED_JSON") or ""
        ).lower() in ("1", "true", "yes")
        self._last_g14_fired = False

        current_messages = list(messages)

        for attempt in range(retries):
            # chat() already retries on connection errors internally
            try:
                raw = self.chat(current_messages, retries=1)
            except LLMError:
                raise  # don't swallow — let caller handle

            # G14 detection runs on RAW output before any fence-stripping
            # / brace-walking, so we see what the model actually emitted.
            if _detect_fenced_json(raw):
                self._last_g14_fired = True
                try:
                    from gitoma.core.trace import current as _g14_ct
                    _g14_ct().emit(
                        "g14_fenced_json",
                        role=getattr(self, "_role", "planner"),
                        model=self.model,
                        attempt=attempt + 1,
                        chars=len(raw),
                    )
                except Exception:
                    pass
                if _g14_strict:
                    raise LLMError(
                        "G14: model emitted fenced JSON despite the prompt's "
                        "'no fences, no explanation' instruction. "
                        "Set GITOMA_G14_REJECT_FENCED_JSON=0 (or unset) to "
                        "fall back to silent repair. "
                        f"Raw (head): {raw[:160]}"
                    )

            cleaned = _extract_json(raw)

            try:
                parsed = json.loads(cleaned)
                if not isinstance(parsed, dict):
                    raise json.JSONDecodeError("Expected a JSON object", cleaned, 0)
                return parsed
            except json.JSONDecodeError:
                # One in-process repair attempt before burning an LLM
                # round-trip. The Defender prompt occasionally emits
                # JSON with bare double-quotes inside string values
                # (``"the test \"asserts\" X"`` written without the
                # backslashes) and trailing commas — both deterministic
                # to fix without a re-prompt. Saves ~30s of round-trip
                # latency per recovered call on a 4B-class model.
                try:
                    repaired = _attempt_json_repair(cleaned)
                    if repaired != cleaned:
                        parsed = json.loads(repaired)
                        if isinstance(parsed, dict):
                            return parsed
                except json.JSONDecodeError:
                    pass
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

_NO_THINK_PRELUDE = (
    "NEVER output reasoning, thinking, analysis, <think> tags, or "
    "chain-of-thought. Reply with ONLY the final answer — no preamble, "
    "no commentary."
)


def _prepend_no_think_prelude(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prepend a system-prompt prelude that forbids reasoning output.

    Fourth prong of the no-think kill-switch family. Verified 2026-04-30
    on ``qwen/qwen3.5-9b`` (LM Studio): the model ignores both
    ``/no_think`` AND ``chat_template_kwargs={"enable_thinking": false}``,
    but a system message that explicitly forbids thinking collapses
    reasoning_content from 3425 → 391 chars and turnaround from 182s →
    14.8s on a 1-line diff task.

    Behaviour:
      * Empty list → returned unchanged.
      * Idempotent — the prelude string is a sentinel; if it appears
        anywhere in any system message, no-op.
      * If a system message already exists, prepend the prelude to its
        content with ``\\n\\n`` separator. The directive goes FIRST so
        the model sees "no thinking" before any task-specific persona.
      * If no system message exists, insert a new one at index 0.

    Returns a copy so the caller's messages list isn't mutated.
    """
    if not messages:
        return messages
    # Idempotency check across all system messages.
    for m in messages:
        if m.get("role") == "system" and _NO_THINK_PRELUDE in (m.get("content") or ""):
            return [dict(m) for m in messages]
    out = [dict(m) for m in messages]
    # Find first system message.
    for i, m in enumerate(out):
        if m.get("role") == "system":
            existing = m.get("content") or ""
            out[i]["content"] = _NO_THINK_PRELUDE + "\n\n" + existing if existing else _NO_THINK_PRELUDE
            return out
    # No system message — insert a new one at the front.
    return [{"role": "system", "content": _NO_THINK_PRELUDE}] + out


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


def _detect_fenced_json(text: str) -> bool:
    """G14 detection: did the raw response wrap its JSON in markdown fences?

    Recognises both ``` ```json ... ``` ``` and bare ``` ``` ... ``` ```
    around what looks like JSON. Whitespace-tolerant. Used to flag
    coder/instruct fine-tunes that ignore "no fences, no explanation"
    in the prompt — the silent fence-strip in ``_attempt_json_repair``
    + brace-walk in ``_extract_json`` recover the JSON either way, but
    without telemetry we never see WHICH models do this and HOW often.

    Returns True only when:
      * stripped body starts with three backticks, AND
      * after the optional language tag + first newline, there's a
        ``{`` — i.e. it really is fenced JSON, not e.g. ``` ```python
        def foo() ``` ``` (which is not our concern here).

    Conservative: false negatives on rare shapes are fine — the silent
    repair handles them; we just miss the telemetry bump on those.
    """
    body = text.strip()
    if not body.startswith("```"):
        return False
    # Drop optional language tag through to first newline.
    rest = body[3:]
    nl = rest.find("\n")
    if nl == -1:
        # ``` foo ``` single-line — too unusual to count as fenced JSON.
        return False
    after_tag = rest[nl + 1:].lstrip()
    return after_tag.startswith("{") or after_tag.startswith("[")


def _attempt_json_repair(s: str) -> str:
    """Try to repair the most common LLM JSON authoring slop.

    Three passes, all string-aware:

      1. **Markdown-fence strip** — coder/instruct fine-tunes wrap
         JSON in ``` ```json ... ``` ``` despite the prompt's
         "no fences" instruction. Verified live 2026-04-24 against
         ``google/gemma-4-e4b`` on llmproxy. Strip a single leading
         ``` ```{lang}? ``` and trailing ``` ``` ``` opener-closer
         pair. Idempotent on already-clean strings.

      2. **Trailing-comma strip** — ``{"a": 1,}`` and ``[1, 2,]`` are
         legal in JS / Python but not JSON. Strip commas immediately
         followed (after whitespace) by ``}`` or ``]``, but ONLY when
         we're outside a string literal.

      3. **Bare-quote escape** — ``"rationale": "the test "asserts"
         something"`` parses as three concatenated strings (and fails).
         Walk every string literal: the OPENER is the first ``"`` after
         a ``:`` or ``,``; the CLOSER is the next ``"`` followed by
         ``,`` / ``}`` / ``]`` / whitespace+EOF / newline+JSON syntax.
         Every quote BETWEEN opener and closer that doesn't qualify as
         a closer is content and gets backslash-escaped.

    Returns the (possibly modified) string. Caller is expected to
    re-attempt ``json.loads`` and surrender if it still fails — this
    is a best-effort repair, not a JSON5 parser. Out of scope:
    single-quoted keys, unquoted keys, hex literals, comments. Those
    are JS-isms a 4B-class model rarely emits in JSON-mode prompts.

    Caught live in the Q&A Defender pipeline (rung-3 series): when
    the Defender's ``rationale`` field quoted a test name with double
    quotes, the entire JSON broke and the Q&A phase had to retry +
    burn another LLM round-trip. Now repaired in-process.
    """
    return _escape_bare_quotes(_strip_trailing_commas(_strip_markdown_fences(s)))


def _strip_markdown_fences(s: str) -> str:
    """Remove a single ``` ```{lang}? ... ``` ``` wrapper around the
    payload. Handles ``json``, ``yaml``, ``toml``, etc. or no language
    tag. Trims surrounding whitespace before checking. Returns ``s``
    unchanged when no opener-closer pair is present.

    Examples that get stripped::

        ```json
        {"a": 1}
        ```

        ```
        {"a": 1}
        ```

        ```\\n{"a":1}\\n```

    Idempotent: stripping a fence-less string is a no-op. Conservative:
    only strips ONE wrapper layer (the rare nested case stays intact
    so the caller's parser surfaces the real shape error).
    """
    body = s.strip()
    if not body.startswith("```"):
        return s
    # Find the closing ```
    if body.endswith("```"):
        # Strip leading ``` plus optional language identifier through to newline
        rest = body[3:]
        nl = rest.find("\n")
        if nl == -1:
            # ``` foo ``` single-line form — bare fence without payload
            inner = rest[:-3].strip()
        else:
            # Skip lang tag if present (alphanumeric only, no spaces)
            lang_part = rest[:nl]
            if lang_part.strip().isalnum() or lang_part.strip() == "":
                inner = rest[nl + 1:-3]
            else:
                # Unexpected leading line — bail rather than risk corrupting
                return s
        return inner.strip()
    return s


def _strip_trailing_commas(s: str) -> str:
    """Remove ``,`` immediately preceding ``}``/``]`` (whitespace-tolerant),
    skipping characters inside string literals."""
    out: list[str] = []
    i = 0
    n = len(s)
    in_string = False
    escape_next = False
    while i < n:
        ch = s[i]
        if escape_next:
            out.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == "\\" and in_string:
            out.append(ch)
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if not in_string and ch == ",":
            # Look ahead past whitespace for } or ]. If found, drop the
            # comma; else preserve it.
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                # Skip the comma; whitespace stays for readability.
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_bare_quotes(s: str) -> str:
    """Escape unescaped double-quotes that appear INSIDE a JSON string
    value (or key). The opener/closer of each string is identified by
    surrounding JSON syntax; quotes between them that don't sit
    immediately before ``,``/``:``/``}``/``]``/whitespace-then-syntax
    are treated as content and backslash-escaped.

    The walker only acts when we're inside a string. Outside a string
    every ``"`` is a structural opener. The boundary between
    "structural quote" and "content quote" is decided by what comes
    AFTER the quote — a closer is followed by syntax; a content quote
    is followed by anything else (typically alphanumerics or another
    opening quote).

    Conservative: when uncertain, treat as closer (preserves correct
    JSON unchanged). Only repairs the unambiguous slop pattern where a
    quote appears mid-word.
    """
    n = len(s)
    if n == 0:
        return s
    out: list[str] = []
    i = 0
    in_string = False
    escape_next = False
    while i < n:
        ch = s[i]
        if escape_next:
            out.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escape_next = True
            i += 1
            continue
        if ch != '"':
            if ch == "\n" and in_string:
                # Bare newline inside a string is invalid JSON; escape it
                # so the parser sees a valid \n. Models occasionally emit
                # multi-line rationales without manual escaping.
                out.append("\\n")
            else:
                out.append(ch)
            i += 1
            continue

        # ch == '"' here.
        if not in_string:
            # Opening a string.
            in_string = True
            out.append(ch)
            i += 1
            continue

        # We're inside a string and saw a quote. Decide: closer or
        # content? Look at the next non-whitespace char.
        j = i + 1
        while j < n and s[j] in " \t\r":
            j += 1
        next_ch = s[j] if j < n else ""
        # Closer if followed by JSON structural punctuation, end-of-input,
        # or another newline that itself precedes structural punctuation.
        is_closer = next_ch in (",", ":", "}", "]", "")
        if not is_closer and next_ch == "\n":
            k = j + 1
            while k < n and s[k] in " \t\r\n":
                k += 1
            next_after_nl = s[k] if k < n else ""
            is_closer = next_after_nl in (",", "}", "]", "")

        if is_closer:
            in_string = False
            out.append(ch)
        else:
            # Content quote — escape it.
            out.append("\\")
            out.append(ch)
        i += 1
    return "".join(out)


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
