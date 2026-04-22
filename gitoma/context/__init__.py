"""Repo-wide deterministic context: a tight brief of the project that
every downstream agent (planner, worker, panel, devil, refiner, meta)
can reference without re-discovering it via LLM prompts."""

from gitoma.context.repo_brief import RepoBrief, extract_brief, render_brief

__all__ = ["RepoBrief", "extract_brief", "render_brief"]
