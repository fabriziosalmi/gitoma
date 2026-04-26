"""All LLM prompt templates for the planner and worker."""

from __future__ import annotations

from gitoma.analyzers.base import MetricReport
from gitoma.context import RepoBrief, render_brief
from gitoma.worker.patcher import denylist_summary


# Build-system manifests: files whose bytes are parsed deterministically
# by a build toolchain. Any malformed edit here breaks the ENTIRE project
# before any unit test can fire (caught live on rung-1 v2: worker
# inserted Python-style ``#`` comments into go.mod and corrupted the
# build). The planner must never propose these when the intent is
# "fix a compile error elsewhere". Intersect-with-on-error-path logic
# stays with the worker / patcher, not the planner.
_BUILD_MANIFESTS = (
    "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
    "build.gradle", "build.gradle.kts", "pom.xml",
)


def planner_system_prompt() -> str:
    return (
        "You are FabGPT, an expert software engineer and DevOps specialist. "
        "Your job is to analyze a repository quality report and produce a structured improvement plan. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no prose, no code fences. "
        "Follow the schema exactly."
    )


def planner_user_prompt(
    report: MetricReport,
    file_tree: list[str],
    languages: list[str],
    repo_brief: RepoBrief | None = None,
    prior_runs_context: str | None = None,
    repo_fingerprint_context: str | None = None,
    vertical_addendum: str | None = None,
) -> str:
    metrics_summary = "\n".join(
        f"- {m.display_name}: score={m.score:.2f} status={m.status} | {m.details}"
        + (
            "\n  Suggestions: " + "; ".join(m.suggestions[:2])
            if m.suggestions
            else ""
        )
        for m in report.metrics
    )

    tree_sample = "\n".join(file_tree[:60])
    langs = ", ".join(languages) if languages else "Unknown"

    brief_block = ""
    if repo_brief is not None:
        brief_block = f"\n{render_brief(repo_brief)}\n"

    # Prior-runs context — agent-log replay from Occam Observer.
    # When present, tells the planner which patterns have failed in
    # recent runs on THIS repo so it doesn't re-propose them. Empty
    # string when the feature is off or the log is empty. Designed as
    # a "negative filter" signal (what NOT to emit), consistent with
    # the Meta-Dev Reflection framing.
    prior_runs_block = ""
    if prior_runs_context:
        prior_runs_block = (
            "\n== PRIOR RUNS CONTEXT (from Occam Observer) ==\n"
            f"{prior_runs_context}\n"
        )

    # Repo fingerprint — Occam's verified "what is this repo" snapshot.
    # Tagged GROUND TRUTH on purpose: every line is derived from
    # actually-existing manifest files, not LLM inference. The planner
    # is told NOT to propose subtasks that contradict it. This is the
    # planner-side half of G11 (the worker-side half rejects patches
    # whose CONTENT contradicts the fingerprint — e.g. a doc that
    # claims "React + Redux frontend" in a Rust CLI repo, which is the
    # exact b2v PR #21 hallucination this guard was designed for).
    fingerprint_block = ""
    if repo_fingerprint_context:
        fingerprint_block = (
            "\n== REPO FINGERPRINT (GROUND TRUTH — verified by Occam) ==\n"
            f"{repo_fingerprint_context}\n"
            "Do NOT propose subtasks (especially docs/configs) that "
            "introduce frameworks, deps, or stack elements absent from "
            "the lists above. Anything the lists call out as ``(none)`` "
            "is a hard constraint, not a suggestion.\n"
        )

    # Build Integrity status drives the compile-fix mode — an extra
    # constraint block we inject when the project does not compile.
    build_integrity_fail = any(
        m.name == "build" and m.status == "fail" for m in report.metrics
    )
    # Vertical addendum — declarative narrowing prompt from the active
    # Vertical record (Castelletto Taglio A). When `gitoma docs` is the
    # entry point, this block tells the LLM to emit ONLY doc-file
    # subtasks; future verticals follow the same shape. Empty string
    # when running full-pass `gitoma run`. Placed RIGHT BEFORE the
    # JSON-schema instruction so it is the last narrowing rule the
    # LLM sees before generating output (highest recency, highest
    # weight).
    vertical_block = ""
    if vertical_addendum:
        vertical_block = (
            f"\nHARD RULE — VERTICAL SCOPE: {vertical_addendum}\n"
        )

    compile_fix_block = ""
    if build_integrity_fail:
        manifests = ", ".join(f"`{m}`" for m in _BUILD_MANIFESTS)
        compile_fix_block = f"""
COMPILE-FIX MODE ACTIVE (Build Integrity = fail). Until the build is green:
  * FORBIDDEN file_hints (do NOT emit subtasks that edit these):
    {manifests}
    These are build-system manifests — any malformed edit breaks the
    whole project silently. The only exception is when the build error
    itself points AT one of these files (e.g., "missing module in go.mod"
    — even then, prefer a surgical "add missing line" subtask, not a
    full rewrite).
  * Your plan should be at most 2 tasks: T001 = fix the compile errors,
    optionally T002 = one follow-up if there's a closely-related issue.
    All cosmetic / scaffolding work (LICENSE, CONTRIBUTING, lint config,
    docs structure, CI workflows, dep audits) is DEFERRED until T001
    passes and the build is green.
"""

    return f"""Repository: {report.repo_url}
Languages: {langs}
Overall score: {report.overall_score:.2f}/1.0
{brief_block}{fingerprint_block}{prior_runs_block}
== METRIC REPORT ==
{metrics_summary}

== FILE TREE (sample) ==
{tree_sample}

== TASK ==
Create an improvement plan ONLY for metrics with status "fail" or "warn".
Order tasks by priority (1=highest urgency).
Each task must address a specific metric and include 1-4 concrete subtasks.

HARD RULE — BUILD INTEGRITY BEATS EVERYTHING: if the metric named
"Build Integrity" has status "fail", your plan MUST emit T001 with
priority=1 addressing THAT metric, with subtasks whose ``file_hints``
are the files surfaced in the build-error output (parse the ``details``
field — it lists ``path:line: message`` errors). You MUST NOT emit
cosmetic / scaffolding tasks (LICENSE, CONTRIBUTING, lint config, docs
structure, CI workflows) while the build is failing — a project that
does not compile cannot benefit from any of those. Emit them only once
the build is green.

HARD RULE — TEST RESULTS BEATS COSMETIC WORK (Occam pre-filter): if the
metric named "Test Results" has status "fail", your plan MUST emit a
priority=1 task addressing THOSE specific failing tests. The
``details`` field lists exact failing-test paths (``file::test_name``
or ``module::test_x``); your task's ``file_hints`` MUST point at the
SOURCE files those tests cover (read the test file → find the
``import`` / ``use`` of the production code → put THAT file as the
file_hint). You MUST NOT plan generic-project work (README, LICENSE,
CONTRIBUTING, docs, lint config) when there are failing tests — fix
the broken code first, scaffolding later. If Build Integrity is also
failing, Build wins (T001 = build fix, T002 = test fix).

HARD RULE — README IS A CONSEQUENCE, NOT A GOAL: do NOT emit subtasks
whose SOLE ``file_hints`` entry is ``README.md`` / ``README.rst`` /
``README``. README updates derive from code changes — they are not a
primary planning target. Most legitimate doc improvements live in
the ``docs/`` folder, not README. The only exception: the
"Documentation" metric is "fail" AND its ``details`` field
explicitly cites README by name. Otherwise: skip README entirely or
include it ONLY as a SECONDARY ``file_hints`` entry alongside the
code file actually being changed.
{compile_fix_block}{vertical_block}

Respond with ONLY this JSON schema (no extra text):
{{
  "tasks": [
    {{
      "id": "T001",
      "title": "short task title",
      "priority": 1,
      "metric": "metric_name",
      "description": "what this task accomplishes",
      "subtasks": [
        {{
          "id": "T001-S01",
          "title": "subtask title",
          "description": "exact change to make, be specific",
          "file_hints": ["relative/path/to/file.ext"],
          "action": "create"
        }}
      ]
    }}
  ]
}}

Rules:
- action must be one of: create, modify, delete, verify
- file_hints must be real paths relative to repo root
- file_hints MUST target a file (e.g. 'src/tests/basic.test.ts'), NEVER a bare directory ending with '/'
- Be specific in descriptions — include exact file names, content to add, etc.
- Maximum 8 tasks total, 4 subtasks per task
- Only address metrics with status fail or warn

== FORBIDDEN PATHS (patcher will reject — do not propose subtasks that touch these) ==
{denylist_summary()}

If a metric can only be fixed by editing a forbidden path (e.g. a broken CI
workflow), describe the fix in the task description so the maintainer can
apply it by hand — do NOT emit a subtask that targets the forbidden file.
"""


def worker_system_prompt() -> str:
    return (
        "You are FabGPT, an expert software engineer. "
        "Your job is to implement a specific improvement task for a code repository. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no prose, no code fences. "
        "The JSON must contain file patches to apply to the repository."
    )


def worker_user_prompt(
    subtask_title: str,
    subtask_description: str,
    file_hints: list[str],
    languages: list[str],
    repo_name: str,
    current_files: dict[str, str],
    file_tree: list[str],
    compile_error_feedback: str | None = None,
) -> str:
    langs = ", ".join(languages) if languages else "Unknown"

    files_section = ""
    if current_files:
        files_section = "\n== CURRENT FILE CONTENTS ==\n"
        for path, content in current_files.items():
            # Truncate long files
            if len(content) > 3000:
                content = content[:3000] + "\n... (truncated)"
            files_section += f"\n--- {path} ---\n{content}\n"

    tree_sample = "\n".join(file_tree[:40])

    # Post-write compile-check retry feedback. When the previous attempt
    # broke the build, we re-prompt with the compiler's error messages
    # instead of starting blind. This addresses the failure mode caught
    # on rung-1 v4: worker hallucinated ``Get(id int) (user, error)``
    # when the real signature was ``(string, bool)``. The compiler caught
    # it; now the worker gets a chance to fix based on actual evidence.
    retry_section = ""
    if compile_error_feedback:
        retry_section = f"""
== ⚠️ PREVIOUS ATTEMPT FAILED TO COMPILE ==
Your last patch was applied, the project's build/syntax check ran, and
it failed. Below is the EXACT error from the toolchain. READ it before
emitting a new patch:

{compile_error_feedback[:1500]}

RULES for this retry (non-negotiable):
  1. Do NOT invent function signatures or types. Read the CURRENT FILE
     CONTENTS section above — those are the REAL files in the repo.
     The signatures you see there are the truth; anything else is
     hallucination.
  2. Do NOT re-emit the same patch. The build check is deterministic;
     the same patch produces the same error.
  3. If the error says "assignment mismatch: N variables but f() returns
     M values", the caller must declare M variables with names that
     match what the test expects. Read the test file if you're unsure.
  4. If the error says "undefined", "not declared", or "no field", the
     name you used does NOT exist — pick one that the target file
     actually exports.
  5. Minimal change wins. Fix ONLY what broke; do not also refactor.

"""

    # ── Scope boundaries (rung-3 v13 fallout) ─────────────────────────
    # The worker over-scoped T001-S02 ("Verify Test Coverage for SQLi
    # Fix") from "make sure tests pass" to "rewrite db.py from stdlib
    # sqlite3 to psycopg2 + add src/main.py importing a function that
    # doesn't exist". The patch was syntactically valid TOML/Python,
    # the SQL was even correctly parameterised — but the test suite
    # broke because the entire scaffolding (get_conn/init_schema/seed)
    # was deleted and a phantom ``connect_to_database`` was imported
    # from a module that no longer exported it. None of our existing
    # patcher / build-check / syntax-check guards catch this — it's
    # semantically a different program now. The only place to push
    # back is the prompt: fence the scope BEFORE the LLM commits to
    # a direction.
    boundaries_section = """
== SCOPE BOUNDARIES — read before patching ==
The ``Files to touch`` line above is the COMPLETE scope of this
subtask. Treat the listed paths as a hard fence, not a starting
suggestion.

  1. Do NOT emit patches for files outside ``Files to touch`` unless
     the task body literally names them. Inventing scaffolding (new
     ``__init__.py``, new ``main.py``, "structure" refactors) when
     the task is "fix bug X" is hallucination — those new files have
     no callers, no tests, and silently break imports.

  2. Do NOT add new top-level ``import`` / ``use`` / ``require``
     statements unless the task explicitly requests a new dependency.
     A "fix SQL injection" task is satisfied by parameterising the
     EXISTING query against the EXISTING driver — switching from
     stdlib ``sqlite3`` to ``psycopg2`` (or stdlib ``http`` to
     ``requests``, or stdlib ``json`` to ``orjson``) is an
     architectural rewrite, not a fix. Caught live rung-3 v13:
     worker rewrote db.py to psycopg2; tests failed with
     ModuleNotFoundError before any assertion ran.

  3. Do NOT change public function signatures — names, parameter
     order, parameter types, return types. Tests and other callers
     in the repo depend on them. A signature change ripples across
     the codebase; a function-body change stays local. Read the
     CURRENT FILE CONTENTS section to see the real signatures
     before you start.

  4. Do NOT delete unrelated functions / classes / constants from a
     file just because your patch doesn't reference them. Other code
     (tests, callers, scripts you can't see in the truncated tree)
     uses them. If a file has helper functions you don't recognise,
     LEAVE THEM. Caught live rung-3 v13+v14: worker deleted
     ``get_conn``/``init_schema``/``seed`` from db.py because its
     own rewrite "didn't need them" — every test fixture broke.

     This rule is the most violated one because of a structural
     trap: you must emit COMPLETE new file content (not diffs), and
     it's tempting to write only the function you came to fix.
     Concrete shape:

       WRONG (rung-3 v14 actual output — caused ImportError on
       ``from src.db import init_schema, seed``):

           import sqlite3

           def find_user_by_name(conn, name):
               cursor = conn.cursor()
               cursor.execute("SELECT * FROM users WHERE name = ?", (name,))
               return cursor.fetchall()

           def get_conn():
               return sqlite3.connect(':memory:')

           # init_schema and seed remain unchanged
           # ← LIE. They're gone from the file content above.

       RIGHT — copy every existing function VERBATIM, edit only the
       one you came to fix:

           import sqlite3

           def get_conn() -> sqlite3.Connection:
               conn = sqlite3.connect(":memory:")
               conn.row_factory = sqlite3.Row     # ← preserved
               return conn

           def init_schema(conn): ...             # ← preserved verbatim
           def seed(conn): ...                    # ← preserved verbatim

           def find_user_by_name(conn, name):     # ← THIS is the only
               cur = conn.execute(                #     function you fix
                   "SELECT id, name FROM users WHERE name = ?", (name,)
               )
               return [dict(row) for row in cur]

     If you write a comment like ``# X remains unchanged`` and X is
     NOT in your file content above the comment, you are lying. The
     parser doesn't read comments; tests will fail at import time.

  5. Cross-module imports must reference symbols that ACTUALLY EXIST.
     If you write ``from .db import connect_to_database``, the name
     ``connect_to_database`` MUST be defined in db.py somewhere your
     patch puts it (or already exists there). Inventing import names
     and hoping the runtime has them is the most expensive hallucination
     — it gets past compile checks and only fails on first use.

  6. Minimal-change wins. The smallest patch that satisfies the task
     description and keeps existing tests green is correct; anything
     more is risk without reward.
"""

    return f"""Repository: {repo_name}
Languages: {langs}

Task: {subtask_title}
Description: {subtask_description}
Files to touch: {', '.join(file_hints) if file_hints else 'determine appropriate files'}
{boundaries_section}
{retry_section}
{files_section}

== FILE TREE ==
{tree_sample}

== INSTRUCTIONS ==
Implement the task described above. Create, modify, or delete files as needed.
For new files, provide complete, production-ready content.
For modifications, provide the COMPLETE new file content (not diffs).

Respond with ONLY this JSON (no extra text, no markdown):
{{
  "commit_message": "conventional commit message (feat/fix/docs/ci/chore)",
  "patches": [
    {{
      "action": "create",
      "path": "relative/path/to/file",
      "content": "full file content here"
    }}
  ]
}}

Rules:
- action must be: create, modify, or delete
- path MUST target a file (e.g. 'src/foo.py'), NEVER a bare directory ending with '/'
- For delete actions, content can be empty string
- Use proper file content for the repo's language(s)
- commit_message must follow Conventional Commits: type(scope): description [gitoma]
- Maximum 5 patches per subtask

== FORBIDDEN PATHS (the patcher will reject these — do not emit patches for them) ==
{denylist_summary()}
"""


def review_integrator_system_prompt() -> str:
    return (
        "You are FabGPT, an expert software engineer. "
        "Your job is to address a specific code review comment on a pull request. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no prose, no code fences."
    )


def review_integrator_user_prompt(
    comment_body: str,
    file_path: str | None,
    file_content: str | None,
    line: int | None,
) -> str:
    file_section = ""
    if file_path and file_content:
        truncated = file_content[:4000] if len(file_content) > 4000 else file_content
        line_ref = f" (around line {line})" if line else ""
        file_section = f"""
== FILE: {file_path}{line_ref} ==
{truncated}
"""

    return f"""A code reviewer left this comment on a pull request:

"{comment_body}"
{file_section}

Address the review comment by modifying the relevant file(s).
Provide the COMPLETE new file content (not a diff).

Respond with ONLY this JSON:
{{
  "commit_message": "fix: address review comment [gitoma]",
  "patches": [
    {{
      "action": "modify",
      "path": "relative/path/to/file",
      "content": "complete new file content"
    }}
  ]
}}
"""
