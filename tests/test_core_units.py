"""Unit tests for core primitives (config, state, task plan, patcher, cache)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.core.config import (
    BotConfig,
    Config,
    GitHubConfig,
    LMStudioConfig,
    load_config,
    save_config_value,
)
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import AgentPhase, AgentState
from gitoma.mcp.cache import GitHubContextCache
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.worker.patcher import PatchError, apply_patches


# ── config ────────────────────────────────────────────────────────────────────


def test_config_validate_reports_missing_token():
    cfg = Config(github=GitHubConfig(token=""), bot=BotConfig(), lmstudio=LMStudioConfig())
    errors = cfg.validate()
    assert any("GITHUB_TOKEN" in e for e in errors)


def test_config_validate_ok_with_token():
    cfg = Config(github=GitHubConfig(token="ghp_x"), bot=BotConfig(), lmstudio=LMStudioConfig())
    assert cfg.validate() == []


def test_save_and_load_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr("gitoma.core.config.ENV_FILE", tmp_path / ".env")
    # Neutralize dotenv so the developer's local .env doesn't leak into the test.
    monkeypatch.setattr("gitoma.core.config.load_dotenv", lambda *a, **kw: False)
    # Clear potentially interfering env vars
    for k in ("GITHUB_TOKEN", "BOT_NAME", "LM_STUDIO_MODEL", "GITOMA_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    save_config_value("GITHUB_TOKEN", "ghp_saved")
    save_config_value("LM_STUDIO_MODEL", "test-model")

    cfg = load_config()
    assert cfg.github.token == "ghp_saved"
    assert cfg.lmstudio.model == "test-model"


def test_save_config_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.CONFIG_FILE", tmp_path / "config.toml")
    with pytest.raises(ValueError, match="Unknown config key"):
        save_config_value("NOT_A_KEY", "x")


# ── state ─────────────────────────────────────────────────────────────────────


def test_agent_state_advance_updates_phase_and_timestamp():
    s = AgentState(repo_url="u", owner="o", name="r", branch="b")
    t0 = s.updated_at
    s.advance(AgentPhase.PLANNING)
    assert s.phase == AgentPhase.PLANNING
    assert s.updated_at >= t0


def test_agent_state_roundtrip_via_dict():
    s = AgentState(repo_url="u", owner="o", name="r", branch="b", pr_number=42)
    s.advance(AgentPhase.WORKING)
    restored = AgentState.from_dict(s.to_dict())
    assert restored.pr_number == 42
    assert restored.phase == AgentPhase.WORKING
    assert restored.slug == "o__r"


# ── parse_repo_url ────────────────────────────────────────────────────────────


def test_parse_repo_url_accepts_https_and_git_variants():
    assert parse_repo_url("https://github.com/foo/bar") == ("foo", "bar")
    assert parse_repo_url("https://github.com/foo/bar.git") == ("foo", "bar")


def test_parse_repo_url_rejects_junk():
    with pytest.raises(ValueError):
        parse_repo_url("not a url")


# ── TaskPlan / Task ───────────────────────────────────────────────────────────


def test_task_progress_is_zero_when_no_subtasks():
    t = Task(id="t1", title="", priority=1, metric="m", description="")
    assert t.progress == 0.0
    assert t.total_subtasks == 0


def test_task_progress_counts_completed_subtasks():
    sub_a = SubTask(id="s1", title="", description="", file_hints=[], status="completed")
    sub_b = SubTask(id="s2", title="", description="", file_hints=[])
    t = Task(id="t1", title="", priority=1, metric="m", description="", subtasks=[sub_a, sub_b])
    assert t.progress == 0.5
    assert t.completed_subtasks == 1


def test_taskplan_roundtrip_preserves_subtasks():
    plan = TaskPlan(
        tasks=[
            Task(
                id="t1",
                title="a",
                priority=1,
                metric="m",
                description="",
                subtasks=[SubTask(id="s1", title="", description="", file_hints=["x.py"])],
            )
        ]
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert len(restored.tasks) == 1
    assert len(restored.tasks[0].subtasks) == 1
    assert restored.tasks[0].subtasks[0].file_hints == ["x.py"]


# ── patcher ───────────────────────────────────────────────────────────────────


def test_apply_patches_creates_and_modifies_files(tmp_path: Path):
    touched = apply_patches(
        tmp_path,
        [
            {"action": "create", "path": "sub/new.py", "content": "print(1)\n"},
            {"action": "modify", "path": "sub/new.py", "content": "print(2)\n"},
        ],
    )
    assert touched == ["sub/new.py", "sub/new.py"]
    assert (tmp_path / "sub/new.py").read_text() == "print(2)\n"


def test_apply_patches_deletes(tmp_path: Path):
    target = tmp_path / "gone.txt"
    target.write_text("x")
    touched = apply_patches(tmp_path, [{"action": "delete", "path": "gone.txt"}])
    assert touched == ["gone.txt"]
    assert not target.exists()


def test_apply_patches_blocks_traversal(tmp_path: Path):
    with pytest.raises(PatchError, match="traversal"):
        apply_patches(tmp_path, [{"action": "create", "path": "../evil.py", "content": ""}])


def test_apply_patches_rejects_unknown_action(tmp_path: Path):
    with pytest.raises(PatchError, match="Unknown patch action"):
        apply_patches(tmp_path, [{"action": "teleport", "path": "a.py"}])


def test_apply_patches_skips_patches_with_empty_path(tmp_path: Path):
    touched = apply_patches(tmp_path, [{"action": "create", "path": "", "content": "x"}])
    assert touched == []


# ── MCP cache ────────────────────────────────────────────────────────────────


def test_cache_set_and_get():
    c = GitHubContextCache(max_entries=4, default_ttl=60.0)
    c.set("k", "v")
    assert c.get("k") == "v"


def test_cache_expiry_evicts_stale_entries():
    c = GitHubContextCache(max_entries=4, default_ttl=60.0)
    c.set("k", "v", ttl=0.001)
    import time

    time.sleep(0.01)
    assert c.get("k") is None


def test_cache_lru_eviction():
    c = GitHubContextCache(max_entries=2, default_ttl=60.0)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)  # evicts "a"
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_cache_invalidate_prefix():
    c = GitHubContextCache(max_entries=8, default_ttl=60.0)
    c.set("file:foo/bar:x", "a")
    c.set("file:foo/bar:y", "b")
    c.set("ci:foo/bar", "c")
    removed = c.invalidate_prefix("file:foo/bar")
    assert removed == 2
    assert c.get("ci:foo/bar") == "c"


def test_cache_stats_tracks_hits_and_misses():
    c = GitHubContextCache(max_entries=4, default_ttl=60.0)
    c.set("k", "v")
    c.get("k")  # hit
    c.get("miss")  # miss
    stats = c.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1
