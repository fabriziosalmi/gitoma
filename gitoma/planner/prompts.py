"""All LLM prompt templates for the planner and worker."""

from __future__ import annotations

from gitoma.analyzers.base import MetricReport
from gitoma.worker.patcher import denylist_summary


def planner_system_prompt() -> str:
    return (
        "You are FabGPT, an expert software engineer and DevOps specialist. "
        "Your job is to analyze a repository quality report and produce a structured improvement plan. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no prose, no code fences. "
        "Follow the schema exactly."
    )


def planner_user_prompt(report: MetricReport, file_tree: list[str], languages: list[str]) -> str:
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

    return f"""Repository: {report.repo_url}
Languages: {langs}
Overall score: {report.overall_score:.2f}/1.0

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

    return f"""Repository: {repo_name}
Languages: {langs}

Task: {subtask_title}
Description: {subtask_description}
Files to touch: {', '.join(file_hints) if file_hints else 'determine appropriate files'}

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
