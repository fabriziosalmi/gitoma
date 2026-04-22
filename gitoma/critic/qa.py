"""Q&A phase — post-meta adversarial self-consistency check.

Runs AFTER the meta-eval has chosen a winner (v0 or v1). Two models
interrogate the fix across three Pareto slots:

  Q1_evidence — Questioner asks: "cite the file:line that proves the
                fix exists"; Defender must quote 1-3 lines or concede
                ``gap``. Catches rung-3's failure mode: devil claimed
                a security fix that was never in the diff.
  Q2_edge     — "name one concrete input that breaks the patch";
                Defender replies with either a test-covered edge or
                emits a revised patch.
  Q3_scope    — "name one change in the diff NOT required by the
                task"; Defender cites path:line of the over-scope or
                removes it in a revised patch.

Two-model architecture is the core discipline: same model on both
sides = correlated biases confirm each other. Different families
(default: devil endpoint for Questioner, worker endpoint for
Defender) = independent observation. Config:

  CRITIC_QA_ENABLED=true        enable the phase (off by default)
  CRITIC_QA_APPLY=true          enable revised-patch application
                                (off by default — observation-only
                                first, then promote once we see the
                                Questioner catches real failures)
  CRITIC_QA_QUESTIONER_MODEL    override, defaults to devil_model
  CRITIC_QA_QUESTIONER_BASE_URL override, defaults to devil_base_url
  CRITIC_QA_DEFENDER_MODEL      override, defaults to worker model

Five-gate discipline on the Defender (see memory
``project_idea_qa_self_critique``): strict schema, evidence-required
verdicts, LOC cap on revisions, deterministic post-check (build +
existing tests), anti-sycophancy system prompt.
"""

from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

from gitoma.core.trace import current as current_trace
from gitoma.critic.types import (
    LLMQADefenderOutput,
    LLMQAQuestionerOutput,
    QAResult,
    ValidationError,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config, CriticPanelConfig
    from gitoma.planner.llm_client import LLMClient


# ── Prompts ──────────────────────────────────────────────────────────────────

_QUESTIONER_SYSTEM = """You are a senior engineer with 15+ years of production scars.
Your job is to interrogate a code change with THREE brutal, scenario-specific questions.

You DO NOT write prose. You DO NOT praise. You DO NOT hedge.
You ask the SHORTEST possible question that forces the defender to either produce
evidence or concede a gap.

Rules:
  1. Every question must name a SPECIFIC file / line / input / scenario.
     No "could there be edge cases?" — that's soft. Do say "if id=-1 is passed,
     does Greet return 'Hello, stranger!' or panic? Cite the line."
  2. Assume uncharitable interpretation — if the defender didn't prove X, X is missing.
  3. Use the THREE FIXED SLOTS. Do not invent new slot IDs:
     Q1_evidence — demand a file:line proof that the fix EXISTS
     Q2_edge     — name ONE concrete input that breaks the patch
     Q3_scope    — point at ONE change in the diff that was NOT required
  4. Respond with ONLY the JSON object below. No markdown, no code fences, no prose.

Schema:
{
  "questions": [
    {"id": "Q1_evidence", "question": "<brutal, specific, demands evidence>"},
    {"id": "Q2_edge",     "question": "<names a concrete input>"},
    {"id": "Q3_scope",    "question": "<points at specific over-scope>"}
  ]
}"""


_DEFENDER_SYSTEM = """You are the engineer who wrote this patch. A senior reviewer
just asked three brutal questions. You respond SHORT and EVIDENCE-BASED.

IMPORTANT: the reviewer's questions are THEIR opinion. You DO NOT have to act on
all of them — only on the ones that surface a REAL gap you can't defend with
evidence. A bad question deserves a one-sentence rebuttal and `verdict: "handled"`
with a citation, not a rewrite. Pleasing the reviewer is NOT the goal.

Rules (non-negotiable):
  1. For each question, verdict is one of:
       "handled"   — the fix already covers it; MUST cite a path:line that exists
                     in the diff or the current file contents. One sentence
                     rationale is enough.
       "gap"       — the fix does NOT cover it AND the gap is real; may emit a
                     revised patch IF the gap is worth fixing.
       "uncertain" — you can't tell from the diff; NO revised patch.
  2. Do NOT concede just because the reviewer asked. A question without a real
     gap gets "handled" + the citation that proves it.
  3. Do NOT add defensive code that no question concretely motivates. Dead
     validation on "could happen" scenarios = NOT a valid gap.
  4. A patch is worse than no patch if it's not necessary. Prefer
     ``revised_patches: []`` when the original fix is already correct. Three
     "handled" answers + empty revised_patches is the EXPECTED shape for a
     good fix — it's not a failure, it's the right answer.
  5. If you DO emit a revised patch, it must be MINIMAL (≤ 15 LOC added) and
     must directly close a gap that the evidence actually supports.
  6. Respond with ONLY the JSON object below. No markdown, no prose.

Schema:
{
  "answers": [
    {"id": "Q1_evidence", "verdict": "handled", "evidence_loc": "src/db.py:55",
     "rationale": "<= 1 sentence"},
    {"id": "Q2_edge", "verdict": "gap", "evidence_loc": null,
     "rationale": "<= 1 sentence"},
    {"id": "Q3_scope", "verdict": "handled", "evidence_loc": null,
     "rationale": "<= 1 sentence"}
  ],
  "revised_patches": []
}"""


def _questioner_user(
    subtask_goal: str,
    branch_diff: str,
    current_files: dict[str, str],
) -> str:
    files_block = "\n".join(
        f"\n--- {path} ---\n{content[:2500]}\n" for path, content in current_files.items()
    )
    return f"""STATED TASK: {subtask_goal}

== BRANCH DIFF ==
```diff
{branch_diff[:6000]}
```

== CURRENT FILE CONTENTS (what the repo looks like now) ==
{files_block[:6000]}

Emit three brutal, specific questions per the fixed slot schema."""


def _defender_user(
    subtask_goal: str,
    branch_diff: str,
    current_files: dict[str, str],
    questions: list[dict],
) -> str:
    files_block = "\n".join(
        f"\n--- {path} ---\n{content[:2500]}\n" for path, content in current_files.items()
    )
    q_block = "\n".join(f"{q['id']}: {q['question']}" for q in questions)
    return f"""STATED TASK: {subtask_goal}

== THE DIFF YOU SHIPPED ==
```diff
{branch_diff[:6000]}
```

== CURRENT FILE CONTENTS ==
{files_block[:6000]}

== THE REVIEWER'S THREE QUESTIONS ==
{q_block}

Answer each. Cite path:line when verdict is "handled". Only propose a revised
patch if at least one answer is "gap" AND the patch genuinely closes the gap."""


# ── Agent ───────────────────────────────────────────────────────────────────


class QAAgent:
    """Orchestrates Questioner + Defender across the three Pareto slots."""

    def __init__(
        self,
        critic_config: "CriticPanelConfig",
        primary_llm: "LLMClient",
        full_config: "Config",
    ) -> None:
        self._cfg = critic_config
        self._primary_llm = primary_llm
        self._full_config = full_config
        self._questioner_llm: "LLMClient | None" = None
        self._defender_llm: "LLMClient | None" = None

    # ── Client selection ────────────────────────────────────────────────

    def _llm_for_questioner(self) -> "LLMClient":
        """Questioner uses the devil's endpoint by default (same worldview
        as the adversarial reviewer upstream), overridable via env."""
        base_url = os.environ.get("CRITIC_QA_QUESTIONER_BASE_URL") or self._cfg.devil_base_url
        model = os.environ.get("CRITIC_QA_QUESTIONER_MODEL") or self._cfg.devil_model or self._full_config.lmstudio.model
        if not base_url:
            # Same endpoint as worker — different model via chat() kwarg only.
            if self._questioner_llm is None:
                self._questioner_llm = self._primary_llm
            return self._questioner_llm
        if self._questioner_llm is None:
            from gitoma.planner.llm_client import LLMClient
            sub_cfg = deepcopy(self._full_config)
            sub_cfg.lmstudio.base_url = base_url
            sub_cfg.lmstudio.model = model
            self._questioner_llm = LLMClient(sub_cfg)
        return self._questioner_llm

    def _llm_for_defender(self) -> "LLMClient":
        """Defender uses the worker's endpoint by default — same codebase
        familiarity as the model that emitted the patch."""
        base_url = os.environ.get("CRITIC_QA_DEFENDER_BASE_URL") or self._full_config.lmstudio.base_url
        model = os.environ.get("CRITIC_QA_DEFENDER_MODEL") or self._full_config.lmstudio.model
        if base_url == self._full_config.lmstudio.base_url and model == self._full_config.lmstudio.model:
            return self._primary_llm
        if self._defender_llm is None:
            from gitoma.planner.llm_client import LLMClient
            sub_cfg = deepcopy(self._full_config)
            sub_cfg.lmstudio.base_url = base_url
            sub_cfg.lmstudio.model = model
            self._defender_llm = LLMClient(sub_cfg)
        return self._defender_llm

    # ── Public API ──────────────────────────────────────────────────────

    def review(
        self,
        *,
        subtask_goal: str,
        branch_diff: str,
        current_files: dict[str, str],
    ) -> QAResult:
        """Run the full Q&A phase. Never raises — defensive fail-soft
        keeps the PR flow working even if Q&A itself crashes."""
        t0 = time.monotonic()
        tr = current_trace()

        q_model = os.environ.get("CRITIC_QA_QUESTIONER_MODEL") or self._cfg.devil_model or ""
        d_model = os.environ.get("CRITIC_QA_DEFENDER_MODEL") or self._full_config.lmstudio.model

        result = QAResult(
            ran=True, questioner_model=q_model, defender_model=d_model,
        )

        # ── Step 1: Questioner ──────────────────────────────────────────
        try:
            q_raw = self._llm_for_questioner().chat_json([
                {"role": "system", "content": _QUESTIONER_SYSTEM},
                {"role": "user", "content": _questioner_user(
                    subtask_goal, branch_diff, current_files,
                )},
            ])
            q_out = LLMQAQuestionerOutput.model_validate(q_raw)
            result.questions = [q.model_dump() for q in q_out.questions]
            tr.emit("critic_qa.questions", count=len(result.questions))
        except (ValidationError, Exception) as exc:  # noqa: BLE001
            tr.exception("critic_qa.questioner_failed", exc)
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        # ── Step 2: Defender ────────────────────────────────────────────
        try:
            d_raw = self._llm_for_defender().chat_json([
                {"role": "system", "content": _DEFENDER_SYSTEM},
                {"role": "user", "content": _defender_user(
                    subtask_goal, branch_diff, current_files, result.questions,
                )},
            ])
            d_out = LLMQADefenderOutput.model_validate(d_raw)
            result.answers = [a.model_dump() for a in d_out.answers]
            patches = [p.model_dump() for p in d_out.revised_patches]
            tr.emit(
                "critic_qa.answers",
                gap=sum(1 for a in result.answers if a["verdict"] == "gap"),
                handled=sum(1 for a in result.answers if a["verdict"] == "handled"),
                uncertain=sum(1 for a in result.answers if a["verdict"] == "uncertain"),
                revised_proposed=len(patches),
            )
        except (ValidationError, Exception) as exc:  # noqa: BLE001
            tr.exception("critic_qa.defender_failed", exc)
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        # ── Gate 2: evidence-required for "handled" verdicts ─────────────
        # Programmatic check — if the LLM said "handled" with an
        # ``evidence_loc`` that doesn't actually exist in the diff, we
        # flip the verdict to "gap". Prevents wishful-thinking citations.
        result.answers = [
            _validate_evidence(a, branch_diff, current_files) for a in result.answers
        ]

        result.duration_ms = (time.monotonic() - t0) * 1000

        # Application of revised patches is a separate gated step —
        # callers decide via CRITIC_QA_APPLY to enter the apply flow.
        # This keeps the Q&A phase observation-only by default; once
        # the Questioner is proven to catch real failures, the apply
        # path is promoted.
        _apply_env = os.environ.get("CRITIC_QA_APPLY") or ""
        if patches and _apply_env.lower() in ("1", "true", "yes"):
            # The caller (run.py) is better positioned to run the
            # BuildAnalyzer + tests post-apply, so we hand the patches
            # back in a structured shape. QAResult already carries them
            # via the revised_patches slot when we store them.
            # For now: emit a trace event so the operator sees the
            # phase proposed patches (even if not applied).
            tr.emit("critic_qa.revised_proposed_but_apply_disabled",
                    count=len(patches))
        return result


# ── Helpers ─────────────────────────────────────────────────────────────────


_LOC_RE = re.compile(r"^([\w./\\-]+\.[A-Za-z0-9]+):(\d+)(?::\d+)?$")


def _validate_evidence(
    answer: dict, branch_diff: str, current_files: dict[str, str],
) -> dict:
    """Flip ``handled`` → ``gap`` when ``evidence_loc`` is a bluff.

    The citation must (a) parse as ``path:line``, (b) name a file that
    exists in ``current_files``, and (c) point at a line that is
    actually present (not beyond file length). Missing any of those =
    the defender's "handled" claim is unverifiable, so it's a gap.
    """
    if answer.get("verdict") != "handled":
        return answer
    loc = (answer.get("evidence_loc") or "").strip()
    if not loc:
        return _flip_to_gap(answer, "handled-without-evidence")
    m = _LOC_RE.match(loc)
    if not m:
        return _flip_to_gap(answer, f"unparseable-loc:{loc!r}")
    path, line_s = m.group(1), m.group(2)
    # Normalise leading "./" or "a/"/"b/" (diff-style prefixes)
    path = path.lstrip("./").removeprefix("a/").removeprefix("b/")
    content = current_files.get(path)
    if content is None:
        # Try a suffix match — the model may have cited a partial path
        for fp in current_files:
            if fp.endswith(path) or path.endswith(fp):
                content = current_files[fp]
                break
    if content is None:
        return _flip_to_gap(answer, f"file-not-in-diff:{path}")
    try:
        line = int(line_s)
    except ValueError:
        return _flip_to_gap(answer, f"bad-line-number:{line_s}")
    n_lines = content.count("\n") + 1
    if line < 1 or line > n_lines:
        return _flip_to_gap(answer, f"line-out-of-range:{line}/{n_lines}")
    return answer


def _flip_to_gap(answer: dict, why: str) -> dict:
    flipped = dict(answer)
    flipped["verdict"] = "gap"
    original = answer.get("rationale") or ""
    flipped["rationale"] = f"[auto-flipped from handled: {why}] {original}"[:400]
    return flipped
