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

    # Build Integrity status drives the compile-fix mode — an extra
    # constraint block we inject when the project does not compile.
    build_integrity_fail = any(
        m.name == "build" and m.status == "fail" for m in report.metrics
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
{brief_block}
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
{compile_fix_block}

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

    return f"""Repository: {repo_name}
Languages: {langs}

Task: {subtask_title}
Description: {subtask_description}
Files to touch: {', '.join(file_hints) if file_hints else 'determine appropriate files'}
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
