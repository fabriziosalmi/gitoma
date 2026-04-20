"""Quick unit tests for the health check utilities."""
import sys
sys.path.insert(0, "/Users/fab/Documents/git/gitoma")

from gitoma.planner.llm_client import (
    _model_matches, _suggest_similar,
    _extract_json, HealthLevel, HealthCheckResult,
)

# ── Fuzzy model matching ──────────────────────────────────────────────────────
assert _model_matches("gemma-4-e2b-it", ["google/gemma-4-e2b-it"]) is True
assert _model_matches("gemma-4-e2b-it", ["gemma-4-e2b-it"]) is True
assert _model_matches("gemma-4-e2b-it", ["llama-3"]) is False
assert _model_matches("google/gemma-4-e2b-it", ["gemma-4-e2b-it"]) is True
assert _model_matches("GEMMA-4-E2B-IT", ["gemma-4-e2b-it"]) is True   # case-insensitive
print("✓ Fuzzy model matching: all cases pass")

# ── Suggestions ───────────────────────────────────────────────────────────────
sug = _suggest_similar("gemma-4-e2b-it", ["google/gemma-3-4b-it", "llama-3-8b", "gemma-3-12b-it"])
assert len(sug) > 0
assert all("gemma" in s for s in sug)
print(f"✓ Model suggestions: {sug}")

# ── JSON extraction ───────────────────────────────────────────────────────────
assert _extract_json('{"a": 1}') == '{"a": 1}'
assert _extract_json('Sure!\n{"a": 1}\nHope that helps') == '{"a": 1}'

fenced = '```json\n{"tasks": []}\n```'
result = _extract_json(fenced)
assert result == '{"tasks": []}', f"Got: {repr(result)}"

fenced2 = '```\n{"tasks": []}\n```'
result2 = _extract_json(fenced2)
assert result2 == '{"tasks": []}', f"Got: {repr(result2)}"

# Nested braces (must find outermost match correctly)
nested = '{"a": {"b": 1}, "c": [1, 2]}'
result3 = _extract_json("Here: " + nested + " end")
assert result3 == nested, f"Got: {repr(result3)}"

# String with brace inside string value
with_str = '{"msg": "hello {world}", "val": 42}'
result4 = _extract_json("result: " + with_str)
assert result4 == with_str, f"Got: {repr(result4)}"
print("✓ JSON extraction: all cases pass")

# ── HealthCheckResult ─────────────────────────────────────────────────────────
ok = HealthCheckResult(level=HealthLevel.OK, message="all good")
assert ok.ok is True
assert ok.failed is False

err = HealthCheckResult(level=HealthLevel.ERROR, message="bad")
assert err.ok is False
assert err.failed is True
print("✓ HealthCheckResult: ok/failed properties correct")

print()
print("All tests passed ✅")
