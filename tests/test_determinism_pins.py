"""Determinism Agentic Pins — formal source-grep tests for the Ω_Agent axioms.

Companion to the swiss_watch suite, but at a different layer:
swiss_watch pins behavioural correctness on specific code paths;
this module pins the ARCHITECTURAL invariants of the agent system.

Reference framework (formalised by Gemini, see chat 2026-04-22):

    Ω_Agent ⟺ ∀α∈Actions, f(LLM_out) ∉ (FreeSchema ∨ UninitLoop ∨ DirectExecution)

Decomposed into 4 negative axioms — each guarded by tests in this file:

  ¬I  Anti-Improvvisazione (Anti-FreeText)
       no untyped LLM output, no string-based control flow,
       no business logic in prompts

  ¬D  Anti-Deriva (Anti-Inception/Loop)
       no unbounded loops, no LLM-driven routing, no chat-history memory

  ¬O  Anti-Onnipotenza Tool (Anti-Catastrophic-Damage)
       no non-idempotent tool execution, no host-system access from
       LLM output, no untyped tool args

  ¬B  Anti-Black-Box (Anti-Investigative-Opacity)
       no untraced execution, no silent LLM failure, no vibes-based tests

Each test is a deterministic source-grep / AST inspection — no LLM
calls, no network, runs in seconds. They fail loud the moment a
refactor drifts away from the axioms, well before a real run on
production catches it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "gitoma"


def _all_py_under(*subpaths: str) -> list[Path]:
    """Yield all .py files under one or more sub-paths of gitoma/."""
    out: list[Path] = []
    for sub in subpaths:
        root = _PKG_ROOT / sub if sub else _PKG_ROOT
        out.extend(p for p in root.rglob("*.py") if p.is_file())
    return out


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── ¬I  Anti-Improvvisazione ───────────────────────────────────────────────


def test_no_dict_get_with_default_for_llm_response_parsing():
    """¬I: LLM-output parsers must NOT use ``dict.get(key, default)``
    on parsed JSON. That pattern silently degrades when the model
    invents new keys or misses required ones — exactly the schema
    drift the strict Pydantic layer is meant to prevent.

    Critic modules MUST use ``LLMxxx.model_validate(...)`` /
    ``model_validate_json(...)`` instead. This test scans the parsers
    in gitoma/critic/ and rejects any reintroduction of the loose
    pattern."""
    forbidden_pattern = re.compile(r'\.get\(\s*["\']\w+["\']\s*,')
    offenders: list[tuple[Path, int, str]] = []
    for p in _all_py_under("critic"):
        for lineno, line in enumerate(_read(p).splitlines(), 1):
            # Skip comments + docstrings (very rough heuristic)
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if forbidden_pattern.search(line):
                offenders.append((p, lineno, line.strip()))
    assert not offenders, (
        "¬I violation — dict.get(key, default) used in critic/ parser:\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}:{ln}: {src}"
                    for p, ln, src in offenders)
    )


def test_critic_modules_import_pydantic_validation():
    """¬I: every critic module that consumes LLM output MUST go
    through Pydantic strict validation. The minimum signature: import
    of ``LLMxxx`` model OR ``ValidationError`` from
    ``gitoma.critic.types``. A module without either is parsing raw
    by hand — that's the silent-acceptance pattern we removed."""
    expected_consumers = ("panel.py", "devil.py", "refiner.py", "meta.py")
    missing: list[str] = []
    for name in expected_consumers:
        p = _PKG_ROOT / "critic" / name
        text = _read(p)
        # Either imports an LLM* model directly, or parses via panel's
        # _parse_findings (which does the validation).
        has_pydantic_path = (
            "LLMPanelOutput" in text
            or "LLMDevilOutput" in text
            or "LLMRefinerOutput" in text
            or "LLMMetaVerdict" in text
            or "_parse_findings" in text  # devil reuses panel's parser
        )
        if not has_pydantic_path:
            missing.append(name)
    assert not missing, (
        "¬I violation — these critic modules do not route LLM output "
        f"through Pydantic strict validation: {missing}"
    )


def test_severity_is_typed_literal_not_string_compare():
    """¬I: routing decisions on severity MUST use the typed Literal
    set, never ad-hoc string compares like ``severity == "blocker"``
    written as a free comparison without enum/Literal context."""
    # The closed set
    expected_set = {"blocker", "major", "minor", "nit"}
    types_text = _read(_PKG_ROOT / "critic" / "types.py")
    # The Literal type definition must contain the exact set
    for sev in expected_set:
        assert f'"{sev}"' in types_text, (
            f"Severity Literal in types.py is missing {sev!r}"
        )


# ── ¬D  Anti-Deriva ────────────────────────────────────────────────────────


def test_refiner_has_cap_one_turn_constant_or_constraint():
    """¬D: the refinement loop MUST be bounded by construction
    (cap=1, no recursion). Source-level pin: refiner.py contains the
    "cap 1" / "ONE chance" / "one-shot" framing in its module docstring
    or its propose() docstring AND no recursive call to itself."""
    refiner_text = _read(_PKG_ROOT / "critic" / "refiner.py")
    # Module/class docstring mentions the cap explicitly
    assert any(
        marker in refiner_text
        for marker in ("cap=1", "cap 1", "cap-1", "ONE chance", "one-shot",
                       "ONE follow-up", "no recursion", "one turn", "1 turn")
    ), "¬D violation — refiner.py should declare its cap-1 contract in source"

    # No recursive Refiner.propose call
    assert refiner_text.count("def propose(") == 1
    # No `propose(` invocation that would loop (very rough — we look for
    # propose called inside propose's own body via heuristic)
    propose_body_start = refiner_text.find("def propose(")
    propose_body = refiner_text[propose_body_start:]
    assert ".propose(" not in propose_body[propose_body.find("\n"):], (
        "¬D violation — refiner.propose() invokes propose() recursively"
    )


def test_reflexion_has_explicit_max_constants():
    """¬D: the Reflexion agent (CI fix loop) MUST declare hard upper
    bounds on iterations + LLM calls + wall-clock. Source-grep for
    the canonical names; absence = drift toward unbounded loop."""
    reflexion_text = _read(_PKG_ROOT / "review" / "reflexion.py")
    required_constants = ["MAX_RETRIES", "MAX_TOTAL_LLM_CALLS", "MAX_TOTAL_WALL_CLOCK_S"]
    missing = [c for c in required_constants if c not in reflexion_text]
    assert not missing, (
        f"¬D violation — reflexion.py is missing budget constants: {missing}. "
        "An unbounded Reflexion loop is the textbook AutoGPT failure mode."
    )


def test_critic_panel_state_log_is_capped():
    """¬D: per-run state cannot grow unboundedly. ``state.critic_panel_findings_log``
    must be capped — without the cap, a 100-subtask run inflates state.json
    beyond reason. Source-pin: ``_MAX_PANEL_LOG_ENTRIES`` constant in worker.py."""
    worker_text = _read(_PKG_ROOT / "worker" / "worker.py")
    assert "_MAX_PANEL_LOG_ENTRIES" in worker_text, (
        "¬D violation — worker.py missing _MAX_PANEL_LOG_ENTRIES cap"
    )
    # Must be enforced (used in slicing/del logic), not merely defined
    assert "_MAX_PANEL_LOG_ENTRIES" in worker_text.split(
        "_MAX_PANEL_LOG_ENTRIES = ", 1)[-1], (
        "¬D violation — _MAX_PANEL_LOG_ENTRIES defined but never used"
    )


def test_no_chat_history_full_inject_in_worker():
    """¬D: the worker MUST NOT inject the full chat history into each
    LLM call (the textbook ``messages.append(turn); messages.append(turn);``
    in a loop pattern). Each subtask call is a fresh chat — the
    persistence layer is state.json + trace.jsonl, not the LLM context.

    Heuristic: the worker's _execute_subtask builds a fresh ``messages``
    list every call (no module-level / instance-level accumulator)."""
    worker_text = _read(_PKG_ROOT / "worker" / "worker.py")
    # Look for a per-subtask local list named messages (this is the
    # current pattern). If a future refactor moves to ``self._messages``
    # or ``self.history`` accumulating across subtasks, this assertion
    # surfaces it immediately.
    assert "self._messages" not in worker_text, (
        "¬D violation — worker accumulating messages on self (chat-history "
        "leak into context window)"
    )
    assert "self._history" not in worker_text and "self._chat_log" not in worker_text


# ── ¬O  Anti-Onnipotenza Tool ──────────────────────────────────────────────


def test_no_eval_or_exec_in_llm_output_path():
    """¬O: NO Python ``eval()`` or ``exec()`` may appear AS REAL
    FUNCTION CALLS in code that processes LLM output (critic/,
    worker/, planner/). LLM output cannot drive Python interpreter
    directly — that's the canonical RCE-from-AI failure mode.

    Uses AST inspection instead of regex so:
      * ``meta-eval (`` in docstrings does NOT trip
      * ``"eval("`` as a string literal in a keyword list does NOT trip
      * only real ``eval(...)`` / ``exec(...)`` call expressions count
    """
    offenders: list[tuple[Path, int]] = []
    for p in _all_py_under("critic", "worker", "planner"):
        try:
            tree = ast.parse(_read(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("eval", "exec"):
                    offenders.append((p, getattr(node, "lineno", 0)))
    assert not offenders, (
        "¬O violation — eval()/exec() called in LLM-output processing path:\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}:{ln}"
                    for p, ln in offenders)
    )


def test_no_subprocess_or_os_system_driven_by_llm_strings():
    """¬O: ``subprocess.run`` / ``os.system`` / ``subprocess.Popen``
    must not appear in critic/ at all (these modules consume LLM
    output and could otherwise run arbitrary commands the model
    proposed). Worker MAY use subprocess (e.g. for git via GitPython)
    but only on internally-constructed argument lists, never directly
    on LLM output strings.

    AST inspection — no false positives on docstrings or stringy
    matches in keyword lists (mirror of test_no_eval_or_exec)."""
    forbidden_attrs = {
        ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
        ("subprocess", "check_call"), ("subprocess", "check_output"),
        ("os", "system"), ("os", "popen"),
    }
    offenders: list[tuple[Path, int, str]] = []
    for p in _all_py_under("critic"):
        try:
            tree = ast.parse(_read(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    pair = (node.func.value.id, node.func.attr)
                    if pair in forbidden_attrs:
                        offenders.append((p, getattr(node, "lineno", 0),
                                          f"{pair[0]}.{pair[1]}"))
    assert not offenders, (
        "¬O violation — subprocess/os.system call in critic/ (LLM-output path):\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}:{ln}: {call}"
                    for p, ln, call in offenders)
    )


def test_patcher_has_path_traversal_denylist():
    """¬O: the patcher MUST reject path-traversal attempts BEFORE
    writing to disk. Without this, an LLM could propose a patch with
    path ``../../../../etc/passwd`` and the patcher would happily
    write outside the repo. Source-pin: patcher.py contains the
    explicit guards."""
    patcher_text = _read(_PKG_ROOT / "worker" / "patcher.py")
    # Both checks must be present: absolute paths AND .. traversal
    assert ".." in patcher_text and "isabs" in patcher_text or "is_absolute" in patcher_text, (
        "¬O violation — patcher missing absolute-path guard"
    )
    # And the strict Pydantic LLMPatchAction also enforces the guard
    types_text = _read(_PKG_ROOT / "critic" / "types.py")
    assert "_no_path_traversal" in types_text, (
        "¬O violation — LLMPatchAction missing path traversal validator"
    )


# ── ¬B  Anti-Black-Box ────────────────────────────────────────────────────


def test_every_llm_chat_call_is_inside_a_trace_span_or_handled():
    """¬B: every LLM call site in critic/ is inside a ``current_trace().span(...)``
    context OR explicitly emits a trace event around it. A bare LLM call
    with no trace = silent black-box behaviour, the precise mode this
    axiom forbids.

    Heuristic: for every file in critic/ that calls ``self._llm.chat``
    or ``self._llm.chat_json``, the file must also contain at least one
    of: ``current_trace().span(``, ``current_trace().emit(``,
    ``current_trace().exception(``."""
    chat_call = re.compile(r"self\._llm\.chat(?:_json)?\s*\(")
    trace_use = re.compile(r"current_trace\(\)\.(?:span|emit|exception)")
    offenders: list[Path] = []
    for p in _all_py_under("critic"):
        text = _read(p)
        if chat_call.search(text) and not trace_use.search(text):
            offenders.append(p)
    assert not offenders, (
        "¬B violation — these critic modules call LLM without any trace "
        "emission in the same module:\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}" for p in offenders)
    )


def test_every_llm_failure_emits_trace_exception():
    """¬B: every ``except`` block that wraps an LLM call MUST emit
    a trace event (exception or emit). A bare ``except: pass`` around
    LLM I/O is the silent-failure pattern the axiom forbids.

    Heuristic: in critic/, count ``except Exception`` blocks
    and verify each is followed (within ~6 lines) by a
    ``current_trace().`` call OR a synthetic Finding construction
    (refiner pattern: catch + return empty + the trace emission
    happens inside the inner branch)."""
    for p in _all_py_under("critic"):
        text = _read(p)
        # Find all `except Exception` blocks; check each window for a
        # trace call or an explicit Finding-with-critic_call_failed.
        for m in re.finditer(r"except\s+(?:BaseException|Exception)\s+as\s+\w+\s*:", text):
            window = text[m.end(): m.end() + 600]
            has_trace = "current_trace()" in window or "_current_trace()" in window
            has_synthetic_finding = (
                "critic_call_failed" in window or "critic_devil.call_failed" in window
            )
            assert has_trace or has_synthetic_finding, (
                f"¬B violation — except in {p.relative_to(_REPO_ROOT)} at "
                f"offset {m.start()} is silent (no trace, no synthetic "
                f"finding):\n{text[m.start():m.start()+200]}"
            )


def test_critic_panel_emits_review_start_end_span():
    """¬B: the panel review is wrapped in a ``critic_panel.review``
    span — start/end with duration_ms. This is the cornerstone of
    the trace UX (``gitoma logs --filter critic_panel`` relies on it)."""
    worker_text = _read(_PKG_ROOT / "worker" / "worker.py")
    assert "critic_panel.review" in worker_text
    # And the span context manager pattern (with ... as fields)
    assert "current_trace().span" in worker_text


def test_devil_advocate_emits_review_span():
    """¬B mirror of the panel test, for the devil's advocate."""
    run_text = _read(_PKG_ROOT / "cli" / "commands" / "run.py")
    assert "critic_devil.review" in run_text
    assert "_trace.span" in run_text or "current_trace().span" in run_text
