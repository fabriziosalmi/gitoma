"""Regression tests for the Swiss-watch hardening pass.

Each test maps 1:1 to a confirmed audit finding (B1-B6, H1-H4, H9). When
any of these fails, a previously-fixed correctness gap has been
reintroduced — the failure message is intentionally written to be
self-documenting so the next reader doesn't need to dig through the
audit to know what's at stake.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── B3 + H3: committer stages only touched paths, surfaces git-add failures ─


def test_b3_committer_does_not_use_git_add_dash_a(tmp_path: Path):
    """``git add -A`` was scooping up every untracked file in the working
    tree (build artifacts, local junk). Committer must stage exactly the
    patcher's touched paths — nothing more."""
    from gitoma.worker.committer import Committer

    git_calls: list[tuple[str, tuple, dict]] = []

    class _FakeGit:
        def add(self, *args, **kwargs):
            git_calls.append(("add", args, kwargs))

    class _FakeIndex:
        def diff(self, _ref):
            # Pretend something was staged so commit() runs.
            return ["fake-staged"]

    class _FakeRepo:
        git = _FakeGit()
        index = _FakeIndex()
        untracked_files = ["unrelated_local_junk.txt", "dist/old_build"]

        def commit(self, message, author=None, committer=None):
            return MagicMock(hexsha="abc1234")

    fake_git_repo = MagicMock()
    fake_git_repo.repo = _FakeRepo()
    fake_git_repo.commit = MagicMock(return_value="abc1234")

    cfg = MagicMock()
    cfg.bot.name = "bot"
    cfg.bot.email = "bot@example.com"

    Committer(fake_git_repo, cfg).commit_patches(["src/foo.py", "README.md"], "feat: x")

    # Only the touched paths were staged.
    add_paths = [args[0] for action, args, kwargs in git_calls if action == "add" and args]
    assert add_paths == ["src/foo.py", "README.md"], (
        f"committer should stage only touched paths; got {add_paths}"
    )
    # No call to ``git add -A`` (which would be add(A=True) per GitPython convention).
    assert not any(
        kwargs.get("A") for _, _, kwargs in git_calls
    ), "committer must not fall back to `git add -A` (overstages untracked files)"


def test_h3_committer_surfaces_git_add_failures():
    """The previous ``except Exception: pass`` made a failed ``git add``
    indistinguishable from "nothing to commit". The worker would mark
    the subtask "completed" when in reality git refused to stage."""
    from gitoma.worker.committer import Committer, CommitterError

    class _FakeGit:
        def add(self, _path):
            raise OSError("Permission denied")

    class _FakeRepo:
        git = _FakeGit()

    fake_git_repo = MagicMock()
    fake_git_repo.repo = _FakeRepo()

    cfg = MagicMock()
    with pytest.raises(CommitterError, match="git add failed"):
        Committer(fake_git_repo, cfg).commit_patches(["a.py", "b.py"], "feat: x")


# ── B4: patcher denylist covers cloud secrets + lockfiles ──────────────────


@pytest.mark.parametrize("blocked_path", [
    # Cloud / container creds
    ".aws/credentials",
    ".ssh/id_rsa",
    ".docker/config.json",
    ".gcp/service.json",
    ".azure/credentials",
    ".kube/config",
    # Repo-governance
    "CODEOWNERS",
    # Package-manager auth
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    # Lockfiles (supply-chain poison vector)
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
])
def test_b4_patcher_denylist_blocks_supply_chain_paths(tmp_path, blocked_path):
    """The original denylist covered ``.git/`` and ``.env*`` only.
    Cloud creds + lockfiles must also be off-limits — the LLM has no
    legitimate reason to edit any of them and an LLM-generated lockfile
    edit is a silent dependency swap."""
    from gitoma.worker.patcher import PatchError, apply_patches

    with pytest.raises(PatchError, match="[Rr]efusing"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": blocked_path,
            "content": "poisoned",
        }])
    # Belt-and-braces: the file must not exist after the rejection.
    assert not (tmp_path / blocked_path).exists()


# ── B5: LLM truncation detected via finish_reason ──────────────────────────


def test_b5_llm_chat_raises_on_truncated_response(monkeypatch):
    """When ``max_tokens`` cuts the response off mid-output, the response
    parses cleanly via best-effort JSON extraction (the brace-matcher
    closes the dangling object). Caller would silently accept a partial
    plan/patch. We must raise instead."""
    from gitoma.planner.llm_client import LLMClient, LLMTruncatedError

    cfg = MagicMock()
    cfg.lmstudio.base_url = "http://localhost:1234/v1"
    cfg.lmstudio.api_key = "x"
    cfg.lmstudio.model = "m"
    cfg.lmstudio.temperature = 0.0
    cfg.lmstudio.max_tokens = 100

    client = LLMClient.__new__(LLMClient)
    client._config = cfg
    fake_choice = MagicMock()
    fake_choice.message.content = '{"plan": [{"id": "T001", "title": "incompl'
    fake_choice.finish_reason = "length"
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]

    fake_completions = MagicMock()
    fake_completions.create = MagicMock(return_value=fake_response)
    fake_chat = MagicMock()
    fake_chat.completions = fake_completions
    client._client = MagicMock()
    client._client.chat = fake_chat

    with pytest.raises(LLMTruncatedError, match="truncated"):
        client.chat([{"role": "user", "content": "go"}], retries=1)


def test_b5_llm_chat_does_not_raise_on_normal_finish(monkeypatch):
    """Normal ``finish_reason="stop"`` must NOT trigger the truncation guard."""
    from gitoma.planner.llm_client import LLMClient

    cfg = MagicMock()
    cfg.lmstudio.base_url = "http://localhost:1234/v1"
    cfg.lmstudio.api_key = "x"
    cfg.lmstudio.model = "m"
    cfg.lmstudio.temperature = 0.0
    cfg.lmstudio.max_tokens = 100

    client = LLMClient.__new__(LLMClient)
    client._config = cfg
    fake_choice = MagicMock()
    fake_choice.message.content = '{"ok": true}'
    fake_choice.finish_reason = "stop"
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    client._client = MagicMock()
    client._client.chat.completions.create = MagicMock(return_value=fake_response)

    out = client.chat([{"role": "user", "content": "go"}], retries=1)
    assert out == '{"ok": true}'


# ── B6: branch name collisions — microsecond granularity + retry ───────────


def test_b6_run_branch_template_uses_microseconds():
    """Regression guard: the branch template must include the microsecond
    component so two ``gitoma run`` invocations in the same wall-clock
    second don't collide and silently overwrite each other."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")
    # Microsecond format specifier %f is what makes same-second-collision
    # cryptographically improbable in practice.
    assert '"%Y%m%d-%H%M%S-%f"' in text, (
        "Branch template lost the %f (microsecond) component — same-second "
        "collisions can return"
    )


# ── B1: --resume actually resumes from saved phase ─────────────────────────


def test_b1_phase_already_done_ordering():
    """The resume gate decides which phase blocks to skip. Pin the order
    so a renamed/added phase doesn't accidentally cause the gate to
    return wrong answers."""
    from gitoma.cli.commands.run import _PHASE_ORDER, _phase_already_done
    from gitoma.core.state import AgentPhase, AgentState

    # IDLE < ANALYZING < PLANNING < WORKING < PR_OPEN < REVIEWING < DONE.
    expected_order = [
        AgentPhase.IDLE, AgentPhase.ANALYZING, AgentPhase.PLANNING,
        AgentPhase.WORKING, AgentPhase.PR_OPEN, AgentPhase.REVIEWING,
        AgentPhase.DONE,
    ]
    seq = [_PHASE_ORDER[p.value] for p in expected_order]
    assert seq == sorted(seq) and len(set(seq)) == len(seq), (
        f"Phase ordering must be strictly increasing: {seq}"
    )

    # State at WORKING => analyze + plan are past, working/pr_open/etc are not.
    state = AgentState(
        repo_url="x", owner="a", name="b", branch="c",
        phase=AgentPhase.WORKING.value,
    )
    assert _phase_already_done(state, AgentPhase.ANALYZING) is True
    assert _phase_already_done(state, AgentPhase.PLANNING) is True
    assert _phase_already_done(state, AgentPhase.WORKING) is False
    assert _phase_already_done(state, AgentPhase.PR_OPEN) is False


def test_b1_metric_report_roundtrip():
    """Resume requires deserializing the persisted metric report.
    Roundtrip the dict form to confirm no fields are silently dropped."""
    from gitoma.analyzers.base import MetricReport, MetricResult

    original = MetricReport(
        repo_url="https://github.com/a/b",
        owner="a",
        name="b",
        languages=["Python", "Rust"],
        default_branch="main",
        analyzed_at="2026-04-21T10:00:00Z",
        metrics=[
            MetricResult(name="ci", display_name="CI", score=0.5,
                         status="warn", details="needs work",
                         suggestions=["add lint"], weight=2.0),
        ],
    )
    rehydrated = MetricReport.from_dict(original.to_dict())
    assert rehydrated.repo_url == original.repo_url
    assert rehydrated.owner == original.owner
    assert rehydrated.languages == original.languages
    assert len(rehydrated.metrics) == 1
    m = rehydrated.metrics[0]
    assert m.name == "ci" and m.score == 0.5 and m.weight == 2.0
    assert m.suggestions == ["add lint"]


# ── B2: every early-return advances to a terminal phase ────────────────────


def test_b2_early_returns_advance_to_done_in_source():
    """All four early-return branches in the run command (empty plan,
    dry-run, user-cancel, and the existing all-pass) must advance to
    DONE before returning. Otherwise observers see WORKING/PLANNING +
    exit_clean=True (set by the heartbeat finally), which the orphan
    detector reads as "stalled run" — false positive."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")

    # Each early-exit code path must include the AgentPhase.DONE advance
    # before its ``return``. We grep for the surrounding context phrases.
    must_have = [
        "Plan was empty",
        "Dry run complete",
        "Aborted by user",
        "All metrics already pass",
    ]
    for marker in must_have:
        # Find the marker, then read a generous window — the advance(DONE)
        # call lives a few lines past the message + comment block + state
        # mutations, so 1000 chars covers all four early-exit shapes.
        idx = text.find(marker)
        assert idx != -1, f"early-return marker disappeared: {marker!r}"
        window = text[idx : idx + 1000]
        assert "AgentPhase.DONE" in window, (
            f"Early return for {marker!r} no longer advances to DONE — "
            "state will be left in a non-terminal phase"
        )


# ── H2: worker completed==0 advances phase before Exit(1) ──────────────────


def test_h2_zero_completed_advances_to_done_in_source():
    """When the worker phase finishes with 0 completed tasks the run
    aborts with Exit(1). Without advancing phase, the orphan detector
    flags this deliberate failure as "process vanished mid-run". State
    must be DONE + errors populated, then Exit(1)."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")
    chunk_idx = text.find("if completed == 0:")
    assert chunk_idx != -1
    window = text[chunk_idx : chunk_idx + 2000]
    assert "AgentPhase.DONE" in window, (
        "completed==0 path no longer advances to DONE — orphan detector "
        "will misclassify this failure"
    )
    assert "raise typer.Exit(1)" in window
    assert "errors" in window  # the failure message must be recorded


# ── H4: Reflexion loop is bounded by wall-clock + LLM-call budget ──────────


def test_h4_reflexion_caps_are_defined():
    """The Reflexion loop used to have no global timeout or call budget,
    so a flaky LLM could spin tens of minutes burning tokens. Pin the
    cap constants so any future refactor that drops them breaks here."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    assert hasattr(CIDiagnosticAgent, "MAX_TOTAL_WALL_CLOCK_S")
    assert hasattr(CIDiagnosticAgent, "MAX_TOTAL_LLM_CALLS")
    assert CIDiagnosticAgent.MAX_TOTAL_WALL_CLOCK_S > 0
    assert CIDiagnosticAgent.MAX_TOTAL_LLM_CALLS > 0
    # Tight enough that a runaway loop bounded by these caps actually
    # finishes in a reasonable time. Loose enough that a normal multi-job
    # remediation completes well within them.
    assert CIDiagnosticAgent.MAX_TOTAL_WALL_CLOCK_S <= 600
    assert CIDiagnosticAgent.MAX_TOTAL_LLM_CALLS <= 100


def test_h4_reflexion_budget_exhausted_check():
    """The exhaustion check returns True when either ceiling is hit."""
    from gitoma.review.reflexion import CIDiagnosticAgent

    agent = CIDiagnosticAgent.__new__(CIDiagnosticAgent)
    # Wall-clock NOT exhausted, calls below cap → False.
    agent._wallclock_started = time.monotonic()
    agent._llm_calls_used = 0
    assert agent._budget_exhausted() is False

    # Calls at cap → True.
    agent._llm_calls_used = CIDiagnosticAgent.MAX_TOTAL_LLM_CALLS
    assert agent._budget_exhausted() is True

    # Wall-clock exceeded → True.
    agent._llm_calls_used = 0
    agent._wallclock_started = time.monotonic() - CIDiagnosticAgent.MAX_TOTAL_WALL_CLOCK_S - 1
    assert agent._budget_exhausted() is True


# ── B7 (b2v post-mortem): runs always end on a terminal DONE phase ─────────


def test_b7_run_command_advances_to_done_after_post_pr_phases():
    """The ``gitoma run`` command must advance state.phase to DONE after
    its declared scope finishes (analyze → plan → execute → PR →
    self-review → ci-watch). The previous code stopped at PR_OPEN
    (PHASE 4), then PHASE 5 + 6 only updated current_operation. The
    cockpit + orphan detector then treated a finished run as "still
    in flight" forever — exactly the b2v UX bug."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")
    # The terminal advance must live AFTER the PHASE 6 ci-watch block.
    ci_watch_idx = text.rfind("_watch_ci_and_maybe_fix")
    assert ci_watch_idx != -1
    # Look for the canonical terminal block in the trailing portion of
    # the function — within a generous window past the ci-watch call.
    trailing = text[ci_watch_idx:]
    assert "AgentPhase.DONE" in trailing, (
        "gitoma run must advance to DONE after PHASE 6 (ci-watch) — "
        "without this the cockpit shows the run as PR_OPEN forever"
    )


def test_b7_review_command_advances_to_done_after_integration():
    """``gitoma review`` must end on DONE, not REVIEWING. The integrator
    pushes fixes (or doesn't, if nothing matched), and the run is over.
    Leaving phase=REVIEWING made the cockpit show "REVIEWING" indefinitely
    even after the user merged the PR — the b2v UX bug they reported."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/review.py"
    text = src.read_text(encoding="utf-8")
    # The terminal advance must come AFTER the conditional REVIEWING
    # advance — pin both so a refactor that drops the terminal one fails.
    last_done_idx = text.rfind("AgentPhase.DONE")
    last_reviewing_idx = text.rfind("AgentPhase.REVIEWING")
    assert last_done_idx != -1, "gitoma review must end with state.advance(AgentPhase.DONE)"
    assert last_reviewing_idx != -1, "REVIEWING flash must remain (cockpit pipeline lights it briefly)"
    assert last_done_idx > last_reviewing_idx, (
        "AgentPhase.DONE must be the LAST advance in gitoma review — "
        "otherwise REVIEWING stays as the terminal phase and the orphan "
        "detector flags the finished run as 'still going'"
    )


# ── H1: save_state writes are serialized via threading.Lock ────────────────


def test_h1_save_state_uses_module_lock():
    """The CLI main thread + the heartbeat daemon both call save_state
    against the same shared dataclass. Without serialization, the
    dataclass→JSON snapshot taken by one writer can interleave with a
    field mutation from the other. A module-level lock funnels both
    writers through one serial point."""
    from gitoma.core import state as state_module

    assert isinstance(state_module._SAVE_STATE_LOCK, type(threading.Lock())), (
        "module-level lock must be a threading.Lock instance"
    )


def test_h1_save_state_is_thread_safe(tmp_path, monkeypatch):
    """Stress-test: many concurrent writers, no torn JSON, no exceptions."""
    import json
    from gitoma.core import state as state_module
    from gitoma.core.state import AgentState, save_state

    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path)

    s = AgentState(repo_url="x", owner="o", name="r", branch="b")

    errors: list[Exception] = []

    def writer():
        for _ in range(50):
            try:
                save_state(s)
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"save_state raised under concurrency: {errors[:3]}"

    # Final on-disk state must be valid JSON (no torn writes).
    payload = json.loads((tmp_path / "o__r.json").read_text())
    assert payload["owner"] == "o" and payload["name"] == "r"


# ── H9: heartbeat tick survives transient crashes ─────────────────────────


def test_h9_heartbeat_tick_logs_on_crash_and_continues(tmp_path, monkeypatch):
    """The previous ``_tick`` wrapped only ``save_state`` in try/except;
    if the assignment to ``state.last_heartbeat`` ever raised (clock
    anomaly, threading primitive failure), the loop died silently and
    observers flagged a still-running CLI as orphaned. The hardened
    version wraps the whole tick body so a single transient failure
    becomes "stale heartbeat, alive process" instead."""
    from gitoma.cli import _helpers as helpers_module

    src = (Path(helpers_module.__file__)).read_text(encoding="utf-8")
    # The hardened tick uses ``BaseException`` to catch *anything* that
    # would otherwise kill the daemon (including KeyboardInterrupt-like
    # subclasses propagated through the threadlocal). Pin the contract.
    tick_chunk_idx = src.find("def _tick()")
    assert tick_chunk_idx != -1
    tick_chunk = src[tick_chunk_idx : tick_chunk_idx + 2000]
    assert "except BaseException" in tick_chunk, (
        "heartbeat tick must catch BaseException so a transient failure "
        "doesn't silently kill the daemon"
    )
    assert "tr.exception" in tick_chunk, (
        "heartbeat tick crashes must be logged via the trace, not silently swallowed"
    )
