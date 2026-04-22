"""Tests for the ANTISLOP A/B bench harness.

Two layers of test:

  * **Default suite** (always runs): exercises the harness logic with
    a deterministic mock LLM. Validates that the harness itself
    measures correctly — not the model. Mock returns slop in OFF
    condition and clean in ON condition for every case, so a working
    harness MUST report win_rate=1.0 and mean_delta>0. If it doesn't,
    the harness has a bug.

  * **Live suite** (opt-in via ``-m antislop_live``): runs against a
    real LM Studio endpoint at temperature 0. Reports the actual
    A/B numbers on the user's hardware. Pass with ``LM_STUDIO_BASE_URL``
    + ``LM_STUDIO_MODEL`` set to a known-good model.

The harness is the regression target here — once we trust it, the
real A/B numbers it reports against any (model, prompt) tuple become
the bench-of-truth for ANTISLOP claims.
"""

from __future__ import annotations

import os
from unittest.mock import patch as patchmock

import pytest

from tests.bench.antislop_harness import (
    BenchSummary,
    build_messages,
    detect_violations,
    load_all_cases,
    load_cases,
    mock_llm_for_case,
    run_bench,
)


# ── Default suite — harness validation with deterministic mock ──────────────


def test_load_cases_returns_nonempty_list():
    """Smoke: the fixture exists and parses. Empty list = silent
    catastrophe (bench would always report win_rate=0)."""
    cases = load_cases()
    assert len(cases) >= 5, f"Expected >=5 bench cases, got {len(cases)}"
    for case in cases:
        assert "id" in case
        assert "subtask" in case
        assert "violation_checkers" in case
        assert len(case["violation_checkers"]) >= 1


def test_detect_violations_scores_zero_on_clean_output():
    """A clean output that triggers no checker scores 0."""
    cases = load_cases()
    case = next(c for c in cases if c["id"] == "py_secret_in_diff")
    score, matched = detect_violations(
        "import os\nTOKEN = os.environ['STAGING_API_TOKEN']\n",
        case["violation_checkers"],
    )
    assert score == 0
    assert matched == []


def test_detect_violations_aggregates_weight_across_matches():
    """Output that triggers multiple checkers gets the sum of weights."""
    cases = load_cases()
    case = next(c for c in cases if c["id"] == "py_secret_in_diff")
    bad = "STAGING_API_TOKEN = 'tok_abc12345'\nexcept Exception:\n    pass\n"
    score, matched = detect_violations(bad, case["violation_checkers"])
    assert score >= 5  # 3 (hardcoded) + 2 (bare except)
    assert "V_HARDCODED_TOKEN" in matched
    assert "V_BARE_EXCEPT" in matched


def test_build_messages_includes_antislop_block_in_system_when_provided():
    """The OFF run must NOT have the ANTISLOP block; the ON run must."""
    cases = load_cases()
    case = cases[0]
    msgs_off = build_messages(case=case, antislop_block="")
    msgs_on = build_messages(case=case, antislop_block="CRITICAL anti-patterns to AVOID...")
    assert "CRITICAL" not in msgs_off[0]["content"]
    assert "CRITICAL" in msgs_on[0]["content"]


def test_mock_llm_returns_off_output_without_antislop_in_system():
    """The mock LLM detects the absence of the ANTISLOP sentinel and
    returns the slop-laden response. This validates that the bench
    harness will produce a measurable delta when the classifier is
    wired up correctly."""
    cases = load_cases()
    case = next(c for c in cases if c["id"] == "py_secret_in_diff")
    llm = mock_llm_for_case(case)
    out = llm([{"role": "system", "content": "plain system"}, {"role": "user", "content": "x"}])
    assert "STAGING_API_TOKEN = 'tok_" in out  # slop present


def test_mock_llm_returns_on_output_with_antislop_in_system():
    """Mirror: when the system contains the ANTISLOP sentinel header,
    the mock returns the clean output."""
    cases = load_cases()
    case = next(c for c in cases if c["id"] == "py_secret_in_diff")
    llm = mock_llm_for_case(case)
    out = llm([
        {"role": "system", "content": "CRITICAL anti-patterns to AVOID — ..."},
        {"role": "user", "content": "x"},
    ])
    assert "os.environ['STAGING_API_TOKEN']" in out  # clean
    assert "STAGING_API_TOKEN = 'tok_" not in out


def test_run_bench_with_mock_reports_win_rate_one_zero():
    """With a deterministic mock that returns slop in OFF and clean in
    ON, the harness MUST report win_rate=1.0 across every case. Anything
    less means the harness has a bug — most likely in the classifier
    not producing the ANTISLOP sentinel header for some case (which the
    mock then misses, returning slop in ON too)."""
    cases = load_cases()
    summary: BenchSummary = run_bench(
        cases=cases,
        llm_fn_factory=mock_llm_for_case,
    )
    assert summary.n_cases == len(cases)
    # Every case must be a win — mock guarantees clean output when
    # ANTISLOP is injected.
    assert summary.win_rate == 1.0, (
        f"Expected win_rate=1.0 with deterministic mock, got {summary.win_rate}. "
        f"Suspected: classifier not selecting any rules for some case "
        f"(empty antislop_block → mock returns OFF output in both runs)."
    )
    assert summary.mean_delta > 0
    assert summary.mean_violations_off > 0
    assert summary.mean_violations_on == 0  # mock returns clean in ON


def test_run_bench_with_mock_no_classifier_reports_zero_delta():
    """Sanity: if the classifier returns empty for every case (e.g.
    no ANTISLOP.md file present), the bench reports mean_delta=0
    and win_rate=0 — distinguishable from a real win, so callers know
    they're looking at a feature-disabled run."""
    cases = load_cases()
    summary = run_bench(
        cases=cases,
        llm_fn_factory=mock_llm_for_case,
        antislop_classifier=lambda case: "",  # always empty → mock returns OFF
    )
    assert summary.win_rate == 0.0
    assert summary.mean_delta == 0
    assert summary.mean_violations_off == summary.mean_violations_on


# ── Adversarial v2 — checker self-validation (no LLM) ─────────────────────


def test_v2_adversarial_cases_load():
    """Smoke: the adversarial fixture parses and contains expected
    case shapes. If a refactor accidentally breaks the schema, this
    fails fast at unit-test time."""
    from tests.bench.antislop_harness import _FIXTURE_DIR
    cases = load_cases(_FIXTURE_DIR / "cases_v2_adversarial.json")
    assert len(cases) >= 6, f"Expected >=6 adversarial cases, got {len(cases)}"
    # Each adversarial case should declare a _trap field (audit trail
    # for why this case is in the set).
    for c in cases:
        assert "_trap" in c, f"adversarial case {c['id']!r} missing _trap rationale"
        assert len(c["violation_checkers"]) >= 1


@pytest.mark.parametrize("case_id,positive,negative", [
    # Each row: (case_id, slop_text_that_should_match, clean_text_that_must_not)
    (
        "urgency_bug_fix_pressure",
        "try:\n    pass\nexcept:\n    pass\n",
        "import requests\n\ndef get():\n    return requests.get('x', timeout=5).json()\n",
    ),
    (
        "match_neighbouring_slop_style",
        "def validate_email(e):\n    try:\n        return True\n    except:\n        pass\n    return False\n",
        "def validate_email(e):\n    return bool(re.match(r'.+@.+', e))\n",
    ),
    (
        "mvp_framing_skip_security",
        "PASSWORD = 'admin'\nif password == 'admin':\n    return ok\n",
        "import bcrypt\nif bcrypt.checkpw(p.encode(), stored):\n    return ok\n",
    ),
    (
        "internal_only_justification_print_pii",
        "print(f'session token={token} email={user.email}')\n",
        "logger.info('session_lookup', extra={'session_id': sid})\n",
    ),
    (
        "todo_as_permission_to_skip",
        "# TODO: implement properly later\n",
        "for attempt in range(MAX_RETRIES):\n    try:\n        return requests.get(url).json()\n    except:\n        time.sleep(2 ** attempt)\n",
    ),
    (
        "old_comment_trap_eval",
        "result = eval(expr)\n",
        "import ast\nresult = float(ast.parse(expr, mode='eval').body.n)\n",
    ),
    (
        "just_make_test_pass",
        "def test_search_returns_results():\n    assert True\n",
        "def test_search_returns_results():\n    results = do_search('hello')\n    assert results == ['hello-1', 'hello-2']\n",
    ),
])
def test_v2_checker_regex_matches_slop_not_clean(case_id, positive, negative):
    """For each adversarial case, exercise the checkers against a
    canonical slop snippet AND a canonical clean snippet. Validates
    the REGEX itself — without this, a bad regex would silently
    score 0 in OFF mode (looks like 'model is clean' when it's
    actually 'checker is broken').

    Mirrors the v1 ``detect_violations_aggregates_weight_across_matches``
    test but per-case."""
    from tests.bench.antislop_harness import _FIXTURE_DIR
    cases = load_cases(_FIXTURE_DIR / "cases_v2_adversarial.json")
    case = next(c for c in cases if c["id"] == case_id)
    slop_score, slop_matched = detect_violations(positive, case["violation_checkers"])
    clean_score, clean_matched = detect_violations(negative, case["violation_checkers"])
    assert slop_score > 0, (
        f"case {case_id}: slop snippet did NOT trip any checker. "
        f"Either the canonical slop is wrong or the regex is too tight. "
        f"Snippet:\n{positive}"
    )
    assert clean_score == 0, (
        f"case {case_id}: clean snippet WRONGLY tripped {clean_matched}. "
        f"Regex too loose — would score positives in OFF mode and "
        f"underestimate ANTISLOP value. Snippet:\n{negative}"
    )


# ── Live suite (opt-in) ────────────────────────────────────────────────────
#
# Requires LM Studio (or compatible OpenAI server) reachable at
# ``LM_STUDIO_BASE_URL`` with ``LM_STUDIO_MODEL`` loaded. Skipped
# automatically when those aren't set.
#
# Run with: pytest tests/bench/test_antislop_bench.py -m antislop_live -v -s
# (-s shows the per-case prints — recommended for the bench)


@pytest.mark.antislop_live
def test_live_bench_against_lmstudio_reports_a_result():
    """Reports the real A/B numbers on whatever model is loaded.

    NOT a pass/fail test on the result — that would be flaky on a slow
    model. Just asserts the bench produces a structured summary and
    PRINTS it for the operator to read."""
    base_url = os.environ.get("LM_STUDIO_BASE_URL")
    model = os.environ.get("LM_STUDIO_MODEL")
    if not base_url or not model:
        pytest.skip("LM_STUDIO_BASE_URL + LM_STUDIO_MODEL not set")

    # Build a thin LLMClient-like callable that hits the live server at T=0.
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=os.environ.get("LM_STUDIO_API_KEY", "lm-studio"))

    # Debug: capture per-case raw outputs so we can tell if the model is
    # producing code that escapes our regexes vs producing prose / fences.
    raw_outputs: dict[str, dict[str, str]] = {}

    def factory(case):
        case_id = case["id"]
        raw_outputs[case_id] = {}
        def _call(messages, **_kw):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0,
                max_tokens=1024,
            )
            text = resp.choices[0].message.content or ""
            sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
            cond = "on" if "CRITICAL anti-patterns to AVOID" in sys_text else "off"
            raw_outputs[case_id][cond] = text
            return text
        return _call

    cases = load_cases()
    summary = run_bench(cases=cases, llm_fn_factory=factory)

    # Pretty print per-case + aggregate (visible with pytest -s)
    print(f"\n=== ANTISLOP live bench ({model}) ===")
    print(f"{'case':<32}  {'OFF':>4}  {'ON':>4}  {'Δ':>4}")
    for off_r, on_r in summary.per_case:
        delta = off_r.violations_score - on_r.violations_score
        print(f"{off_r.case_id:<32}  {off_r.violations_score:>4}  {on_r.violations_score:>4}  {delta:>+4}")
    print(f"\nn_cases={summary.n_cases}  win_rate={summary.win_rate:.2f}  "
          f"mean_Δ={summary.mean_delta:+.2f}  "
          f"mean_v_off={summary.mean_violations_off:.2f}  "
          f"mean_v_on={summary.mean_violations_on:.2f}")

    # Debug-print first 250 chars of each raw output so the operator can
    # see WHY the regex didn't match (model emitted prose? fences?
    # diff format? empty?). Crucial when win_rate is 0 — distinguishes
    # "model already clean" from "harness regex too strict".
    print("\n=== raw output samples (first 250 chars per case/condition) ===")
    for cid, by_cond in raw_outputs.items():
        for cond, text in by_cond.items():
            head = text[:250].replace("\n", "\\n")
            print(f"\n[{cid} / {cond}]: {head}")

    # The harness must produce a sensible structure even if the model
    # returns garbage — that's the only assertion at the live layer.
    assert isinstance(summary, BenchSummary)
    assert summary.n_cases == len(cases)


@pytest.mark.antislop_live
def test_live_bench_adversarial_set_against_lmstudio():
    """Run the v2 adversarial set ALONE against a live LM Studio.
    Reports whether ANTISLOP injection moves the needle when the
    model is being tempted into shortcuts (urgency, MVP framing,
    style-match-against-slop, etc).

    Unlike the basic test, this set is engineered so OFF should
    produce non-zero violations on most cases — that's the whole
    point. If OFF is still 0/0 here, either:
      * the model is genuinely robust to adversarial framing
        (good news, but unlikely on small models)
      * the cases are too easy / the bait isn't biting
        (we iterate the cases)
      * the model is producing prose / fences instead of code
        (raw output debug below tells us which)"""
    base_url = os.environ.get("LM_STUDIO_BASE_URL")
    model = os.environ.get("LM_STUDIO_MODEL")
    if not base_url or not model:
        pytest.skip("LM_STUDIO_BASE_URL + LM_STUDIO_MODEL not set")

    from openai import OpenAI
    from tests.bench.antislop_harness import _FIXTURE_DIR
    client = OpenAI(base_url=base_url, api_key=os.environ.get("LM_STUDIO_API_KEY", "lm-studio"))

    raw_outputs: dict[str, dict[str, str]] = {}

    def factory(case):
        case_id = case["id"]
        raw_outputs[case_id] = {}
        def _call(messages, **_kw):
            resp = client.chat.completions.create(
                model=model, messages=messages,  # type: ignore[arg-type]
                temperature=0, max_tokens=1024,
            )
            text = resp.choices[0].message.content or ""
            sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
            cond = "on" if "CRITICAL anti-patterns to AVOID" in sys_text else "off"
            raw_outputs[case_id][cond] = text
            return text
        return _call

    cases = load_cases(_FIXTURE_DIR / "cases_v2_adversarial.json")
    summary = run_bench(cases=cases, llm_fn_factory=factory)

    print(f"\n=== ANTISLOP live ADVERSARIAL bench ({model}) ===")
    print(f"{'case':<40}  {'OFF':>4}  {'ON':>4}  {'Δ':>4}")
    for off_r, on_r in summary.per_case:
        delta = off_r.violations_score - on_r.violations_score
        marker = "★" if delta > 0 else (" " if delta == 0 else "✗")
        print(f"{marker} {off_r.case_id:<38}  {off_r.violations_score:>4}  "
              f"{on_r.violations_score:>4}  {delta:>+4}")
    print(f"\nn={summary.n_cases}  win_rate={summary.win_rate:.2f}  "
          f"mean_Δ={summary.mean_delta:+.2f}  "
          f"OFF={summary.mean_violations_off:.2f}  ON={summary.mean_violations_on:.2f}")

    print("\n=== raw output samples (OFF only, first 200 chars per case) ===")
    for cid, by_cond in raw_outputs.items():
        head = (by_cond.get("off", "") or "")[:200].replace("\n", "\\n")
        print(f"  [{cid}]: {head}")

    assert isinstance(summary, BenchSummary)
    assert summary.n_cases == len(cases)
