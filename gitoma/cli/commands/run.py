"""gitoma run command."""

from __future__ import annotations

import atexit
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Annotated, Optional, TYPE_CHECKING


def _qa_run_tests(root: "_Path") -> tuple[bool, str]:
    """Best-effort language-detected test run for the Q&A apply gate.

    Returns (ok, detail). Missing toolchain OR unrecognised project
    layout = ``(True, "skipped")`` — we don't block a run just because
    we can't test. Timeout bounded at 120s; anything longer signals
    deeper problems and would hold up the pipeline.
    """
    # Detect framework (same priority as bench_rung.py)
    has_cargo = (root / "Cargo.toml").is_file()
    has_gomod = (root / "go.mod").is_file()
    has_npm = (root / "package.json").is_file()
    has_py = (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()

    def _run(cmd: list[str]) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return True, f"skipped ({cmd[0]} not in PATH)"
        except subprocess.TimeoutExpired:
            return False, f"timeout after 120s: {' '.join(cmd)}"
        if r.returncode == 0:
            return True, f"pass ({' '.join(cmd)})"
        tail = (r.stderr or r.stdout)[-400:].strip()
        return False, f"fail: {tail}"

    if has_cargo:
        return _run(["cargo", "test", "--quiet"])
    if has_gomod:
        return _run(["go", "test", "./..."])
    if has_py:
        # Use sys.executable so the test runner uses the SAME interpreter
        # that's running gitoma — avoids the silent ``python not in PATH``
        # soft-pass that masked a broken Q&A revision on rung-3 v5.
        import sys as _sys
        return _run([_sys.executable, "-m", "pytest", "-q", "--no-header"])
    if has_npm:
        return _run(["npm", "test", "--silent"])
    return True, "skipped (no recognised test framework)"

import typer
from rich.panel import Panel
from rich.prompt import Confirm

from gitoma import __version__
from gitoma.cli._app import app
from gitoma.cli._helpers import (
    _abort,
    _check_config,
    _check_github,
    _check_lmstudio,
    _clone_repo,
    _heartbeat,
    _ok,
    _phase,
    _run_self_review,
    _safe_cleanup,
    _warn,
    _watch_ci_and_maybe_fix,
)
from gitoma.analyzers.base import MetricReport
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import parse_repo_url
from gitoma.core.state import (
    AgentPhase,
    AgentState,
    acquire_run_lock,
    delete_state,
    load_state,
    release_run_lock,
    save_state,
)
from gitoma.planner.task import TaskPlan


# Ordering used to decide "have we already passed this phase?" when
# resuming. ANALYZING < PLANNING < WORKING < PR_OPEN < REVIEWING < DONE;
# IDLE is a sentinel meaning "never started" and treated as before
# ANALYZING.
_PHASE_ORDER: dict[str, int] = {
    AgentPhase.IDLE.value: 0,
    AgentPhase.ANALYZING.value: 1,
    AgentPhase.PLANNING.value: 2,
    AgentPhase.WORKING.value: 3,
    AgentPhase.PR_OPEN.value: 4,
    AgentPhase.REVIEWING.value: 5,
    AgentPhase.DONE.value: 6,
}


def _phase_already_done(state: AgentState, phase: AgentPhase) -> bool:
    """True when ``state.phase`` is strictly past ``phase``.

    Used by the ``--resume`` path to skip phases whose output is already
    persisted on the state file. A phase that crashed mid-execution leaves
    ``state.phase`` equal to itself (not past it), so it gets re-run from
    scratch — which is correct for ANALYZING/PLANNING (no partial output
    persisted) and harmless for WORKING (the worker skips already-completed
    subtasks via ``status == "completed"``).

    Defensive: an unknown phase value (corrupted state file, version
    drift, manual edit) used to silently fall through as "before
    ANALYZING" via ``_PHASE_ORDER.get(..., 0)`` — re-running the whole
    pipeline without telling the operator the state was broken. We now
    warn loudly so the audit trail shows what happened, then still
    fail-safe (re-plan everything) so resume doesn't crash on bad data.
    """
    if state.phase not in _PHASE_ORDER:
        console.print(
            f"[warning]⚠ Unknown phase {state.phase!r} in state file — "
            "treating as pre-ANALYZING. The state may be corrupt or from "
            "a different gitoma version. Use [primary]--reset[/primary] "
            "if resume misbehaves.[/warning]"
        )
        return False
    return _PHASE_ORDER[state.phase] > _PHASE_ORDER[phase.value]
from gitoma.ui.console import console
from gitoma.ui.panels import (
    make_analyzer_progress,
    print_banner,
    print_commit,
    print_metric_report,
    print_pr_panel,
    print_repo_info,
    print_status_panel,
    print_task_plan,
)

if TYPE_CHECKING:
    from gitoma.core.config import Config  # noqa: F401
    from gitoma.core.repo import GitRepo as _GitRepo  # noqa: F401
    from gitoma.planner.llm_client import LLMClient  # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def run(
    repo_url: Annotated[str, typer.Argument(help="GitHub repo URL (https://github.com/owner/repo)")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Analyze and plan only — no commits or PR")] = False,
    branch: Annotated[str, typer.Option("--branch", help="Branch name to create")] = "",
    base: Annotated[Optional[str], typer.Option("--base", help="Base branch for the PR")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume an existing agent run")] = False,
    reset_state: Annotated[bool, typer.Option("--reset", help="Delete existing state and start fresh")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
    no_self_review: Annotated[
        bool,
        typer.Option(
            "--no-self-review",
            help="Skip the Phase 5 self-critic pass that posts a review comment on the PR",
        ),
    ] = False,
    no_ci_watch: Annotated[
        bool,
        typer.Option(
            "--no-ci-watch",
            help="Skip Phase 6: polling GitHub Actions on the freshly-pushed branch "
            "and auto-invoking fix-ci on failure",
        ),
    ] = False,
    no_auto_fix_ci: Annotated[
        bool,
        typer.Option(
            "--no-auto-fix-ci",
            help="Watch CI but do NOT auto-invoke the Reflexion agent on failure "
            "(the watch still narrates pass/fail)",
        ),
    ] = False,
    skip_lm: Annotated[bool, typer.Option("--skip-lm-check", hidden=True, help="Skip LM Studio check (testing)")] = False,
) -> None:
    """
    🚀 Run the full autonomous improvement pipeline on a GitHub repo.

    Pipeline: Analyze → Plan (LLM) → Execute (LLM + commits) → Open PR

    Always run [primary]gitoma doctor[/primary] first to verify all prerequisites.
    """
    print_banner(__version__)

    # ── Pre-flight ──────────────────────────────────────────────────────────
    config = _check_config()
    owner, name = parse_repo_url(repo_url)

    # ── Concurrent-run lock ────────────────────────────────────────────────
    # Prevent two parallel `gitoma run` invocations on the same repo from
    # corrupting each other's state. The lock is released at the end of
    # this function (and cleaned up automatically if this PID dies).
    acquired, holder_pid = acquire_run_lock(owner, name)
    if not acquired:
        _abort(
            f"Another gitoma run is already active for {owner}/{name} (pid {holder_pid}).",
            hint=(
                "Wait for it to finish, or delete "
                f"~/.gitoma/state/{owner}__{name}.lock if you know the other process is gone."
            ),
        )
    # atexit guarantees release on every normal exit path (typer.Exit, sys.exit,
    # graceful SIGTERM). Hard kills leave a stale lock behind which the next
    # `acquire_run_lock` will detect and take over.
    atexit.register(release_run_lock, owner, name)

    # ── Existing state guard ────────────────────────────────────────────────
    existing_state = load_state(owner, name)
    if existing_state:
        if reset_state:
            delete_state(owner, name)
            existing_state = None
            _warn("Existing state deleted — starting fresh")
        elif resume:
            # Refuse to "resume" something already finished — the operator
            # almost certainly meant --reset. Clearer than silently doing
            # nothing or re-running a DONE run.
            if existing_state.phase == AgentPhase.DONE.value:
                console.print(
                    "[info]✓ This run is already DONE. "
                    "Use [primary]--reset[/primary] to start over.[/info]"
                )
                raise typer.Exit(0)
            console.print(
                f"[info]↩ Resuming from phase: [bold]{existing_state.phase}[/bold][/info]"
            )
        else:
            print_status_panel(existing_state)
            console.print(
                "\n[warning]⚠ An agent run already exists for this repo.[/warning]\n"
                "[muted]Options:[/muted]\n"
                "  [primary]--resume[/primary]  Continue from last checkpoint\n"
                "  [primary]--reset[/primary]   Delete state and restart\n"
                "  [primary]gitoma status <url>[/primary]  Inspect current progress"
            )
            raise typer.Exit(0)

    # ── Branch name ─────────────────────────────────────────────────────────
    # Granularity: microseconds. The previous ``%Y%m%d-%H%M%S`` format
    # collided silently when two ``gitoma run`` invocations started in the
    # same wall-clock second (CI matrix, parallel cockpit dispatches…), so
    # the second run would silently get the same branch name and overwrite
    # the first. ``%f`` (microseconds) makes a same-second collision
    # cryptographically improbable; the explicit collision check below is
    # defence-in-depth in case two invocations land on the same microsecond
    # (e.g. clock-skew across nodes pushing to the same repo).
    def _generate_branch_name() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        return f"gitoma/improve-{ts}"

    if not branch:
        branch = _generate_branch_name()

    # ── GitHub check ────────────────────────────────────────────────────────
    gh = GitHubClient(config)
    repo_info = _check_github(config, owner, name)
    print_repo_info(repo_info)
    base_branch = base or repo_info["default_branch"]

    # Verify the target branch doesn't already exist remotely. We retry
    # generation up to a few times rather than once — if the operator's
    # clock jumped backward or the API call took long enough that the
    # first regenerated name also collides, we don't want to ship the
    # collision either.
    try:
        remote_branches = gh.list_branches(owner, name)
        attempts = 0
        while branch in remote_branches and attempts < 5:
            _warn(
                f"Branch '{branch}' already exists on GitHub",
                hint="Generating a new microsecond-stamped branch instead.",
            )
            branch = _generate_branch_name()
            attempts += 1
            console.print(f"[muted]  New branch: {branch}[/muted]")
        if branch in remote_branches:
            # Five rolls of microsecond timestamps and still colliding —
            # the clock is stuck, or someone is mass-creating ``gitoma/``
            # branches. Either way: stop, don't silently ship a collision.
            _abort(
                f"Could not produce a unique branch name after {attempts} attempts.",
                hint="Check the system clock and the existing 'gitoma/improve-*' "
                "branches on GitHub; pass --branch <name> to override.",
            )
    except Exception:
        pass  # non-fatal — continue

    # ── LM Studio check ─────────────────────────────────────────────────────
    if not skip_lm:
        llm = _check_lmstudio(config)
    else:
        from gitoma.planner.llm_client import LLMClient
        llm = LLMClient(config)
        _warn("LM Studio check skipped (--skip-lm-check)")

    console.print()

    # ── Clone ───────────────────────────────────────────────────────────────
    git_repo = _clone_repo(repo_url, config)

    # ── Pin worktree to base BEFORE anything reads the tree ────────────────
    # Single source of truth: every downstream consumer (language detector,
    # analyzer, file_tree snapshot, planner, worker) sees the same tree —
    # the one rooted at ``base_branch``. Skipping this when --base is the
    # default is a no-op (clone already left us there). Doing it later is
    # incoherent: planner pins paths for the wrong tree, worker then
    # operates on the right tree but with a plan that doesn't match.
    if git_repo.current_branch() != base_branch:
        try:
            git_repo.checkout_base(base_branch)
            console.print(f"[muted]Base branch checked out: {base_branch}[/muted]")
        except Exception as e:
            _abort(
                f"Failed to check out base branch '{base_branch}': {e}",
                hint=f"Ensure '{base_branch}' exists on origin (gh api repos/{owner}/{name}/branches).",
            )

    languages = git_repo.detect_languages() or ["Unknown"]
    console.print(f"[muted]Languages detected: {', '.join(languages)}[/muted]")

    # ── Initialize state (fresh run) or reuse the persisted one (--resume) ─
    # On --resume we keep ``existing_state`` (so phase, metric_report,
    # task_plan and PR fields survive). The branch from the persisted
    # state wins over the freshly-generated one — otherwise we'd push
    # commits to a different branch than the one the prior run created.
    if existing_state and resume:
        state = existing_state
        if state.branch:
            branch = state.branch
    else:
        state = AgentState(
            repo_url=repo_url,
            owner=owner,
            name=name,
            branch=branch,
            phase=AgentPhase.ANALYZING,
        )
    save_state(state)

    # Local handles populated by their respective phases. When --resume
    # skips a phase, we rehydrate the handle from the persisted state so
    # later phases (which take ``report``/``plan``/``pr_info`` as inputs)
    # see the same values they would have on a fresh run.
    report = None
    plan = None
    pr_info = None

    with _heartbeat(state):

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 1 — ANALYZE
        # ────────────────────────────────────────────────────────────────────────
        # Resume gate on ANALYZING: skip only when we can actually rehydrate
        # the persisted metric report. A partial / schema-drifted state
        # file used to crash the whole resume with a KeyError out of
        # ``MetricReport.from_dict`` — we now fall back to re-running
        # analysis with a warning so the run still makes progress.
        report = None
        if _phase_already_done(state, AgentPhase.ANALYZING) and state.metric_report:
            try:
                report = MetricReport.from_dict(state.metric_report)
                console.print("[muted]↩ Skipping PHASE 1 — analysis already in state.[/muted]")
            except Exception as exc:
                console.print(
                    f"[warning]⚠ Could not restore metric_report from state "
                    f"({type(exc).__name__}: {str(exc)[:80]}). "
                    "Re-running PHASE 1.[/warning]"
                )
                report = None
        if report is None:
            with _phase("PHASE 1 — ANALYSIS", cleanup=git_repo, state=state):
                from gitoma.analyzers.registry import AnalyzerRegistry, ALL_ANALYZER_CLASSES

                registry = AnalyzerRegistry(
                    root=git_repo.root,
                    languages=languages,
                    repo_url=repo_url,
                    owner=owner,
                    name=name,
                    default_branch=base_branch,
                )

                n_analyzers = len(ALL_ANALYZER_CLASSES)
                with make_analyzer_progress() as progress:
                    task_id = progress.add_task(
                        "[heading]Analyzing repository…[/heading]", total=n_analyzers
                    )

                    def on_progress(analyzer_name: str, idx: int, total: int) -> None:
                        progress.update(
                            task_id,
                            description=f"[heading]{analyzer_name}[/heading]",
                            advance=1,
                        )
                        state.current_operation = f"Analyzing: {analyzer_name}"
                        save_state(state)

                    report = registry.run(on_progress=on_progress)

                state.metric_report = report.to_dict()
                state.current_operation = "Analysis complete"
                state.advance(AgentPhase.PLANNING)
                save_state(state)

        print_metric_report(report)

        if not report.failing and not report.warning:
            console.print(
                Panel(
                    "[success]✅ All metrics pass! This repo is already in great shape.[/success]",
                    border_style="success",
                )
            )
            _safe_cleanup(git_repo)
            state.current_operation = "All metrics already pass"
            state.advance(AgentPhase.DONE)
            save_state(state)
            return

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 2 — PLAN
        # ────────────────────────────────────────────────────────────────────────
        # Resume gate on PLANNING: skip only if we can actually rehydrate.
        # Same reasoning as PHASE 1 — fall back to re-planning on deser
        # failure instead of crashing the whole resume.
        plan = None
        if _phase_already_done(state, AgentPhase.PLANNING) and state.task_plan:
            try:
                plan = TaskPlan.from_dict(state.task_plan)
                console.print("[muted]↩ Skipping PHASE 2 — task plan already in state.[/muted]")
            except Exception as exc:
                console.print(
                    f"[warning]⚠ Could not restore task_plan from state "
                    f"({type(exc).__name__}: {str(exc)[:80]}). "
                    "Re-running PHASE 2.[/warning]"
                )
                plan = None
        if plan is None:
            with _phase("PHASE 2 — PLANNING", cleanup=git_repo, state=state):
                from gitoma.planner.planner import PlannerAgent
                from gitoma.planner.llm_client import LLMError

                console.print(
                    f"[muted]Asking {config.lmstudio.model} to generate improvement plan…[/muted]"
                )
                state.current_operation = f"Planning with {config.lmstudio.model}"
                save_state(state)
                file_tree = git_repo.file_tree(max_files=100)
                # Deterministic repo-wide brief (title, stack, build/test
                # commands, CI tools) — computed once, injected into the
                # planner prompt as shared ground truth. Silent on empty
                # repos (every field tolerated as None / []).
                from gitoma.context import extract_brief
                repo_brief = extract_brief(git_repo.root)
                planner = PlannerAgent(llm)

                try:
                    plan = planner.plan(report, file_tree, repo_brief=repo_brief)
                except LLMError as e:
                    # LLM-specific error — give actionable hint
                    console.print(
                        Panel(
                            f"[danger]LLM planning failed:[/danger] {e}\n\n"
                            "[muted]Possible causes:\n"
                            "  → LM Studio was closed during inference\n"
                            "  → Model context window exceeded (try a smaller repo)\n"
                            "  → Model returned malformed JSON (retry with --reset)[/muted]",
                            title="[danger]🤖 LLM Error[/danger]",
                            border_style="danger",
                        )
                    )
                    _safe_cleanup(git_repo)
                    raise typer.Exit(1)

                # ── Layer-2 Occam: deterministic plan post-process ──────────
                # Caught live on rung-3 v8: planner respected the prompt's
                # T001-priority-1 rule but interpreted "target failing test
                # paths" as "edit the test files" — wrong file. Layer-1
                # (the prompt) is necessary; Layer-2 (this) is the safety
                # net. Scope: ONLY rewrite T001's file_hints when (a)
                # Test Results metric is fail AND (b) every current
                # file_hint sits under a tests/-like dir. Test → source
                # mapping is per-language regex on imports.
                if plan and plan.tasks:
                    from gitoma.planner.test_to_source import rewrite_plan_in_place
                    _occam = rewrite_plan_in_place(plan, report, git_repo.root)
                    if _occam:
                        console.print(
                            f"[muted]Occam plan-rewrite: T001 file_hints "
                            f"{_occam['before']} → {_occam['after']}[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _ot
                            _ot().emit("plan.occam_rewrite", **_occam)
                        except Exception:
                            pass

                if not plan.tasks:
                    console.print("[warning]⚠ LLM returned an empty task plan. Nothing to do.[/warning]")
                    _safe_cleanup(git_repo)
                    # Advance to DONE so the cockpit / `gitoma status` show a
                    # clean terminal — without this the state was left at
                    # PLANNING + exit_clean=True (set by the heartbeat
                    # finally), which observers correctly read as "WORKING
                    # but stalled" → false orphan flag.
                    state.current_operation = "Plan was empty — nothing to do"
                    state.advance(AgentPhase.DONE)
                    save_state(state)
                    return

                state.task_plan = plan.to_dict()
                state.current_operation = f"Plan ready — {plan.total_tasks} tasks, {plan.total_subtasks} subtasks"
                state.advance(AgentPhase.WORKING)
                save_state(state)

        print_task_plan(plan)

        if dry_run:
            console.print(
                Panel(
                    "[warning]DRY RUN — plan generated but no commits or PR will be created.[/warning]\n"
                    "[muted]Remove [primary]--dry-run[/primary] to execute.[/muted]",
                    border_style="warning",
                    title="[warning]🧪 Dry Run[/warning]",
                )
            )
            _safe_cleanup(git_repo)
            # Dry-run completed its declared scope (analyze + plan, no
            # commit). DONE + an explanatory current_operation makes that
            # explicit instead of leaving phase=WORKING + exit_clean=True.
            state.current_operation = "Dry run complete — plan generated, no commits made"
            state.advance(AgentPhase.DONE)
            save_state(state)
            return

        if not yes:
            if not Confirm.ask(
                f"\n[primary]Proceed? ({plan.total_tasks} tasks · "
                f"{plan.total_subtasks} subtasks on branch [bold]{branch}[/bold])[/primary]",
                default=True,
            ):
                console.print("[muted]Aborted by user.[/muted]")
                _safe_cleanup(git_repo)
                # User-driven abort is a clean exit, but we still need a
                # terminal phase for the cockpit — otherwise the state
                # shows WORKING + exit_clean=True, which previously
                # confused observers ("did it finish? did it crash?").
                state.current_operation = "Aborted by user at confirmation prompt"
                state.advance(AgentPhase.DONE)
                save_state(state)
                return

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 3 — EXECUTE
        # ────────────────────────────────────────────────────────────────────────
        # Pre-declare any phase-3-set state PHASE 4 needs to read. Keeping
        # ``_qa_result`` initialised here means the PR composer can safely
        # treat it as ``None`` when QA was disabled / never reached / crashed.
        from gitoma.critic.types import QAResult as _QAResultT
        _qa_result_outer: _QAResultT | None = None
        with _phase("PHASE 3 — EXECUTION", cleanup=git_repo, state=state):
            from gitoma.worker.worker import WorkerAgent

            # Branch handling. On --resume the prior run may have pushed
            # partial commits to ``origin/<branch>``; we must check out
            # those commits and continue on top of them, otherwise
            # we'd commit fresh on the default base and the eventual
            # push would be a non-fast-forward. ``checkout_existing_branch``
            # returns False if there's no remote branch, in which case
            # we fall back to the normal "fresh local branch" path.
            try:
                resumed_branch = False
                if resume:
                    resumed_branch = git_repo.checkout_existing_branch(branch)
                if resumed_branch:
                    _ok(f"Resumed existing branch: {branch}")
                else:
                    # Worktree is already pinned to ``base_branch`` (done
                    # right after clone). Just branch off it.
                    git_repo.create_branch(branch)
                    _ok(f"Branch created: {branch} (off {base_branch})")
            except Exception as e:
                _abort(
                    f"Failed to create branch '{branch}': {e}",
                    hint="The branch may already exist locally. Use --reset to start fresh.",
                    state=state,
                )

            # Resume integrity check: a subtask marked ``status=completed``
            # with a ``commit_sha`` is only ACTUALLY completed if that
            # commit is reachable from HEAD. After a crash + resume where
            # the prior tempdir was cleaned before PHASE 4's push, the
            # commit lived only locally and is now gone. Flip those
            # subtasks back to "pending" so the worker re-runs them
            # instead of silently skipping lost work. No cost in the
            # happy path (all SHAs reachable → zero mutations).
            if resume and plan:
                lost: list[str] = []
                for task in plan.tasks:
                    any_lost_in_task = False
                    for sub in task.subtasks:
                        if sub.status == "completed" and sub.commit_sha:
                            if not git_repo.sha_reachable(sub.commit_sha):
                                lost.append(f"{sub.id}({sub.commit_sha[:7]})")
                                sub.status = "pending"
                                sub.commit_sha = ""
                                any_lost_in_task = True
                    # If any subtask of this task was lost, the task is
                    # no longer "completed" as a whole.
                    if any_lost_in_task and task.status == "completed":
                        task.status = "in_progress"
                if lost:
                    _warn(
                        f"{len(lost)} subtask(s) marked completed but commits not in branch — re-running",
                        hint=f"Lost SHAs: {', '.join(lost[:5])}"
                              + ("…" if len(lost) > 5 else "")
                              + " (commits likely never pushed before a prior crash)",
                    )
                    state.task_plan = plan.to_dict()
                    save_state(state)

            console.print()
            # Compile-fix mode: active when the Build Integrity analyzer
            # reported failure at audit time. Propagates into the patcher
            # so build-manifest edits are hard-rejected, not merely
            # prompt-discouraged.
            _compile_fix_mode = any(
                m.name == "build" and m.status == "fail" for m in report.metrics
            )
            worker = WorkerAgent(
                llm=llm,
                git_repo=git_repo,
                config=config,
                state=state,
                compile_fix_mode=_compile_fix_mode,
            )

            from gitoma.planner.task import SubTask, Task

            def on_task_start(task: Task) -> None:
                state.current_operation = f"Task {task.id}: {task.title}"
                save_state(state)
                console.print(
                    f"\n[task.current]▶ {task.id}[/task.current] "
                    f"[bold heading]{task.title}[/bold heading]"
                )

            def on_subtask_start(task: Task, sub: SubTask) -> None:
                state.current_operation = f"{sub.id}: {sub.title} — {config.lmstudio.model} generating"
                save_state(state)
                console.print(
                    f"  [muted]◌ {sub.id}[/muted] [info]{sub.title}[/info] "
                    f"[dim]({config.lmstudio.model} generating…)[/dim]"
                )

            def on_subtask_done(task: Task, sub: SubTask, sha: str | None) -> None:
                if sha:
                    state.current_operation = f"{sub.id} committed → {sha[:7]}"
                    save_state(state)
                    print_commit(sha, sub.title, sub.id)
                else:
                    state.current_operation = f"{sub.id} skipped (no changes)"
                    save_state(state)
                    console.print(f"  [warning]◎ {sub.id} — skipped (no file changes)[/warning]")

            def on_subtask_error(task: Task, sub: SubTask, error: str) -> None:
                state.current_operation = f"{sub.id} FAILED: {error[:80]}"
                save_state(state)
                console.print(f"  [danger]✗ {sub.id} failed: {error[:120]}[/danger]")
                # Visible trace event so silent worker failures (LLM
                # JSON-emit failure, all-patches-rejected, build-retry
                # exhaustion) show up in the jsonl alongside critic
                # events. Caught live on rung-3 v12: T001-S01/S02
                # failed with "Could not obtain valid JSON from LLM
                # after 3 attempts" but the trace had ZERO worker
                # events — only the state file recorded the error,
                # making post-mortems painful.
                try:
                    from gitoma.core.trace import current as _ct
                    _ct().emit(
                        "worker.subtask.failed",
                        task_id=task.id,
                        subtask_id=sub.id,
                        title=sub.title,
                        file_hints=sub.file_hints,
                        error=error[:500],
                    )
                except Exception:
                    pass

            plan = worker.execute(
                plan,
                on_task_start=on_task_start,
                on_subtask_start=on_subtask_start,
                on_subtask_done=on_subtask_done,
                on_subtask_error=on_subtask_error,
            )

        completed = plan.completed_tasks
        console.print(
            f"\n[success]✓ Execution complete — "
            f"{completed}/{plan.total_tasks} tasks done "
            f"({sum(s.status=='completed' for t in plan.tasks for s in t.subtasks)}/"
            f"{plan.total_subtasks} subtasks)[/success]"
        )

        if completed == 0:
            console.print(
                Panel(
                    "[danger]No tasks completed — aborting PR creation.[/danger]\n\n"
                    "[muted]All subtasks failed. Check LM Studio model output and retry.\n"
                    "Run with [primary]--dry-run[/primary] to inspect the plan without committing.[/muted]",
                    border_style="danger",
                    title="[danger]💥 Execution Failed[/danger]",
                )
            )
            _safe_cleanup(git_repo)
            # Advance to DONE before Exit so the cockpit's orphan
            # detector doesn't flag this deliberate failure as "process
            # vanished mid-run". The errors already on state explain
            # what went wrong; phase=DONE + non-empty errors is the
            # canonical "tried, failed cleanly" terminal.
            state.current_operation = (
                f"Worker phase failed: 0 of {plan.total_tasks} tasks completed"
            )
            failure_msg = "All subtasks failed during PHASE 3 (WORKING) — no PR created."
            if failure_msg not in state.errors:
                state.errors.append(failure_msg)
            state.advance(AgentPhase.DONE)
            save_state(state)
            raise typer.Exit(1)

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 3.5 — DEVIL'S ADVOCATE (broad-scope critic, iter 3)
        # ────────────────────────────────────────────────────────────────────────
        # Runs once per run, AFTER all subtasks committed and BEFORE the PR
        # opens. Sees the full branch diff (base..HEAD) so it can catch
        # things the per-subtask panel missed by virtue of seeing slices.
        # Configured via CRITIC_PANEL_DEVIL=true; uses a separate model +
        # optional separate endpoint (CRITIC_PANEL_DEVIL_MODEL,
        # CRITIC_PANEL_DEVIL_BASE_URL) so it can run a bigger model on
        # a beefier machine without slowing the worker.
        # Crash-safe: a devil failure is logged, never propagates.
        if (
            config.critic_panel.mode != "off"
            and config.critic_panel.devil_advocate
        ):
            try:
                from gitoma.core.trace import current as _current_trace
                from gitoma.critic import DevilsAdvocate
                _trace = _current_trace()
                _devil_diff = git_repo.repo.git.diff(f"{base_branch}..HEAD")
                with _trace.span(
                    "critic_devil.review",
                    branch=branch,
                    base=base_branch,
                    devil_model=config.critic_panel.devil_model or "(same as worker)",
                    devil_base_url=config.critic_panel.devil_base_url or "(same as worker)",
                ) as fields:
                    _devil = DevilsAdvocate(config.critic_panel, llm, config)
                    _devil_result = _devil.review(
                        full_branch_diff=_devil_diff,
                        branch_name=branch,
                    )
                    fields["verdict"] = _devil_result.verdict
                    fields["findings_count"] = len(_devil_result.findings)
                    fields["has_blocker"] = _devil_result.has_blocker()
                    if _devil_result.tokens_extra is not None:
                        fields["prompt_tokens"] = _devil_result.tokens_extra[0]
                        fields["completion_tokens"] = _devil_result.tokens_extra[1]
                # Per-finding events for greppability via
                # `gitoma logs --filter critic_devil`. The ``axiom`` field
                # surfaces the iter-6 categorisation so dashboards can
                # aggregate {¬M:n, ¬S:n, ¬A:n, ¬O:n} without re-parsing
                # the raw event. None when the model didn't tag the
                # finding (legacy / parse drift).
                for _f in _devil_result.findings:
                    _trace.emit(
                        "critic_devil.finding",
                        persona=_f.persona,
                        severity=_f.severity,
                        category=_f.category,
                        summary=_f.summary,
                        file=_f.file,
                        axiom=_f.axiom,
                    )
                # Persist into the same state log the panel uses; the
                # subtask_id "__devil__" makes it distinguishable.
                state.critic_panel_runs += 1
                state.critic_panel_findings_log.append(_devil_result.to_dict())
                # Cap at 200 entries (panel does the same).
                if len(state.critic_panel_findings_log) > 200:
                    del state.critic_panel_findings_log[
                        : len(state.critic_panel_findings_log) - 200
                    ]
                save_state(state)

                # ── PHASE 3.6 — REFINEMENT TURN (cap 1) + META-EVAL ──
                # If the devil flagged blocker/major findings, give the
                # actor exactly ONE chance to fix them with a follow-up
                # commit. The meta-eval then decides whether to keep the
                # refinement or revert it (default conservative: keep v0
                # on tie / parse failure / crash).
                from gitoma.critic import Refiner, MetaEval
                _refiner = Refiner(config.critic_panel, llm, config)
                if _refiner.should_refine(_devil_result.findings):
                    # Snapshot v0 SHA before any refinement commit so we can
                    # revert if meta-eval votes v0.
                    _v0_sha = git_repo.repo.head.commit.hexsha
                    _v0_diff = git_repo.repo.git.diff(f"{base_branch}..HEAD")

                    # Read current content of files referenced by triggers
                    # so the refiner can perform "modify" actions without
                    # hallucinating. Without this, gemma-4 was emitting
                    # unified-diff fragments into the ``content`` field
                    # (live-observed on iter4 first run).
                    _flagged_paths = {
                        f.file for f in _devil_result.findings
                        if f.severity in ("blocker", "major") and f.file
                    }
                    _flagged_content: dict[str, str] = {}
                    for _fp in _flagged_paths:
                        _txt = git_repo.read_file(_fp)
                        if _txt is not None:
                            _flagged_content[_fp] = _txt

                    with _trace.span(
                        "critic_refiner.propose",
                        triggers_count=sum(
                            1 for f in _devil_result.findings
                            if f.severity in ("blocker", "major")
                        ),
                        files_supplied=len(_flagged_content),
                    ) as rfields:
                        _refine_out = _refiner.propose(
                            branch_diff=_v0_diff,
                            devil_findings=_devil_result.findings,
                            flagged_files_content=_flagged_content,
                        )
                        rfields["patches_count"] = len(_refine_out.get("patches", []))

                    if _refine_out.get("patches"):
                        # Apply + commit the refinement (v1 = v0 + 1 commit).
                        from gitoma.worker.committer import Committer
                        from gitoma.worker.patcher import apply_patches
                        try:
                            # Refiner patches get the same compile-fix gate
                            # as worker patches — otherwise the refiner could
                            # still corrupt a build manifest on its way to
                            # "fixing" a devil-flagged issue. Default
                            # ``allowed_manifests=set()`` (empty) — the refiner
                            # has no per-subtask file_hint context to consent
                            # from, and reshaping deps here is almost always
                            # collateral damage rather than the intended fix.
                            _refine_touched = apply_patches(
                                git_repo.root,
                                _refine_out["patches"],
                                compile_fix_mode=_compile_fix_mode,
                                allowed_manifests=set(),
                            )
                            if _refine_touched:
                                Committer(git_repo, config).commit_patches(
                                    _refine_touched,
                                    _refine_out.get("commit_message")
                                    or "refine: address devil findings (1 turn)",
                                )
                                _v1_diff = git_repo.repo.git.diff(
                                    f"{base_branch}..HEAD"
                                )
                                # Meta-eval: keep v1 only if genuinely better.
                                with _trace.span(
                                    "critic_meta_eval.judge",
                                    v0_sha=_v0_sha[:8],
                                ) as mfields:
                                    _meta = MetaEval(
                                        config.critic_panel, llm, config
                                    )
                                    _winner, _rationale = _meta.judge(
                                        v0_diff=_v0_diff,
                                        v1_diff=_v1_diff,
                                        devil_findings=_devil_result.findings,
                                    )
                                    mfields["winner"] = _winner
                                    mfields["rationale"] = _rationale[:160]
                                # tie / v0 → revert v1 commit (keep v0)
                                if _winner != "v1":
                                    git_repo.repo.git.reset(
                                        "--hard", _v0_sha
                                    )
                                    _trace.emit(
                                        "critic_refiner.reverted",
                                        winner=_winner,
                                        rationale=_rationale[:160],
                                    )
                                else:
                                    _trace.emit(
                                        "critic_refiner.kept",
                                        rationale=_rationale[:160],
                                    )
                        except Exception as _refine_exc:  # noqa: BLE001
                            # Refinement / commit / meta failed AFTER
                            # patches applied — revert any partial changes.
                            from gitoma.core.trace import current as _ct
                            _ct().exception(
                                "critic_refiner.apply_failed", _refine_exc,
                            )
                            try:
                                git_repo.repo.git.reset("--hard", _v0_sha)
                            except Exception:
                                pass
            except Exception as _devil_exc:  # noqa: BLE001
                from gitoma.core.trace import current as _current_trace
                _current_trace().exception(
                    "critic_devil.crashed",
                    _devil_exc,
                )

            # ── PHASE 3.9 — Q&A self-consistency (gated by env) ─────────────
            # Rung-3 of the bench (2026-04-22pm) caught a devil
            # hallucination: the adversarial critic claimed a SQL-injection
            # fix existed when no change to db.py was in the diff. The Q&A
            # phase is the brutal-Questioner / gated-Defender check that
            # would have caught it: "Cite the file:line where the fix is."
            # Two-model architecture (Questioner = devil endpoint, Defender
            # = worker endpoint by default) decorrelates biases.
            #
            # OFF by default via ``CRITIC_QA_ENABLED``. Observation-only
            # unless ``CRITIC_QA_APPLY=true`` — the first few rungs collect
            # data on whether the Questioner's probes actually surface
            # real gaps before we let the Defender apply revised patches.
            #
            if (os.environ.get("CRITIC_QA_ENABLED") or "").lower() in ("1", "true", "yes"):
                try:
                    from gitoma.critic.qa import QAAgent
                    _qa_diff = git_repo.repo.git.diff(f"{base_branch}..HEAD")
                    if _qa_diff.strip():
                        # Collect the current content of any file that
                        # appears in the branch diff — gives both the
                        # Questioner and Defender real source to cite.
                        _qa_paths: list[str] = []
                        for _line in _qa_diff.splitlines():
                            _m = re.match(r"^diff --git a/(\S+) b/\S+", _line)
                            if _m and _m.group(1) not in _qa_paths:
                                _qa_paths.append(_m.group(1))
                        _qa_files: dict[str, str] = {}
                        for _p in _qa_paths[:8]:  # cap prompt size
                            _t = git_repo.read_file(_p)
                            if _t is not None:
                                _qa_files[_p] = _t
                        _qa_goal = (
                            plan.tasks[0].title if plan and plan.tasks
                            else "improve repository quality"
                        )
                        _qa = QAAgent(
                            config.critic_panel, llm, config,
                        )
                        _qa_result = _qa.review(
                            subtask_goal=_qa_goal,
                            branch_diff=_qa_diff,
                            current_files=_qa_files,
                        )

                        # ── Apply gate: when the Defender proposed revised
                        # patches AND CRITIC_QA_APPLY is on, try to land them
                        # under BuildAnalyzer + test-run gate. Any failure =
                        # full revert so a bad "fix" never lands in the PR.
                        _qa_apply = (os.environ.get("CRITIC_QA_APPLY") or "").lower() in ("1", "true", "yes")
                        if _qa_apply and _qa_result.revised_patches:
                            _qa_pre_sha = git_repo.repo.head.commit.hexsha
                            try:
                                from gitoma.worker.patcher import apply_patches as _qa_apply_fn
                                # Q&A's revised patches: same conservative
                                # default as the refiner — manifests blocked
                                # unless the original Defender output
                                # explicitly named one. The Defender doesn't
                                # currently set per-patch sanction context,
                                # so default ``set()`` (= block all) is the
                                # honest stance.
                                _qa_touched = _qa_apply_fn(
                                    git_repo.root,
                                    _qa_result.revised_patches,
                                    compile_fix_mode=_compile_fix_mode,
                                    allowed_manifests=set(),
                                )
                                if not _qa_touched:
                                    raise ValueError("Q&A patches produced no file changes")

                                # Gate A: BuildAnalyzer
                                from gitoma.analyzers.build import BuildAnalyzer as _QABA
                                _qa_ba = _QABA(root=git_repo.root, languages=languages).analyze()
                                if _qa_ba.status == "fail":
                                    raise RuntimeError(
                                        f"Q&A revised build check failed: {_qa_ba.details[:200]}"
                                    )

                                # Gate B: test run (best-effort, language-detected)
                                _qa_test_ok, _qa_test_detail = _qa_run_tests(git_repo.root)
                                if not _qa_test_ok:
                                    raise RuntimeError(
                                        f"Q&A revised tests failed: {_qa_test_detail}"
                                    )

                                # Commit + keep
                                from gitoma.worker.committer import Committer as _QACommitter
                                _QACommitter(git_repo, config).commit_patches(
                                    _qa_touched,
                                    "critic(qa): revise patch per evidence-flip + gated re-check",
                                )
                                _qa_result.revised_applied = True
                                _trace.emit(
                                    "critic_qa.revised_kept",
                                    touched=_qa_touched,
                                )
                                console.print(
                                    f"[good]Q&A revised patch applied: {_qa_touched}[/good]"
                                )
                            except Exception as _qa_apply_exc:  # noqa: BLE001
                                # Hard revert: restore the pre-QA state so the
                                # PR ships with the original worker diff only.
                                try:
                                    git_repo.repo.git.reset("--hard", _qa_pre_sha)
                                except Exception:
                                    pass
                                _qa_result.revert_reason = str(_qa_apply_exc)[:200]
                                _trace.emit(
                                    "critic_qa.revised_reverted",
                                    reason=str(_qa_apply_exc)[:200],
                                )
                                console.print(
                                    f"[warn]Q&A revised patch reverted: {str(_qa_apply_exc)[:100]}[/warn]"
                                )

                        console.print(
                            f"[muted]{_qa_result.summary_line()}[/muted]"
                        )
                        # Persist the result to state for post-run inspection.
                        state.current_operation = _qa_result.summary_line()
                        save_state(state)
                        # Hoist to outer scope so PHASE 4 can annotate the PR
                        # body when Q&A reported a gap that wasn't closed.
                        _qa_result_outer = _qa_result
                except Exception as _qa_exc:  # noqa: BLE001
                    from gitoma.core.trace import current as _ct2
                    _ct2().exception("critic_qa.crashed", _qa_exc)
                    # Synthesise a crash-flagged QAResult so PHASE 4 can
                    # annotate the PR body. Silent absence ≠ all clear:
                    # without this, a reviewer would merge thinking the
                    # Q&A gate had passed when in reality it never
                    # produced answers. Rung-3 v13/v14 hit this — log
                    # had ``critic_qa.crashed`` but the PR body looked
                    # identical to a clean run.
                    try:
                        from gitoma.critic.types import QAResult as _QR
                        _qa_result_outer = _QR(
                            ran=False,
                            crashed=True,
                            crash_reason=f"{type(_qa_exc).__name__}: {str(_qa_exc)[:200]}",
                        )
                    except Exception:
                        # Defensive: never let the crash-annotation path
                        # itself crash the run. The trace event is the
                        # authoritative record either way.
                        pass

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 4 — PULL REQUEST
        # ────────────────────────────────────────────────────────────────────────
        # On --resume from PR_OPEN (or later), the prior run already pushed
        # the branch and opened the PR; state.pr_number / state.pr_url are
        # populated. We rebuild a thin _PRInfo from those instead of
        # re-running push_and_open_pr (which is mostly idempotent on an
        # existing PR but would still re-push, re-edit the body, and burn
        # GitHub API quota for nothing).
        from dataclasses import dataclass as _pr_dc

        @_pr_dc
        class _PRInfo:
            number: int
            url: str

        # Whether the PR was resolved (merged / closed) on a prior run and
        # we're only pulling it back into state. When True, phases 5 + 6
        # (self-review, CI watch) are skipped — self-reviewing a merged
        # PR is noise, CI watching a closed branch is a 404 hunt.
        pr_finalised = False

        # Persisted-PR validation: the cockpit may dispatch --resume long
        # after the prior run opened a PR. Between runs the PR might be
        # merged, closed, or (rarely) 404 if the repo was archived. We
        # query GitHub BEFORE trusting the skip so a stale state never
        # drives self-review / ci-watch against a non-existent PR.
        pr_state = "unknown"  # "open" | "merged" | "closed" | "missing" | "unknown"
        if (
            _phase_already_done(state, AgentPhase.WORKING)
            and state.pr_number
            and state.pr_url
        ):
            try:
                from github import GithubException
                gh_pr = gh.get_pr(owner, name, state.pr_number)
                if gh_pr.merged:
                    pr_state = "merged"
                elif gh_pr.state == "closed":
                    pr_state = "closed"
                else:
                    pr_state = "open"
            except GithubException as exc:
                if getattr(exc, "status", None) == 404:
                    pr_state = "missing"
                else:
                    # Transient API error — degrade to "unknown". Trust the
                    # persisted state (skip PHASE 4) to avoid re-creating a
                    # PR that probably still exists.
                    console.print(
                        f"[warning]⚠ Could not verify PR #{state.pr_number} "
                        f"on GitHub ({exc}); trusting persisted state.[/warning]"
                    )
                    pr_state = "open"
            except Exception as exc:
                console.print(
                    f"[warning]⚠ Unexpected error verifying PR #{state.pr_number}: "
                    f"{exc}; trusting persisted state.[/warning]"
                )
                pr_state = "open"

        # Decide the flow based on what GitHub told us.
        if pr_state == "open":
            console.print(
                f"[muted]↩ Skipping PHASE 4 — PR #{state.pr_number} already open.[/muted]"
            )
            pr_info = _PRInfo(number=state.pr_number, url=state.pr_url)
            _safe_cleanup(git_repo)
        elif pr_state in ("merged", "closed"):
            # PR terminal on GitHub — run's declared scope is effectively
            # done. Preserve the persisted pr_info for the final report,
            # but skip self-review + ci-watch below.
            verb = "was merged" if pr_state == "merged" else "is closed"
            console.print(
                f"[info]PR #{state.pr_number} {verb} on GitHub — "
                "skipping self-review + CI watch (nothing useful to do).[/info]"
            )
            pr_info = _PRInfo(number=state.pr_number, url=state.pr_url)
            pr_finalised = True
            _safe_cleanup(git_repo)
        elif pr_state == "missing":
            # The persisted PR was deleted (repo archived, or the PR was
            # hard-deleted by a GH admin). Clear the stale fields and
            # fall through to PHASE 4 so we re-create.
            console.print(
                f"[warning]⚠ Persisted PR #{state.pr_number} no longer exists on GitHub — "
                "clearing state and re-opening the PR.[/warning]"
            )
            state.pr_number = None
            state.pr_url = None
            save_state(state)
            # Fall through to the else branch below (PHASE 4 creation).
            pr_state = "unknown"
        if pr_state == "unknown":
            with _phase("PHASE 4 — PULL REQUEST", cleanup=git_repo, state=state):
                from gitoma.pr.pr_agent import PRAgent

                console.print(f"[muted]Pushing {branch} to origin and opening PR…[/muted]")
                state.current_operation = f"Pushing branch {branch} to origin"
                save_state(state)
                pr_agent = PRAgent(git_repo=git_repo, gh_client=gh, config=config, state=state)

                try:
                    pr_info = pr_agent.push_and_open_pr(
                        report=report,
                        plan=plan,
                        branch=branch,
                        base=base_branch,
                        qa_result=_qa_result_outer,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "push" in err_str.lower() or "rejected" in err_str.lower():
                        _abort(
                            f"Git push failed: {e}",
                            hint=(
                                "Ensure your token has 'contents:write' permission "
                                "on this repo. Also check the branch isn't protected."
                            ),
                            state=state,
                        )
                    elif "422" in err_str or "Unprocessable" in err_str:
                        _abort(
                            "GitHub rejected the PR (422 Unprocessable Entity)",
                            hint=(
                                "Possible causes: PR already exists, branch not pushed, "
                                "or head/base branch names are wrong."
                            ),
                            state=state,
                        )
                    else:
                        raise  # re-raise for the _phase guard to catch

            print_pr_panel(pr_info.url, pr_info.number, branch)

            state.current_operation = f"PR #{pr_info.number} opened"
            state.pr_number = pr_info.number
            state.pr_url = pr_info.url
            state.advance(AgentPhase.PR_OPEN)
            save_state(state)
            _safe_cleanup(git_repo)

        # ────────────────────────────────────────────────────────────────────
        # PHASE 5 — SELF-REVIEW (optional, default on)
        # Adversarial critic LLM reads the PR diff + posts findings as a
        # summary comment. The run stays at PR_OPEN; current_operation
        # narrates the critic pass so the cockpit shows progress.
        #
        # Skipped when the PR is already merged/closed (pr_finalised) —
        # posting critique on finalized PRs is noise the maintainer
        # explicitly moved past.
        # ────────────────────────────────────────────────────────────────────
        if no_self_review:
            console.print("\n[muted]Self-review skipped (--no-self-review).[/muted]")
        elif pr_finalised:
            console.print(
                "\n[muted]Self-review skipped — PR is already finalised on GitHub.[/muted]"
            )
        else:
            _run_self_review(config, owner, name, pr_info.number, state)

        # ────────────────────────────────────────────────────────────────────
        # PHASE 6 — CI WATCH (optional, default on)
        # Poll GitHub Actions on the freshly-pushed branch. On failure,
        # invoke the Reflexion agent (same as `gitoma fix-ci`) up to
        # `max_fix_attempts` times. Never re-raises — the PR is open and
        # any remediation failure just annotates the state so the cockpit
        # + `gitoma logs` surface the outcome.
        # ────────────────────────────────────────────────────────────────────
        if no_ci_watch:
            console.print(
                f"\n[muted]CI watch skipped (--no-ci-watch). "
                f"Next: run [primary]gitoma review {repo_url}[/primary] "
                "when you want to integrate external review comments.[/muted]"
            )
        elif pr_finalised:
            # Polling a closed branch for CI runs is either noise (merged)
            # or a wild goose chase (closed → branch may be deleted).
            console.print(
                "\n[muted]CI watch skipped — PR is already finalised on GitHub.[/muted]"
            )
        else:
            _watch_ci_and_maybe_fix(
                config, owner, name, branch, repo_url, state,
                auto_fix=not no_auto_fix_ci,
            )
            console.print(
                f"\n[muted]Next: run [primary]gitoma review {repo_url}[/primary] "
                "when you want to integrate external review comments.[/muted]"
            )

        # ────────────────────────────────────────────────────────────────────
        # Terminal phase: ``gitoma run`` has finished its declared scope
        # (analyze → plan → execute → PR → self-review → ci-watch). The
        # subsequent ``gitoma review`` is an explicit, separate user
        # action; until the user invokes it, *this* run is done. Without
        # this advance the state was left at PR_OPEN forever, which the
        # cockpit (and the orphan detector — PR_OPEN ∈ _NON_TERMINAL)
        # read as "still working". Phase=DONE + a clear current_operation
        # is the canonical "succeeded, ready for next manual step".
        state.current_operation = (
            f"Run complete — PR #{state.pr_number} open at {state.pr_url}"
            if state.pr_number
            else "Run complete"
        )
        state.advance(AgentPhase.DONE)
        save_state(state)
