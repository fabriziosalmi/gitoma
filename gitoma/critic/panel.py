"""Critic panel orchestrator — iteration 1 (walking skeleton).

Wires N personas (currently only ``dev``) against a single subtask diff,
collects findings, returns a structured ``PanelResult``. Intentionally
minimal: no devil's advocate, no refinement, no meta-eval. Those land
in iteration 2/3 once we have baseline numbers from advisory mode.

The orchestrator deliberately catches and logs LLM/parse failures
instead of propagating: a critic that crashes the worker is worse
than no critic at all. A failed persona becomes a single 'minor'
finding describing the failure, so we still get the audit trail.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from gitoma.core.trace import current as current_trace
from gitoma.critic.personas import system_prompt_for
from gitoma.critic.types import Finding, PanelResult

if TYPE_CHECKING:
    from gitoma.core.config import CriticPanelConfig
    from gitoma.planner.llm_client import LLMClient

# When ``CRITIC_PANEL_DEBUG_RAW=1`` (env), the orchestrator emits a
# ``critic_panel.persona_raw`` trace event carrying the first 1.5 KB of
# the LLM's raw response, so we can see WHY the parser is finding nothing
# (prosa-only output? schema drift? empty findings list?). Off in prod.
_DEBUG_RAW = os.getenv("CRITIC_PANEL_DEBUG_RAW", "").strip().lower() in ("1", "true", "yes", "on")


# Best-effort JSON extractor — small models often wrap output in
# ``` fences or sandwich JSON between explanatory prose. We grab the
# first balanced ``{...}`` block and try to parse that. If parsing
# fails we surface the raw text so the trace can show what came back.
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


class CriticPanel:
    """Run one or more personas against a diff and return their findings.

    Parameters
    ----------
    config : CriticPanelConfig
        Holds ``mode``, ``personas``, ``temperature``. The orchestrator
        respects ``mode != "off"`` (caller is responsible for skipping
        the panel call entirely when mode is off; we still defend with
        a no-op return here for safety).
    llm : LLMClient
        Already-initialised LLM client. We piggyback on the worker's
        client to avoid double-loading the model on hot paths.
    """

    def __init__(self, config: "CriticPanelConfig", llm: "LLMClient") -> None:
        self._config = config
        self._llm = llm

    def review(
        self,
        *,
        subtask_id: str,
        diff_text: str,
        repo_files_summary: str = "",
    ) -> PanelResult:
        """Run the configured personas against ``diff_text`` and aggregate findings.

        ``repo_files_summary`` is an optional short context (e.g. ``ls``-like
        listing of touched directories) — gives personas the file-system view
        the audit-correction lesson said they need. Empty string is fine for
        the walking skeleton; we'll wire a real summary in step 3.
        """
        # Defensive no-op: if we got called with mode=off, return empty.
        # The caller in worker.py also gates this — belt + suspenders.
        if self._config.mode == "off":
            return PanelResult(subtask_id=subtask_id, verdict="no_op")
        if not diff_text.strip():
            # Empty diff — nothing to critique. Happens for subtasks that
            # only touched whitespace or that the patcher de-duped.
            return PanelResult(subtask_id=subtask_id, verdict="no_op")

        personas = [p.strip() for p in self._config.personas.split(",") if p.strip()]
        all_findings: list[Finding] = []
        called: list[str] = []
        # Token totals across all persona calls; if any single call doesn't
        # report usage we surface None so the caller knows the number is
        # incomplete rather than zero.
        prompt_total = 0
        completion_total = 0
        any_usage_seen = False

        for persona in personas:
            called.append(persona)
            try:
                findings, usage = self._call_one_persona(persona, diff_text, repo_files_summary)
                all_findings.extend(findings)
                if usage is not None:
                    prompt_total += usage[0]
                    completion_total += usage[1]
                    any_usage_seen = True
            except Exception as exc:  # noqa: BLE001 — see module docstring
                all_findings.append(
                    Finding(
                        persona=persona,
                        severity="minor",
                        category="critic_call_failed",
                        summary=f"Persona {persona!r} failed: {type(exc).__name__}: {exc}",
                    )
                )

        return PanelResult(
            subtask_id=subtask_id,
            verdict="advisory_logged",
            personas_called=called,
            findings=all_findings,
            tokens_extra=(prompt_total, completion_total) if any_usage_seen else None,
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _call_one_persona(
        self,
        persona: str,
        diff_text: str,
        repo_files_summary: str,
    ) -> tuple[list[Finding], tuple[int, int] | None]:
        """Single LLM call for one persona; returns its findings + usage."""
        system = system_prompt_for(persona)
        user_parts: list[str] = []
        if repo_files_summary:
            user_parts.append("Repository file context (touched dirs):\n" + repo_files_summary)
        user_parts.append(
            "Unified diff to review (newly applied, NOT yet committed):\n"
            "```diff\n" + diff_text.rstrip() + "\n```"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
        # ``temperature`` override: LLMClient.chat() may or may not accept
        # a per-call temperature; if it doesn't, we fall back to the
        # client's default — slightly higher than ideal for review but
        # not a correctness issue.
        try:
            raw = self._llm.chat(messages, temperature=self._config.temperature)
        except TypeError:
            # Older signature without temperature kwarg — safe fallback.
            raw = self._llm.chat(messages)
        usage = getattr(self._llm, "_last_usage", None)
        # Optional debug emission — gives us a window into WHY the parser
        # might be returning nothing on real model output. Truncated so
        # the JSONL line stays readable.
        if _DEBUG_RAW:
            try:
                current_trace().emit(
                    "critic_panel.persona_raw",
                    persona=persona,
                    raw_head=str(raw or "")[:1500],
                    raw_len=len(raw or ""),
                )
            except Exception:
                pass  # debug emission must never break the panel
        # Parse with the best-effort JSON extractor.
        findings = _parse_findings(raw, persona=persona)
        return findings, usage


def _parse_findings(raw: str, *, persona: str) -> list[Finding]:
    """Best-effort parse of the persona's raw output into Finding objects.

    Returns an empty list when the output cannot be parsed — but always
    returns a list, never raises. Parse failures bubble up via a synthetic
    ``critic_call_failed`` finding in the orchestrator above (this function
    intentionally does NOT generate one because it's also used in tests
    where empty-on-malformed is the cleaner contract).
    """
    if not raw:
        return []
    match = _JSON_BLOCK.search(raw)
    if not match:
        return []
    try:
        parsed: Any = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        return []
    out: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        try:
            severity = item.get("severity") or "minor"
            if severity not in ("blocker", "major", "minor", "nit"):
                severity = "minor"  # tolerate tiny model schema drift
            line_range_raw = item.get("line_range")
            line_range: tuple[int, int] | None = None
            if isinstance(line_range_raw, list) and len(line_range_raw) == 2:
                try:
                    line_range = (int(line_range_raw[0]), int(line_range_raw[1]))
                except (TypeError, ValueError):
                    line_range = None
            out.append(
                Finding(
                    persona=persona,
                    severity=severity,  # type: ignore[arg-type]
                    category=str(item.get("category") or "uncategorised")[:64],
                    summary=str(item.get("summary") or "")[:500],
                    file=item.get("file") if isinstance(item.get("file"), str) else None,
                    line_range=line_range,
                )
            )
        except Exception:  # noqa: BLE001 — schema drift is expected
            # Skip the malformed item, keep the well-formed ones.
            continue
    return out
