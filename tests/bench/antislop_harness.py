"""Deterministic A/B bench harness for ANTISLOP injection.

Definitions
-----------
For each case ``i`` and condition ``c ∈ {off, on}``:

    output_{i,c} = LLM( prompt(subtask_i, c), T=0 )
    violations_{i,c} = sum_v weight_v · 1[regex_v matches output_{i,c}]
    Δ_i = violations_{i,off} - violations_{i,on}

ANTISLOP wins on case i iff Δ_i > 0. Aggregate metrics:

    win_rate    = mean( 1[Δ_i > 0] )
    mean_Δ      = mean( Δ_i )
    mean_v_off  = mean( violations_{i,off} )
    mean_v_on   = mean( violations_{i,on} )

The harness is mode-agnostic — it accepts a callable ``llm_fn(messages,
*, system: str) -> str`` and threads it through. Two callable shapes
ship with this module:

  * ``mock_llm_for_case(case)`` — deterministic fake that returns a
    pre-defined OFF response (slop-laden) and a pre-defined ON
    response (clean). Used by the default suite to validate the
    HARNESS itself, not the model. Stable across runs by design.
  * ``live_llm_via_lmstudio()`` — opt-in real LM Studio call at
    temperature 0. Used in ``pytest -m antislop_live`` mode.

Why deterministic
-----------------
Live LLMs at T>0 add variance that drowns out the actual ANTISLOP
signal. T=0 helps but isn't sufficient (some backends have nondet
in tokenizer / KV-cache). The mock mode lets the harness logic
itself be tested in CI without ever touching a model; the live mode
gives the real number when the operator opts in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Output shape for one case run. Compact on purpose — when 100 cases
# ship into a dataframe later, narrow fields keep the table readable.
@dataclass
class CaseResult:
    case_id: str
    condition: str  # "off" | "on"
    violations_score: int  # sum of weight_v over matched checkers
    matched_checkers: list[str] = field(default_factory=list)


@dataclass
class BenchSummary:
    n_cases: int
    win_rate: float
    mean_delta: float
    mean_violations_off: float
    mean_violations_on: float
    per_case: list[tuple[CaseResult, CaseResult]]  # (off_result, on_result)


# ── Loading + checker engine ────────────────────────────────────────────────


_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "antislop_bench"


def load_cases(path: Path | str | None = None) -> list[dict]:
    """Load one bench cases file. Default = v1 basic (cases.json)."""
    if path is None:
        path = _FIXTURE_DIR / "cases.json"
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def load_all_cases() -> list[dict]:
    """Load both basic (v1) and adversarial (v2) case sets, concatenated.

    Each case carries a ``_trap`` or its absence to distinguish — but the
    bench runner doesn't care. Used by the live-LLM bench so the operator
    sees one consolidated table covering both tiers."""
    basic = load_cases(_FIXTURE_DIR / "cases.json")
    adversarial = load_cases(_FIXTURE_DIR / "cases_v2_adversarial.json")
    return basic + adversarial


def detect_violations(output: str, checkers: list[dict]) -> tuple[int, list[str]]:
    """Apply each checker's regex against ``output``. Returns (score, ids).

    Score is the sum of weights of matched checkers; ids is the list of
    checker IDs that matched. NEVER calls an LLM — pure regex match.
    """
    score = 0
    matched: list[str] = []
    for ch in checkers:
        if re.search(ch["regex"], output, flags=re.MULTILINE):
            score += int(ch.get("weight", 1))
            matched.append(ch["id"])
    return score, matched


# ── Prompt builder shared between modes ────────────────────────────────────


def build_messages(
    *, case: dict, antislop_block: str = "",
) -> list[dict]:
    """Build the LLM-shaped message list for one case run.

    The system prompt is intentionally minimal — the bench measures how
    much ANTISLOP injection alone shifts the output. Adding a verbose
    worker_system_prompt would dilute the signal."""
    system_parts = [
        "You are a senior software engineer. Reply with ONLY the file content "
        "for the requested change — no markdown fences, no commentary, just the code.",
    ]
    if antislop_block:
        system_parts.append(antislop_block)

    files_block = ""
    if case.get("current_files"):
        files_block = "\n\nCurrent files in the repo:\n" + "\n\n".join(
            f"=== {p} ===\n{c}" for p, c in case["current_files"].items()
        )

    user = (
        f"Subtask: {case['subtask']['title']}\n\n"
        f"Description: {case['subtask']['description']}\n\n"
        f"Target file: {case['subtask']['file_hints'][0]}"
        f"{files_block}"
    )

    return [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": user},
    ]


# ── Mock LLM (deterministic, used in default suite) ─────────────────────────


# Two output banks per case: one slopful (returned in OFF condition), one
# clean (returned in ON condition). The harness doesn't see these labels —
# it sees only the function signature, exactly like a real LLM.
_MOCK_OUTPUTS: dict[str, dict[str, str]] = {
    "py_secret_in_diff": {
        "off": (
            "STAGING_API_TOKEN = 'tok_AbC123xyz789'\n"
            "STAGING_DB_URL = 'postgres://user:pwd@db.staging:5432/main'\n"
            "def load_config():\n"
            "    try:\n"
            "        return {'token': STAGING_API_TOKEN, 'db': STAGING_DB_URL}\n"
            "    except Exception:\n"
            "        pass\n"
        ),
        "on": (
            "import os\n"
            "def load_config():\n"
            "    return {'token': os.environ['STAGING_API_TOKEN'],\n"
            "            'db': os.environ['STAGING_DB_URL']}\n"
        ),
    },
    "js_jquery_dom_react_repo": {
        "off": (
            "import $ from 'jquery';\n"
            "import { useState } from 'react';\n"
            "export function TodoList() {\n"
            "  const [items, setItems] = useState([]);\n"
            "  document.getElementById('new-todo-btn').addEventListener('click', () => setItems([...items, 'x']));\n"
            "  return <div id=\"todos\"><button id=\"new-todo-btn\">Add</button></div>;\n"
            "}\n"
        ),
        "on": (
            "import { useState } from 'react';\n"
            "export function TodoList() {\n"
            "  const [items, setItems] = useState([]);\n"
            "  return <div><button onClick={() => setItems([...items, 'x'])}>Add</button></div>;\n"
            "}\n"
        ),
    },
    "html_no_alt_text": {
        "off": (
            "<!DOCTYPE html>\n<html><body>\n"
            "  <section class=\"hero\"><img src=\"hero.png\" /></section>\n"
            "</body></html>\n"
        ),
        "on": (
            "<!DOCTYPE html>\n<html><body>\n"
            "  <section class=\"hero\"><img src=\"hero.png\" alt=\"Product hero shot\" /></section>\n"
            "</body></html>\n"
        ),
    },
    "py_magic_number": {
        "off": (
            "import functools\n"
            "@functools.lru_cache(maxsize=128)\n"
            "def get_user(uid: str) -> dict:\n"
            "    # cache for 24h\n"
            "    if _cache_age(uid) > 86400:\n"
            "        return _fetch(uid)\n"
            "    return _cache.get(uid)\n"
        ),
        "on": (
            "import functools\n"
            "SECONDS_IN_DAY = 24 * 60 * 60\n"
            "@functools.lru_cache(maxsize=128)\n"
            "def get_user(uid: str) -> dict:\n"
            "    if _cache_age(uid) > SECONDS_IN_DAY:\n"
            "        return _fetch(uid)\n"
            "    return _cache.get(uid)\n"
        ),
    },
    "rust_unwrap_in_lib": {
        "off": (
            "use std::env;\nuse std::path::PathBuf;\n"
            "pub fn main_path() -> PathBuf {\n"
            "    PathBuf::from(env::args().nth(1).unwrap())\n"
            "}\n"
        ),
        "on": (
            "use std::env;\nuse std::path::PathBuf;\n"
            "pub fn main_path() -> Option<PathBuf> {\n"
            "    env::args().nth(1).map(PathBuf::from)\n"
            "}\n"
        ),
    },
}


def mock_llm_for_case(case: dict) -> Callable[[list[dict], dict], str]:
    """Returns a callable that mimics ``llm.chat`` for one specific case.

    The callable inspects the system message: if it contains the ANTISLOP
    sentinel header, returns the ``on`` output; otherwise the ``off``
    output. Same logic the bench would observe with a real well-prompted
    model — but deterministically."""
    bank = _MOCK_OUTPUTS.get(case["id"])
    if bank is None:
        raise KeyError(f"No mock output bank for case {case['id']!r}")

    def _llm(messages: list[dict], **_kwargs) -> str:
        sys_text = next(
            (m["content"] for m in messages if m["role"] == "system"),
            "",
        )
        cond = "on" if "CRITICAL anti-patterns to AVOID" in sys_text else "off"
        return bank[cond]

    return _llm


# ── Bench runner ────────────────────────────────────────────────────────────


def run_bench(
    *, cases: list[dict], llm_fn_factory: Callable[[dict], Callable],
    antislop_classifier: Callable | None = None,
) -> BenchSummary:
    """Run all cases under both conditions, return a summary.

    ``llm_fn_factory(case) -> callable`` builds a per-case LLM function.
    For mock mode, it's ``mock_llm_for_case``; for live mode, it returns
    a closure over a single LLMClient.

    ``antislop_classifier`` is a function ``(case) -> str`` that returns
    the system-prompt block for the ON condition. Defaults to the real
    gitoma classifier so the bench measures the real injection logic."""
    if antislop_classifier is None:
        antislop_classifier = _default_antislop_block

    per_case: list[tuple[CaseResult, CaseResult]] = []
    deltas: list[int] = []

    for case in cases:
        llm_fn = llm_fn_factory(case)

        # OFF run
        msgs_off = build_messages(case=case, antislop_block="")
        out_off = llm_fn(msgs_off)
        score_off, matched_off = detect_violations(out_off, case["violation_checkers"])
        r_off = CaseResult(case_id=case["id"], condition="off",
                           violations_score=score_off, matched_checkers=matched_off)

        # ON run
        antislop_block = antislop_classifier(case)
        msgs_on = build_messages(case=case, antislop_block=antislop_block)
        out_on = llm_fn(msgs_on)
        score_on, matched_on = detect_violations(out_on, case["violation_checkers"])
        r_on = CaseResult(case_id=case["id"], condition="on",
                          violations_score=score_on, matched_checkers=matched_on)

        per_case.append((r_off, r_on))
        deltas.append(score_off - score_on)

    n = max(len(per_case), 1)
    return BenchSummary(
        n_cases=len(per_case),
        win_rate=sum(1 for d in deltas if d > 0) / n,
        mean_delta=sum(deltas) / n,
        mean_violations_off=sum(r.violations_score for r, _ in per_case) / n,
        mean_violations_on=sum(r.violations_score for _, r in per_case) / n,
        per_case=per_case,
    )


def _default_antislop_block(case: dict) -> str:
    """Run the real gitoma antislop classifier against the case's
    subtask context. This is what `worker.py` does at runtime — bench
    measuring the real path, not a fake.

    Honours ``ANTISLOP_FORMAT=flat|axioms`` env (mirror of the worker)
    so the bench can A/B between iter-5 flat and iter-6 axiom format.
    """
    import os
    from gitoma.critic.antislop import classify_for_subtask, format_for_injection, load_rules

    rules = load_rules()
    if not rules:
        # No ANTISLOP.md available in this environment — bench reports
        # ON ≡ OFF, win_rate=0, mean_delta=0. Caller can tell the
        # difference from a meaningful run.
        return ""
    selected = classify_for_subtask(
        rules=rules,
        file_hints=case["subtask"].get("file_hints", []),
        languages=case.get("languages", []),
        action_hint=case["subtask"].get("action", ""),
        top_n=10,
    )
    fmt = os.getenv("ANTISLOP_FORMAT", "flat").strip().lower()
    if fmt not in ("flat", "axioms"):
        fmt = "flat"
    return format_for_injection(selected, mode=fmt)
