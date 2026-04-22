"""ANTISLOP: parse + classify + inject anti-pattern rules into the worker prompt.

Iter 5 of the critic stack. Where iter 1-4 catch slop AFTER it's
generated (via panel/devil/refiner), iter 5 attacks slop at the SOURCE
by injecting a relevant subset of "do-not" rules into the worker's
system prompt before it generates anything.

Design constraints (from the brief):
  * 200 rules total (100 code/sec/arch + 100 UI/UX/DX) is too many for
    a small model. Inject 5-15 RELEVANT rules per subtask, not all.
  * Classifier MUST be deterministic — A/B benchmarks need to compare
    runs without seed-induced variance polluting the signal.
  * The rules file lives at ANTISLOP.md (gitignored in this repo —
    a user customization, not part of gitoma itself). Missing file =
    feature off, no error.

This module exposes:
  * ``Rule`` dataclass + ``load_rules(path)`` parser
  * ``tag_rule()`` deterministic keyword-based tagger (no LLM)
  * ``classify_for_subtask(...)`` heuristic subset selector
  * ``format_for_injection(...)`` punchy prompt-ready bullets

The classifier is called by the worker right before chat() and the
result is logged to trace as ``antislop.injected``. A/B-able via the
``CRITIC_PANEL_ANTISLOP`` env var (off | on | auto).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ── Tag taxonomy ────────────────────────────────────────────────────────────
#
# Keep narrow. The classifier maps subtask context → tag set, and intersects
# with rule tags. Adding more tags = more selectivity but also more risk of
# missing relevant rules. 12 tags is the sweet spot we landed on after the
# first manual pass over ANTISLOP.md.
#
# Each rule in the file has 1-3 tags inferred via the keyword dict below.
# A rule that matches NO keyword falls back to the universal tag, which is
# always considered relevant (e.g. "magic numbers", "swallowing errors").

TAGS_UNIVERSAL = "universal"

# Tag → list of substrings (case-insensitive). The first match wins for
# adding the tag; multiple tags can apply per rule.
_TAG_KEYWORDS: dict[str, list[str]] = {
    "security": [
        "api key", "secret", "password", "sql injection", "chmod", "eval(",
        "innerhtml", "xss", "https://", "noopener", "cve", "csp",
        "rel=\"noreferrer\"", "rel='noreferrer'", "captcha",
    ],
    "git": [
        "commit", "branch", "git ", "submodule", ".gitignore", "lfs", "force push",
    ],
    "js": [
        "package.json", " npm ", "node_modules", "var ", "let/const", "promise",
        "event loop", "node.js", "jquery", "tree-shaking", "ts ", "typescript",
        "any type", ".vscode", ".idea", "javascript",
    ],
    "css": [
        "!important", "css", "z-index", "stacking", "duplicate css", "br tag",
        "<br", "letter-spacing", "tracking", "kerning", "blurred background",
    ],
    "html": [
        "<div", "div soup", "<br", "semantic html", "innerhtml", "alt tag",
        "aria", "target=\"_blank\"", "target='_blank'", "inline style",
    ],
    "frontend-framework": [
        "react", "vue", "angular", "jsx", "virtual dom",
    ],
    "ui-pattern": [
        "scroll", "modal", "menu", "carousel", "slider", "dropdown",
        "tooltip", "tap target", "navigation", "breadcrumb", "hover",
        "tab", "sticky header", "ghost button", "skeleton screen",
        "pagination", "popup", "banner", "toggle", "split-button",
        "footer", "context menu",
    ],
    "ui-feedback": [
        "alert(", "toast", "notification", "loading indicator",
        "empty state", "error state", "error message", "confetti",
    ],
    "a11y": [
        "accessibility", "aria", "screen reader", "color blind", "colorblind",
        "captcha", "deaf", "blind", "vestibular", "prefers-reduced-motion",
        "skip to content", "tab out", "focus trap", "tap target",
        "low contrast", "ambiguous icon", "mystery meat",
    ],
    "perf": [
        "infinite loop", "event loop", "blocking", "performance",
        "premature optimization", "bloat", "tree-shaking", "memory leak",
        "lag", "freeze", "slow", "gpu lag", "parallax",
    ],
    "docs": [
        "readme", "documentation", "doc", "todo:", "license file", "changelog",
        "outdated screenshot", "sample code", "tutorials",
    ],
    "test": [
        "unit test", "tests that assert", "fake test", "ci/cd",
    ],
    "api": [
        "200 ok for errors", "user_id", "userid", "api response", "endpoint",
        "naming in apis", "global sudo",
    ],
    "dx": [
        "dx:", "cli tool", "hot reload", "logs", "config format",
        "global system", ".nvmrc", "git hook", "setup script",
    ],
    "mobile": [
        "mobile", "tap target", "pinch-to-zoom", "screen dimension",
        "responsiveness", "touch",
    ],
}


@dataclass
class Rule:
    """One ANTISLOP rule, parsed and tagged."""
    id: int
    title: str
    rationale: str
    tags: set[str] = field(default_factory=set)

    def punchy(self) -> str:
        """Render as a single-line bullet for prompt injection."""
        # Strip trailing period from title to avoid "X.. (rationale)"
        title = self.title.rstrip(". ")
        return f"- {title} — {self.rationale}"


# ── Parser ──────────────────────────────────────────────────────────────────


# Each rule line in ANTISLOP.md is indented 4 spaces, then has a title and
# parenthesised rationale. Tolerant of inner parens (e.g. "Using var (instead of let/const)")
# — we anchor on the LAST closing paren before end-of-line to find the rationale.
_RULE_LINE = re.compile(r"^    (.+?)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\.?\s*$")


def load_rules(path: Path | str | None = None) -> list[Rule]:
    """Parse the ANTISLOP markdown file into a list of Rule objects.

    Resolution order when ``path`` is None:
      1. ``$PWD/ANTISLOP.md`` (per-repo customization, ignored by gitoma's gitignore)
      2. ``~/.gitoma/antislop.md`` (user-wide default)

    Returns an empty list (not an error) if no file is found — antislop
    injection becomes a no-op in that case so existing setups keep working.
    """
    if path is None:
        candidates = [
            Path.cwd() / "ANTISLOP.md",
            Path.home() / ".gitoma" / "antislop.md",
        ]
        for cand in candidates:
            if cand.is_file():
                path = cand
                break
        else:
            return []

    p = Path(path)
    if not p.is_file():
        return []

    rules: list[Rule] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _RULE_LINE.match(line)
        if not m:
            continue
        title = m.group(1).strip()
        rationale = m.group(2).strip()
        rule_id = len(rules) + 1
        tags = tag_rule(title, rationale)
        rules.append(Rule(id=rule_id, title=title, rationale=rationale, tags=tags))
    return rules


def tag_rule(title: str, rationale: str) -> set[str]:
    """Deterministic keyword-based tagger. Always returns at least
    ``TAGS_UNIVERSAL``; specific tags added on top when keywords match.

    Uses word-boundary matching for short keywords (≤4 chars) — without
    that, ``"tab"`` matches ``"database"``, ``"div"`` matches ``"divide"``,
    etc. Caught during the first sanity pass on ANTISLOP.md when SQL
    Injection rule got tagged ui-pattern."""
    blob = f"{title} {rationale}".lower()
    tags: set[str] = {TAGS_UNIVERSAL}
    for tag, keywords in _TAG_KEYWORDS.items():
        for kw in keywords:
            if _kw_matches(kw, blob):
                tags.add(tag)
                break
    return tags


def _kw_matches(kw: str, blob: str) -> bool:
    """Substring match for keywords with non-word terminators (``alert(``,
    ``api key``, ``200 ok``, etc) — these self-bound. Word-boundary match
    for short alphanumeric keywords where substring would be too greedy."""
    # If the keyword itself contains punctuation or whitespace, the
    # surrounding context already disambiguates — substring is safe.
    if not kw.replace(" ", "").isalnum() or " " in kw:
        return kw in blob
    # Short alphanumeric — require word boundaries to dodge false positives.
    if len(kw) <= 5:
        return re.search(r"\b" + re.escape(kw) + r"\b", blob) is not None
    return kw in blob


# ── Classifier (heuristic, deterministic) ───────────────────────────────────


# Subtask context → relevant tags. Inferred from file extensions + action.
_EXT_TO_TAGS: dict[str, set[str]] = {
    ".rs": {"perf", "test"},
    ".py": {"test", "perf"},
    ".js": {"js", "perf", "test"},
    ".ts": {"js", "test"},
    ".jsx": {"js", "frontend-framework", "html", "css", "ui-pattern"},
    ".tsx": {"js", "frontend-framework", "html", "css", "ui-pattern"},
    ".vue": {"js", "frontend-framework", "html", "css", "ui-pattern"},
    ".html": {"html", "a11y", "ui-pattern", "css"},
    ".css": {"css", "ui-pattern"},
    ".scss": {"css", "ui-pattern"},
    ".md": {"docs"},
    ".yml": {"dx", "docs"},
    ".yaml": {"dx", "docs"},
    ".toml": {"dx"},
    ".json": {"dx"},
}


def classify_for_subtask(
    *,
    rules: list[Rule],
    file_hints: list[str],
    languages: list[str],
    action_hint: str = "",
    top_n: int = 12,
) -> list[Rule]:
    """Pick the most relevant rules for a given subtask.

    Strategy:
      1. Always include "universal" rules (security, error handling, naming —
         the rules that apply regardless of language).
      2. Add tags inferred from file extensions in ``file_hints``.
      3. Add tags inferred from ``languages`` list (analyzer output).
      4. Score each rule by tag overlap with the active set; ties broken
         by lower rule id (rules earlier in the file are typically more
         severe by the file's own ordering).
      5. Return top-N.

    Deterministic: same inputs → same output. No LLM calls.
    """
    if not rules:
        return []

    active_tags: set[str] = {TAGS_UNIVERSAL}

    # File-extension-driven tags
    for hint in file_hints:
        ext = Path(hint).suffix.lower()
        if ext in _EXT_TO_TAGS:
            active_tags |= _EXT_TO_TAGS[ext]

    # Language-driven tags (analyzer-detected)
    for lang in languages:
        L = lang.lower()
        if L in ("javascript", "typescript", "js", "ts"):
            active_tags |= {"js", "test"}
        elif L in ("python",):
            active_tags |= {"test", "perf"}
        elif L in ("rust",):
            active_tags |= {"test", "perf"}
        elif L in ("go", "golang"):
            active_tags |= {"test", "perf"}
        elif L in ("css", "scss", "sass"):
            active_tags |= {"css", "ui-pattern"}
        elif L in ("html",):
            active_tags |= {"html", "a11y", "ui-pattern"}

    # Action-specific tags
    a = action_hint.lower()
    if a in ("create", "modify"):
        # creating/modifying files — naming, comments, structure all relevant
        active_tags |= {"docs"}
    # delete actions: keep universal only — no point flagging UI patterns
    # on a deletion subtask.

    # Score by tag overlap.
    #
    # Two-tier scoring (caught during sanity pass):
    #   * Truly-universal rules (tags == {universal}) are RELEVANT but
    #     low-priority. Score -1. They fill slots only when no
    #     domain-specific rule competes for the same slot.
    #   * Domain-specific rules (tags include js/css/a11y/etc) score by
    #     how many active domain tags they overlap, with a +1 baseline
    #     so even a single overlap (-2) outranks a truly-universal (-1).
    #
    # Without this asymmetry, the top-N gets monopolised by universal
    # rules and Rust subtasks see the same prompt as JS subtasks — that
    # defeats the whole point of the classifier.
    scored: list[tuple[int, int, Rule]] = []
    non_universal_active = active_tags - {TAGS_UNIVERSAL}
    for r in rules:
        non_universal_rule_tags = r.tags - {TAGS_UNIVERSAL}
        if not non_universal_rule_tags:
            # Truly universal — relevant but low priority (will fill
            # tail slots after the domain-specific picks).
            scored.append((-1, r.id, r))
            continue
        overlap = len(non_universal_rule_tags & non_universal_active)
        if overlap == 0:
            # Specific-domain rule, none of its domains match this subtask.
            continue
        # Domain match: -(overlap + 1) so even a single match (-2) beats
        # a truly universal (-1).
        scored.append((-(overlap + 1), r.id, r))

    scored.sort()  # best score first, then lowest id as tiebreak
    return [r for _, _, r in scored[:top_n]]


# ── Injection ───────────────────────────────────────────────────────────────


_INJECTION_HEADER = (
    "CRITICAL anti-patterns to AVOID in this patch (auto-selected for relevance):\n"
)
_INJECTION_FOOTER = (
    "\nA patch that violates any of these will likely be rejected by the "
    "downstream critic panel and devil's-advocate review."
)


def format_for_injection(rules: list[Rule]) -> str:
    """Render selected rules as a punchy bullet list ready to paste into a
    system prompt. Empty list → empty string (caller can no-op the inject)."""
    if not rules:
        return ""
    body = "\n".join(r.punchy() for r in rules)
    return _INJECTION_HEADER + body + _INJECTION_FOOTER
