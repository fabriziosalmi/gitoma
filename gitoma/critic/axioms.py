"""Iter 6 — Axiom layer over ANTISLOP rules.

The 200 rules in ANTISLOP.md flatten the conceptual landscape: every
rule is an atom, the classifier picks N atoms, and the model receives
a list. The first live adversarial bench (cases_v2) showed this is
insufficient — Δ=0 win_rate=0 on 8 trapped cases. Suspected cause:
the prompt is too long AND too soft; the model treats the bullet
list as background.

This module reorganises the same rules along the 4 axioms of the
first Gemini formalisation:

    S_valid ⟺ ∀x ∈ (Code ∪ Docs), ∄(Implicit ∨ Subjective ∨ Synchronous)

  ¬M  Anti-Mutation        — destructive state changes, side effects,
                              mutable infra, lock-based concurrency
  ¬S  Anti-Hope            — assuming success, network/service trust,
                              sync retry, no-circuit-breaker
  ¬A  Anti-Ambiguity       — qualifiers, magic numbers, vague signatures,
                              implicit context, passive voice
  ¬O  Anti-Opacity         — joined read/write, silent failures,
                              missing logs, infra coupling

The output prompt becomes shorter (axiom-organised, ~80 tokens vs
~200 for a flat 15-rule list) AND more imperative (each axiom is a
binary filter, not a vague suggestion). That's the design hypothesis
iter 6 is about to test against the v2 adversarial bench.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from gitoma.critic.antislop import Rule


@dataclass(frozen=True)
class Axiom:
    """One of the 4 negative axioms."""
    id: str
    symbol: str
    principle: str
    binary_filter: str
    # Keywords (substrings, case-insensitive) that route a rule to this
    # axiom. Order doesn't matter; a rule can map to multiple axioms.
    keywords: frozenset[str]


# The 4 axioms — order matters only for output presentation
# (severity descending: ¬M and ¬S are usually higher-stakes than ¬A and ¬O).
AXIOMS: tuple[Axiom, ...] = (
    Axiom(
        id="anti_mutation",
        symbol="¬M",
        principle="Reject uncontrolled mutation",
        binary_filter="Does this patch overwrite state without an audit trail, share mutable memory across calls, or modify infrastructure that should be replaced?",
        keywords=frozenset({
            "overwrite", "mutation", "mutable", "destructive", "lock",
            "race condition", "global", "shared state", "monolith",
            "infrastructure", "dependency hell", "dependencies", "node_modules",
            "vendor", "package-lock", "binary file", "git submodule",
            "force push", "delete history", "outdated", "cve",
            "infinite loop", "recursion", "blocking",
        }),
    ),
    Axiom(
        id="anti_hope",
        symbol="¬S",
        principle="Reject the assumption of success",
        binary_filter="Does this code assume the network, dependency, or downstream service will succeed; assume the user will input valid data; or retry in tight sync loops?",
        keywords=frozenset({
            "assume", "assumption", "trust", "fail", "failure", "retry",
            "wait", "sleep", "hope", "synchron", "rate limit", "race",
            "input validation", "invalid", "uncaught", "promise",
            "cascade", "circuit breaker", "fallback", "graceful",
            "outdated dependencies", "hardcoded", "secret", "api key",
            "password", "sql injection", "xss", "csrf", "innerhtml",
            "noopener", "https", "auth", "session", "valid data",
            "input from user", "test", "fuzzing", "property-based",
            "happy path",
        }),
    ),
    Axiom(
        id="anti_ambiguity",
        symbol="¬A",
        principle="Reject ambiguity",
        binary_filter="Does this code or doc use vague language, magic numbers, implicit context, ambiguous naming, or rely on the reader inferring what was meant?",
        keywords=frozenset({
            "magic number", "magic string", "todo", "fixme",
            "comment", "comments", "documentation", "docs", "readme",
            "license", "naming", "rename", "snake_case", "camelcase",
            "single-letter", "single letter", "abbreviation",
            "obscure", "ambiguous", "implicit", "lorem ipsum",
            "placeholder", "convention", "inconsistent", "indentation",
            "var instead", "any type", "vague", "function names that lie",
            "misleading", "passive", "passive-aggressive",
            "screenshot", "outdated screenshot", "dead link",
            "memes", "ascii art", "presumptuous",
        }),
    ),
    Axiom(
        id="anti_opacity",
        symbol="¬O",
        principle="Reject opacity and tight coupling",
        binary_filter="Does this code couple components that should be isolatable, fail silently, leak debug noise to users, or hide architectural decisions in the code?",
        keywords=frozenset({
            "swallowing", "swallow", "silent", "empty catch", "bare except",
            "generic exception", "ignored", "uncaught",
            "console.log", "alert(", "debugger", "print(",
            "global", "monolithic", "god object", "god class",
            "circular dependencies", "tight coupling", "coupled",
            "mixing", "presentation", "logic", "business logic",
            "bypass", "direct dom", "fallthrough", "implicit",
            "deeply nested", "arrow code", "hadouken", "z-index war",
            "dead link", "loud failure", "stack trace",
            "telemetry", "tracing", "log", "logs", "structured",
            "memory leak", "unwrap", "raw error",
        }),
    ),
)


_AXIOM_BY_SYMBOL: dict[str, Axiom] = {a.symbol: a for a in AXIOMS}
_AXIOM_BY_ID: dict[str, Axiom] = {a.id: a for a in AXIOMS}


def axioms_for_rule(rule: Rule) -> set[str]:
    """Return the set of axiom symbols this rule belongs to.

    A rule can belong to 1+ axioms (e.g. "Hardcoded API Keys" hits both
    ¬S anti-hope-on-secrets and ¬O anti-opacity-secret-storage).

    Returns at least one axiom — falls back to ¬A (ambiguity) for rules
    that don't match any keyword, since unmapped rules are usually
    documentation/comment style suggestions."""
    blob = f"{rule.title} {rule.rationale}".lower()
    matched: set[str] = set()
    for axiom in AXIOMS:
        for kw in axiom.keywords:
            # Word-boundary for short alphanumeric keywords (mirror of
            # the antislop tagger's logic to dodge "tab" → "database"
            # style false positives).
            if len(kw) <= 5 and kw.replace(" ", "").isalnum():
                if re.search(r"\b" + re.escape(kw) + r"\b", blob):
                    matched.add(axiom.symbol)
                    break
            elif kw in blob:
                matched.add(axiom.symbol)
                break
    if not matched:
        matched.add("¬A")  # unmapped rule = treat as ambiguity by default
    return matched


def stratify_by_axiom(
    rules: Iterable[Rule],
    *,
    per_axiom_cap: int = 3,
) -> dict[str, list[Rule]]:
    """Group rules by axiom, capping each group at ``per_axiom_cap``.

    The cap matters: an axiom that owns 80 rules would dominate the
    output otherwise. Sorting within axiom is by rule.id (lower = earlier
    in ANTISLOP.md = usually higher severity per the file's own ordering)."""
    buckets: dict[str, list[Rule]] = {a.symbol: [] for a in AXIOMS}
    seen_per_axiom: dict[str, set[int]] = {a.symbol: set() for a in AXIOMS}
    rules_sorted = sorted(rules, key=lambda r: r.id)
    for r in rules_sorted:
        for sym in axioms_for_rule(r):
            if r.id in seen_per_axiom[sym]:
                continue
            if len(buckets[sym]) >= per_axiom_cap:
                continue
            buckets[sym].append(r)
            seen_per_axiom[sym].add(r.id)
    return buckets


def format_axiom_block(buckets: dict[str, list[Rule]]) -> str:
    """Render the stratified rules as an axiom-organised injection
    block. Each axiom gets a header with its symbol, principle, and
    binary filter, followed by 1-3 punchy rule examples.

    The format is intentionally MORE IMPERATIVE than the flat-list
    format: each axiom is a discrete decision the reviewer must make,
    and the rule examples are illustrations, not the full law.
    """
    populated = [a for a in AXIOMS if buckets.get(a.symbol)]
    if not populated:
        return ""
    parts: list[str] = [
        "BEFORE writing the patch, audit your output against these 4 binary filters. "
        "If any returns YES on a part of your patch, REWRITE that part:",
    ]
    for axiom in populated:
        rules = buckets[axiom.symbol]
        if not rules:
            continue
        parts.append("")
        parts.append(f"{axiom.symbol} ({axiom.principle}) — {axiom.binary_filter}")
        for r in rules:
            title = r.title.rstrip(". ")
            parts.append(f"  · {title}")
    parts.append("")
    parts.append(
        "These 4 filters are NON-NEGOTIABLE. A patch that violates any "
        "axiom will be rejected by the downstream devil's-advocate review."
    )
    return "\n".join(parts)


def axiom_violation_profile(findings_with_axiom_tags: Iterable[str]) -> dict[str, int]:
    """Aggregate a list of finding-tagged-by-axiom-symbol into a
    {¬M:n, ¬S:n, ¬A:n, ¬O:n} dict. Every per-PR or per-run trace can
    emit this as a single 4-vector for cross-repo / cross-time comparison.

    Unknown axiom tags are silently dropped — caller's responsibility to
    feed valid symbols. The 4 known symbols always appear in the output
    (with 0 if absent) so the dict shape is stable for downstream
    aggregation."""
    profile: dict[str, int] = {a.symbol: 0 for a in AXIOMS}
    for tag in findings_with_axiom_tags:
        if tag in profile:
            profile[tag] += 1
    return profile


__all__ = [
    "AXIOMS",
    "Axiom",
    "axiom_violation_profile",
    "axioms_for_rule",
    "format_axiom_block",
    "stratify_by_axiom",
]
