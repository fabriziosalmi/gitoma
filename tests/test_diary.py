"""Tests for the PHASE 7 diary hook (gitoma.cli.diary).

Pure-function tests + frontmatter sanity. The git-clone-and-push
path is exercised end-to-end via mocked subprocess in the round-
trip test; we don't hit a real remote in unit tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gitoma.cli.diary import (
    DiaryConfig,
    DiaryWriteResult,
    _compose_entry,
    _derive_verdict,
    _extract_guard_firings,
    _slugify,
    write_diary_entry,
)


# ── DiaryConfig.from_env ──────────────────────────────────────────


def test_from_env_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITOMA_DIARY_REPO", raising=False)
    monkeypatch.delenv("GITOMA_DIARY_TOKEN", raising=False)
    assert DiaryConfig.from_env() is None


def test_from_env_returns_none_when_only_one_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_DIARY_REPO", "owner/repo")
    monkeypatch.delenv("GITOMA_DIARY_TOKEN", raising=False)
    assert DiaryConfig.from_env() is None
    monkeypatch.setenv("GITOMA_DIARY_TOKEN", "ghp_x")
    monkeypatch.delenv("GITOMA_DIARY_REPO", raising=False)
    assert DiaryConfig.from_env() is None


def test_from_env_rejects_repo_without_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_DIARY_REPO", "no-slash")
    monkeypatch.setenv("GITOMA_DIARY_TOKEN", "ghp_x")
    assert DiaryConfig.from_env() is None


def test_from_env_returns_config_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITOMA_DIARY_REPO", "fabgpt-coder/log")
    monkeypatch.setenv("GITOMA_DIARY_TOKEN", "ghp_secret")
    cfg = DiaryConfig.from_env()
    assert cfg is not None
    assert cfg.repo == "fabgpt-coder/log"
    assert cfg.token == "ghp_secret"


# ── _slugify ──────────────────────────────────────────────────────


@pytest.mark.parametrize("inp,expected", [
    ("simple", "simple"),
    ("Hello World", "hello-world"),
    ("trim/slashes/here", "trim-slashes-here"),
    ("---multi---hyphens---", "multi-hyphens"),
    ("with.dots+plus*stars", "with-dots-plus-stars"),
    ("UPPER_case", "upper-case"),
    ("", "untitled"),
    ("!!!", "untitled"),
])
def test_slugify(inp: str, expected: str) -> None:
    assert _slugify(inp) == expected


def test_slugify_caps_length() -> None:
    out = _slugify("a" * 100, max_len=20)
    assert len(out) <= 20
    assert out == "a" * 20


# ── _derive_verdict ───────────────────────────────────────────────


def test_verdict_no_plan() -> None:
    assert _derive_verdict(plan_total=0, subtasks_done=0, pr_url="") == "no-plan"


def test_verdict_failed() -> None:
    assert _derive_verdict(plan_total=5, subtasks_done=0, pr_url="https://x/pr/1") == "failed"


def test_verdict_partial() -> None:
    assert _derive_verdict(plan_total=5, subtasks_done=3, pr_url="https://x/pr/1") == "partial"


def test_verdict_clean() -> None:
    assert _derive_verdict(plan_total=5, subtasks_done=5, pr_url="https://x/pr/1") == "clean"


def test_verdict_clean_requires_pr() -> None:
    """All subtasks done but no PR URL → still partial; the verdict
    captures both signals."""
    assert _derive_verdict(plan_total=5, subtasks_done=5, pr_url="") == "partial"


# ── _extract_guard_firings ────────────────────────────────────────


def test_extract_guard_firings_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _extract_guard_firings(tmp_path / "nope.jsonl") == []


def test_extract_guard_firings_handles_no_path() -> None:
    assert _extract_guard_firings(None) == []


def test_extract_guard_firings_picks_critic_events(tmp_path: Path) -> None:
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join([
        json.dumps({"event": "phase.start"}),
        json.dumps({"event": "critic_g16_dead_code.fail"}),
        json.dumps({"event": "worker.subtask.done"}),
        json.dumps({"event": "critic_g19_echo.fail"}),
        json.dumps({"event": "critic_g16_dead_code.fail"}),  # duplicate
    ]))
    out = _extract_guard_firings(p)
    assert out == ["critic_g16_dead_code.fail", "critic_g19_echo.fail"]


def test_extract_guard_firings_caps_at_12(tmp_path: Path) -> None:
    p = tmp_path / "trace.jsonl"
    lines = [
        json.dumps({"event": f"critic_X{i}.fail"})
        for i in range(20)
    ]
    p.write_text("\n".join(lines))
    out = _extract_guard_firings(p)
    assert len(out) == 12


def test_extract_guard_firings_tolerates_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join([
        "not-json garbage",
        json.dumps({"event": "critic_g18.fail"}),
        "{another bad line",
    ]))
    assert _extract_guard_firings(p) == ["critic_g18.fail"]


# ── _compose_entry ────────────────────────────────────────────────


def _stub_state(**overrides: object) -> SimpleNamespace:
    base = dict(
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        branch="gitoma/improve-001",
        task_plan={
            "tasks": [
                {
                    "id": "T001",
                    "subtasks": [
                        {"id": "T001-S01", "status": "completed"},
                        {"id": "T001-S02", "status": "failed"},
                    ],
                },
                {
                    "id": "T002",
                    "subtasks": [
                        {"id": "T002-S01", "status": "completed"},
                    ],
                },
            ]
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_plan(total_tasks: int, total_subtasks: int, llm_model: str = "qwen3-8b") -> SimpleNamespace:
    return SimpleNamespace(
        total_tasks=total_tasks,
        total_subtasks=total_subtasks,
        llm_model=llm_model,
    )


def _stub_config(model: str = "gemma-4-e4b-it-mlx",
                 base_url: str = "http://100.98.112.23:1234/v1") -> SimpleNamespace:
    return SimpleNamespace(
        lmstudio=SimpleNamespace(model=model, base_url=base_url),
    )


def test_compose_entry_filename_shape() -> None:
    fn, _ = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(2, 3),
        config=_stub_config(),
        guard_firings=[],
        now=datetime(2026, 4, 28, 22, 15, tzinfo=timezone.utc),
    )
    assert fn.startswith("entries/2026-04-28-2215-")
    assert fn.endswith(".md")
    assert "owner-repo" in fn
    assert "gitoma-improve-001" in fn


def test_compose_entry_frontmatter_basics() -> None:
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(2, 3),
        config=_stub_config(),
        guard_firings=["critic_g16_dead_code.fail"],
    )
    assert content.startswith("---\n")
    assert "repo: owner/repo" in content
    assert "branch: gitoma/improve-001" in content
    assert "pr: 42" in content
    assert "pr_url: https://github.com/owner/repo/pull/42" in content
    assert "model: gemma-4-e4b-it-mlx" in content
    assert "endpoint: http://100.98.112.23:1234/v1" in content
    assert "plan_tasks: 2" in content
    assert "plan_subtasks: 3" in content
    assert "subtasks_done: 2/3" in content   # 2 completed, 1 failed
    assert "guards_fired:" in content
    assert "  - critic_g16_dead_code.fail" in content


def test_compose_entry_verdict_partial() -> None:
    """2/3 subtasks done with PR open → partial."""
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(2, 3),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "verdict: partial" in content


def test_compose_entry_no_pr_uses_null() -> None:
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(pr_number=None, pr_url=""),
        plan=_stub_plan(1, 1),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "pr: null" in content
    assert "pr_url: null" in content


def test_compose_entry_plan_source_curated() -> None:
    """When the plan was loaded from file, llm_model is stamped
    `plan-from-file:<name>` and we surface it in plan_source."""
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(1, 1, llm_model="plan-from-file:my-tasks.json"),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "plan_source: plan-from-file:my-tasks.json" in content


def test_compose_entry_plan_source_llm() -> None:
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(1, 1, llm_model="qwen/qwen3-8b"),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "plan_source: llm" in content


def test_compose_entry_empty_guards_list() -> None:
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(1, 1),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "guards_fired: []" in content


def test_compose_entry_empty_branch_uses_no_branch_slug() -> None:
    """Defensive: an empty branch must still produce a valid filename."""
    fn, _ = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(branch=""),
        plan=_stub_plan(1, 1),
        config=_stub_config(),
        guard_firings=[],
    )
    assert "no-branch" in fn


def test_compose_entry_no_pat_shaped_string_in_output() -> None:
    """Sanity: no PAT-shaped string (ghp_/gho_/ghr_/ghs_/github_pat_)
    must ever appear in the entry. The body's literal mention of
    'GITOMA_DIARY_TOKEN' as an env-var NAME is fine — what we
    forbid is leaking actual tokens."""
    _, content = _compose_entry(
        repo_url="https://github.com/owner/repo",
        state=_stub_state(),
        plan=_stub_plan(1, 1),
        config=_stub_config(base_url="http://localhost:1234/v1"),
        guard_firings=[],
    )
    for prefix in ("ghp_", "gho_", "ghr_", "ghs_", "github_pat_"):
        assert prefix not in content, (
            f"diary entry leaked a {prefix}* PAT-shaped string"
        )


# ── write_diary_entry — error path (no IO needed) ─────────────────


def test_write_diary_entry_returns_failure_on_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When clone/commit/push fails, write_diary_entry MUST NOT raise."""
    cfg = DiaryConfig(repo="owner/log", token="ghp_x")
    state = _stub_state()
    plan = _stub_plan(1, 1)
    config = _stub_config()

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated git failure")

    monkeypatch.setattr("gitoma.cli.diary._commit_and_push", _boom)
    result = write_diary_entry(
        diary_config=cfg, repo_url="https://github.com/owner/repo",
        state=state, plan=plan, config=config, trace_path=None,
    )
    assert isinstance(result, DiaryWriteResult)
    assert result.ok is False
    assert "simulated git failure" in result.error
