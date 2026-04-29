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
    plan_from_file: Annotated[
        Optional[str],
        typer.Option(
            "--plan-from-file",
            help="Path to a JSON file containing a hand-curated TaskPlan. "
            "Skips PHASE 2 (LLM planning) and uses the file as the plan. "
            "See gitoma/planner/plan_loader.py for the schema.",
        ),
    ] = None,
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
        # CPG-lite: build BEFORE PHASE 2 so both planner (Skeletal v1)
        # and worker (BLAST RADIUS) consume the same index. Was inside
        # PHASE 3 in v0; moved here in Skeletal v1 to expose the
        # signature view to the planner. Build failures are silent;
        # both consumers degrade to "no CPG signal" gracefully.
        # ────────────────────────────────────────────────────────────────────────
        _cpg_index = None
        if (os.environ.get("GITOMA_CPG_LITE") or "").strip().lower() == "on":
            # Check the repo against ALL languages CPG-lite supports
            # (Python, TypeScript, JavaScript, Rust, Go) — not just
            # Python. Bug caught by end-to-end bench (b2v is Rust+TS
            # +JS only and was silently skipping CPG build).
            _CPG_LANGUAGES = {
                "python", "typescript", "javascript", "rust", "go",
            }
            _has_indexable = any(
                lang.lower() in _CPG_LANGUAGES
                for lang in (git_repo.detect_languages() or [])
            )
            if _has_indexable:
                try:
                    import time as _t
                    from gitoma.cpg import build_index as _build_cpg
                    _cpg_t0 = _t.perf_counter()
                    _cpg_index = _build_cpg(git_repo.root)
                    _cpg_ms = int((_t.perf_counter() - _cpg_t0) * 1000)
                    console.print(
                        f"[muted]CPG-lite: indexed "
                        f"{_cpg_index.file_count()} indexable files, "
                        f"{_cpg_index.symbol_count()} symbols "
                        f"({_cpg_ms}ms)[/muted]"
                    )
                    try:
                        from gitoma.core.trace import current as _ct
                        _ct().emit(
                            "cpg.index_built",
                            file_count=_cpg_index.file_count(),
                            symbol_count=_cpg_index.symbol_count(),
                            reference_count=_cpg_index.reference_count(),
                            build_ms=_cpg_ms,
                        )
                    except Exception:
                        pass
                except Exception as _exc:  # noqa: BLE001 — defensive
                    console.print(
                        f"[muted]CPG-lite: build failed "
                        f"({type(_exc).__name__}); continuing without "
                        f"BLAST RADIUS / Skeletal signal[/muted]"
                    )
                    try:
                        from gitoma.core.trace import current as _ct2
                        _ct2().exception("cpg.index_build_failed", _exc)
                    except Exception:
                        pass

        # ────────────────────────────────────────────────────────────────────────
        # PHASE 2 — PLAN
        # ────────────────────────────────────────────────────────────────────────
        # Defaults for variables that PHASE 3 reads but only the LLM
        # branch of PHASE 2 sets (Occam fingerprint, repo brief, …).
        # Without these, --plan-from-file paths trip an UnboundLocalError
        # in PHASE 3.
        _repo_fp: dict | None = None
        _semgrep_baseline: set[tuple[str, str]] | None = None
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
        if plan is None and plan_from_file is not None:
            # Operator-curated plan path: skip PHASE 2 entirely. Load
            # the JSON, validate, and treat the result as if the LLM
            # planner had emitted it. The rest of the pipeline (post-
            # plan filters, worker, critic stack, PR, self-review)
            # runs unchanged. See plan_loader docstring + the
            # bench-generation 2026-04-28 finding for why this exists.
            from gitoma.planner.plan_loader import (
                PlanFileError,
                load_plan_from_file,
            )
            with _phase("PHASE 2 — PLANNING (curated plan)", cleanup=git_repo, state=state):
                console.print(
                    f"[muted]Loading hand-curated plan from "
                    f"{plan_from_file} (skipping LLM call)…[/muted]"
                )
                try:
                    plan = load_plan_from_file(plan_from_file)
                except PlanFileError as exc:
                    console.print(
                        Panel(
                            f"[danger]Could not load plan from file:[/danger] {exc}\n\n"
                            "[muted]The file must conform to TaskPlan.from_dict — "
                            "see gitoma/planner/plan_loader.py docstring for the "
                            "exact schema.[/muted]",
                            title="[danger]🤖 Plan File Error[/danger]",
                            border_style="danger",
                        )
                    )
                    _safe_cleanup(git_repo)
                    raise typer.Exit(1)
                console.print(
                    f"[primary]✓ Loaded {plan.total_tasks} task(s) / "
                    f"{plan.total_subtasks} subtask(s) from {plan_from_file}[/primary]"
                )
                state.task_plan = plan.to_dict()
                save_state(state)
        if plan is None:
            with _phase("PHASE 2 — PLANNING", cleanup=git_repo, state=state):
                from gitoma.planner.planner import PlannerAgent
                from gitoma.planner.llm_client import LLMError

                # Vertical-mode scope filter (Castelletto Taglio A):
                # when GITOMA_SCOPE=<name> is set (by `gitoma docs`,
                # `gitoma quality`, …) and the name matches a registered
                # Vertical, filter the audit to that vertical's allow-
                # listed metrics ONLY before the planner sees it. This
                # prevents the planner from proposing tasks for failing
                # Build/Test/Security metrics that are out-of-scope for
                # a narrowed vertical (avoids the lws dry-run failure
                # mode where a security false-positive caused T001 to
                # attempt edits to lws.py while the operator wanted a
                # docs-only PR).
                from gitoma.planner.scope_filter import (
                    active_vertical as _active_vertical,
                    filter_metrics_by_vertical as _filter_metrics_by_v,
                )
                _vertical = _active_vertical()
                if _vertical is not None:
                    _scope_summary = _filter_metrics_by_v(report, _vertical)
                    if _scope_summary:
                        console.print(
                            f"[muted]Scope={_vertical.name}: kept "
                            f"{_scope_summary['metrics_kept']}, "
                            f"dropped {_scope_summary['metrics_dropped']}[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _st
                            _st().emit("scope.metrics_filtered", **_scope_summary)
                        except Exception:
                            pass

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

                # Occam Observer prior-runs context — queried ONCE at
                # plan time. Feature off (no-op) when OCCAM_URL env
                # var is unset. Empty agent-log → empty string →
                # planner prompt unchanged. See
                # ``gitoma.context.occam_client`` for the fail-open
                # contract.
                from gitoma.context.occam_client import (
                    default_client as _occam_client,
                    format_agent_log_for_prompt as _fmt_agent_log,
                    format_fingerprint_for_prompt as _fmt_fingerprint,
                )
                _occam_cli = _occam_client()
                _log: list = []
                _prior_runs = ""
                # Repo fingerprint — Occam's verified "what is this repo"
                # snapshot. Powers TWO consumers:
                #   1. Planner prompt injection (== REPO FINGERPRINT (GROUND
                #      TRUTH) ==) — keeps the planner from inventing tasks
                #      around frameworks the repo doesn't use.
                #   2. G11 content-grounding guard in the worker apply loop
                #      — rejects e.g. an architecture.md that claims
                #      React+Redux in a Rust CLI repo.
                # Captured ONCE here so both consumers see the same
                # snapshot. ``None`` (Occam off / failed) → both consumers
                # silently skip; the rest of the pipeline runs unchanged.
                _repo_fp: dict | None = None
                _fingerprint_block = ""
                if _occam_cli.enabled:
                    _log = _occam_cli.get_agent_log(since="24h", limit=20)
                    _prior_runs = _fmt_agent_log(_log, max_bullets=15)
                    if _prior_runs:
                        console.print(
                            f"[muted]Occam: injected {len(_log)} prior-runs entries into planner context[/muted]"
                        )
                    _repo_fp = _occam_cli.get_repo_fingerprint(str(git_repo.root))
                    if _repo_fp:
                        _fingerprint_block = _fmt_fingerprint(_repo_fp)
                        if _fingerprint_block:
                            _fws = _repo_fp.get("declared_frameworks") or []
                            _manifests = _repo_fp.get("manifest_files") or []
                            console.print(
                                f"[muted]Occam fingerprint: "
                                f"{len(_manifests)} manifest(s), "
                                f"{len(_fws)} framework(s) — "
                                f"{', '.join(_fws[:3]) or '(none)'}[/muted]"
                            )

                # ────────────────────────────────────────────────────
                # PHASE 1.5 — LAYER0 CROSS-RUN MEMORY QUERY
                # ────────────────────────────────────────────────────
                # When LAYER0_GRPC_URL is set, ask Layer0 for the
                # top-K most relevant memories from this repo's
                # namespace. Memories are appended to ``_prior_runs``
                # so they reach the planner via the same context
                # channel Occam Observer's agent-log uses. Disabled
                # path: Layer0Client returns [] and nothing changes.
                #
                # Why this matters: without persistent memory the
                # planner re-proposes the same generic boilerplate
                # tasks every run on the same repo. Layer0 lets the
                # planner see "we already shipped Ruff config 3 days
                # ago" / "G18 fired on core_helpers.py last time" /
                # "PR #5 closed without merge — model gemma-4-e4b".
                try:
                    from gitoma.integrations.layer0 import (
                        Layer0Client as _L0Client,
                        dedupe_hits as _l0_dedupe,
                        namespace_for_repo as _l0_ns,
                    )
                    _l0 = _L0Client()
                    if _l0.enabled:
                        _l0_namespace = _l0_ns(owner, name)
                        _l0_query_seed = " ".join(
                            m.display_name for m in report.metrics
                            if m.status in ("fail", "warn")
                        ) or f"recent activity on {owner}/{name}"
                        # Single grouped call: top-K from each of
                        # 4 high-signal buckets in ONE round-trip.
                        # Pinned-fact comes FIRST in the prompt
                        # because architectural facts must override
                        # everything else when they exist.
                        # Backward-compat: if the server is older
                        # (pre-2026-04-29 ships) and doesn't expose
                        # SearchGroupedByText, the call returns []
                        # and we degrade to the legacy single search.
                        _bucket_tags = [
                            "pinned-fact",
                            "guard-fail",
                            "pr-shipped",
                            "plan-shipped",
                        ]
                        _l0_groups = _l0.search_grouped(
                            query=_l0_query_seed,
                            namespace=_l0_namespace,
                            group_tags=_bucket_tags,
                            k_per_group=3,
                        )
                        _injected = 0
                        if _l0_groups and any(g.hits for g in _l0_groups):
                            _l0_block_lines = [
                                "",
                                "## Cross-run memory (Layer0 — bucketised prior context)",
                                "",
                            ]
                            for _g in _l0_groups:
                                if not _g.hits:
                                    continue
                                # Dedup within bucket — same prefix
                                # (= near-identical text) collapses to
                                # the closer-matching hit. Across-bucket
                                # dedup intentionally NOT done so the
                                # planner sees the bucket structure
                                # even when one fact is tagged twice.
                                _bucket_hits = _l0_dedupe(list(_g.hits))
                                _l0_block_lines.append(
                                    f"### {_g.tag} (top {len(_bucket_hits)})"
                                )
                                for _h in _bucket_hits:
                                    _l0_block_lines.append(f"- {_h.text}")
                                    _injected += 1
                                _l0_block_lines.append("")
                            _l0_block = "\n".join(_l0_block_lines)
                            _prior_runs = (
                                _prior_runs + "\n" + _l0_block
                                if _prior_runs else _l0_block
                            )
                            console.print(
                                f"[muted]Layer0: injected {_injected} prior-runs "
                                f"memories across {sum(1 for g in _l0_groups if g.hits)}/"
                                f"{len(_bucket_tags)} buckets from ns={_l0_namespace}[/muted]"
                            )
                        else:
                            # Fallback to flat search for older servers
                            # OR brand-new namespaces where no tagged
                            # memories exist yet.
                            _l0_hits = _l0.search_memory(
                                query=_l0_query_seed,
                                namespace=_l0_namespace,
                                k=8,
                            )
                            # Flat search collides way more often than
                            # the bucketised path because there's no
                            # tag separation — dedup is essential here.
                            _l0_hits = _l0_dedupe(_l0_hits)
                            if _l0_hits:
                                _l0_block_lines = [
                                    "",
                                    "## Cross-run memory (Layer0 — most-relevant prior runs on this repo)",
                                    "",
                                ]
                                for _h in _l0_hits:
                                    _tag_str = (
                                        f" [{', '.join(_h.tags)}]" if _h.tags else ""
                                    )
                                    _l0_block_lines.append(
                                        f"- {_h.text}{_tag_str}"
                                    )
                                _l0_block = "\n".join(_l0_block_lines)
                                _prior_runs = (
                                    _prior_runs + "\n" + _l0_block
                                    if _prior_runs else _l0_block
                                )
                                console.print(
                                    f"[muted]Layer0: injected {len(_l0_hits)} "
                                    f"flat memories from ns={_l0_namespace} "
                                    f"(no tagged buckets matched)[/muted]"
                                )
                        _l0.close()
                except Exception as _l0_exc:  # noqa: BLE001 — must never escape
                    try:
                        from gitoma.core.trace import current as _ct_l0
                        _ct_l0().exception("layer0.query_failed", _l0_exc)
                    except Exception:
                        pass

                # Skeletal v1: compressed per-file signature view from
                # the CPG-lite index. Off by default OR when CPG isn't
                # built — falls back to file-tree-only behavior. Opt-out
                # independently via GITOMA_CPG_SKELETAL=off (so the
                # operator can keep CPG on for BLAST RADIUS but skip
                # the skeleton's prompt cost).
                _skeleton_block: str | None = None
                _skel_off = (
                    os.environ.get("GITOMA_CPG_SKELETAL") or ""
                ).strip().lower() == "off"
                if _cpg_index is not None and not _skel_off:
                    try:
                        from gitoma.cpg.skeletal import (
                            DEFAULT_MAX_CHARS as _SKEL_DEFAULT_MAX,
                            render_skeleton,
                        )
                        _skel_budget = _SKEL_DEFAULT_MAX
                        _budget_raw = os.environ.get(
                            "GITOMA_CPG_SKELETAL_BUDGET", "",
                        )
                        if _budget_raw:
                            try:
                                _skel_budget = max(0, int(_budget_raw))
                            except ValueError:
                                pass
                        _rendered = render_skeleton(_cpg_index, _skel_budget)
                        if _rendered:
                            _skeleton_block = _rendered
                            console.print(
                                f"[muted]Skeletal: injected "
                                f"{len(_rendered)} chars into planner "
                                f"prompt[/muted]"
                            )
                            try:
                                from gitoma.core.trace import current as _ctsk
                                _ctsk().emit(
                                    "cpg.skeletal_rendered",
                                    chars=len(_rendered),
                                    budget=_skel_budget,
                                )
                            except Exception:
                                pass
                    except Exception as _exc:  # noqa: BLE001 — defensive
                        try:
                            from gitoma.core.trace import current as _ctsk2
                            _ctsk2().exception(
                                "cpg.skeletal_render_failed", _exc,
                            )
                        except Exception:
                            pass

                # ────────────────────────────────────────────────────
                # PHASE 1.6 — SEMGREP STATIC-ANALYSIS CONTEXT
                # ────────────────────────────────────────────────────
                # When the `semgrep` binary is on PATH, run the
                # registry's auto-config ruleset, sort findings by
                # severity (ERROR first), and inject the top-N as
                # actionable security/quality issues for the planner.
                # Skipped when binary missing / scan errors / repo
                # has no findings / GITOMA_PHASE16_OFF=1. Cap at
                # 20 findings to protect prompt budget.
                _semgrep_block: str | None = None
                # _semgrep_baseline initialised above PHASE 2 to None so
                # --plan-from-file paths still construct WorkerAgent OK
                _phase16_off = (
                    os.environ.get("GITOMA_PHASE16_OFF") or ""
                ).strip().lower() in ("1", "true", "yes")
                if not _phase16_off:
                    try:
                        from gitoma.integrations.semgrep_scan import (
                            SemgrepClient as _SgClient,
                            render_findings_block as _sg_render,
                        )
                        from gitoma.worker.semgrep_regression import (
                            compute_baseline_fingerprints as _sg_baseline,
                            g21_severity_floor as _sg_floor,
                        )
                        _sg = _SgClient()
                        if _sg.enabled:
                            # Scan with a wide cap so the baseline gets
                            # the full picture; the prompt block uses
                            # only the top-20 (severity-sorted) for
                            # token budget. baseline = ALL findings at
                            # or above the configured severity floor.
                            _sg_findings = _sg.scan(
                                git_repo.root, max_findings=500,
                            )
                            if _sg_findings:
                                _semgrep_block = _sg_render(_sg_findings[:20])
                                _semgrep_baseline = _sg_baseline(
                                    _sg_findings, severity_floor=_sg_floor(),
                                )
                                _err_count = sum(
                                    1 for f in _sg_findings
                                    if f.severity.upper() == "ERROR"
                                )
                                console.print(
                                    f"[muted]PHASE 1.6: semgrep — "
                                    f"{len(_sg_findings)} finding(s) "
                                    f"({_err_count} ERROR, "
                                    f"baseline={len(_semgrep_baseline)})[/muted]"
                                )
                                try:
                                    from gitoma.core.trace import current as _ct16
                                    _ct16().emit(
                                        "phase16.semgrep_findings",
                                        total=len(_sg_findings),
                                        errors=_err_count,
                                        baseline_size=len(_semgrep_baseline),
                                    )
                                except Exception:
                                    pass
                    except Exception as _sg_exc:  # noqa: BLE001 — must never escape
                        try:
                            from gitoma.core.trace import current as _ct_sg
                            _ct_sg().exception("phase16.failed", _sg_exc)
                        except Exception:
                            pass

                # ────────────────────────────────────────────────────
                # PHASE 1.7 — STACK-SHAPE CONTEXT (occam-trees)
                # ────────────────────────────────────────────────────
                # When OCCAM_TREES_URL is reachable AND the RepoBrief
                # has stack signals, infer (stack, level), pull the
                # canonical scaffold from occam-trees, diff against
                # the current file_tree, and inject the missing-paths
                # delta as additive-only context for the planner.
                #
                # Skipped when: no stack signals, server unreachable,
                # match below threshold, or GITOMA_PHASE17_OFF=1.
                # Composes the 3 spider-web legs (occam-trees +
                # RepoBrief + planner) without any new dependencies.
                _scaffold_block: str | None = None
                _phase17_off = (
                    os.environ.get("GITOMA_PHASE17_OFF") or ""
                ).strip().lower() in ("1", "true", "yes")
                if (
                    not _phase17_off
                    and repo_brief is not None
                    and repo_brief.stack
                ):
                    try:
                        from gitoma.integrations.occam_trees import (
                            OccamTreesClient as _OTClient,
                        )
                        from gitoma.planner.scaffold_shape import (
                            compute_delta as _shape_delta,
                            infer_level as _shape_level,
                            infer_stack as _shape_infer,
                            render_shape_context as _shape_render,
                        )
                        _ot = _OTClient()
                        if _ot.enabled:
                            _stacks = _ot.list_stacks()
                            _inf = _shape_infer(repo_brief, _stacks) if _stacks else None
                            if _inf is not None:
                                _level = _shape_level(file_tree)
                                _resolved = _ot.resolve(_inf.stack_id, _level)
                                if _resolved is not None:
                                    _delta = _shape_delta(
                                        _resolved.flatten(), file_tree,
                                    )
                                    _block = _shape_render(
                                        stack_id=_inf.stack_id,
                                        stack_name=_inf.stack_name,
                                        level=_level,
                                        matched_components=_inf.matched_components,
                                        delta=_delta,
                                    )
                                    if _block:
                                        _scaffold_block = _block
                                        console.print(
                                            f"[muted]PHASE 1.7: shape inferred "
                                            f"= {_inf.stack_name} ({_inf.stack_id}) "
                                            f"L{_level} — {len(_delta)} canonical "
                                            f"path(s) missing[/muted]"
                                        )
                                        try:
                                            from gitoma.core.trace import current as _ct17
                                            _ct17().emit(
                                                "phase17.shape_inferred",
                                                stack_id=_inf.stack_id,
                                                level=_level,
                                                match_count=_inf.match_count,
                                                missing_count=len(_delta),
                                            )
                                        except Exception:
                                            pass
                            _ot.close()
                    except Exception as _ot_exc:  # noqa: BLE001 — must never escape
                        try:
                            from gitoma.core.trace import current as _ct_ot
                            _ct_ot().exception("phase17.failed", _ot_exc)
                        except Exception:
                            pass

                # ────────────────────────────────────────────────────
                # PHASE 1.8 — TRIVY SUPPLY-CHAIN CONTEXT
                # ────────────────────────────────────────────────────
                # When the `trivy` binary is on PATH, scan the repo
                # for dep CVEs + secrets + IaC misconfigs and inject
                # the top-N as concrete supply-chain issues for the
                # planner. Complementary to PHASE 1.6 (semgrep covers
                # in-code; trivy covers deps/secrets/IaC). Skipped
                # when binary missing / scan errors / no findings /
                # GITOMA_PHASE18_OFF=1. Cap at 20 findings (prompt
                # budget shared with the other context blocks).
                _trivy_block: str | None = None
                _phase18_off = (
                    os.environ.get("GITOMA_PHASE18_OFF") or ""
                ).strip().lower() in ("1", "true", "yes")
                if not _phase18_off:
                    try:
                        from gitoma.integrations.trivy_scan import (
                            TrivyClient as _TvClient,
                            render_findings_block as _tv_render,
                        )
                        _tv = _TvClient()
                        if _tv.enabled:
                            _tv_findings = _tv.scan(
                                git_repo.root, max_findings=20,
                            )
                            if _tv_findings:
                                _trivy_block = _tv_render(_tv_findings)
                                _by_kind = {"vuln": 0, "secret": 0, "misconfig": 0}
                                for _f in _tv_findings:
                                    _by_kind[_f.kind] = _by_kind.get(_f.kind, 0) + 1
                                console.print(
                                    f"[muted]PHASE 1.8: trivy — "
                                    f"{_by_kind.get('vuln', 0)} vuln, "
                                    f"{_by_kind.get('secret', 0)} secret, "
                                    f"{_by_kind.get('misconfig', 0)} misconfig[/muted]"
                                )
                                try:
                                    from gitoma.core.trace import current as _ct18
                                    _ct18().emit(
                                        "phase18.trivy_findings",
                                        total=len(_tv_findings),
                                        vulns=_by_kind.get("vuln", 0),
                                        secrets=_by_kind.get("secret", 0),
                                        misconfigs=_by_kind.get("misconfig", 0),
                                    )
                                except Exception:
                                    pass
                    except Exception as _tv_exc:  # noqa: BLE001 — must never escape
                        try:
                            from gitoma.core.trace import current as _ct_tv
                            _ct_tv().exception("phase18.failed", _tv_exc)
                        except Exception:
                            pass

                try:
                    plan = planner.plan(
                        report, file_tree,
                        repo_brief=repo_brief,
                        prior_runs_context=_prior_runs or None,
                        repo_fingerprint_context=_fingerprint_block or None,
                        vertical_addendum=(
                            _vertical.prompt_addendum if _vertical else None
                        ),
                        skeleton_context=_skeleton_block,
                        scaffold_context=_scaffold_block,
                        semgrep_context=_semgrep_block,
                        trivy_context=_trivy_block,
                    )
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
                # ── Layer-A: synthesize T000 if planner missed the real bug ───
                # Deterministic pre-filter: when Test Results metric is
                # failing AND the LLM-emitted plan doesn't touch the
                # source-under-test, prepend a synthesized priority-1
                # T000 task that does. Closes the rung-0 pattern where
                # the planner emits 12 generic-project subtasks and
                # never touches the actual broken file.
                if plan and plan.tasks:
                    from gitoma.planner.real_bug_filter import synthesize_real_bug_task
                    _real_bug = synthesize_real_bug_task(plan, report, git_repo.root)
                    if _real_bug:
                        console.print(
                            f"[muted]Real-bug pre-filter: synthesized T000 "
                            f"({_real_bug['failing_test_count']} failing test(s), "
                            f"sources: {', '.join(_real_bug['source_files'][:2])})[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _rbt
                            _rbt().emit("plan.real_bug_synthesized", **_real_bug)
                        except Exception:
                            pass

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

                # ── Layer-B: banish README-only subtasks ────────────────────
                # The recurring b2v PR #24/#26/#27 README destruction
                # pattern was dominantly the result of "Update README
                # with Documentation Links"-style subtasks the planner
                # invented. Drop deterministically unless the
                # Documentation metric is failing AND its details
                # cite README explicitly. Cheaper than catching the
                # destruction post-hoc with G13/G14.
                if plan and plan.tasks:
                    from gitoma.planner.real_bug_filter import banish_readme_only_subtasks
                    _readme_banish = banish_readme_only_subtasks(plan, report)
                    if _readme_banish:
                        _names = [
                            f"{d['subtask_id']}({d['file_hints'][0]})"
                            for d in _readme_banish["dropped_subtasks"][:3]
                        ]
                        console.print(
                            f"[muted]README-banish: dropped "
                            f"{_readme_banish['drop_count']} subtask(s) "
                            f"— {', '.join(_names)}[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _rbt2
                            _rbt2().emit("plan.readme_banished", **_readme_banish)
                        except Exception:
                            pass

                # ── Vertical scope filter: registry-driven ────────────────────
                # When a vertical is active (set by `gitoma docs`,
                # `gitoma quality`, …), drop every subtask whose
                # file_hints contain any path outside the vertical's
                # allow-list. Stricter than Layer-B (which only drops
                # README-only): here a subtask hinting BOTH an in-scope
                # and an out-of-scope file is OUT — under a narrowed
                # vertical, mixed-hint = boundary cross = drop.
                if plan and plan.tasks and _vertical is not None:
                    from gitoma.planner.scope_filter import (
                        filter_plan_by_vertical as _filter_plan_by_v,
                    )
                    _scope_plan_summary = _filter_plan_by_v(plan, _vertical)
                    if _scope_plan_summary:
                        _names = [
                            f"{d['subtask_id']}({','.join(d['file_hints'][:1])})"
                            for d in _scope_plan_summary["dropped_subtasks"][:4]
                        ]
                        console.print(
                            f"[muted]Scope={_vertical.name}: dropped "
                            f"{_scope_plan_summary['drop_count']} subtask(s) "
                            f"— {', '.join(_names)}[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _sft
                            _sft().emit("scope.plan_filtered", **_scope_plan_summary)
                        except Exception:
                            pass

                # ── G9: Post-plan filter against Occam failure history ───────
                # Soft prompt injection (the PRIOR RUNS CONTEXT block fed
                # to the planner) is too gentle for 4B-class planners.
                # Caught live on rung-3 v24: planner saw the v23 fail log
                # for T001-S02 on tests/test_db.py with ast_diff, then
                # rephrased the subtask title but kept identical
                # file_hints — worker hit the same slop again.
                # This filter is deterministic: drop any subtask whose
                # file_hints overlap with paths that have failed ≥
                # threshold times in the recent agent-log window.
                # No-op when Occam is off / agent-log empty / nothing
                # overlaps. Threshold defaults to 2, env override via
                # ``GITOMA_OCCAM_FILTER_THRESHOLD``.
                #
                # Wider window than the planner-prompt fetch above:
                # the prompt only renders ~15 bullets so 24h/20 is fine
                # for display, but the failure counter needs the full
                # accumulated history (failure patterns from yesterday
                # are still relevant). Caught live v26b: 24h/20 sliced
                # the agent-log to the most-recent successes and
                # under-counted older repeated fails on
                # ``.github/workflows/ci.yml`` → those subtasks slipped
                # through the filter and re-failed in worker.
                if plan and plan.tasks and _occam_cli.enabled:
                    from gitoma.context.occam_client import count_failed_hints
                    from gitoma.planner.occam_filter import (
                        filter_plan_by_failure_history, resolve_threshold,
                    )
                    _g9_log = _occam_cli.get_agent_log(since="7d", limit=200)
                    _hints_count = count_failed_hints(_g9_log)
                    _g9_threshold = resolve_threshold()
                    _g9_summary = filter_plan_by_failure_history(
                        plan, _hints_count, threshold=_g9_threshold,
                    )
                    if _g9_summary["filtered_subtasks"]:
                        _dropped_names = [
                            f"{s['subtask_id']}({','.join(s['file_hints'][:2])})"
                            for s in _g9_summary["filtered_subtasks"][:4]
                        ]
                        console.print(
                            f"[muted]Occam filter: dropped "
                            f"{len(_g9_summary['filtered_subtasks'])} subtask(s) "
                            f"with file_hints failed ≥{_g9_threshold}× recently "
                            f"— {', '.join(_dropped_names)}[/muted]"
                        )
                        try:
                            from gitoma.core.trace import current as _oct
                            _oct().emit("plan.occam_filter", **_g9_summary)
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
            # CPG-lite index: built once before PHASE 2 (above) and
            # passed through to the worker here. ``_cpg_index`` may
            # be None when the env opt-in isn't set OR the repo has
            # no Python — the worker treats None as "no CPG signal"
            # and silently skips BLAST RADIUS injection.
            worker = WorkerAgent(
                llm=llm,
                git_repo=git_repo,
                config=config,
                state=state,
                compile_fix_mode=_compile_fix_mode,
                repo_fingerprint=_repo_fp,
                cpg_index=_cpg_index,
                semgrep_baseline=_semgrep_baseline,
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

            # Shared Occam client for the subtask callbacks. Lazy-
            # created here (same instance across all subtasks) so we
            # don't re-read the env var per callback fire. Disabled
            # when OCCAM_URL is unset — every method returns None /
            # empty and the pipeline proceeds unchanged.
            from gitoma.context.occam_client import (
                default_client as _occam_cb_client,
                map_error_to_failure_modes as _map_modes,
            )
            _occam_cb = _occam_cb_client()

            def _post_occam_observation(
                task: Task, sub: SubTask, *,
                outcome: str,
                sha: str | None,
                failure_modes: list[str],
            ) -> None:
                if not _occam_cb.enabled:
                    return
                try:
                    _occam_cb.post_observation({
                        "run_id": branch,
                        "agent": "gitoma",
                        "subtask_id": sub.id,
                        "model": llm.model,
                        "branch": branch,
                        "commit_sha": sha or "",
                        "outcome": outcome,
                        "touched_files": list(sub.file_hints or []),
                        "failure_modes": failure_modes,
                        "confidence": 0.7 if outcome == "success" else 0.3,
                    })
                except Exception:
                    pass  # fail-open — Occam is never critical path

            def on_subtask_done(task: Task, sub: SubTask, sha: str | None) -> None:
                if sha:
                    state.current_operation = f"{sub.id} committed → {sha[:7]}"
                    save_state(state)
                    print_commit(sha, sub.title, sub.id)
                    _post_occam_observation(
                        task, sub,
                        outcome="success", sha=sha, failure_modes=[],
                    )
                else:
                    state.current_operation = f"{sub.id} skipped (no changes)"
                    save_state(state)
                    console.print(f"  [warning]◎ {sub.id} — skipped (no file changes)[/warning]")
                    _post_occam_observation(
                        task, sub,
                        outcome="skipped", sha=None, failure_modes=[],
                    )

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
                _post_occam_observation(
                    task, sub,
                    outcome="fail", sha=None,
                    failure_modes=_map_modes(error),
                )

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
                            # Capture originals + test baseline BEFORE
                            # apply for G7 AST-diff and G8 runtime test
                            # gate. Both pure-read; safe even when the
                            # downstream check ends up no-op.
                            from gitoma.worker.patcher import (
                                read_modify_originals,
                                validate_post_write_syntax,
                                validate_top_level_preservation,
                            )
                            from gitoma.worker.config_grounding import (
                                validate_config_grounding,
                            )
                            from gitoma.worker.content_grounding import (
                                validate_content_grounding,
                            )
                            from gitoma.worker.doc_preservation import (
                                validate_doc_preservation,
                            )
                            from gitoma.worker.url_grounding import (
                                validate_url_grounding,
                            )
                            from gitoma.worker.schema_validator import (
                                validate_config_semantics,
                            )
                            from gitoma.analyzers.test_runner import (
                                detect_failing_tests,
                            )
                            _refine_originals = read_modify_originals(
                                git_repo.root, _refine_out["patches"],
                            )
                            # G8 baseline at v0: tests currently failing
                            # AFTER the worker's commits but BEFORE the
                            # refiner apply. Refiner is a regression if
                            # current - baseline becomes non-empty.
                            _refine_test_baseline = detect_failing_tests(
                                git_repo.root, languages,
                            )
                            _refine_touched = apply_patches(
                                git_repo.root,
                                _refine_out["patches"],
                                compile_fix_mode=_compile_fix_mode,
                                allowed_manifests=set(),
                            )
                            if _refine_touched:
                                # Per-file syntax check on refiner output.
                                # The refiner has no build-retry loop and
                                # didn't go through worker.py's
                                # _apply_with_build_retry — so without
                                # this we'd ship whatever the refiner
                                # wrote, syntax errors and all. Caught
                                # live rung-3 v16: refiner changed a
                                # triple-quote opener to an empty-string-
                                # plus-bare-bracket sequence in
                                # src/db.py, breaking pytest collection.
                                # On failure: revert touched files to v0
                                # and skip meta-eval entirely (v0 wins
                                # by default — a broken patch can never
                                # be a refinement).
                                _refine_syntax = validate_post_write_syntax(
                                    git_repo.root, _refine_touched,
                                )
                                if _refine_syntax is not None:
                                    _bad_path, _parser_msg = _refine_syntax
                                    _trace.emit(
                                        "critic_syntax_check.fail",
                                        phase="refiner",
                                        path=_bad_path,
                                        error=_parser_msg[:300],
                                    )
                                    git_repo.repo.git.reset(
                                        "--hard", _v0_sha
                                    )
                                    _trace.emit(
                                        "critic_refiner.reverted",
                                        winner="v0",
                                        rationale=(
                                            "syntax_check_failed: "
                                            f"{_bad_path}: "
                                            f"{_parser_msg[:120]}"
                                        ),
                                    )
                                else:
                                    # G10 semantic config check on refiner
                                    # output — same shape as the syntax
                                    # check above. JSON/YAML/TOML parses
                                    # valid but must also match the tool's
                                    # schema. If refiner ships a broken
                                    # ``.eslintrc.json`` shape, revert to
                                    # v0.
                                    _refine_schema_ok = True
                                    _refine_schema = validate_config_semantics(
                                        git_repo.root, _refine_touched,
                                    )
                                    if _refine_schema is not None:
                                        _bad_p, _sch_msg = _refine_schema
                                        _trace.emit(
                                            "critic_schema_check.fail",
                                            phase="refiner",
                                            path=_bad_p,
                                            error=_sch_msg[:300],
                                        )
                                        git_repo.repo.git.reset(
                                            "--hard", _v0_sha
                                        )
                                        _trace.emit(
                                            "critic_refiner.reverted",
                                            winner="v0",
                                            rationale=(
                                                "schema_check_failed: "
                                                f"{_bad_p}: {_sch_msg[:120]}"
                                            ),
                                        )
                                        _refine_schema_ok = False
                                    # G11 content-grounding on refiner
                                    # output — same shape as the schema
                                    # check above. Doc files (.md/.rst/
                                    # .txt) get checked against Occam's
                                    # ``/repo/fingerprint``. Catches the
                                    # b2v PR #21 failure mode in the
                                    # refiner path too: an architecture.md
                                    # that claims React+Redux in a Rust
                                    # CLI repo. Silent pass when Occam is
                                    # off / fingerprint missing.
                                    _refine_grounding_ok = True
                                    if _refine_schema_ok:
                                        _refine_grounding = (
                                            validate_content_grounding(
                                                git_repo.root,
                                                _refine_touched,
                                                _repo_fp,
                                            )
                                        )
                                        if _refine_grounding is not None:
                                            _bad_p, _gr_msg = _refine_grounding
                                            _trace.emit(
                                                "critic_content_grounding.fail",
                                                phase="refiner",
                                                path=_bad_p,
                                                error=_gr_msg[:300],
                                            )
                                            git_repo.repo.git.reset(
                                                "--hard", _v0_sha
                                            )
                                            _trace.emit(
                                                "critic_refiner.reverted",
                                                winner="v0",
                                                rationale=(
                                                    "content_grounding_failed: "
                                                    f"{_bad_p}: {_gr_msg[:120]}"
                                                ),
                                            )
                                            _refine_grounding_ok = False
                                    # G12 config-grounding on refiner
                                    # output — same shape as G11 above.
                                    # Catches the b2v PR #21 prettier
                                    # case in the refiner path too:
                                    # references to npm packages absent
                                    # from package.json.
                                    _refine_cfg_ok = True
                                    if _refine_schema_ok and _refine_grounding_ok:
                                        _refine_cfg = (
                                            validate_config_grounding(
                                                git_repo.root,
                                                _refine_touched,
                                                _repo_fp,
                                            )
                                        )
                                        if _refine_cfg is not None:
                                            _bad_p, _cf_msg = _refine_cfg
                                            _trace.emit(
                                                "critic_config_grounding.fail",
                                                phase="refiner",
                                                path=_bad_p,
                                                error=_cf_msg[:300],
                                            )
                                            git_repo.repo.git.reset(
                                                "--hard", _v0_sha
                                            )
                                            _trace.emit(
                                                "critic_refiner.reverted",
                                                winner="v0",
                                                rationale=(
                                                    "config_grounding_failed: "
                                                    f"{_bad_p}: {_cf_msg[:120]}"
                                                ),
                                            )
                                            _refine_cfg_ok = False
                                    # G13 doc-preservation on refiner output —
                                    # same shape as G12 above. Catches the
                                    # b2v PR #24/#26/#27 README destruction
                                    # in the refiner path too.
                                    _refine_doc_ok = True
                                    if _refine_schema_ok and _refine_grounding_ok and _refine_cfg_ok:
                                        _refine_doc = (
                                            validate_doc_preservation(
                                                git_repo.root,
                                                _refine_touched,
                                                _refine_originals,
                                            )
                                        )
                                        if _refine_doc is not None:
                                            _bad_p, _doc_msg = _refine_doc
                                            _trace.emit(
                                                "critic_doc_preservation.fail",
                                                phase="refiner",
                                                path=_bad_p,
                                                error=_doc_msg[:300],
                                            )
                                            git_repo.repo.git.reset(
                                                "--hard", _v0_sha
                                            )
                                            _trace.emit(
                                                "critic_refiner.reverted",
                                                winner="v0",
                                                rationale=(
                                                    "doc_preservation_failed: "
                                                    f"{_bad_p}: {_doc_msg[:120]}"
                                                ),
                                            )
                                            _refine_doc_ok = False
                                    # G14 URL-grounding on refiner output —
                                    # same shape as G13 above. Catches the
                                    # b2v PR #24/#27 fabricated URL/path
                                    # patterns when they sneak in via the
                                    # refiner instead of the worker.
                                    _refine_url_ok = True
                                    if _refine_schema_ok and _refine_grounding_ok and _refine_cfg_ok and _refine_doc_ok:
                                        _refine_url = (
                                            validate_url_grounding(
                                                git_repo.root,
                                                _refine_touched,
                                                _refine_originals,
                                            )
                                        )
                                        if _refine_url is not None:
                                            _bad_p, _url_msg = _refine_url
                                            _trace.emit(
                                                "critic_url_grounding.fail",
                                                phase="refiner",
                                                path=_bad_p,
                                                error=_url_msg[:300],
                                            )
                                            git_repo.repo.git.reset(
                                                "--hard", _v0_sha
                                            )
                                            _trace.emit(
                                                "critic_refiner.reverted",
                                                winner="v0",
                                                rationale=(
                                                    "url_grounding_failed: "
                                                    f"{_bad_p}: {_url_msg[:120]}"
                                                ),
                                            )
                                            _refine_url_ok = False
                                    # AST-diff guard on refiner output —
                                    # same shape as the syntax check above.
                                    # Catches the rung-3 v17/v18 pattern
                                    # extended to the refiner: a "modify"
                                    # patch that drops sibling functions
                                    # without flagging the deletion.
                                    _refine_ast = None
                                    if _refine_schema_ok and _refine_grounding_ok and _refine_cfg_ok and _refine_doc_ok and _refine_url_ok:
                                        _refine_ast = (
                                            validate_top_level_preservation(
                                                git_repo.root,
                                                _refine_touched,
                                                _refine_originals,
                                            )
                                        )
                                    if not _refine_schema_ok or not _refine_grounding_ok or not _refine_cfg_ok or not _refine_doc_ok or not _refine_url_ok:
                                        pass  # earlier guard already reset; skip
                                    elif _refine_ast is not None:
                                        _bad_p, _missing = _refine_ast
                                        _missing_list = ", ".join(
                                            sorted(_missing)
                                        )
                                        _trace.emit(
                                            "critic_ast_diff.fail",
                                            phase="refiner",
                                            path=_bad_p,
                                            missing=sorted(_missing),
                                        )
                                        git_repo.repo.git.reset(
                                            "--hard", _v0_sha
                                        )
                                        _trace.emit(
                                            "critic_refiner.reverted",
                                            winner="v0",
                                            rationale=(
                                                "ast_diff_failed: "
                                                f"{_bad_p}: missing "
                                                f"{_missing_list[:100]}"
                                            ),
                                        )
                                    else:
                                        # G8 runtime test regression
                                        # gate on refiner output. G6
                                        # (syntax) + G7 (AST) both
                                        # static — they miss content-
                                        # level semantic regressions
                                        # inside valid syntax. Caught
                                        # live rung-3 v16/v24: refiner
                                        # injected a stray ``>`` into
                                        # init_schema's SQL string —
                                        # file parses fine, top-level
                                        # defs preserved, tests error
                                        # at sqlite3 execute time.
                                        # On fail: reset to v0, skip
                                        # meta-eval.
                                        _refine_test_err: tuple[str, int] | None = None
                                        if _refine_test_baseline is not None:
                                            _refine_test_current = (
                                                detect_failing_tests(
                                                    git_repo.root,
                                                    languages,
                                                )
                                            )
                                            if _refine_test_current is not None:
                                                _refine_regressions = (
                                                    _refine_test_current
                                                    - _refine_test_baseline
                                                )
                                                if _refine_regressions:
                                                    _refine_test_err = (
                                                        sorted(_refine_regressions)[0],
                                                        len(_refine_regressions),
                                                    )
                                        if _refine_test_err is not None:
                                            _sample_test, _n = _refine_test_err
                                            _trace.emit(
                                                "critic_test_regression.fail",
                                                phase="refiner",
                                                sample=_sample_test,
                                                total_count=_n,
                                            )
                                            git_repo.repo.git.reset(
                                                "--hard", _v0_sha
                                            )
                                            _trace.emit(
                                                "critic_refiner.reverted",
                                                winner="v0",
                                                rationale=(
                                                    "test_regression_failed: "
                                                    f"{_n} test(s), e.g. {_sample_test}"
                                                ),
                                            )
                                        else:
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

        # ────────────────────────────────────────────────────────────
        # PHASE 7 — DIARY (opt-in, best-effort)
        # ────────────────────────────────────────────────────────────
        # When GITOMA_DIARY_REPO + GITOMA_DIARY_TOKEN are both set,
        # write a markdown summary of this run to the configured
        # remote log repo (e.g. fabgpt-coder/log). Filename includes
        # a timestamp + repo + branch slug so concurrent parallel
        # runs land different files and never conflict on commit.
        # All errors are swallowed + traced — a flaky log must NEVER
        # fail an otherwise-good gitoma run.
        try:
            from gitoma.cli.diary import DiaryConfig, write_diary_entry
            _diary_cfg = DiaryConfig.from_env()
            if _diary_cfg is not None:
                # Best-effort find of the trace JSONL for guard-firing
                # extraction. The path convention is set in
                # gitoma.core.trace (one file per run under
                # ~/.gitoma/logs/<owner>__<name>/<timestamp>-run.jsonl).
                _trace_dir = (
                    _Path.home() / ".gitoma" / "logs" / f"{owner}__{name}"
                )
                _trace_path = None
                if _trace_dir.is_dir():
                    _candidates = sorted(_trace_dir.glob("*-run.jsonl"))
                    _trace_path = _candidates[-1] if _candidates else None
                _diary_result = write_diary_entry(
                    diary_config=_diary_cfg,
                    repo_url=repo_url,
                    state=state,
                    plan=plan,
                    config=config,
                    trace_path=_trace_path,
                )
                if _diary_result.ok:
                    console.print(
                        f"[muted]Diary entry written to {_diary_cfg.repo}/"
                        f"{_diary_result.entry_path}[/muted]"
                    )
                else:
                    console.print(
                        f"[warning]Diary write failed (non-fatal): "
                        f"{_diary_result.error[:120]}[/warning]"
                    )
        except Exception:  # noqa: BLE001 — must never escape
            pass

        # ────────────────────────────────────────────────────────────
        # PHASE 8 — LAYER0 CROSS-RUN MEMORY INGEST (opt-in, best-effort)
        # ────────────────────────────────────────────────────────────
        # When LAYER0_GRPC_URL is set, push a small set of memories
        # about this run into the repo's namespace so the NEXT run
        # of `gitoma` on this same repo can query them. Memories
        # are short, tag-rich, single-fact strings — what Layer0
        # was designed for.
        #
        # We ingest at most 1 + N + 1 memories per run:
        #   * 1 plan-source line (LLM-planned vs operator-curated)
        #   * N guard-firing lines (one per unique critic_*.fail
        #     event, capped at 8)
        #   * 1 outcome line (PR opened or aborted, with subtask
        #     completion ratio)
        #
        # All errors swallowed — Layer0 down must never fail an
        # otherwise-good gitoma run. Same contract as PHASE 7 + the
        # client wrapper itself.
        try:
            from gitoma.integrations.layer0 import (
                Layer0Client as _L0WClient,
                namespace_for_repo as _l0_ns_w,
            )
            _l0w = _L0WClient()
            if _l0w.enabled:
                _l0w_namespace = _l0_ns_w(owner, name)

                # ── Plan source memory ──────────────────────────────
                # Curated plans (--plan-from-file) get pinned=True so
                # they survive retention pruning indefinitely. The
                # operator chose them deliberately; losing them to a
                # background TTL sweep would erase reproducibility.
                # LLM-generated plans are ephemeral (the planner
                # regenerates equivalents on demand) and follow the
                # namespace's TTL.
                _plan_src = (plan.llm_model if plan else "") or "llm"
                _is_curated = _plan_src.startswith("plan-from-file:")
                _plan_summary = (
                    f"Plan loaded: {plan.total_tasks if plan else 0} task(s), "
                    f"{plan.total_subtasks if plan else 0} subtask(s) — "
                    f"source={_plan_src} model={config.lmstudio.model}"
                )
                _plan_tags = ["plan-loaded", _plan_src.split(":")[0]]
                if _is_curated:
                    _plan_tags.append("pinned-fact")
                _l0w.ingest_one(
                    text=_plan_summary,
                    namespace=_l0w_namespace,
                    tags=_plan_tags,
                    pinned=_is_curated,
                )

                # ── Guard-firings memories ──────────────────────────
                # Reuse the trace JSONL the diary hook already
                # located. Re-extract here independently — both
                # hooks are best-effort and we don't want to share
                # state (PHASE 7 may have failed early).
                _trace_dir_w = (
                    _Path.home() / ".gitoma" / "logs" / f"{owner}__{name}"
                )
                _trace_path_w = None
                if _trace_dir_w.is_dir():
                    _candidates_w = sorted(_trace_dir_w.glob("*-run.jsonl"))
                    _trace_path_w = _candidates_w[-1] if _candidates_w else None
                _guard_events: list[str] = []
                if _trace_path_w is not None and _trace_path_w.exists():
                    import json as _json
                    try:
                        with _trace_path_w.open("r", encoding="utf-8") as _fh:
                            for _line in _fh:
                                try:
                                    _ev = _json.loads(_line)
                                except Exception:  # noqa: BLE001
                                    continue
                                _name = _ev.get("event") or ""
                                if (
                                    _name.startswith("critic_")
                                    and _name.endswith(".fail")
                                    and _name not in _guard_events
                                ):
                                    _guard_events.append(_name)
                                    if len(_guard_events) >= 8:
                                        break
                    except OSError:
                        pass
                # Optional TTL on guard-fail memories — high-volume
                # noise like repeated G14 firings on flaky test files
                # can flood the namespace within weeks. Operators set
                # LAYER0_GUARD_TTL_DAYS to age them out automatically;
                # 0 (default) = forever, matching prior behaviour.
                _guard_ttl_ms = 0
                try:
                    _guard_ttl_days = float(
                        os.environ.get("LAYER0_GUARD_TTL_DAYS") or "0",
                    )
                    if _guard_ttl_days > 0:
                        _guard_ttl_ms = int(_guard_ttl_days * 24 * 60 * 60 * 1000)
                except ValueError:
                    pass
                for _g in _guard_events:
                    _l0w.ingest_one(
                        text=f"Guard fired during run: {_g}",
                        namespace=_l0w_namespace,
                        tags=["guard-fail", _g.split(".")[0]],
                        ttl_ms=_guard_ttl_ms,
                    )

                # ── Outcome memory ──────────────────────────────────
                _subtasks_done = 0
                if state.task_plan:
                    for _t in state.task_plan.get("tasks", []) or []:
                        for _s in _t.get("subtasks", []) or []:
                            if _s.get("status") == "completed":
                                _subtasks_done += 1
                _total_st = plan.total_subtasks if plan else 0
                if state.pr_url:
                    _outcome = (
                        f"PR shipped #{state.pr_number} {_subtasks_done}/{_total_st} "
                        f"subtasks — {state.pr_url}"
                    )
                    _outcome_tags = ["pr-shipped"]
                else:
                    _outcome = (
                        f"Run finished without PR — {_subtasks_done}/{_total_st} "
                        f"subtasks completed"
                    )
                    _outcome_tags = ["run-no-pr"]
                _l0w.ingest_one(
                    text=_outcome, namespace=_l0w_namespace,
                    tags=_outcome_tags,
                )

                _written = 1 + len(_guard_events) + 1
                console.print(
                    f"[muted]Layer0: ingested {_written} memories into "
                    f"ns={_l0w_namespace}[/muted]"
                )
                _l0w.close()
        except Exception:  # noqa: BLE001 — must never escape
            try:
                from gitoma.core.trace import current as _ct_l0w
                _ct_l0w().emit("layer0.ingest_failed")
            except Exception:
                pass
