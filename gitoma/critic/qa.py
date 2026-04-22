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

CRITICAL — patch shape (caught live rung-3 v4d: Defender emitted a
unified-diff string and Pydantic rejected it):

  Each element of ``revised_patches`` MUST be an OBJECT, never a string.
  The ``content`` field MUST be the COMPLETE FINAL file content — a new
  full copy of the file with your changes merged in. It is NOT a diff
  hunk, NOT a unified-diff, NOT just the changed lines.

  WRONG (will be rejected):
    "revised_patches": [
      "--- a/src/db.py\\n+++ b/src/db.py\\n@@ -1,5 +1,5 @@\\n-bad\\n+good\\n"
    ]

  WRONG (will be rejected):
    "revised_patches": [
      {"action": "modify", "path": "src/db.py",
       "content": "@@ -53,1 +53,4 @@\\n+    cur = conn.execute(...)\\n"}
    ]

  RIGHT:
    "revised_patches": [
      {
        "action": "modify",
        "path": "src/db.py",
        "content": "# full file content here, every line, including untouched ones\\nimport sqlite3\\n\\n... rest of the file with your fix merged in ..."
      }
    ]

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


def _round_trip_user(
    subtask_goal: str,
    branch_diff: str,
    current_files: dict[str, str],
    questions: list[dict],
    answers_with_flips: list[dict],
) -> str:
    """Second-pass prompt: the evidence validator caught bluffs; the
    Defender is now asked to propose CONCRETE patches that close
    those specific gaps."""
    files_block = "\n".join(
        f"\n--- {path} ---\n{content[:2500]}\n" for path, content in current_files.items()
    )
    q_by_id = {q["id"]: q["question"] for q in questions}
    flipped = [a for a in answers_with_flips
               if a["verdict"] == "gap"
               and isinstance(a.get("rationale"), str)
               and a["rationale"].startswith("[auto-flipped")]
    flipped_block = "\n".join(
        f"- {a['id']} (question was: {q_by_id.get(a['id'], '?')!r})\n"
        f"  your first answer tried to claim handled with {a.get('evidence_loc')!r}.\n"
        f"  VALIDATOR FLIP REASON: {a['rationale']}"
        for a in flipped
    )
    return f"""STATED TASK: {subtask_goal}

== THE DIFF YOU SHIPPED ==
```diff
{branch_diff[:6000]}
```

== CURRENT FILE CONTENTS ==
{files_block[:6000]}

== YOUR PREVIOUS ANSWERS WERE BUSTED BY THE VALIDATOR ==
The cross-check of your ``evidence_loc`` citations against the actual diff
and current files caught bluffs. These answers were flipped from "handled"
to "gap" because the citation does not hold up:

{flipped_block}

== YOUR JOB NOW ==
For each flipped gap, either:
  (a) Emit a MINIMAL revised patch (≤ 15 LOC added, direct fix, no
      decorative changes) that ACTUALLY closes the gap. The patch will
      be applied to the worktree and validated by a compile check +
      test run BEFORE it is committed — if your patch breaks the build
      or existing tests, it is REVERTED and we ship the original PR
      (worse for you than an honest "no fix possible").
  (b) Honestly admit you can't fix it from here — keep the answer as
      "gap" and return ``"revised_patches": []``. No patch is better
      than a broken patch.

Respond with ONLY the same JSON schema as before. The ``answers``
array should include all three slots; update the flipped ones if your
revised patch addresses them."""


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
        """Questioner uses the devil's endpoint/model by default (same
        worldview as the adversarial reviewer upstream). Overridable
        via env — if ONLY the model is set (no base_url), we keep the
        worker's URL and swap just the model."""
        override_base = os.environ.get("CRITIC_QA_QUESTIONER_BASE_URL") or self._cfg.devil_base_url
        override_model = os.environ.get("CRITIC_QA_QUESTIONER_MODEL") or self._cfg.devil_model
        # No override at all → reuse primary (same model as worker)
        if not override_base and not override_model:
            return self._primary_llm
        if self._questioner_llm is None:
            from gitoma.planner.llm_client import LLMClient
            sub_cfg = deepcopy(self._full_config)
            if override_base:
                sub_cfg.lmstudio.base_url = override_base
            if override_model:
                sub_cfg.lmstudio.model = override_model
            self._questioner_llm = LLMClient(sub_cfg)
        return self._questioner_llm

    def _llm_for_defender(self) -> "LLMClient":
        """Defender uses the worker's endpoint/model by default — same
        codebase familiarity as the model that emitted the patch."""
        override_base = os.environ.get("CRITIC_QA_DEFENDER_BASE_URL")
        override_model = os.environ.get("CRITIC_QA_DEFENDER_MODEL")
        if not override_base and not override_model:
            return self._primary_llm
        if self._defender_llm is None:
            from gitoma.planner.llm_client import LLMClient
            sub_cfg = deepcopy(self._full_config)
            if override_base:
                sub_cfg.lmstudio.base_url = override_base
            if override_model:
                sub_cfg.lmstudio.model = override_model
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

        # Round-trip: if validator flipped any "handled" → "gap" and
        # the Defender didn't already propose patches, ask it ONCE more
        # (naming the bluffed citations explicitly) to propose real
        # fixes. Caught rung-3 v2b: Defender claimed 3 handled, one
        # citation was fake, validator flipped it, but Defender's
        # original revised_patches was empty. Round-trip closes the
        # gap the validator opened.
        _apply_env = os.environ.get("CRITIC_QA_APPLY") or ""
        apply_enabled = _apply_env.lower() in ("1", "true", "yes")
        auto_flipped = [
            a for a in result.answers
            if a["verdict"] == "gap"
            and isinstance(a.get("rationale"), str)
            and a["rationale"].startswith("[auto-flipped")
        ]
        if apply_enabled and auto_flipped and not patches:
            tr.emit("critic_qa.round_trip.start",
                    flipped_count=len(auto_flipped))
            try:
                rt_raw = self._llm_for_defender().chat_json([
                    {"role": "system", "content": _DEFENDER_SYSTEM},
                    {"role": "user", "content": _round_trip_user(
                        subtask_goal, branch_diff, current_files,
                        result.questions, result.answers,
                    )},
                ])
                rt_out = LLMQADefenderOutput.model_validate(rt_raw)
                result.answers = [a.model_dump() for a in rt_out.answers]
                result.answers = [
                    _validate_evidence(a, branch_diff, current_files)
                    for a in result.answers
                ]
                patches = [p.model_dump() for p in rt_out.revised_patches]
                tr.emit("critic_qa.round_trip.end",
                        revised_proposed=len(patches))
            except (ValidationError, Exception) as exc:  # noqa: BLE001
                tr.exception("critic_qa.round_trip_failed", exc)

        # Hand revised_patches (possibly empty) back to the caller.
        # run.py owns the apply+gate loop because it has access to
        # git_repo + BuildAnalyzer + pytest.
        result.revised_patches = patches
        result.duration_ms = (time.monotonic() - t0) * 1000
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
