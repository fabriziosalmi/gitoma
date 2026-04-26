"""WorkerAgent — iterates TaskPlan, calls LLM for patches, commits each subtask."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import os

from gitoma.core.config import Config
from gitoma.core.repo import GitRepo
from gitoma.core.state import AgentState, save_state
from gitoma.core.trace import current as current_trace
from gitoma.critic import CriticPanel, PanelResult
from gitoma.critic.antislop import (
    Rule as AntislopRule,
    classify_for_subtask as antislop_classify,
    format_for_injection as antislop_format,
    load_rules as antislop_load_rules,
)
from gitoma.planner.llm_client import LLMClient
from gitoma.planner.prompts import worker_system_prompt, worker_user_prompt
from gitoma.planner.task import SubTask, Task, TaskPlan
from gitoma.worker.committer import Committer
from gitoma.worker.patcher import (
    BUILD_MANIFESTS,
    apply_patches,
    read_modify_originals,
    validate_post_write_syntax,
    validate_top_level_preservation,
)
from gitoma.worker.config_grounding import validate_config_grounding
from gitoma.worker.content_grounding import validate_content_grounding
from gitoma.worker.doc_preservation import validate_doc_preservation
from gitoma.worker.schema_validator import validate_config_semantics
from gitoma.worker.url_grounding import validate_url_grounding

# Cap on how many critic panel runs we keep in AgentState before dropping
# the oldest. State.json must stay manageable on long runs (60+ subtasks);
# the trace JSONL keeps the full history regardless.
_MAX_PANEL_LOG_ENTRIES = 200


class WorkerAgent:
    """Executes a TaskPlan by generating and committing file patches for each subtask."""

    def __init__(
        self,
        llm: LLMClient,
        git_repo: GitRepo,
        config: Config,
        state: AgentState,
        *,
        compile_fix_mode: bool = False,
        repo_fingerprint: dict[str, Any] | None = None,
        cpg_index: Any = None,
    ) -> None:
        self._llm = llm
        self._git = git_repo
        self._config = config
        self._state = state
        # When the Build Integrity analyzer reports failure, the worker
        # enters "compile-fix mode" — the patcher rejects any edit to
        # a build manifest (go.mod, pyproject.toml, package.json, …)
        # because a mid-compile-fix manifest edit is a near-certain
        # regression (caught rung-1 v2: ``# comments`` corrupted go.mod).
        self._compile_fix_mode = compile_fix_mode
        self._committer = Committer(git_repo, config)
        # Critic panel is created lazily — only when mode != "off" — so a
        # fresh-clone test that never touches the panel doesn't pay for an
        # unused object. None until first subtask in a non-off mode.
        self._critic_panel: CriticPanel | None = None
        # ANTISLOP rules — loaded lazily on first subtask. Cached here so
        # we don't re-parse the markdown for every subtask. Empty list
        # means "no ANTISLOP file found / feature off" — injection becomes
        # a no-op without erroring out.
        self._antislop_rules: list[AntislopRule] | None = None
        # G8 runtime-test regression gate: baseline set of tests that
        # were failing BEFORE the current subtask. After a patch is
        # applied, we re-run tests and compute ``current - baseline``
        # — anything in that set is a regression (a test that used to
        # pass/collect and now fails/errors). Populated lazily on first
        # use by ``_ensure_test_baseline``; ``None`` means "never
        # enforced this run" (toolchain missing, no languages, etc).
        # Opt-out via ``GITOMA_TEST_REGRESSION_GATE=off``.
        self._test_baseline: set[str] | None = None
        self._test_baseline_initialized: bool = False
        # G11 content-grounding: Occam's verified ``/repo/fingerprint``
        # snapshot. Drives the doc-grounding guard (rejects e.g. a
        # ``docs/architecture.md`` that claims React+Redux in a Rust
        # repo). ``None`` when Occam is disabled / unreachable, in
        # which case G11 silently passes.
        self._repo_fingerprint: dict[str, Any] | None = repo_fingerprint
        # CPG-lite v0 index — when non-None, the worker injects a
        # BLAST RADIUS block into the user prompt for every Python
        # file_hint, so the LLM sees who calls the symbols it's about
        # to modify. ``None`` (default) → silent skip; behavior
        # identical to pre-v0. Opt-in via ``GITOMA_CPG_LITE=on`` set
        # in the run pipeline before PHASE 3.
        self._cpg_index = cpg_index

    def execute(
        self,
        plan: TaskPlan,
        on_task_start: Callable[[Task], None] | None = None,
        on_subtask_start: Callable[[Task, SubTask], None] | None = None,
        on_subtask_done: Callable[[Task, SubTask, str | None], None] | None = None,
        on_subtask_error: Callable[[Task, SubTask, str], None] | None = None,
    ) -> TaskPlan:
        """
        Execute all pending tasks in the plan.
        Updates plan in-place and persists state after each subtask.

        Returns the updated TaskPlan.
        """
        file_tree = self._git.file_tree(max_files=100)
        languages = self._git.detect_languages()

        for task in plan.tasks:
            if task.status == "completed":
                continue

            task.status = "in_progress"
            if on_task_start:
                on_task_start(task)
            self._persist_plan(plan)

            all_subtasks_ok = True
            for subtask in task.subtasks:
                if subtask.status == "completed":
                    continue

                subtask.status = "in_progress"
                if on_subtask_start:
                    on_subtask_start(task, subtask)
                self._persist_plan(plan)

                try:
                    sha = self._execute_subtask(subtask, file_tree, languages)
                    subtask.status = "completed"
                    subtask.commit_sha = sha or ""
                    # Refresh file tree after changes
                    file_tree = self._git.file_tree(max_files=100)
                    if on_subtask_done:
                        on_subtask_done(task, subtask, sha)
                except Exception as e:
                    error_msg = str(e)[:200]
                    subtask.status = "failed"
                    subtask.error = error_msg
                    all_subtasks_ok = False
                    if on_subtask_error:
                        on_subtask_error(task, subtask, error_msg)

                self._persist_plan(plan)

            task.status = "completed" if all_subtasks_ok else "failed"
            self._persist_plan(plan)

        return plan

    def _execute_subtask(
        self,
        subtask: SubTask,
        file_tree: list[str],
        languages: list[str],
    ) -> str | None:
        """Generate patches for one subtask, apply them, and commit."""
        # Read current content of hinted files
        current_files: dict[str, str] = {}
        for hint in subtask.file_hints[:3]:  # cap to 3 files to control context
            content = self._git.read_file(hint)
            if content:
                current_files[hint] = content

        # ── ANTISLOP injection (iter 5) ──────────────────────────────────
        # Auto-select 5-15 anti-pattern rules relevant to THIS subtask
        # and append them to the worker's system prompt as "do not"
        # rules. Goal: shrink the search space at the source so the
        # model is less likely to emit slop in the first place.
        # Off when ANTISLOP_INJECTION=off; auto-on when the file exists
        # at $PWD/ANTISLOP.md or ~/.gitoma/antislop.md.
        antislop_block = self._antislop_block_for_subtask(subtask, languages)
        system_prompt = worker_system_prompt()
        if antislop_block:
            system_prompt = system_prompt + "\n\n" + antislop_block

        # CPG-lite v0 BLAST RADIUS — when the index is loaded and
        # the subtask touches Python files, render a compact "who
        # calls what you're about to modify" block. Empty string
        # when no Python in file_hints OR no relevant symbols
        # found; truthiness checked before injection to avoid an
        # empty header that the LLM would just ignore.
        blast_block: str | None = None
        if self._cpg_index is not None and subtask.file_hints:
            try:
                from gitoma.cpg.blast_radius import render_blast_radius_block
                rendered = render_blast_radius_block(
                    list(subtask.file_hints), self._cpg_index,
                )
                if rendered:
                    blast_block = rendered
            except Exception as exc:  # noqa: BLE001 — defensive
                # CPG failures must NOT kill the worker. Log + continue
                # without the block (graceful degradation).
                try:
                    current_trace().exception("cpg.blast_radius_failed", exc)
                except Exception:
                    pass

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": worker_user_prompt(
                    subtask_title=subtask.title,
                    subtask_description=subtask.description,
                    file_hints=subtask.file_hints,
                    languages=languages,
                    repo_name=self._git.name,
                    current_files=current_files,
                    file_tree=file_tree,
                    extra_context_block=blast_block,
                ),
            },
        ]

        # ── Apply + post-write compile-check + retry loop ──────────────────
        # If the patch breaks the build (compile/syntax error on the fresh
        # worktree), we revert the filesystem, inject the compiler's error
        # into the next prompt, and retry. Caught live on rung-1 v4:
        # worker hallucinated a function signature; compile failed; PR
        # still landed with broken code. Self-healing closes that gap.
        #
        # Budget: GITOMA_WORKER_BUILD_RETRIES env var (default 1 retry →
        # 2 total attempts). Zero disables the loop entirely.
        touched, commit_msg = self._apply_with_build_retry(
            messages, subtask, languages,
        )

        # ── Critic panel (M7, walking-skeleton iteration 1) ────────────────
        # Runs AFTER patches hit the filesystem but BEFORE the commit. In
        # advisory mode it only logs findings; the commit proceeds unchanged.
        # Wrapped in try/except defensively — a critic crash must not kill
        # the worker (the patch is good, the meta-observation is the cherry).
        if self._config.critic_panel.mode != "off":
            try:
                self._run_critic_panel(subtask, touched)
            except Exception as exc:  # noqa: BLE001 — see comment above
                current_trace().exception(
                    "critic_panel.crashed",
                    exc,
                    subtask_id=subtask.id,
                )

        # Commit
        sha = self._committer.commit_patches(touched, commit_msg)
        return sha

    # ── Post-write compile check + retry ────────────────────────────────────

    def _apply_with_build_retry(
        self,
        messages: list[dict[str, str]],
        subtask: SubTask,
        languages: list[str],
    ) -> tuple[list[str], str]:
        """Apply patches → build-check → on failure revert + re-prompt.

        Returns (touched_paths, commit_msg). Raises on exhausted retries.
        """
        max_retries = max(0, int(os.environ.get("GITOMA_WORKER_BUILD_RETRIES", "1")))
        max_attempts = max_retries + 1
        compile_error_feedback: str | None = None

        for attempt in range(1, max_attempts + 1):
            # On retry attempts (attempt > 1), rebuild the user prompt
            # with the compiler error attached as explicit feedback.
            active_messages = messages
            if compile_error_feedback is not None:
                active_messages = self._rebuild_prompt_with_error(
                    messages, subtask, languages, compile_error_feedback,
                )

            raw = self._llm.chat_json(active_messages)
            patches = raw.get("patches", [])
            commit_msg = raw.get("commit_message", f"chore: {subtask.title} [gitoma]")
            if not patches:
                raise ValueError("LLM returned no patches for subtask")
            if "[gitoma]" not in commit_msg:
                commit_msg += " [gitoma]"

            # Manifest-edit allow-list: only those manifests the planner
            # EXPLICITLY hinted at survive the always-on patcher block
            # (rung-3 v11 fallout — the worker's pyproject.toml collateral
            # broke pytest config-parse before any test could run).
            allowed = {
                Path(h).name for h in (subtask.file_hints or [])
                if Path(h).name in BUILD_MANIFESTS
            }
            # Capture the BEFORE-write content of every "modify" target.
            # Used by ``validate_top_level_preservation`` (post-apply)
            # to detect dropped functions / classes.
            originals = read_modify_originals(self._git.root, patches)
            touched = apply_patches(
                self._git.root, patches,
                compile_fix_mode=self._compile_fix_mode,
                allowed_manifests=allowed,
            )
            if not touched:
                raise ValueError("Patches produced no file changes")

            # G10 semantic config validation: for known config files
            # (ESLint, Prettier, package.json, tsconfig, github-
            # workflow, dependabot, Cargo), verify the content matches
            # the tool's JSON Schema. Catches the b2v v0.2.0 case:
            # ``.eslintrc.json`` with ``parser`` as object instead of
            # string (valid JSON → G2 silent, but ESLint refuses it).
            schema_err = validate_config_semantics(self._git.root, touched)
            if schema_err is not None:
                bad_path, schema_msg = schema_err
                err_text = (
                    f"Config schema check failed on {bad_path}: {schema_msg}. "
                    "The file parses as valid JSON/YAML/TOML but doesn't "
                    "match the tool's schema. Re-emit a patch with the "
                    "field shapes and option names the tool actually "
                    "accepts (check the tool's docs)."
                )
                current_trace().emit(
                    "critic_schema_check.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=schema_msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"Config schema check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Last error: {schema_msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # G11 content-grounding: doc files (.md/.rst/.txt) get
            # checked against Occam's verified ``/repo/fingerprint``.
            # Catches the b2v PR #21 failure mode (generated
            # ``architecture.md`` claimed a React+Redux frontend in
            # a pure-Rust CLI repo). Silent pass when fingerprint
            # is unavailable (Occam off / unreachable / no manifests
            # detected) — fail-open by design, like every other
            # Occam-derived feature.
            grounding_err = validate_content_grounding(
                self._git.root, touched, self._repo_fingerprint,
            )
            if grounding_err is not None:
                bad_path, msg = grounding_err
                err_text = (
                    f"Content-grounding check failed on {bad_path}: {msg} "
                    "Re-emit a patch that ONLY makes claims supported by "
                    "the actual repo (deps in the manifests, frameworks "
                    "in declared_frameworks). Either drop the unsupported "
                    "claim entirely or scope the doc to what's really there."
                )
                current_trace().emit(
                    "critic_content_grounding.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"Content-grounding check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Last error: {msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # G12 config-grounding: JS/TS config files (prettier,
            # tailwind, vite, webpack, …) get checked for package
            # references that don't appear in npm deps. Catches the
            # b2v PR #21 failure mode (prettier.config.js with
            # ``plugins: ['prettier-plugin-tailwindcss']`` shipped
            # without tailwindcss in package.json). Silent pass when
            # fingerprint missing or no package.json declared.
            cfg_grounding_err = validate_config_grounding(
                self._git.root, touched, self._repo_fingerprint,
            )
            if cfg_grounding_err is not None:
                bad_path, msg = cfg_grounding_err
                err_text = (
                    f"Config-grounding check failed on {bad_path}: {msg} "
                    "Re-emit a patch that ONLY references npm packages "
                    "actually declared in package.json. If the package "
                    "really IS needed, also propose adding it to the "
                    "deps in the same patch — but the patcher's manifest "
                    "block may reject; safer to drop the unsupported "
                    "reference."
                )
                current_trace().emit(
                    "critic_config_grounding.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"Config-grounding check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Last error: {msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # G13 doc-preservation: doc files (.md/.rst/.txt/.mdx)
            # MUST keep their fenced code blocks across modify ops.
            # Catches the recurring b2v PR #24/#26/#27 README
            # destruction (bash examples deleted, replaced with
            # prose, or corrupted with literal ``\n`` text). Two
            # deterministic checks: char-count preservation +
            # literal-newline corruption signature.
            doc_err = validate_doc_preservation(
                self._git.root, touched, originals,
            )
            if doc_err is not None:
                bad_path, msg = doc_err
                err_text = (
                    f"Doc-preservation check failed on {bad_path}: {msg} "
                    "Re-emit the patch keeping every original ``` ```...``` ``` "
                    "fenced code block VERBATIM. Modify only the surrounding "
                    "prose if needed; never collapse multi-line examples or "
                    "drop runnable commands."
                )
                current_trace().emit(
                    "critic_doc_preservation.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"Doc-preservation check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Last error: {msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # G14 URL/path grounding: doc files (.md/.rst/.txt/.mdx)
            # MUST not introduce links to fabricated URLs (DNS fail
            # or HTTP 404) or non-existent relative paths. Catches
            # the b2v PR #24 invented-github.io-hostname pattern +
            # PR #27 invented-docs/guide/code/* paths. Carry-over
            # links exempt; opt-out via GITOMA_URL_GROUNDING_OFFLINE.
            url_err = validate_url_grounding(
                self._git.root, touched, originals,
            )
            if url_err is not None:
                bad_path, msg = url_err
                err_text = (
                    f"URL-grounding check failed on {bad_path}: {msg} "
                    "Re-emit the patch with link targets that actually "
                    "exist (real domains, real files in this repo) or "
                    "drop the unsupported references."
                )
                current_trace().emit(
                    "critic_url_grounding.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"URL-grounding check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Last error: {msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # AST-diff guard: every top-level def the original had,
            # the new content must still have. Catches the rung-3
            # v17/v18 failure mode (worker emitted a "modify" patch
            # on a test file that dropped the ``db`` fixture + 3
            # sibling tests because its rewrite "didn't need them").
            # New file parses cleanly so the syntax check above
            # passes — but test collection breaks at import. Same
            # revert+retry shape as the syntax check.
            ast_err = validate_top_level_preservation(
                self._git.root, touched, originals,
            )
            if ast_err is not None:
                bad_path, missing = ast_err
                missing_list = ", ".join(sorted(missing))
                err_text = (
                    f"AST-diff check failed on {bad_path}: top-level "
                    f"definition(s) {missing_list} were present in the "
                    "original file but missing from your new content. "
                    "Re-emit a patch that copies every existing function "
                    "and class verbatim, modifying ONLY the body of the "
                    "one you came to fix. Do NOT delete sibling functions, "
                    "fixtures, or classes from the file."
                )
                current_trace().emit(
                    "critic_ast_diff.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    missing=sorted(missing),
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"AST-diff check failed after {max_attempts} "
                        f"attempt(s) on {bad_path}. Missing: {missing_list}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            # Per-file post-write syntax check (TOML/JSON/YAML).
            # ``BuildAnalyzer`` covers source files; this catches
            # config/manifest authoring slop the language compiler
            # never sees. Caught live on rung-3 v12: a planner-
            # sanctioned T004 edit on ``pyproject.toml`` shipped
            # ``source = src`` (bare ident) — invalid TOML, pytest
            # config-parse fails at runtime, entire suite uncollectable.
            syntax_err = validate_post_write_syntax(self._git.root, touched)
            if syntax_err is not None:
                bad_path, parser_msg = syntax_err
                err_text = (
                    f"Syntax check failed on {bad_path}: {parser_msg}. "
                    "The patcher wrote the file but the parser refused "
                    "it. Re-emit a patch for this file with valid "
                    "syntax (TOML strings need quotes, JSON needs no "
                    "trailing commas, YAML respects indentation)."
                )
                current_trace().emit(
                    "critic_syntax_check.fail",
                    subtask_id=subtask.id,
                    attempt=attempt,
                    path=bad_path,
                    error=parser_msg[:300],
                )
                if attempt >= max_attempts:
                    self._revert_touched(touched)
                    raise ValueError(
                        f"Syntax check failed after {max_attempts} attempt(s) "
                        f"on {bad_path}. Last error: {parser_msg[:200]}"
                    )
                self._revert_touched(touched)
                compile_error_feedback = err_text
                continue

            err = self._post_write_build_check(languages)
            if err is None:
                # G8 runtime-test regression gate. BuildAnalyzer ensures
                # files compile; this ensures previously-passing tests
                # still pass. Catches body-level changes that AST-diff
                # (G7) can't see by design — e.g. rung-3 v20/v21 where
                # the worker simplified get_conn and dropped
                # ``row_factory = sqlite3.Row``: function signature
                # preserved, top-level def preserved, but the caller's
                # ``dict(row)`` explodes at runtime.
                regression_err = self._check_test_regression(
                    subtask, languages, attempt,
                )
                if regression_err is not None:
                    # revert + retry with feedback
                    if attempt >= max_attempts:
                        self._revert_touched(touched)
                        raise ValueError(
                            f"Test regression after {max_attempts} attempt(s). "
                            f"{regression_err[:300]}"
                        )
                    self._revert_touched(touched)
                    compile_error_feedback = regression_err
                    continue

                # Ψ gate: last quality check before commit. Pure-
                # math scoring — composes WITH the structural guards
                # above as the scalar layer that catches "passed every
                # binary check but is meh". Two flavors auto-selected
                # by the dispatcher in evaluate_psi_gate:
                #   * GITOMA_PSI_FULL=on → Ψ-full (Γ + Φ + ΔI + Ω +
                #     hard-min on Φ); requires CPG-lite for full signal.
                #   * GITOMA_PSI_LITE=on → Ψ-lite (Γ + Ω); legacy.
                # We always pass originals + cpg_index so Ψ-full has
                # what it needs; Ψ-lite ignores them.
                from gitoma.worker.psi_score import (
                    compute_psi_lite, evaluate_psi_gate,
                    _is_enabled as _psi_enabled,
                    _is_full_enabled as _psi_full_enabled,
                )
                if _psi_enabled() or _psi_full_enabled():
                    psi_score, psi_breakdown = compute_psi_lite(
                        self._git.root, touched, self._repo_fingerprint,
                    )
                    if psi_breakdown:
                        current_trace().emit(
                            "psi_lite.scored",
                            subtask_id=subtask.id,
                            attempt=attempt,
                            psi=psi_score,
                            weakest_file=psi_breakdown.get("weakest_file", ""),
                        )
                    psi_block = evaluate_psi_gate(
                        self._git.root, touched, self._repo_fingerprint,
                        originals=originals,
                        cpg_index=self._cpg_index,
                    )
                    if psi_block is not None:
                        bad_path, msg, br = psi_block
                        # Identify which flavor failed via breakdown
                        # shape: Ψ-full carries a "components" dict.
                        flavor = "Ψ-full" if "components" in br else "Ψ-lite"
                        err_text = (
                            f"{flavor} gate failed on {bad_path}: {msg} "
                            "Re-emit a patch with content that grounds "
                            "against the repo's actual deps and avoids "
                            "the listed slop patterns."
                        )
                        current_trace().emit(
                            "critic_psi_full.fail" if flavor == "Ψ-full"
                            else "critic_psi_lite.fail",
                            subtask_id=subtask.id,
                            attempt=attempt,
                            path=bad_path,
                            psi=br.get("psi", psi_score),
                            error=msg[:300],
                            components=br.get("components"),
                        )
                        if attempt >= max_attempts:
                            self._revert_touched(touched)
                            raise ValueError(
                                f"{flavor} gate failed after {max_attempts} "
                                f"attempt(s) on {bad_path}. Last Ψ={psi_score:.2f}"
                            )
                        self._revert_touched(touched)
                        compile_error_feedback = err_text
                        continue

                # Test Gen v1 (5th critic, opt-in) — autogenerate
                # tests for the new/changed public symbols in this
                # patch. Runs AFTER all gates pass so we know the
                # source patch itself is solid; the new tests
                # undergo a re-check on top. Failures here NEVER
                # block the patch — we just don't add the tests.
                from gitoma.critic.test_gen import (
                    TestGenAgent, is_test_gen_enabled,
                )
                if is_test_gen_enabled():
                    try:
                        agent = TestGenAgent(self._llm)
                        gen = agent.generate_for_patch(
                            touched=list(touched),
                            originals=originals,
                            repo_root=self._git.root,
                        )
                    except Exception as exc:  # noqa: BLE001 — defensive
                        current_trace().exception(
                            "test_gen.failed", exc,
                        )
                        gen = None
                    if gen:
                        test_patches = [
                            {"action": "create", "path": p, "content": c}
                            for p, c in gen.items()
                        ]
                        try:
                            added = apply_patches(
                                self._git.root, test_patches,
                                compile_fix_mode=False,
                            )
                        except Exception as exc:  # noqa: BLE001
                            current_trace().exception(
                                "test_gen.apply_failed", exc,
                            )
                            added = []
                        if added:
                            current_trace().emit(
                                "test_gen.generated",
                                subtask_id=subtask.id,
                                files=list(added),
                            )
                            # Re-run regression check on the combined
                            # patch. If the new tests broke the baseline
                            # (or themselves fail), revert ONLY the
                            # additions — keep the source patch.
                            re_regression = self._check_test_regression(
                                subtask, languages, attempt,
                            )
                            if re_regression is not None:
                                current_trace().emit(
                                    "test_gen.reverted",
                                    subtask_id=subtask.id,
                                    files=list(added),
                                    reason=re_regression[:200],
                                )
                                self._revert_touched(added)
                            else:
                                current_trace().emit(
                                    "test_gen.applied",
                                    subtask_id=subtask.id,
                                    files=list(added),
                                )
                                touched = list(touched) + list(added)

                if attempt > 1:
                    current_trace().emit(
                        "critic_build_retry.success",
                        subtask_id=subtask.id,
                        attempt=attempt,
                    )
                return touched, commit_msg

            # Build failed on this attempt. Decide: revert + retry, or give up.
            current_trace().emit(
                "critic_build_retry.fail",
                subtask_id=subtask.id,
                attempt=attempt,
                error=err[:500],
            )

            if attempt >= max_attempts:
                # Out of budget — revert, surface as subtask failure.
                self._revert_touched(touched)
                raise ValueError(
                    f"Build check failed after {max_attempts} attempt(s). "
                    f"Last error: {err[:300]}"
                )

            # Prepare retry: revert filesystem, carry the error forward.
            self._revert_touched(touched)
            compile_error_feedback = err

        # Unreachable — the loop either returns or raises.
        raise RuntimeError("unreachable: apply_with_build_retry loop exited")

    def _ensure_test_baseline(self, languages: list[str]) -> None:
        """Capture the set of currently-failing tests. Called once,
        lazily, on the first subtask that reaches the regression-gate
        step. ``None`` means "toolchain not available / no recognised
        stack / gate disabled" — we then silently skip regression
        checks for the rest of the run rather than flag false
        regressions from a broken baseline."""
        if self._test_baseline_initialized:
            return
        self._test_baseline_initialized = True
        if (os.environ.get("GITOMA_TEST_REGRESSION_GATE") or "").lower() in (
            "0", "off", "false", "no",
        ):
            self._test_baseline = None
            return
        try:
            from gitoma.analyzers.test_runner import detect_failing_tests
            self._test_baseline = detect_failing_tests(
                self._git.root, languages,
            )
        except Exception:
            # Defensive: a broken analyzer must not kill the run.
            self._test_baseline = None

    def _check_test_regression(
        self, subtask: SubTask, languages: list[str], attempt: int,
    ) -> str | None:
        """After a subtask's patches pass the build check, re-run
        tests and compare to baseline. Returns a feedback message on
        regression (tests that weren't in baseline but are now
        failing) or ``None`` on clean.

        Tests that MOVE from failing→passing (legit fix) do NOT
        trigger a regression — we compute ``current - baseline``,
        not set-inequality.
        """
        self._ensure_test_baseline(languages)
        if self._test_baseline is None:
            return None
        try:
            from gitoma.analyzers.test_runner import detect_failing_tests
            current = detect_failing_tests(self._git.root, languages)
        except Exception:
            return None
        if current is None:
            return None
        regressions = current - self._test_baseline
        if not regressions:
            # Baseline may shift forward on a legitimate fix. Keep
            # baseline = current so T-next doesn't re-flag whatever
            # was already failing before this patch but isn't any more.
            self._test_baseline = current
            return None
        sample = list(sorted(regressions))[:8]
        more = f" … (+{len(regressions) - 8} more)" if len(regressions) > 8 else ""
        current_trace().emit(
            "critic_test_regression.fail",
            subtask_id=subtask.id,
            attempt=attempt,
            regressions=sample,
            total_count=len(regressions),
        )
        bullets = "\n  • ".join(sample)
        return (
            f"Test regression detected: the following tests were passing "
            f"BEFORE your patch but are failing AFTER:\n  • {bullets}{more}\n\n"
            "Your patch changed behaviour those tests depend on. Re-emit "
            "preserving the semantics the tests expect — copy unchanged "
            "attributes (e.g. ``conn.row_factory``), unchanged string "
            "literals (SQL, regex patterns, URLs), and unchanged helper "
            "functions verbatim from the original file."
        )

    def _rebuild_prompt_with_error(
        self,
        original_messages: list[dict[str, str]],
        subtask: SubTask,
        languages: list[str],
        feedback: str,
    ) -> list[dict[str, str]]:
        """Rebuild the user prompt with compile_error_feedback attached,
        keeping the same system prompt (antislop + worker system)."""
        # Re-read current file content — the retry reads FRESH truth each
        # time so the model sees exactly what it just reverted from.
        current_files: dict[str, str] = {}
        for hint in subtask.file_hints[:3]:
            content = self._git.read_file(hint)
            if content:
                current_files[hint] = content
        file_tree = self._git.file_tree(max_files=100)

        new_user = worker_user_prompt(
            subtask_title=subtask.title,
            subtask_description=subtask.description,
            file_hints=subtask.file_hints,
            languages=languages,
            repo_name=self._git.name,
            current_files=current_files,
            file_tree=file_tree,
            compile_error_feedback=feedback,
        )
        # Keep the system message, replace the user message.
        return [original_messages[0], {"role": "user", "content": new_user}]

    def _post_write_build_check(self, languages: list[str]) -> str | None:
        """Run BuildAnalyzer on the CURRENT worktree. Return the error
        ``details`` string on failure, or None on clean / unknown-toolchain.

        Uses the same analyzer as audit time, so the error text format is
        consistent between planner-input and retry-feedback — the worker
        learns one error vocabulary, not two."""
        try:
            from gitoma.analyzers.build import BuildAnalyzer
            a = BuildAnalyzer(root=self._git.root, languages=languages)
            r = a.analyze()
        except Exception as exc:  # noqa: BLE001
            # A build-check crash must not block the pipeline.
            current_trace().exception("critic_build_check.crashed", exc)
            return None
        if r.status == "fail":
            return r.details
        return None

    def _revert_touched(self, touched: list[str]) -> None:
        """Undo filesystem changes made by the just-applied patches.

        Tracked files → ``git checkout HEAD -- path`` restores them.
        Untracked files (newly-created by the patch) → remove from disk.
        Directories are not touched — the patcher creates dirs under
        existing parents, so orphan dirs are harmless noise.
        """
        import subprocess
        for path in touched:
            abs_path = self._git.root / path
            try:
                check = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", "--", path],
                    cwd=str(self._git.root),
                    capture_output=True,
                    text=True,
                )
            except Exception:
                continue
            if check.returncode == 0:
                # Tracked: restore content from HEAD
                subprocess.run(
                    ["git", "checkout", "HEAD", "--", path],
                    cwd=str(self._git.root),
                    capture_output=True,
                )
            else:
                # Untracked: remove the file we just created
                try:
                    if abs_path.is_file():
                        abs_path.unlink()
                except OSError:
                    pass

    def _antislop_block_for_subtask(
        self,
        subtask: SubTask,
        languages: list[str],
    ) -> str:
        """Return the formatted ANTISLOP injection block for this subtask,
        or empty string when the feature is off / no file present.

        Lazy-loads ``ANTISLOP.md`` on first call. Subsequent subtasks reuse
        the cached rule list.

        Env vars:
          * ``ANTISLOP_INJECTION`` — ``off`` disables; any other value
            (including unset) enables the auto path that loads the file
            when present and no-ops when absent.
          * ``ANTISLOP_TOP_N`` — max rules per subtask (default 10).
        """
        if os.getenv("ANTISLOP_INJECTION", "auto").strip().lower() == "off":
            return ""
        if self._antislop_rules is None:
            self._antislop_rules = antislop_load_rules()
        if not self._antislop_rules:
            return ""

        try:
            top_n = int(os.getenv("ANTISLOP_TOP_N", "10"))
        except ValueError:
            top_n = 10

        # action_hint helps the classifier tighten on what's about to happen
        action_hint = (subtask.action or "").strip()
        selected = antislop_classify(
            rules=self._antislop_rules,
            file_hints=subtask.file_hints,
            languages=languages,
            action_hint=action_hint,
            top_n=top_n,
        )
        if not selected:
            return ""

        # iter 6: ``ANTISLOP_FORMAT=axioms`` switches injection to the
        # 4-axiom stratified format (¬M ¬S ¬A ¬O). Default ``flat``
        # preserves iter-5 behaviour. A/B by toggling on alternating runs.
        fmt = os.getenv("ANTISLOP_FORMAT", "flat").strip().lower()
        if fmt not in ("flat", "axioms"):
            fmt = "flat"

        # Trace event for A/B + observability — what was injected, in
        # what categories, for which subtask, in what format.
        try:
            current_trace().emit(
                "antislop.injected",
                subtask_id=subtask.id,
                rule_ids=[r.id for r in selected],
                rule_count=len(selected),
                top_n=top_n,
                format=fmt,
                tags_active=sorted({t for r in selected for t in r.tags}),
            )
        except Exception:
            pass  # trace must never break the worker

        return antislop_format(selected, mode=fmt)

    def _run_critic_panel(self, subtask: SubTask, touched: list[str]) -> None:
        """Build the diff for the touched files, run the panel, log the result.

        Pure side-effect method: emits trace events, mutates
        ``self._state.critic_panel_runs`` and ``critic_panel_findings_log``,
        does NOT alter the patch or the commit decision. Refinement /
        blocking behaviour lands in iteration 2/3.
        """
        if self._critic_panel is None:
            self._critic_panel = CriticPanel(self._config.critic_panel, self._llm)

        # Diff of files vs HEAD — what the committer is about to commit.
        # Scoped to ``touched`` so we never feed the panel unrelated noise
        # (other in-flight worktree edits, IDE-droppings).
        #
        # ``git add --intent-to-add`` first: without this, brand-new files
        # created by the patcher are UNTRACKED, and ``git diff HEAD`` does
        # not see untracked files at all — the panel would always get an
        # empty string for new-file subtasks (which is most of them) and
        # short-circuit to no_op. Caught live in the first real run on b2v:
        # subtasks that created PR templates / docs files all hit the
        # diff_text=="" branch silently.
        # Intent-to-add registers the path in the index with a NULL sha
        # WITHOUT staging the content. The committer's own ``git add``
        # later upgrades these entries to real index entries with content,
        # so this is an additive no-op for the commit pipeline.
        # Failures here are non-fatal — for already-tracked modified files
        # the diff still works and the panel still gets useful input.
        try:
            self._git.repo.git.add("--intent-to-add", "--", *touched)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception(
                "critic_panel.intent_to_add_failed",
                exc,
                subtask_id=subtask.id,
            )

        try:
            diff_text = self._git.repo.git.diff("HEAD", "--", *touched)
        except Exception as exc:  # noqa: BLE001
            current_trace().exception(
                "critic_panel.diff_failed",
                exc,
                subtask_id=subtask.id,
            )
            return

        # Lightweight repo context — list of touched paths only for now.
        # Iteration 2 may add `git ls-files` of the touched dirs so personas
        # can spot duplicates / orphans (lesson from PR#10 F002 audit fix).
        repo_context = "Touched files:\n" + "\n".join(f"  - {p}" for p in touched)

        with current_trace().span(
            "critic_panel.review",
            subtask_id=subtask.id,
            mode=self._config.critic_panel.mode,
            personas=self._config.critic_panel.personas,
        ) as fields:
            result: PanelResult = self._critic_panel.review(
                subtask_id=subtask.id,
                diff_text=diff_text,
                repo_files_summary=repo_context,
            )
            fields["verdict"] = result.verdict
            fields["findings_count"] = len(result.findings)
            fields["has_blocker"] = result.has_blocker()
            if result.tokens_extra is not None:
                fields["prompt_tokens"] = result.tokens_extra[0]
                fields["completion_tokens"] = result.tokens_extra[1]

        # Per-finding trace events — easier to grep ``gitoma logs`` for a
        # specific category than to walk the aggregated span. Includes
        # ``axiom`` for the iter-6 categorisation; None when the panel
        # persona didn't emit it (panel personas are pre-iter-6 by
        # design — only the devil's prompt requires axiom output).
        for f in result.findings:
            current_trace().emit(
                "critic_panel.finding",
                subtask_id=subtask.id,
                persona=f.persona,
                severity=f.severity,
                category=f.category,
                summary=f.summary,
                file=f.file,
                axiom=f.axiom,
            )

        # Persist into AgentState so the cockpit can render it without
        # parsing trace JSONL. Cap the log so a 60-subtask run doesn't
        # bloat state.json beyond reason.
        self._state.critic_panel_runs += 1
        log = self._state.critic_panel_findings_log
        log.append(result.to_dict())
        if len(log) > _MAX_PANEL_LOG_ENTRIES:
            del log[: len(log) - _MAX_PANEL_LOG_ENTRIES]
        save_state(self._state)

    def _persist_plan(self, plan: TaskPlan) -> None:
        """Update state with current plan and save to disk."""
        self._state.task_plan = plan.to_dict()
        save_state(self._state)
