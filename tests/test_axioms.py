"""Tests for the iter 6 axiom layer (gitoma/critic/axioms.py).

The 4 axioms (¬M ¬S ¬A ¬O) come from Gemini's first formalisation.
Each rule in ANTISLOP.md is mapped to 1+ axioms via deterministic
keyword matching; the worker can render the injection block in
"flat" (iter 5) or "axioms" (iter 6) mode.

These tests pin the deterministic mapping + stratified selection
+ the format output. Live-LLM A/B comparison lives in the
``tests/bench/test_antislop_bench.py`` adversarial suite, opt-in.
"""

from __future__ import annotations

from gitoma.critic.antislop import Rule
from gitoma.critic.axioms import (
    AXIOMS,
    Axiom,
    axiom_violation_profile,
    axioms_for_rule,
    format_axiom_block,
    stratify_by_axiom,
)


def _r(rid: int, title: str, rationale: str, *tags: str) -> Rule:
    return Rule(id=rid, title=title, rationale=rationale, tags=set(tags) | {"universal"})


# ── Axiom registry ─────────────────────────────────────────────────────────


def test_exactly_four_axioms_with_canonical_symbols():
    """The framework is the 4 negative axioms — never more, never less.
    A future refactor that adds a 5th must revise the entire prompt
    template, the metric vector shape, and the bench harness — so this
    test forces conscious change."""
    assert len(AXIOMS) == 4
    symbols = [a.symbol for a in AXIOMS]
    assert symbols == ["¬M", "¬S", "¬A", "¬O"]


def test_each_axiom_carries_principle_and_binary_filter():
    """Every axiom must declare its principle (the 'positive' statement
    of what it rejects) AND its binary filter (the YES/NO question the
    reviewer asks). Both go into the prompt template; missing either
    silently weakens the injection."""
    for a in AXIOMS:
        assert isinstance(a, Axiom)
        assert a.principle, f"axiom {a.symbol} missing principle"
        assert a.binary_filter, f"axiom {a.symbol} missing binary_filter"
        assert "?" in a.binary_filter, (
            f"axiom {a.symbol} binary_filter must be a question — "
            "the imperative phrasing matters for prompt strength"
        )
        assert a.keywords, f"axiom {a.symbol} has no routing keywords"


# ── Rule → axiom mapping ───────────────────────────────────────────────────


def test_axioms_for_rule_routes_known_examples():
    """Spot-check the keyword-based router on canonical rules from
    ANTISLOP.md — these specific rules MUST land in the right axiom,
    otherwise the prompt loses signal."""
    cases: list[tuple[Rule, set[str]]] = [
        # Anti-mutation: destructive + git submodules + force push
        (_r(1, "Force pushing to main", "rewrites history destructively"), {"¬M"}),
        # Anti-hope: assumptions, retries, secrets
        (_r(2, "Hardcoded API Keys and Secrets", "Bots scrape secrets in seconds"), {"¬S"}),
        (_r(3, "Wait/Sleep commands to fix race conditions",
            "A band-aid that hides the real issue"), {"¬M", "¬S"}),
        # Anti-ambiguity: TODOs, magic numbers, comments
        (_r(4, "Magic Numbers", "Use named constants like SECONDS_IN_DAY"), {"¬A"}),
        (_r(5, "// TODO: Fix this later comments from 3 years ago",
            "Admit you are never going to fix it"), {"¬A"}),
        # Anti-opacity: silent failures, swallowing
        (_r(6, "Swallowing errors with empty catch blocks",
            "Silent failure makes debugging impossible"), {"¬O"}),
        (_r(7, "Production code relying on console.log debugging",
            "Pollutes logs and impacts performance"), {"¬O"}),
    ]
    for rule, expected_subset in cases:
        got = axioms_for_rule(rule)
        missing = expected_subset - got
        assert not missing, (
            f"rule #{rule.id} ({rule.title!r}) expected to map to "
            f"{expected_subset}, missing {missing}; got {got}"
        )


def test_axioms_for_rule_unmapped_falls_back_to_anti_ambiguity():
    """A rule whose keywords don't match any axiom defaults to ¬A
    (Anti-Ambiguity). Reasonable default — most unmapped style rules
    are about clarity / conventions / docs."""
    rule = _r(99, "totally_invented_pattern", "some completely orthogonal advice")
    got = axioms_for_rule(rule)
    assert got == {"¬A"}, f"unmapped rule should fall back to ¬A; got {got}"


def test_axioms_for_rule_is_deterministic():
    """Cornerstone of A/B benchmarks: same rule input → same axiom set,
    every call. No random sampling, no time-based selection."""
    rule = _r(10, "SQL Injection vulnerabilities via string concatenation",
              "fastest way to lose your database")
    a = axioms_for_rule(rule)
    b = axioms_for_rule(rule)
    c = axioms_for_rule(rule)
    assert a == b == c


# ── Stratified selection ───────────────────────────────────────────────────


def test_stratify_caps_per_axiom():
    """Each axiom bucket is capped at ``per_axiom_cap`` even if many
    rules would apply. Without the cap, a popular axiom (¬S typically)
    would dominate the prompt and starve the others."""
    # 10 rules all pointing at ¬S
    rules = [
        _r(i, "secret hardcoded somewhere",
          "tokens leak; do not assume the network is safe", "security")
        for i in range(1, 11)
    ]
    buckets = stratify_by_axiom(rules, per_axiom_cap=3)
    assert len(buckets["¬S"]) == 3, "¬S should be capped at 3"


def test_stratify_keeps_lowest_id_first():
    """Within an axiom, rules are kept in id order (lower id = earlier
    in ANTISLOP.md = usually higher severity)."""
    rules = [
        _r(99, "magic number a", "use named constants"),
        _r(7, "magic number b", "use named constants"),
        _r(43, "magic number c", "use named constants"),
    ]
    buckets = stratify_by_axiom(rules, per_axiom_cap=3)
    ids_in_order = [r.id for r in buckets["¬A"]]
    assert ids_in_order == [7, 43, 99]


def test_stratify_handles_multi_axiom_rule_only_once_per_bucket():
    """A rule that maps to ¬M AND ¬S appears in BOTH buckets, but only
    once in each. Critical: without the per-bucket dedup, the same
    rule could fill all 4 slots in one axiom."""
    rule = _r(1, "Wait/Sleep commands to fix race conditions",
              "band-aid for race conditions; assumption of timing")
    # This should map to ¬M (race) AND ¬S (assumption / wait)
    axs = axioms_for_rule(rule)
    assert len(axs) >= 2

    # Even when given multiple times, dedup ensures one slot per bucket
    buckets = stratify_by_axiom([rule, rule, rule], per_axiom_cap=3)
    for sym, bucket in buckets.items():
        assert len(bucket) <= 1, (
            f"bucket {sym} contained {len(bucket)} entries of the same rule"
        )


# ── Format output ──────────────────────────────────────────────────────────


def test_format_axiom_block_empty_when_no_rules():
    """No rules → empty string. Caller skips the inject without
    emitting an empty 'BEFORE writing the patch' header."""
    buckets = {a.symbol: [] for a in AXIOMS}
    assert format_axiom_block(buckets) == ""


def test_format_axiom_block_includes_each_populated_axiom():
    """The output renders one section per populated axiom with:
      * symbol + principle + binary filter (the imperative question)
      * 1-3 rule examples as bullets

    A populated axiom that's missing any of these silently weakens
    the prompt — the binary filter is the key imperative."""
    rules = [
        _r(1, "Hardcoded API Keys", "tokens leak", "security"),  # ¬S
        _r(2, "Magic Numbers", "use named constants", "universal"),  # ¬A
        _r(3, "Swallowing errors with empty catch blocks", "silent failure", "universal"),  # ¬O
    ]
    buckets = stratify_by_axiom(rules)
    out = format_axiom_block(buckets)
    # Symbols + at least one rule per populated axiom
    assert "¬S" in out and "Hardcoded API Keys" in out
    assert "¬A" in out and "Magic Numbers" in out
    assert "¬O" in out and "Swallowing errors" in out
    # Header + footer with the imperative framing
    assert "binary filters" in out.lower()
    assert "non-negotiable" in out.lower()


def test_format_axiom_block_skips_empty_axioms():
    """Axioms with zero rules in the bucket MUST NOT appear in the
    output — otherwise we'd emit a header with no content under it,
    confusing the model."""
    rules = [_r(1, "Magic Numbers", "use named constants")]
    buckets = stratify_by_axiom(rules)
    out = format_axiom_block(buckets)
    # ¬A is populated; the others are empty
    populated = [a.symbol for a in AXIOMS if buckets[a.symbol]]
    assert populated == ["¬A"]
    # Other axioms must not appear in the output
    for sym in ("¬M", "¬S", "¬O"):
        assert sym not in out, (
            f"empty axiom {sym} appeared in output despite no rules"
        )


# ── Profile aggregation ───────────────────────────────────────────────────


def test_axiom_violation_profile_counts_correctly():
    """The output is the per-PR / per-run vector that becomes the
    cross-repo dashboard signal. Stable shape (4 keys, always),
    zero counts for absent axioms."""
    profile = axiom_violation_profile(
        ["¬M", "¬S", "¬S", "¬S", "¬A", "¬O", "¬O"]
    )
    assert profile == {"¬M": 1, "¬S": 3, "¬A": 1, "¬O": 2}


def test_axiom_violation_profile_unknown_tags_dropped():
    """Caller-fed garbage doesn't pollute the profile — the dict shape
    stays {¬M, ¬S, ¬A, ¬O} for downstream aggregation."""
    profile = axiom_violation_profile(["¬M", "fake", "X", "¬S"])
    assert profile == {"¬M": 1, "¬S": 1, "¬A": 0, "¬O": 0}


def test_axiom_violation_profile_empty_input_zero_vector():
    """Empty input → zero on every axiom. NEVER an empty dict (callers
    iterate the 4 keys to render dashboards)."""
    profile = axiom_violation_profile([])
    assert profile == {"¬M": 0, "¬S": 0, "¬A": 0, "¬O": 0}


# ── Format integration: antislop format_for_injection passes mode ─────────


def test_antislop_format_for_injection_axioms_mode_uses_axiom_block():
    """End-to-end: ``format_for_injection(rules, mode='axioms')`` MUST
    produce axiom-organised output (with ¬-symbols), not the flat list."""
    from gitoma.critic.antislop import format_for_injection

    rules = [
        _r(1, "Hardcoded API Keys", "tokens leak", "security"),
        _r(2, "Magic Numbers", "use named constants"),
    ]
    flat_out = format_for_injection(rules, mode="flat")
    axiom_out = format_for_injection(rules, mode="axioms")

    assert "CRITICAL anti-patterns to AVOID" in flat_out
    assert "¬" not in flat_out  # flat doesn't carry axiom symbols

    assert "¬S" in axiom_out or "¬A" in axiom_out
    assert "binary filters" in axiom_out.lower()
    assert "CRITICAL anti-patterns to AVOID" not in axiom_out


def test_antislop_format_for_injection_invalid_mode_falls_back_to_flat():
    """An unknown mode value should NOT crash — it should silently
    fall back to flat (the safe default). Future modes can be added
    without breaking deployments that still use legacy values."""
    from gitoma.critic.antislop import format_for_injection

    rules = [_r(1, "Magic Numbers", "named constants")]
    out = format_for_injection(rules, mode="totally_invented_mode")
    assert "CRITICAL anti-patterns to AVOID" in out
