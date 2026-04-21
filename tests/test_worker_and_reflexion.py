"""Unit tests for WorkerAgent and CIDiagnosticAgent.

These were flagged as "zero coverage on the modules that actually mutate
the user's repo" in the draconian audit. The tests exercise the control
flow (callbacks, status transitions, retry/approve/reject loops) against
a mocked LLM + mocked GitRepo, since spinning up real clones per-test is
overkill for behavioral coverage.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from gitoma.core.config import BotConfig, Config, GitHubConfig, LMStudioConfig
from gitoma.core.state import AgentState
from gitoma.planner.task import SubTask, Task, TaskPlan


# ── WorkerAgent ─────────────────────────────────────────────────────────────


@pytest.fixture
def worker_state(tmp_path, monkeypatch):
    from gitoma.core import state as state_module
    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path)
    return AgentState(repo_url="u", owner="o", name="r", branch="b")


@pytest.fixture
def fake_config():
    return Config(
        github=GitHubConfig(token="x"),
        bot=BotConfig(name="bot", email="bot@x"),
        lmstudio=LMStudioConfig(),
    )


def _mock_git(root: Path) -> mock.MagicMock:
    git_repo = mock.MagicMock()
    git_repo.root = root
    git_repo.name = "r"
    git_repo.file_tree.return_value = ["a.py", "b.py"]
    git_repo.detect_languages.return_value = ["Python"]
    git_repo.read_file.side_effect = lambda p: "pass\n" if p == "a.py" else None
    return git_repo


def _make_plan(subtask_titles: list[list[str]]) -> TaskPlan:
    tasks = []
    for i, subs in enumerate(subtask_titles):
        tasks.append(Task(
            id=f"t{i}", title=f"task-{i}", priority=1, metric="m", description="",
            subtasks=[
                SubTask(id=f"s{i}_{j}", title=t, description="", file_hints=["a.py"])
                for j, t in enumerate(subs)
            ],
        ))
    return TaskPlan(tasks=tasks)


def test_worker_executes_subtasks_and_fires_callbacks(tmp_path, worker_state, fake_config):
    from gitoma.worker.worker import WorkerAgent

    llm = mock.MagicMock()
    llm.chat_json.return_value = {
        "patches": [{"action": "create", "path": "new.py", "content": "x\n"}],
        "commit_message": "chore: foo [gitoma]",
    }
    git_repo = _mock_git(tmp_path)
    worker = WorkerAgent(llm, git_repo, fake_config, worker_state)
    with mock.patch.object(worker._committer, "commit_patches", return_value="abc123"):
        started: list[str] = []
        done: list[tuple[str, str | None]] = []
        plan = _make_plan([["sub-a"], ["sub-b"]])
        worker.execute(
            plan,
            on_task_start=lambda t: started.append(t.id),
            on_subtask_done=lambda t, s, sha: done.append((s.id, sha)),
        )

    assert started == ["t0", "t1"]
    assert done == [("s0_0", "abc123"), ("s1_0", "abc123")]
    assert all(t.status == "completed" for t in plan.tasks)
    assert all(s.commit_sha == "abc123" for t in plan.tasks for s in t.subtasks)


def test_worker_records_failure_per_subtask_and_marks_task_failed(
    tmp_path, worker_state, fake_config
):
    """A single subtask failure must not abort the whole plan — the task
    with the failure is marked failed, others keep running. The error
    message is truncated to 200 chars so a runaway LLM traceback doesn't
    blow up the state file."""
    from gitoma.worker.worker import WorkerAgent

    llm = mock.MagicMock()
    llm.chat_json.side_effect = [
        {"patches": [{"action": "create", "path": "ok.py", "content": "ok\n"}]},
        RuntimeError("x" * 500),  # second subtask blows up
        {"patches": [{"action": "create", "path": "ok2.py", "content": "ok\n"}]},
    ]
    git_repo = _mock_git(tmp_path)
    worker = WorkerAgent(llm, git_repo, fake_config, worker_state)

    errors: list[tuple[str, str]] = []
    with mock.patch.object(worker._committer, "commit_patches", return_value="sha"):
        plan = _make_plan([["a", "b"], ["c"]])
        worker.execute(
            plan, on_subtask_error=lambda t, s, msg: errors.append((s.id, msg))
        )

    assert plan.tasks[0].status == "failed"
    assert plan.tasks[0].subtasks[0].status == "completed"
    assert plan.tasks[0].subtasks[1].status == "failed"
    assert plan.tasks[1].status == "completed"
    assert errors and errors[0][0] == "s0_1"
    assert len(errors[0][1]) <= 200


def test_worker_skips_already_completed_subtasks(tmp_path, worker_state, fake_config):
    """Resuming a run: subtasks already marked completed must not call the
    LLM again — otherwise we'd re-commit the same work."""
    from gitoma.worker.worker import WorkerAgent

    llm = mock.MagicMock()
    llm.chat_json.return_value = {
        "patches": [{"action": "create", "path": "x.py", "content": "x"}],
    }
    git_repo = _mock_git(tmp_path)
    plan = _make_plan([["first", "second"]])
    plan.tasks[0].subtasks[0].status = "completed"
    plan.tasks[0].subtasks[0].commit_sha = "prior"

    worker = WorkerAgent(llm, git_repo, fake_config, worker_state)
    with mock.patch.object(worker._committer, "commit_patches", return_value="new-sha"):
        worker.execute(plan)

    assert llm.chat_json.call_count == 1  # only the second subtask
    assert plan.tasks[0].subtasks[0].commit_sha == "prior"
    assert plan.tasks[0].subtasks[1].commit_sha == "new-sha"


def test_worker_raises_when_llm_returns_no_patches(tmp_path, worker_state, fake_config):
    from gitoma.worker.worker import WorkerAgent

    llm = mock.MagicMock()
    llm.chat_json.return_value = {"patches": [], "commit_message": "noop"}
    git_repo = _mock_git(tmp_path)
    worker = WorkerAgent(llm, git_repo, fake_config, worker_state)

    errors: list[str] = []
    plan = _make_plan([["s"]])
    worker.execute(plan, on_subtask_error=lambda t, s, m: errors.append(m))

    assert plan.tasks[0].subtasks[0].status == "failed"
    assert "no patches" in errors[0].lower()


def test_worker_appends_gitoma_tag_to_commit_msg(tmp_path, worker_state, fake_config):
    from gitoma.worker.worker import WorkerAgent

    llm = mock.MagicMock()
    llm.chat_json.return_value = {
        "patches": [{"action": "create", "path": "x.py", "content": "x"}],
        "commit_message": "feat: something",  # no [gitoma]
    }
    git_repo = _mock_git(tmp_path)
    worker = WorkerAgent(llm, git_repo, fake_config, worker_state)

    with mock.patch.object(worker._committer, "commit_patches", return_value="s") as cp:
        worker.execute(_make_plan([["s"]]))

    # commit message received by committer must include [gitoma]
    called_msg = cp.call_args[0][1]
    assert "[gitoma]" in called_msg


# ── CIDiagnosticAgent (reflexion) ───────────────────────────────────────────


@pytest.fixture
def reflexion_config():
    return Config(
        github=GitHubConfig(token="x"),
        bot=BotConfig(name="ci-bot", email="ci@x"),
        lmstudio=LMStudioConfig(critic_model="critic-model"),
    )


def test_reflexion_returns_early_when_no_failed_jobs(reflexion_config):
    """No CI failures → no LLM calls, no commits. Fast-path."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    with mock.patch("gitoma.review.reflexion.GitHubClient") as gh_cls, \
         mock.patch("gitoma.review.reflexion.LLMClient") as llm_cls, \
         mock.patch("gitoma.review.observer.ObserverAgent"):
        gh_cls.return_value.get_failed_jobs.return_value = []
        agent = CIDiagnosticAgent(reflexion_config)
        agent.analyze_and_fix("https://github.com/o/r", "main")

        llm_cls.return_value.chat.assert_not_called()


def test_reflexion_applies_patch_when_critic_approves(tmp_path, reflexion_config):
    """Critic says yes → GitRepo.clone, fix file, commit, push are all called."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    (tmp_path / "Makefile").write_text("build:\n\techo bad\n")

    gh = mock.MagicMock()
    gh.get_failed_jobs.return_value = [{"job_id": 1, "name": "build"}]
    gh.get_job_log.return_value = "Error: echo bad failed"

    fixer_resp = '{"fixes": [{"file": "Makefile", "find": "bad", "replace": "good"}]}'
    critic_resp = '{"approved": true, "feedback": "OK"}'

    git_repo_cm = mock.MagicMock()
    git_repo_cm.root = tmp_path
    git_repo_cm.repo.git.checkout = mock.MagicMock()
    git_repo_cm.stage_all = mock.MagicMock()
    git_repo_cm.commit = mock.MagicMock()
    git_repo_cm.push = mock.MagicMock()
    git_repo_cm.__enter__ = mock.MagicMock(return_value=git_repo_cm)
    git_repo_cm.__exit__ = mock.MagicMock(return_value=False)

    with mock.patch("gitoma.review.reflexion.GitHubClient", return_value=gh), \
         mock.patch("gitoma.review.reflexion.LLMClient") as llm_cls, \
         mock.patch("gitoma.review.reflexion.GitRepo", return_value=git_repo_cm), \
         mock.patch("gitoma.review.observer.ObserverAgent"):

        llm_cls.return_value.chat.side_effect = [fixer_resp, critic_resp]
        agent = CIDiagnosticAgent(reflexion_config)
        agent.analyze_and_fix("https://github.com/o/r", "main")

    assert (tmp_path / "Makefile").read_text() == "build:\n\techo good\n"
    git_repo_cm.commit.assert_called_once()
    git_repo_cm.push.assert_called_once_with("main", force=False)


def test_reflexion_does_not_commit_when_critic_rejects(tmp_path, reflexion_config):
    """Critic says no → no GitRepo instantiation, no commit, no push. The
    retry loop kicks in but stays within MAX_RETRIES."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    gh = mock.MagicMock()
    gh.get_failed_jobs.return_value = [{"job_id": 1, "name": "build"}]
    gh.get_job_log.return_value = "Error"

    fixer_resp = '{"fixes": [{"file": "x.py", "find": "a", "replace": "b"}]}'
    critic_resp = '{"approved": false, "feedback": "bad idea"}'

    with mock.patch("gitoma.review.reflexion.GitHubClient", return_value=gh), \
         mock.patch("gitoma.review.reflexion.LLMClient") as llm_cls, \
         mock.patch("gitoma.review.reflexion.GitRepo") as gr, \
         mock.patch("gitoma.review.reflexion.time.sleep"), \
         mock.patch("gitoma.review.observer.ObserverAgent"):

        # Every retry hits the same approve=False critic.
        llm_cls.return_value.chat.side_effect = [fixer_resp, critic_resp] * 10
        agent = CIDiagnosticAgent(reflexion_config)
        agent.analyze_and_fix("https://github.com/o/r", "main")

        gr.assert_not_called()  # never cloned because never approved
        # Retry loop ran MAX_RETRIES times.
        assert llm_cls.return_value.chat.call_count == 2 * agent.MAX_RETRIES


def test_reflexion_treats_unparseable_fixer_json_as_failed_attempt(reflexion_config):
    """Fixer returns malformed JSON → attempt is dropped (no critic call
    for that iteration), session state records FAILED_FIXER_JSON."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    gh = mock.MagicMock()
    gh.get_failed_jobs.return_value = [{"job_id": 1, "name": "build"}]
    gh.get_job_log.return_value = "logs"

    with mock.patch("gitoma.review.reflexion.GitHubClient", return_value=gh), \
         mock.patch("gitoma.review.reflexion.LLMClient") as llm_cls, \
         mock.patch("gitoma.review.reflexion.time.sleep"), \
         mock.patch("gitoma.review.observer.ObserverAgent"):
        # Fixer always returns garbage.
        llm_cls.return_value.chat.return_value = "not valid json {{{"
        agent = CIDiagnosticAgent(reflexion_config)
        agent.analyze_and_fix("https://github.com/o/r", "main")

        # One fixer call per retry, NO critic calls because we short-circuit.
        assert llm_cls.return_value.chat.call_count == agent.MAX_RETRIES
        assert agent._last_session_data["status"] in {"FAILED_FIXER_JSON", "BREAKER_TRIPPED"}


def test_reflexion_aborts_on_unfetchable_log(reflexion_config):
    """If we can't pull the job log, we abort THAT attempt without burning
    an LLM call on garbage."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    gh = mock.MagicMock()
    gh.get_failed_jobs.return_value = [{"job_id": 1, "name": "build"}]
    gh.get_job_log.return_value = "Could not fetch log: 404"

    with mock.patch("gitoma.review.reflexion.GitHubClient", return_value=gh), \
         mock.patch("gitoma.review.reflexion.LLMClient") as llm_cls, \
         mock.patch("gitoma.review.reflexion.time.sleep"), \
         mock.patch("gitoma.review.observer.ObserverAgent"):
        agent = CIDiagnosticAgent(reflexion_config)
        agent.analyze_and_fix("https://github.com/o/r", "main")
        llm_cls.return_value.chat.assert_not_called()
