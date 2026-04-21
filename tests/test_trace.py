"""Tests for the structured tracing module.

The trace's one job is to land every event on disk as valid JSONL with
a stable shape — if it ever corrupts a line, scrubs too aggressively,
or loses events under load, ``gitoma logs`` becomes worthless.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gitoma.core import trace as trace_module
from gitoma.core.trace import NULL_TRACE, current, latest_log_path, open_trace


@pytest.fixture
def log_root(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_module, "_LOG_ROOT", tmp_path / "logs")
    return tmp_path / "logs"


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Basic emission ──────────────────────────────────────────────────────────


def test_open_trace_creates_jsonl_with_open_and_close_records(log_root):
    with open_trace("o__r", label="run") as tr:
        tr.emit("hello", who="world")
    files = list((log_root / "o__r").glob("*.jsonl"))
    assert len(files) == 1
    records = _read_lines(files[0])
    events = [r["event"] for r in records]
    assert events[0] == "trace.open"
    assert "hello" in events
    assert events[-1] == "trace.close"


def test_emit_writes_all_required_fields(log_root):
    with open_trace("o__r") as tr:
        tr.set_phase("WORKING")
        tr.emit("phase.subtask", level="info", id="t1-s2", commit="abc123")
    records = _read_lines(latest_log_path("o__r"))
    tgt = [r for r in records if r["event"] == "phase.subtask"][0]
    for key in ("ts", "slug", "phase", "level", "event", "data"):
        assert key in tgt
    assert tgt["phase"] == "WORKING"
    assert tgt["level"] == "info"
    assert tgt["slug"] == "o__r"
    assert tgt["data"] == {"id": "t1-s2", "commit": "abc123"}


# ── Span timing ─────────────────────────────────────────────────────────────


def test_span_emits_start_and_end_with_duration(log_root):
    with open_trace("o__r") as tr:
        with tr.span("git.clone", url="https://github.com/x/y"):
            pass
    events = [r for r in _read_lines(latest_log_path("o__r")) if r["event"].startswith("git.clone")]
    assert events[0]["event"] == "git.clone.start"
    assert events[-1]["event"] == "git.clone.end"
    assert "duration_ms" in events[-1]["data"]


def test_span_emits_error_record_and_reraises(log_root):
    with pytest.raises(RuntimeError, match="boom"):
        with open_trace("o__r") as tr:
            with tr.span("git.push"):
                raise RuntimeError("boom")
    records = _read_lines(latest_log_path("o__r"))
    err = [r for r in records if r["event"] == "git.push.error"][0]
    assert err["data"]["exc_type"] == "RuntimeError"
    assert err["data"]["exc_msg"] == "boom"
    assert "traceback" in err["data"]


# ── Sensitive-data scrubbing ────────────────────────────────────────────────


def test_sanitize_masks_token_password_secret(log_root):
    with open_trace("o__r") as tr:
        tr.emit(
            "llm.request",
            model="gemma",
            token="ghp_super_secret_xyz",     # should be masked
            api_key="sk-abcdef1234",          # should be masked
            password="qwerty",                # should be masked
            ok_field="visible",
        )
    rec = [r for r in _read_lines(latest_log_path("o__r")) if r["event"] == "llm.request"][0]
    assert rec["data"]["token"].startswith("***")
    assert rec["data"]["token"].endswith("t_xyz") or rec["data"]["token"].endswith("xyz")
    assert rec["data"]["api_key"].startswith("***")
    assert rec["data"]["password"] == "***"[:3] + rec["data"]["password"][3:]  # not cleartext
    assert rec["data"]["ok_field"] == "visible"
    assert "ghp_super_secret_xyz" not in json.dumps(rec)


def test_sanitize_recurses_into_nested_dicts(log_root):
    with open_trace("o__r") as tr:
        tr.emit("http.request", headers={"Authorization": "Bearer ghp_abc", "Accept": "json"})
    rec = [r for r in _read_lines(latest_log_path("o__r")) if r["event"] == "http.request"][0]
    assert "ghp_abc" not in json.dumps(rec)
    assert rec["data"]["headers"]["Accept"] == "json"


# ── current() binding ──────────────────────────────────────────────────────


def test_current_binds_inside_open_trace_and_resets_after(log_root):
    assert current() is NULL_TRACE
    with open_trace("o__r") as tr:
        assert current() is tr
        current().emit("ambient.event")
    assert current() is NULL_TRACE
    records = _read_lines(latest_log_path("o__r"))
    assert any(r["event"] == "ambient.event" for r in records)


def test_nested_open_trace_restores_outer_on_exit(log_root):
    with open_trace("a__a") as outer:
        with open_trace("b__b") as inner:
            assert current() is inner
        assert current() is outer


# ── Concurrency: 10 threads hammering a single trace never corrupt a line ──


def test_concurrent_writers_produce_only_valid_json_lines(log_root):
    with open_trace("stress__test") as tr:
        threads = []
        for i in range(10):
            def writer(idx=i):
                for j in range(50):
                    tr.emit("noise", thread=idx, seq=j)
            t = threading.Thread(target=writer)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    # Every line (open + 500 noise + close) must parse cleanly.
    path = latest_log_path("stress__test")
    raw = path.read_text().splitlines()
    assert len(raw) >= 500
    for line in raw:
        json.loads(line)  # raises if any line is malformed


# ── Retention ──────────────────────────────────────────────────────────────


def test_prune_keeps_only_most_recent_N_per_slug(log_root, monkeypatch):
    monkeypatch.setattr(trace_module, "MAX_RUNS_PER_SLUG", 3)
    # Create 7 trace files in quick succession.
    for _ in range(7):
        with open_trace("o__r"):
            pass
    files = list((log_root / "o__r").glob("*.jsonl"))
    assert len(files) == 3


# ── latest_log_path ─────────────────────────────────────────────────────────


def test_latest_log_path_returns_none_when_no_logs_exist(log_root):
    assert latest_log_path("no__repo") is None


def test_latest_log_path_returns_newest_by_mtime(log_root):
    import time as _time

    with open_trace("o__r"):
        pass
    _time.sleep(0.01)
    with open_trace("o__r"):
        pass
    latest = latest_log_path("o__r")
    all_files = sorted((log_root / "o__r").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    assert latest == all_files[-1]


# ── NullTrace no-ops cleanly ───────────────────────────────────────────────


def test_null_trace_never_raises_and_writes_nothing(log_root):
    # NULL_TRACE has no real file; calling emit / span / set_phase must be safe.
    NULL_TRACE.emit("this.should.be.dropped", any="data")
    NULL_TRACE.set_phase("PLANNING")
    with NULL_TRACE.span("spanning"):
        pass
    # Nothing should have been created under the log root.
    assert not log_root.exists() or not any(log_root.rglob("*.jsonl"))
