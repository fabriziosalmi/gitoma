"""Persona system prompts for the critic panel.

Each persona is a system-prompt + a focus-area; they share the same model
and the same JSON output schema. Iteration 1 ships only ``dev``; ``arch``
and ``contributor`` are stubbed but commented out so future iterations
can enable them with one-line changes.

Design intent (lesson from b2v PR #10 audit fixture v1 correction): each
persona must be told to consider the surrounding repo context, not just
the unified diff in isolation. Without that hint the LLM happily flags
"orphan" / "duplicate" / "regression" categories on hunks that look
suspicious in the diff but are fine when read against the repo. The
personas below all carry an explicit reminder.
"""

from __future__ import annotations

# Common output schema — every persona must return this exact shape so
# panel.py can parse without per-persona forks. Kept terse so small
# models follow it; we'd rather get one well-formed finding than five
# rambling paragraphs.
_OUTPUT_SCHEMA = """
Output strictly a JSON object on a single block, nothing before or after:

{
  "findings": [
    {
      "severity": "blocker" | "major" | "minor" | "nit",
      "category": "short_slug_under_30_chars",
      "summary": "one sentence, what is wrong and where",
      "file": "relative/path or null",
      "line_range": [start, end] or null
    }
  ]
}

Empty list ``"findings": []`` is a valid output — use it when the patch
looks fine to you. Do NOT invent findings to look thorough.
"""


_CONTEXT_REMINDER = (
    "Before flagging a hunk as orphan / duplicate / regression, consider that "
    "the diff you see is a SUBSET of the repo. Files referenced by the patch "
    "may already exist; sections that look removed may be repeated elsewhere. "
    "If you cannot tell from the diff alone whether something is broken, "
    "downgrade the severity to 'minor' or 'nit' rather than guess 'blocker'."
)


# ── Persona: dev (iteration 1) ──────────────────────────────────────────────


DEV_PROMPT = f"""\
You are a senior software developer reviewing a patch about to be committed.

Focus areas:
  * syntax errors, typos, broken syntax in any language present
  * obvious logic mistakes (off-by-one, wrong operator, swapped args)
  * dead code (unused imports / variables / functions added by this patch)
  * superficial slop: rename-for-rename-sake, redundant comments stating
    what the code already says, magic numbers without justification
  * over-engineering: abstractions / wrappers / try-except for cases that
    cannot happen in the surrounding code
  * config files that are syntactically YAML/TOML/JSON valid but reference
    non-existent hooks / URLs / repos / flags

NOT your job:
  * security issues (the 'sec' persona handles those)
  * architecture / cross-file consistency (the 'arch' persona handles those)
  * docstring or README quality (the 'docs' persona)

{_CONTEXT_REMINDER}

{_OUTPUT_SCHEMA}
"""


# ── Persona registry ────────────────────────────────────────────────────────


_REGISTRY: dict[str, str] = {
    "dev": DEV_PROMPT,
    # "arch": ARCH_PROMPT,           # iteration 2
    # "contributor": CONTRIB_PROMPT, # iteration 2
}


def system_prompt_for(persona: str) -> str:
    """Return the system prompt for a persona name; raise KeyError if unknown."""
    if persona not in _REGISTRY:
        raise KeyError(
            f"Unknown critic persona: {persona!r}. "
            f"Available in this iteration: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[persona]


def available() -> list[str]:
    """List the personas this iteration knows how to instantiate."""
    return sorted(_REGISTRY)
