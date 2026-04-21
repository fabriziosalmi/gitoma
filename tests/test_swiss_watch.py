"""Regression tests for the Swiss-watch hardening pass.

Each test maps 1:1 to a confirmed audit finding:

  * **B1-B6**: blocking correctness issues (resume, early-returns,
    committer overstaging, patcher denylist, LLM truncation, branch
    collisions).
  * **B7**: terminal DONE-advance in ``gitoma run`` and ``gitoma review``
    (caught live on the first b2v run — cockpit stuck on REVIEWING
    after merge).
  * **H1-H4, H9**: high-severity correctness issues (save_state race,
    worker zero-completion phase, committer silent failure, Reflexion
    budget, heartbeat tick crashes).
  * **F1-F4**: follow-ups from the b2v live run post-mortem — trailing-
    slash patch paths, planner/worker prompt denylist awareness,
    REVIEWING placement before integration, lockfile denylist coverage.

When any of these fails, a previously-fixed correctness gap has been
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


# ── F1 (b2v post-mortem): patcher rejects trailing-slash directory paths ──


@pytest.mark.parametrize("bad_path", [
    "./src/tests/",          # the exact shape T002-S02 hit on b2v
    "docs/",
    "nested/path/",
    "dir\\",                 # Windows-style trailing separator
    "a/b/c/",
])
def test_f1_patcher_rejects_trailing_slash_paths(tmp_path, bad_path):
    """The patcher used to accept directory-style paths, create the dir
    via ``parent.mkdir``, and then crash on ``os.open(abs_path, ...)``
    with a confusing ``[Errno 17] File exists``. The b2v live run hit
    exactly this (``T002-S02: path='./src/tests/'``). The hardened
    version rejects up-front with an actionable message so the LLM's
    next attempt targets a real filename."""
    from gitoma.worker.patcher import PatchError, apply_patches

    with pytest.raises(PatchError, match="[Ff]ile.*not.*directory"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": bad_path,
            "content": "ignored",
        }])
    # Nothing should have been created (not even the parent dir). The
    # rejection must happen before any FS mutation.
    assert not any(tmp_path.iterdir())


def test_f1_patcher_error_message_is_actionable(tmp_path):
    """A new contributor debugging an LLM-generated patch that hits this
    branch should see a message that tells them *exactly* what to do
    instead — not just "file exists"."""
    from gitoma.worker.patcher import PatchError, apply_patches

    with pytest.raises(PatchError) as ei:
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "src/tests/",
            "content": "",
        }])
    msg = str(ei.value)
    # Must include the offending path (so the LLM can correlate on retry).
    assert "src/tests/" in msg
    # Must hint at the fix (specify a filename).
    assert "filename" in msg.lower()


# ── F2 (b2v post-mortem): LLM prompts know about the patcher denylist ────


def test_f2_planner_prompt_lists_forbidden_paths():
    """Three b2v subtasks (T001-S03, T005-S01, T005-S02) were generated
    against ``.github/workflows/deploy-docs.yml`` — a forbidden path —
    and burned three LLM round-trips before the patcher rejected each
    one. Feeding the denylist into the planner prompt lets the LLM
    route around the problem up-front (e.g. describe a workflow fix in
    the task description instead of emitting a doomed subtask)."""
    from gitoma.analyzers.base import MetricReport, MetricResult
    from gitoma.planner.prompts import planner_user_prompt

    report = MetricReport(
        repo_url="https://github.com/a/b",
        owner="a", name="b",
        languages=["Rust"],
        default_branch="main",
        metrics=[MetricResult.from_score("ci", "CI", 0.5, "broken")],
        analyzed_at="2026-04-21T10:00:00Z",
    )
    prompt = planner_user_prompt(report, ["Cargo.toml"], ["Rust"])

    # The denylist section must be present in every planner prompt.
    assert "FORBIDDEN PATHS" in prompt, (
        "planner prompt must tell the LLM which paths the patcher will reject"
    )
    # Sample check: a few known-denied entries must be listed.
    assert ".github/workflows" in prompt
    assert "Cargo.lock" in prompt or "package-lock.json" in prompt
    # And a routing hint so the LLM knows what to do *instead* of emitting
    # a doomed subtask.
    assert "do NOT" in prompt or "do not" in prompt.lower()


def test_f2_worker_prompt_lists_forbidden_paths():
    """Defence in depth: even if a future planner change drops the hint,
    the worker prompt still tells the LLM about forbidden paths before
    it emits a patch."""
    from gitoma.planner.prompts import worker_user_prompt

    prompt = worker_user_prompt(
        subtask_title="Add rustfmt.toml",
        subtask_description="Add formatting config",
        file_hints=["rustfmt.toml"],
        languages=["Rust"],
        repo_name="b2v",
        current_files={},
        file_tree=["Cargo.toml", "src/main.rs"],
    )
    assert "FORBIDDEN PATHS" in prompt
    # Also pin the trailing-slash rule from F1 — the worker prompt must
    # explicitly tell the LLM not to emit directory-style paths.
    assert "directory ending with '/'" in prompt


def test_f2_denylist_summary_shape():
    """The summary format must stay grep-friendly (one bullet per category)
    so a future contributor who adds a denied path doesn't have to hunt
    for where the prompt injection happens."""
    from gitoma.worker.patcher import denylist_summary

    summary = denylist_summary()
    # Four bullet categories (parts / prefixes / filenames / filename prefixes).
    assert summary.count("\n") >= 3
    # Every category starts with a dash for readability.
    for line in summary.splitlines():
        assert line.startswith("- "), f"non-bullet line in summary: {line!r}"


# ── F3 (b2v post-mortem): REVIEWING phase set before integration ─────────


def test_f3_review_command_advances_to_reviewing_before_integrate_call():
    """Before this fix, the REVIEWING phase was set AFTER push — a
    <500ms flash right before DONE, essentially invisible in the
    cockpit. Moving it BEFORE the ``integrator.integrate(...)`` call
    means the cockpit shows REVIEWING for the entire LLM-driven
    integration, which is the useful window."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/review.py"
    text = src.read_text(encoding="utf-8")
    # The ``advance(REVIEWING)`` call must precede the ``integrator.integrate``
    # call textually in the source — that guarantees the runtime ordering.
    reviewing_idx = text.find("AgentPhase.REVIEWING")
    integrate_idx = text.find("integrator.integrate(")
    assert reviewing_idx != -1, "review command must advance to REVIEWING"
    assert integrate_idx != -1
    assert reviewing_idx < integrate_idx, (
        "state.advance(REVIEWING) must come BEFORE integrator.integrate() — "
        "the prior ordering made REVIEWING a <500ms flash right before DONE"
    )
    # The DONE advance stays the terminal one (pinned by test_b7 above).


# ── F4 (b2v post-mortem): lockfiles / npmrc blocked as expected ──────────


@pytest.mark.parametrize("lockfile", [
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
    ".npmrc",
])
def test_f4_lockfile_denylist_coverage(tmp_path, lockfile):
    """The b2v analyzer spotted package-lock.json alongside Cargo.toml.
    Pin the fact that every lockfile + publish-token file rejects a
    create patch — a future refactor that drops one of these from the
    denylist fails here instead of in the next live run."""
    from gitoma.worker.patcher import PatchError, apply_patches

    with pytest.raises(PatchError, match="[Rr]efusing"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": lockfile,
            "content": "poisoned",
        }])
    assert not (tmp_path / lockfile).exists()


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


# ── R2: GitRepo.sha_reachable() — behavioral test against a real git repo ──


def test_r2_sha_reachable_rejects_empty_and_invalid(tmp_path: Path):
    """``sha_reachable("")`` and unknown SHAs must return False. A crashed
    resume trusts an empty ``commit_sha`` string from corrupted state —
    the helper must treat that as "not reachable" instead of letting
    ``git merge-base`` barf with a non-boolean error."""
    import git as gitpython
    from gitoma.core.repo import GitRepo

    r = gitpython.Repo.init(tmp_path)
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    r.index.add(["seed.txt"])
    actor = gitpython.Actor("t", "t@t")
    r.index.commit("init", author=actor, committer=actor)

    gr = GitRepo.__new__(GitRepo)
    gr._tmpdir = str(tmp_path)
    gr._repo = r

    assert gr.sha_reachable("") is False
    assert gr.sha_reachable("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") is False
    assert gr.sha_reachable("not-a-sha") is False


def test_r2_sha_reachable_true_for_head_ancestor(tmp_path: Path):
    """A commit that IS an ancestor of HEAD must return True — this is
    the happy path that resume relies on to trust persisted completions."""
    import git as gitpython
    from gitoma.core.repo import GitRepo

    r = gitpython.Repo.init(tmp_path)
    actor = gitpython.Actor("t", "t@t")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    r.index.add(["a.txt"])
    c1 = r.index.commit("first", author=actor, committer=actor)
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    r.index.add(["b.txt"])
    c2 = r.index.commit("second", author=actor, committer=actor)

    gr = GitRepo.__new__(GitRepo)
    gr._tmpdir = str(tmp_path)
    gr._repo = r

    # HEAD points at c2 → both c1 (ancestor) and c2 (equal) are reachable.
    assert gr.sha_reachable(c1.hexsha) is True
    assert gr.sha_reachable(c2.hexsha) is True


def test_r2_sha_reachable_false_for_detached_branch(tmp_path: Path):
    """The R2 scenario in flesh: prior run pushed partial commits but
    crashed before PHASE 4's branch push. On resume, ``checkout -B``
    resets to ``origin/<branch>`` and the commit that only ever existed
    in the prior tempdir is gone. ``sha_reachable`` must return False
    for that orphaned SHA — otherwise the worker silently skips the
    subtask and the final branch misses the work."""
    import git as gitpython
    from gitoma.core.repo import GitRepo

    r = gitpython.Repo.init(tmp_path)
    actor = gitpython.Actor("t", "t@t")
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    r.index.add(["seed.txt"])
    base = r.index.commit("base", author=actor, committer=actor)

    # Simulate a "lost" commit: created on a side branch then HEAD never
    # includes it — from main's perspective it's not an ancestor.
    r.git.checkout("-b", "lost-work")
    (tmp_path / "lost.txt").write_text("lost", encoding="utf-8")
    r.index.add(["lost.txt"])
    lost = r.index.commit("lost", author=actor, committer=actor)
    # HEAD back to base, discarding the lost-work branch from history.
    r.git.checkout(base.hexsha)

    gr = GitRepo.__new__(GitRepo)
    gr._tmpdir = str(tmp_path)
    gr._repo = r

    assert gr.sha_reachable(base.hexsha) is True, "base commit must be reachable from HEAD"
    assert gr.sha_reachable(lost.hexsha) is False, (
        "orphaned commit (exists in repo but not ancestor of HEAD) must be "
        "reported unreachable — this is the signal that drives resume to "
        "re-run the subtask"
    )


# ── R3: resume flips lost subtasks back to pending ────────────────────────


def test_r3_resume_rewinds_completed_subtasks_with_unreachable_shas():
    """Source-level pin on the resume loop: when a subtask is marked
    ``status=completed`` with a ``commit_sha`` that is NOT an ancestor
    of HEAD, the loop MUST (a) flip it to ``pending``, (b) clear the
    ``commit_sha``, (c) demote the parent task from ``completed`` to
    ``in_progress``, and (d) persist via ``save_state``.

    The prior resume path trusted the persisted ``completed`` marker
    unconditionally — after a pre-push crash, the worker silently
    skipped the subtask and the final PR was missing work. This test
    fails loudly the moment any of those four guarantees is removed."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")

    # The loop must be gated on the resume flag + a loaded plan — running
    # it on a fresh run would be pointless and risks breaking the happy path.
    assert "if resume and plan:" in text, (
        "SHA-reachability loop must be gated on ``resume and plan`` — "
        "never run it on fresh (non-resume) invocations"
    )
    # The core check: sha_reachable(sub.commit_sha) called inside the loop.
    assert "git_repo.sha_reachable(sub.commit_sha)" in text, (
        "resume loop must call ``git_repo.sha_reachable(sub.commit_sha)`` "
        "to detect lost commits"
    )
    # Recovery mutations — all four must be present.
    assert 'sub.status = "pending"' in text, (
        "lost subtask must be flipped to status=pending so the worker re-runs it"
    )
    assert 'sub.commit_sha = ""' in text, (
        "lost subtask must have its stale commit_sha cleared"
    )
    assert 'task.status = "in_progress"' in text, (
        "parent task must be demoted from completed → in_progress when any "
        "subtask is lost"
    )
    # Persistence — without save_state the rewind is lost on the next crash.
    loop_start = text.find("if resume and plan:")
    loop_slice = text[loop_start : loop_start + 2500]
    assert "save_state(state)" in loop_slice, (
        "resume loop must persist the rewound plan via save_state(state) — "
        "otherwise a second crash before the worker commits loses the fix"
    )


# ── R1: PR-state validation before skipping PHASE 4 ───────────────────────


def test_r1_resume_validates_persisted_pr_on_github():
    """Source-level pin: on resume, a persisted ``pr_number`` is NOT
    trusted blindly. Before skipping PHASE 4 we query GitHub and branch
    on the live state (open / merged / closed / missing).

    The prior path trusted the persisted PR unconditionally — if the PR
    was merged or closed between runs, self-review + CI-watch still ran
    against a finalised PR (noise at best, 404 chases at worst). If the
    PR was deleted outright, every follow-up API call was a dead end."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")

    assert "from github import GithubException" in text, (
        "run.py must import GithubException to handle a missing PR (404) "
        "distinctly from transient API errors"
    )
    assert "gh.get_pr(owner, name, state.pr_number)" in text, (
        "run.py must query GitHub for the persisted PR before skipping PHASE 4"
    )
    # All five states must be present as string literals — losing any one
    # collapses the branching logic back to the prior blind-trust path.
    for lit in ('"open"', '"merged"', '"closed"', '"missing"', '"unknown"'):
        assert lit in text, (
            f"pr_state literal {lit} missing from run.py — the four "
            "GitHub-reality branches + unknown fallback are the whole point "
            "of the validation"
        )
    # 404 handling specifically — a missing PR must clear the stale fields
    # and fall through to PHASE 4 re-creation.
    assert 'getattr(exc, "status", None) == 404' in text, (
        "run.py must special-case the 404 via GithubException.status so a "
        "deleted PR triggers re-creation instead of a crash"
    )
    assert "state.pr_number = None" in text and "state.pr_url = None" in text, (
        "on PR missing/404, run.py must null out pr_number + pr_url so the "
        "re-created PR's identity isn't confused with the stale one"
    )


def test_r1_finalised_pr_skips_self_review_and_ci_watch():
    """If GitHub reports the persisted PR as merged or closed, ``pr_finalised``
    must flip True and BOTH PHASE 5 (self-review) and PHASE 6 (CI watch)
    must skip. Self-reviewing a merged PR is noise the maintainer already
    moved past; polling CI on a closed branch is a 404 hunt."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/run.py"
    text = src.read_text(encoding="utf-8")

    assert "pr_finalised = False" in text, (
        "pr_finalised flag must be initialised — it gates phases 5 + 6"
    )
    assert "pr_finalised = True" in text, (
        "pr_finalised must be set True on merged/closed branches so "
        "follow-up phases are skipped"
    )
    # The merged/closed branch must set pr_finalised True.
    merged_closed_idx = text.find('pr_state in ("merged", "closed")')
    assert merged_closed_idx != -1, (
        "run.py must branch on pr_state in (merged, closed) — this is the "
        "signal that the PR is terminal on GitHub"
    )
    # Within a reasonable window after the branch, pr_finalised must be set.
    merged_block = text[merged_closed_idx : merged_closed_idx + 1200]
    assert "pr_finalised = True" in merged_block, (
        "merged/closed branch must set pr_finalised=True — otherwise "
        "phases 5+6 still run against a finalised PR"
    )

    # Both phases must check pr_finalised before running.
    self_review_idx = text.find("if no_self_review:")
    assert self_review_idx != -1
    self_review_block = text[self_review_idx : self_review_idx + 1000]
    assert "elif pr_finalised:" in self_review_block, (
        "PHASE 5 gate must check pr_finalised — otherwise self-review posts "
        "comments on merged/closed PRs"
    )

    ci_watch_idx = text.find("if no_ci_watch:")
    assert ci_watch_idx != -1
    ci_watch_block = text[ci_watch_idx : ci_watch_idx + 1000]
    assert "elif pr_finalised:" in ci_watch_block, (
        "PHASE 6 gate must check pr_finalised — otherwise CI-watch polls "
        "Actions on a deleted branch and chases 404s"
    )


# ── R1b (review): --integrate short-circuits on merged/closed PRs ─────────


def test_r1b_review_integrate_skips_before_cloning_when_pr_finalised():
    """Sibling of the R1 ``run`` guard: ``gitoma review --integrate`` must
    check the live PR state BEFORE cloning. Cloning a 40MB repo only to
    bomb at ``git checkout <auto-deleted-branch>`` is wasteful at best and
    misleading at worst — the cockpit live-ran this exact failure
    (RC=1, pathspec did not match) against a merged PR whose branch
    GitHub auto-deleted."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/review.py"
    text = src.read_text(encoding="utf-8")

    # The guard must live in the integrate branch; locate it by anchoring
    # on "INTEGRATING REVIEW COMMENTS" and slicing forward.
    integrate_idx = text.find("INTEGRATING REVIEW COMMENTS")
    assert integrate_idx != -1
    guard_region = text[integrate_idx : integrate_idx + 4000]

    # Must query GitHub for the PR state.
    assert "gh.get_pr(owner, name, pr_number)" in guard_region, (
        "review --integrate must query GitHub for the PR state before "
        "cloning — a merged/closed PR is a waste of clone + a misleading "
        "checkout failure"
    )
    # Must short-circuit on merged explicitly.
    assert "gh_pr.merged" in guard_region, (
        "review --integrate must check ``gh_pr.merged`` — merged PRs "
        "should not receive further fix commits"
    )
    # Must short-circuit on closed explicitly.
    assert 'gh_pr.state == "closed"' in guard_region, (
        "review --integrate must check for closed PRs — pushing to a "
        "closed PR's (possibly revived) branch is wrong"
    )
    # Must advance state to DONE on the skip paths so the cockpit pipeline
    # lights up correctly and the orphan detector doesn't flag the run.
    assert "AgentPhase.DONE" in guard_region, (
        "review --integrate skip paths must advance state to DONE — "
        "otherwise the cockpit's Pipeline strip is stuck mid-flight"
    )
    # Must early-return via ``return`` (not _abort) — this is a clean
    # no-op, not an error condition.
    skip_block = guard_region[guard_region.find("gh_pr.merged") : guard_region.find("gh_pr.merged") + 1500]
    assert skip_block.count("return") >= 2, (
        "review --integrate must ``return`` cleanly (not _abort) on merged "
        "and closed — both are expected states, not errors"
    )
    # 404 on the PR must _abort with a clear message.
    assert 'getattr(exc, "status", None) == 404' in guard_region, (
        "review --integrate must special-case 404 on the PR lookup — a "
        "deleted PR is distinct from a transient API error"
    )


def test_r1b_review_integrate_guard_runs_before_clone():
    """Belt-and-braces: the guard's GitHub query must textually precede
    the clone call. If a future refactor moves clone above the guard the
    performance + UX win evaporates — we'd clone, then discover the PR
    is merged, then throw away the clone. Pin the ordering."""
    src = Path(__file__).resolve().parents[1] / "gitoma/cli/commands/review.py"
    text = src.read_text(encoding="utf-8")

    integrate_idx = text.find("INTEGRATING REVIEW COMMENTS")
    assert integrate_idx != -1
    region = text[integrate_idx:]

    guard_idx = region.find("gh_pr.merged")
    clone_idx = region.find("_clone_repo(repo_url, config)")
    assert guard_idx != -1, "PR-state guard missing"
    assert clone_idx != -1, "_clone_repo call missing"
    assert guard_idx < clone_idx, (
        "PR-state guard must textually precede _clone_repo — otherwise "
        "we pay for a 40MB clone before discovering the PR is finalised"
    )
