"""Regression tests for the ANTISLOP rule classifier (iter 5).

The classifier MUST stay deterministic so A/B benchmarks are meaningful —
no LLM calls, no random sampling, no time-based selection. These tests
pin the deterministic contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.critic.antislop import (
    Rule,
    classify_for_subtask,
    format_for_injection,
    load_rules,
    tag_rule,
)


# ── Parser ──────────────────────────────────────────────────────────────────


def _write_minimal_antislop(tmp_path: Path) -> Path:
    """Write a tiny synthetic ANTISLOP.md so tests don't depend on the
    real 200-rule file existing (it's gitignored)."""
    content = """\
Heading text that is not a rule.

    Hardcoded API Keys (Bots scrape secrets in seconds).
    Magic Numbers (Use named constants).
    Using !important in CSS globally (Breaks the cascade).
    Single-letter variable names outside of loops (x and data tell the reader nothing).
    Tiny Mobile Tap Targets (<44px) (Frustrates users with fat fingers).
    DX: Bloated Docker images for simple apps (Waiting 10 minutes for Hello World).
"""
    p = tmp_path / "ANTISLOP.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_rules_parses_indented_lines_only(tmp_path: Path):
    """Only 4-space-indented title-and-rationale lines are rules. Headings
    and blank lines are ignored. IDs assigned in source order."""
    p = _write_minimal_antislop(tmp_path)
    rules = load_rules(p)
    assert len(rules) == 6
    assert rules[0].id == 1
    assert rules[0].title == "Hardcoded API Keys"
    assert "Bots scrape secrets" in rules[0].rationale
    assert rules[5].id == 6
    assert rules[5].title.startswith("DX:")


def test_load_rules_handles_inner_parens_in_rationale(tmp_path: Path):
    """Rationales with nested parens (like 'Tiny Mobile Tap Targets (<44px)')
    must parse cleanly — the regex anchors on the OUTER paren pair."""
    p = _write_minimal_antislop(tmp_path)
    rules = load_rules(p)
    tap = next(r for r in rules if "Tap Targets" in r.title)
    # Title contains the inner parens
    assert "<44px" in tap.title
    # Rationale is the outermost-paren contents
    assert "fat fingers" in tap.rationale


def test_load_rules_returns_empty_when_file_missing(tmp_path: Path):
    """Missing file is NOT an error — it's the "feature off" signal.
    The worker checks for empty list and skips injection silently."""
    rules = load_rules(tmp_path / "does_not_exist.md")
    assert rules == []


# ── Tagger ──────────────────────────────────────────────────────────────────


def test_tag_rule_always_has_universal():
    """Every rule gets the universal tag. That's the floor — even a rule
    that matches no specific keyword is still considered "always relevant"
    by the floor tag, so we never lose rules to over-aggressive filtering."""
    tags = tag_rule("totally generic title", "and a rationale with no keywords")
    assert tags == {"universal"}


def test_tag_rule_short_keyword_uses_word_boundaries():
    """Short keywords like ``tab`` MUST use word boundaries — without
    them, ``tab`` matches inside ``database``, ``stable``, ``tablet``,
    polluting unrelated rules with false ui-pattern tags. Caught when
    the SQL Injection rule got tagged ui-pattern on first sanity pass."""
    # SQL injection content — no real ui-pattern context
    tags = tag_rule(
        "SQL Injection vulnerabilities via string concatenation",
        "The fastest way to lose your database because you didn't use parameterized queries",
    )
    assert "security" in tags
    assert "ui-pattern" not in tags  # NOT a false positive on "tab" → "database"


def test_tag_rule_legitimate_short_keyword_match():
    """Conversely, a real ``tab`` standalone word still matches — the
    word-boundary check rejects substring matches but accepts the
    intended ones."""
    tags = tag_rule(
        "Keyboard Focus Traps",
        "Modals or menus that capture the keyboard focus and never let the user tab out",
    )
    # 'tab' as a real word → ui-pattern (and a11y via 'focus trap')
    assert "ui-pattern" in tags or "a11y" in tags


# ── Classifier ──────────────────────────────────────────────────────────────


def _sample_rules() -> list[Rule]:
    """Build a small handcrafted rule set for classifier tests so we don't
    couple test outcomes to the real ANTISLOP.md numbering."""
    return [
        Rule(1, "Hardcoded API Keys", "secrets leak in seconds",
             tags={"universal", "security"}),
        Rule(2, "Magic Numbers", "named constants",
             tags={"universal"}),  # truly universal
        Rule(3, "Using !important in CSS globally", "breaks the cascade",
             tags={"universal", "css"}),
        Rule(4, "Direct DOM manipulation in React", "bypasses virtual DOM",
             tags={"universal", "frontend-framework"}),
        Rule(5, "Lack of unit tests", "hope is not a strategy",
             tags={"universal", "test"}),
        Rule(6, "Tiny Mobile Tap Targets", "fat fingers",
             tags={"universal", "ui-pattern", "a11y", "mobile"}),
    ]


def test_classify_truly_universal_rules_always_included():
    """A rule tagged ONLY {universal} (e.g. magic numbers, naming, error
    handling) MUST appear in the selection regardless of subtask context.
    The classifier prioritises domain-specific rules but always leaves
    room for these universals as filler."""
    rules = _sample_rules()
    selected = classify_for_subtask(
        rules=rules, file_hints=["src/main.rs"], languages=["Rust"],
        action_hint="create", top_n=10,
    )
    selected_ids = {r.id for r in selected}
    # Magic Numbers (#2) is the only truly-universal rule here
    assert 2 in selected_ids


def test_classify_excludes_irrelevant_domain_rules():
    """A rule tagged {universal, css} is "specific in application even
    if universal in concept" — it must NOT appear on a Rust subtask.
    Without this gate, every subtask saw the same prompt and the
    classifier was useless."""
    rules = _sample_rules()
    selected = classify_for_subtask(
        rules=rules, file_hints=["src/main.rs"], languages=["Rust"],
        action_hint="create", top_n=10,
    )
    selected_ids = {r.id for r in selected}
    # CSS rule (#3), React rule (#4), Mobile/UI rule (#6) — all out of scope for Rust
    assert 3 not in selected_ids
    assert 4 not in selected_ids
    assert 6 not in selected_ids


def test_classify_includes_domain_rules_when_context_matches():
    """Mirror image: on a CSS / HTML subtask, the css/ui rules MUST
    appear and the test/perf-only ones can be skipped."""
    rules = _sample_rules()
    selected = classify_for_subtask(
        rules=rules,
        file_hints=["public/index.html", "styles/main.css"],
        languages=["HTML", "CSS"],
        action_hint="modify",
        top_n=10,
    )
    selected_ids = {r.id for r in selected}
    assert 3 in selected_ids  # !important in CSS
    assert 6 in selected_ids  # Tap targets — has both ui-pattern and a11y


def test_classify_domain_rule_outranks_universal_when_competing():
    """When top_n is tight (say 2), domain-specific matches MUST take
    priority over truly-universal — the universal rules fill tail slots,
    not the prime ones. This is the core score-asymmetry the rebalance
    enforced."""
    rules = _sample_rules()
    selected = classify_for_subtask(
        rules=rules,
        file_hints=["src/server.js"],
        languages=["JavaScript"],
        action_hint="modify",
        top_n=2,
    )
    # JS-relevant rules with multiple tags should win 2 slots before #2 (universal-only)
    # Among _sample_rules, the ones that overlap JS context (test/perf) are:
    #   - #5 Lack of unit tests (test) — overlap 1
    # Hardcoded API keys (#1) is truly-universal-feeling but tagged security; from
    # _sample_rules it's NOT truly universal (has security tag) — won't match JS.
    # Therefore on a JS subtask with top_n=2, #5 (test match) takes 1 slot, #2
    # (truly universal) takes the other.
    selected_ids = {r.id for r in selected}
    assert 5 in selected_ids
    assert 2 in selected_ids


def test_classify_is_deterministic_repeated_calls_same_output():
    """Cornerstone of A/B benchmarking: same inputs → same outputs.
    No LLM, no time-dependent selection, no set-iteration order leak."""
    rules = _sample_rules()
    args = dict(
        rules=rules, file_hints=["src/main.rs"], languages=["Rust"],
        action_hint="create", top_n=4,
    )
    a = [r.id for r in classify_for_subtask(**args)]
    b = [r.id for r in classify_for_subtask(**args)]
    c = [r.id for r in classify_for_subtask(**args)]
    assert a == b == c


# ── Formatter ───────────────────────────────────────────────────────────────


def test_format_for_injection_empty_list_returns_empty_string():
    """No rules selected → empty string. Worker checks truthiness and
    skips appending to system prompt without injecting an empty
    'CRITICAL anti-patterns' header."""
    assert format_for_injection([]) == ""


def test_format_for_injection_renders_punchy_bullets():
    """Each rule rendered as ``- Title — Rationale`` (em dash). The
    header tells the LLM what these are; the footer warns about
    downstream rejection. Both are needed — without the header the
    bullets read like a TODO list, without the footer the LLM
    treats them as soft suggestions."""
    rules = _sample_rules()[:2]
    out = format_for_injection(rules)
    assert "CRITICAL anti-patterns to AVOID" in out
    assert "- Hardcoded API Keys" in out
    assert "- Magic Numbers" in out
    assert "rejected" in out  # footer


def test_format_for_injection_strips_trailing_period_from_title():
    """Some titles in the source file end in a period; if we render
    ``Title. — Rationale`` it reads weirdly. Strip the trailing period
    before joining with the em dash."""
    r = Rule(1, "Some Rule.", "with rationale", tags={"universal"})
    out = format_for_injection([r])
    assert "Some Rule —" in out
    assert "Some Rule. —" not in out
