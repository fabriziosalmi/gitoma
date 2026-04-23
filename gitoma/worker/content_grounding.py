"""G11 — Content-grounding guard against hallucinated docs/configs.

The patcher's structural guards (G1–G10) catch malformed JSON,
broken Python AST, missing top-level defs, schema-invalid configs,
runtime test regressions, etc. None of them catch the failure mode
that bit gitoma on b2v PR #21:

  * Generated ``docs/guide/architecture.md`` claimed a React + Redux
    + WebSocket frontend for a repo that is a pure Rust CLI.
  * Generated ``prettier.config.js`` referenced
    ``prettier-plugin-tailwindcss`` without tailwindcss anywhere in
    the dependency graph.
  * Generated ``docs/guide/index.md`` used absolute paths that broke
    VitePress' relative-link resolution.

These all parse fine, lint fine, compile fine. They're CONTENT
hallucinations: the LLM invented technical claims that do not match
the actual repo.

G11 grounds doc/config content against Occam's ``/repo/fingerprint``
(the verified "what is this repo" snapshot — declared deps, inferred
frameworks, manifest files, entrypoints). If a doc mentions a
framework that the fingerprint shows zero evidence for, the patch is
rejected before commit.

Architecture:
  * ``DOC_FRAMEWORK_PATTERNS`` — regex → canonical-framework-id map.
    Same identifiers Occam uses in ``declared_frameworks``, so a
    direct set membership test works.
  * Silent pass when fingerprint is missing / has no manifests
    detected — we only ground claims when we have evidence to ground
    AGAINST. Better a false-negative than punishing greenfield repos.
  * Same revert+retry shape as G2/G7/G10 in the worker apply loop:
    on flag, emit ``critic_content_grounding.fail`` trace event,
    revert, retry with the violation injected as feedback.

What's deliberately OUT of scope for v1:
  * Source-code grounding (does the doc mention a function name that
    doesn't exist?). Needs a symbol index — defer to a future iter.
  * Config-file plugin checks (``prettier.config.js`` referencing a
    plugin not in npm deps). Easy follow-up: extend the per-extension
    handler with a JS string-literal scanner. Skipped for v1 because
    the b2v case needed real handling, not a half-fix.
  * Negative claims ("Unlike React, we use vanilla DOM"). Accepted
    as a known false-positive — treat as low-volume noise; if it
    becomes a problem, gate behind a sentence-context heuristic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "validate_content_grounding",
    "DOC_FRAMEWORK_PATTERNS",
    "DOC_EXTENSIONS",
]


# Documentation file extensions we scan. ``.html`` excluded for now —
# the b2v case was Markdown, and HTML carries enough non-prose noise
# (boilerplate, framework hints in comments) to inflate false positives.
DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})


# Doc keyword → canonical framework-id (same identifiers Occam emits
# in ``declared_frameworks``). Patterns are case-insensitive and
# word-boundary anchored to avoid matching ``next`` inside ``nextSlide``.
# Order doesn't matter — first match per file is what triggers; the
# returned message names the matched literal so the developer can find it.
DOC_FRAMEWORK_PATTERNS: dict[str, str] = {
    # Frontend
    r"\b(?:React(?:JS|\.js)?|React Native)\b":     "react",
    r"\b(?:Vue(?:JS|\.js| 3| 2)?)\b":              "vue",
    r"\b(?:Angular(?:JS)?)\b":                     "angular",
    r"\b(?:Svelte(?:Kit)?)\b":                     "svelte",
    r"\b(?:Solid(?:\.js| JS)?)\b":                 "solid",
    r"\b(?:Preact)\b":                             "preact",
    # Meta-frameworks
    r"\b(?:Next(?:\.js| JS)?)\b":                  "next",
    r"\b(?:Nuxt(?:\.js| JS)?)\b":                  "nuxt",
    r"\b(?:Remix)\b":                              "remix",
    r"\b(?:Gatsby)\b":                             "gatsby",
    r"\b(?:Astro)\b":                              "astro",
    # State
    r"\b(?:Redux(?: Toolkit| RTK| Saga| Thunk)?|RTK Query)\b": "redux",
    r"\b(?:Zustand)\b":                            "zustand",
    r"\b(?:MobX)\b":                               "mobx",
    r"\b(?:Jotai)\b":                              "jotai",
    r"\b(?:Pinia)\b":                              "pinia",
    # Backend (node)
    r"\b(?:Express(?:\.js| JS)?)\b":               "express",
    r"\b(?:Fastify)\b":                            "fastify",
    r"\b(?:NestJS|Nest\.js)\b":                    "nestjs",
    r"\b(?:Koa(?:\.js)?)\b":                       "koa",
    # Python web
    r"\b(?:Django)\b":                             "django",
    r"\b(?:Flask)\b":                              "flask",
    r"\b(?:FastAPI)\b":                            "fastapi",
    r"\b(?:Starlette)\b":                          "starlette",
    r"\b(?:Tornado)\b":                            "tornado",
    # Python CLI
    r"\b(?:Click(?: framework)?)\b":               "click",
    r"\b(?:Typer)\b":                              "typer",
    # UI
    r"\b(?:Tailwind(?:CSS| CSS)?)\b":              "tailwindcss",
    r"\b(?:Material[ -]?UI|MUI)\b":                "mui",
    r"\b(?:Chakra(?: UI)?)\b":                     "chakra",
    r"\b(?:Ant Design|antd)\b":                    "antd",
    r"\b(?:Bootstrap)\b":                          "bootstrap",
    # Go
    r"\b(?:Gin(?: framework)?)\b":                 "gin",
    r"\b(?:Echo(?: framework)?)\b":                "echo",
    r"\b(?:Fiber(?: framework)?)\b":               "fiber",
    r"\b(?:Cobra)\b":                              "cobra",
    # Rust
    r"\b(?:Actix(?:[ -]web)?)\b":                  "actix",
    r"\b(?:Rocket(?: framework)?)\b":              "rocket",
    r"\b(?:Axum)\b":                               "axum",
    r"\b(?:Warp(?: framework)?)\b":                "warp",
    r"\b(?:Clap(?: framework)?)\b":                "clap",
    r"\b(?:Tokio)\b":                              "tokio",
}


# Anti-FP allow-list: words that LOOK like framework mentions but
# are common english/programming nouns. Hits inside an allow-listed
# context are NOT counted as ground-violation evidence. Currently
# small — extend conservatively (each entry is a hole in the guard).
_GENERIC_PROSE_CONTEXTS: tuple[str, ...] = (
    # "react to changes" / "fast api" / "next step" — all generic English
    # — but our regexes are word-boundary anchored, so matches are exact
    # tokens. We don't strip any here for now; revisit if FP rate climbs.
)


def _grounded(framework: str, declared: set[str], all_deps: set[str]) -> bool:
    """A framework mention is grounded when EITHER the framework id
    appears in the declared set, OR a dep name matches the id (handles
    aliases not in our dep→fw map). Cheap set lookups."""
    if framework in declared:
        return True
    if framework in all_deps:
        return True
    # Substring fallback — handles ``@reduxjs/toolkit`` vs ``redux``
    # without listing every npm scope variant.
    return any(framework in d for d in all_deps)


def validate_content_grounding(
    root: Path,
    touched: list[str],
    fingerprint: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Validate every touched doc file against the repo fingerprint.

    Returns ``(rel_path, message)`` on the FIRST violation, ``None``
    on clean. Silent pass (no error) when:

      * fingerprint is None / empty
      * fingerprint reports no manifest_files (greenfield repo —
        nothing to ground against)
      * touched file is not a doc extension
      * file doesn't exist on disk (deleted in this commit)
      * file is unreadable
      * no DOC_FRAMEWORK_PATTERNS match in the file's content
      * every match resolves against fingerprint deps/frameworks

    The message format is human-readable and points at the
    contradicting framework + the matched literal so the worker can
    re-prompt with concrete feedback (e.g. ``"docs/intro.md mentions
    'React' but no matching dep/framework in fingerprint
    (declared_frameworks=['clap'])"``)."""
    if not fingerprint:
        return None
    if not fingerprint.get("manifest_files"):
        # No evidence of what this repo IS — bail rather than punish
        # greenfield projects with empty grounding sets.
        return None

    declared = {str(x).lower() for x in (fingerprint.get("declared_frameworks") or [])}
    all_deps: set[str] = set()
    for lang_deps in (fingerprint.get("declared_deps") or {}).values():
        for d in lang_deps or []:
            all_deps.add(str(d).lower())

    for rel in touched:
        full = root / rel
        if full.suffix.lower() not in DOC_EXTENSIONS:
            continue
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, fw_id in DOC_FRAMEWORK_PATTERNS.items():
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            if _grounded(fw_id, declared, all_deps):
                continue
            literal = m.group(0)
            return (
                rel,
                f"mentions {literal!r} (framework={fw_id}) but no matching "
                f"dep/framework in fingerprint "
                f"(declared_frameworks={sorted(declared) or '(none)'}, "
                f"sample_deps={sorted(list(all_deps))[:5]}…). "
                f"Either remove the claim or wire up the actual dep first."
            )

    return None
