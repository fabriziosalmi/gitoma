"""Tests for ``_attempt_json_repair`` — best-effort in-process repair
of LLM JSON authoring slop. Saves a re-prompt round-trip when the
slop is deterministic to fix (trailing commas, unescaped content
quotes, bare newlines inside strings).

Caught live in the rung-3 series: Defender prompts that quoted a
test name with double quotes ("the test \"asserts\" X" written
without backslashes) broke the entire Q&A phase JSON parse and
forced a retry."""

from __future__ import annotations

import json

import pytest

from gitoma.planner.llm_client import (
    _attempt_json_repair,
    _escape_bare_quotes,
    _strip_trailing_commas,
)


# ── Trailing-comma strip ────────────────────────────────────────────────


def test_strip_trailing_comma_in_object() -> None:
    s = '{"a": 1, "b": 2,}'
    out = _strip_trailing_commas(s)
    assert json.loads(out) == {"a": 1, "b": 2}


def test_strip_trailing_comma_in_array() -> None:
    s = '{"items": [1, 2, 3,]}'
    out = _strip_trailing_commas(s)
    assert json.loads(out) == {"items": [1, 2, 3]}


def test_strip_trailing_comma_with_whitespace() -> None:
    s = '{"a": 1  ,\n  }'
    out = _strip_trailing_commas(s)
    assert json.loads(out) == {"a": 1}


def test_strip_does_not_touch_commas_inside_strings() -> None:
    """A comma followed by ``}`` INSIDE a string literal must survive
    — it's content, not a trailing comma."""
    s = '{"msg": "hello,}", "n": 1}'
    out = _strip_trailing_commas(s)
    # Not a JSONDecodeError, content preserved verbatim.
    assert json.loads(out) == {"msg": "hello,}", "n": 1}


def test_strip_handles_escaped_quotes_in_strings() -> None:
    s = '{"msg": "she said \\"hi,\\" loudly"}'
    out = _strip_trailing_commas(s)
    assert json.loads(out)["msg"] == 'she said "hi," loudly'


def test_strip_idempotent_on_clean_json() -> None:
    """A well-formed JSON must round-trip unchanged."""
    s = '{"a": [1, 2], "b": {"c": 3}}'
    assert _strip_trailing_commas(s) == s


# ── Bare-quote escape ───────────────────────────────────────────────────


def test_escape_bare_quote_inside_string() -> None:
    """The exact rung-3 Defender failure mode: an unescaped quote in
    the middle of a string value."""
    s = '{"rationale": "the test "asserts" something"}'
    out = _escape_bare_quotes(s)
    assert json.loads(out) == {"rationale": 'the test "asserts" something'}


def test_escape_preserves_already_correct_strings() -> None:
    s = '{"rationale": "the test \\"asserts\\" something"}'
    out = _escape_bare_quotes(s)
    assert json.loads(out)["rationale"] == 'the test "asserts" something'


def test_escape_handles_multiple_bare_quotes() -> None:
    s = '{"a": "one"two"three"}'
    out = _escape_bare_quotes(s)
    assert json.loads(out) == {"a": 'one"two"three'}


def test_escape_handles_string_keys_correctly() -> None:
    """Keys are also strings — they must NOT be misidentified as
    content. ``"key":`` ends a string (the colon is a structural
    boundary)."""
    s = '{"key1": "value1", "key2": "v2"}'
    assert _escape_bare_quotes(s) == s
    assert json.loads(_escape_bare_quotes(s)) == {"key1": "value1", "key2": "v2"}


def test_escape_bare_newline_in_string() -> None:
    """Raw newlines inside a string literal are invalid JSON. The
    repair escapes them to ``\\n``."""
    s = '{"msg": "line one\nline two"}'
    out = _escape_bare_quotes(s)
    assert json.loads(out) == {"msg": "line one\nline two"}


def test_escape_idempotent_on_clean_json() -> None:
    s = '{"a": "ok", "b": [1, 2, 3]}'
    assert _escape_bare_quotes(s) == s


# ── Combined repair ─────────────────────────────────────────────────────


def test_combined_repair_handles_both_slop_types() -> None:
    """Trailing comma AND bare quotes in one payload — the actual
    shape of typical 4B-class model slop."""
    s = '{"rationale": "the test "asserts" something",}'
    out = _attempt_json_repair(s)
    assert json.loads(out) == {"rationale": 'the test "asserts" something'}


def test_combined_repair_idempotent_on_valid_json() -> None:
    """A well-formed JSON dict from the Defender must NEVER be
    modified — the repair is a fallback, not a transformer that
    runs on every parse. False-positives here would silently
    change Defender output."""
    s = json.dumps({
        "answers": [
            {"id": "Q1_evidence", "verdict": "handled",
             "evidence_loc": "src/db.py:42",
             "rationale": 'parameterised query — see "?" placeholder'},
        ],
        "revised_patches": [],
    })
    assert _attempt_json_repair(s) == s


def test_repair_preserves_unicode() -> None:
    """Non-ASCII content (emoji, Italian, etc.) must round-trip
    untouched. Models occasionally emit it; we must not corrupt it."""
    s = '{"msg": "ciò è "perfetto" 🎯",}'
    out = _attempt_json_repair(s)
    parsed = json.loads(out)
    assert parsed == {"msg": 'ciò è "perfetto" 🎯'}


def test_repair_handles_defender_qa_shape() -> None:
    """A realistic Defender output: list of dict answers, each with
    string fields. Quote slop in any rationale must repair without
    destroying the surrounding structure."""
    s = '''
    {
      "answers": [
        {"id": "Q1_evidence", "verdict": "handled",
         "evidence_loc": "src/db.py:42",
         "rationale": "uses "?" placeholder"},
        {"id": "Q2_edge", "verdict": "gap",
         "evidence_loc": null,
         "rationale": "no test for "alice'; --" payload"}
      ],
      "revised_patches": []
    }
    '''.strip()
    out = _attempt_json_repair(s)
    parsed = json.loads(out)
    assert len(parsed["answers"]) == 2
    assert parsed["answers"][0]["rationale"] == 'uses "?" placeholder'
    assert "alice'; --" in parsed["answers"][1]["rationale"]


# ── Negative: things repair should NOT touch ────────────────────────────


def test_repair_does_not_swallow_genuinely_broken_json() -> None:
    """Some slop is irreparable from a single pass (missing key,
    wrong type, structural truncation). The repair must surrender
    cleanly — leave the input alone or return something that still
    fails to parse — rather than silently swap in a wrong value."""
    s = '{"a": 1, "b":}'  # value missing
    out = _attempt_json_repair(s)
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_repair_safe_on_empty_string() -> None:
    assert _attempt_json_repair("") == ""


def test_repair_safe_on_short_non_json() -> None:
    """Short non-JSON input shouldn't crash the repair function."""
    assert _attempt_json_repair("hello") == "hello"
