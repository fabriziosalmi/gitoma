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
)
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
    if not branch:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"gitoma/improve-{ts}"

    # ── GitHub check ────────────────────────────────────────────────────────
    gh = GitHubClient(config)
    repo_info = _check_github(config, owner, name)
    print_repo_info(repo_info)
    base_branch = base or repo_info["default_branch"]

    # Verify the target branch doesn't already exist remotely
    try:
        remote_branches = gh.list_branches(owner, name)
        if branch in remote_branches:
            _warn(
                f"Branch '{branch}' already exists on GitHub",
                hint="A new timestamped branch will be used instead.",
            )
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            branch = f"gitoma/improve-{ts}"
            console.print(f"[muted]  New branch: {branch}[/muted]")
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

    # ── Initialize state ────────────────────────────────────────────────────
    state = AgentState(
        repo_url=repo_url,
        owner=owner,
        name=name,
        branch=branch,
        phase=AgentPhase.ANALYZING,
    )
    save_state(state)

    with _heartbeat(state):

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 1 — ANALYZE
        # ────────────────────────────────────────────────────────────────────────
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
            return

        if not yes:
            if not Confirm.ask(
                f"\n[primary]Proceed? ({plan.total_tasks} tasks · "
                f"{plan.total_subtasks} subtasks on branch [bold]{branch}[/bold])[/primary]",
                default=True,
            ):
                console.print("[muted]Aborted by user.[/muted]")
                _safe_cleanup(git_repo)
                return

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 3 — EXECUTE
        # ────────────────────────────────────────────────────────────────────────
        with _phase("PHASE 3 — EXECUTION", cleanup=git_repo, state=state):
            from gitoma.worker.worker import WorkerAgent

            # Create branch
            try:
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
            raise typer.Exit(1)

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 4 — PULL REQUEST
        # ────────────────────────────────────────────────────────────────────────
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
            console.print(
                f"\n[muted]Self-review skipped (--no-self-review). "
                f"Next: run [primary]gitoma review {repo_url}[/primary] "
                "once Copilot reviews the PR.[/muted]"
            )
        else:
            _run_self_review(config, owner, name, pr_info.number, state)
            console.print(
                f"\n[muted]Next: run [primary]gitoma review {repo_url}[/primary] "
                "when you want to integrate external review comments.[/muted]"
            )
