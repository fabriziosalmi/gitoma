"""gitoma run command."""

from __future__ import annotations

import atexit
from datetime import datetime, timezone
from typing import Annotated, Optional, TYPE_CHECKING

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
                planner = PlannerAgent(llm)

                try:
                    plan = planner.plan(report, file_tree)
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
                    git_repo.create_branch(branch)
                    _ok(f"Branch created: {branch}")
            except Exception as e:
                _abort(
                    f"Failed to create branch '{branch}': {e}",
                    hint="The branch may already exist locally. Use --reset to start fresh.",
                    state=state,
                )

            console.print()
            worker = WorkerAgent(llm=llm, git_repo=git_repo, config=config, state=state)

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

        if (
            _phase_already_done(state, AgentPhase.WORKING)
            and state.pr_number
            and state.pr_url
        ):
            console.print(
                f"[muted]↩ Skipping PHASE 4 — PR #{state.pr_number} already open.[/muted]"
            )
            pr_info = _PRInfo(number=state.pr_number, url=state.pr_url)
            _safe_cleanup(git_repo)
        else:
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
        # ────────────────────────────────────────────────────────────────────
        if no_self_review:
            console.print("\n[muted]Self-review skipped (--no-self-review).[/muted]")
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
