"""Test Gen v1 — 5th critic, autogenerates tests for shipped patches.

Closes the gap from the horizon Multi-Agent Pipeline blueprint
(Architect / Implementer / Verifier / **Test Gen — MISSING**).
After the worker applies a patch and before the commit, this agent:

  1. For each touched source file (any of the 5 CPG-indexed
     languages), index BEFORE + AFTER content via
     :mod:`gitoma.cpg.diff` to find new / signature-changed
     public symbols.
  2. Detect the project's test framework via marker file
     (``pyproject.toml`` → pytest, ``package.json`` → jest,
     ``Cargo.toml`` → cargo test, ``go.mod`` → go test).
  3. For each language-with-changes, ask the LLM to generate ONE
     test file targeting the new/changed symbols.
  4. Return a ``{path: content}`` dict the worker applies via the
     existing patcher, then re-runs G8's test-baseline check on
     the combined patch — regression in the new tests reverts
     just the test additions, never the source patch.

Defensive contract: ANY failure (LLM error, parse error, framework
not detected, no symbols changed) returns ``None``. Test Gen NEVER
blocks a patch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gitoma.cpg._base import Symbol
from gitoma.cpg.diff import INDEXABLE_EXTS, diff_symbols
from gitoma.critic.test_gen_prompts import (
    LANG_SPECS,
    LangSpec,
    test_gen_system_prompt,
    test_gen_user_prompt,
)

__all__ = ["TestGenAgent", "is_test_gen_enabled", "MAX_SYMBOLS_PER_FILE"]


MAX_SYMBOLS_PER_FILE = 5
"""Cap on symbols sent to the LLM per source file. Hot files
(many added functions) would otherwise blow the prompt budget;
the first N are sufficient signal for v1."""


_MAX_SOURCE_SNIPPET_CHARS = 3000
"""Match the worker prompt's source-file truncation limit."""


_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
}


def is_test_gen_enabled() -> bool:
    """Read the ``GITOMA_TEST_GEN`` env var. Default off until benched
    in production — so existing benches stay reproducible."""
    return (os.environ.get("GITOMA_TEST_GEN") or "").lower() in (
        "1", "on", "true", "yes",
    )


class TestGenAgent:
    """Orchestrates test generation across languages for one patch.

    The agent is stateless across patches — instantiate, call
    :meth:`generate_for_patch`, discard. The LLM client is shared
    with the rest of the worker (same backend, same model).
    """

    # Pytest collects classes starting with "Test*" by default;
    # tell it to skip — this is a production agent, not a test class.
    __test__ = False

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    # ── Public API ─────────────────────────────────────────────────

    def generate_for_patch(
        self,
        touched: list[str],
        originals: dict[str, str],
        repo_root: Path,
    ) -> dict[str, str] | None:
        """Generate one test file per language with new/changed
        public symbols. Returns ``{test_path: content}`` or ``None``.

        ``touched`` is the worker's list of post-apply file paths
        (relative to ``repo_root``). ``originals`` is the pre-patch
        content per modified file (empty / missing for created
        files). Test files (paths matching test conventions) are
        skipped — we don't generate tests for tests.
        """
        # 1. Group changed symbols by language
        per_lang: dict[str, list[tuple[str, list[Symbol]]]] = {}
        for rel in touched:
            if _is_test_path(rel):
                continue
            ext = _suffix(rel)
            lang = _EXT_TO_LANG.get(ext)
            if lang is None:
                continue
            try:
                before = originals.get(rel, "")
                after = (repo_root / rel).read_text(errors="replace")
            except OSError:
                continue
            new, changed = diff_symbols(rel, before, after)
            interesting = (new + changed)[:MAX_SYMBOLS_PER_FILE]
            if not interesting:
                continue
            per_lang.setdefault(lang, []).append((rel, interesting))

        if not per_lang:
            return None

        # 2. For each language, check framework manifest + generate
        results: dict[str, str] = {}
        for lang, file_groups in per_lang.items():
            spec = LANG_SPECS.get(lang)
            if spec is None:
                continue
            if not (repo_root / spec.manifest_marker).exists():
                # Framework manifest missing — caller's tests
                # wouldn't run anyway. Skip silently.
                continue
            test_payload = self._generate_one_file(
                spec=spec,
                file_groups=file_groups,
                repo_root=repo_root,
            )
            if test_payload is not None:
                test_path, content = test_payload
                results[test_path] = content

        return results or None

    # ── Internal: one file per language ────────────────────────────

    def _generate_one_file(
        self,
        spec: LangSpec,
        file_groups: list[tuple[str, list[Symbol]]],
        repo_root: Path,
    ) -> tuple[str, str] | None:
        """Pick the first source file in ``file_groups`` (the most
        recent or alphabetically-first touched), build a prompt
        targeting ITS symbols, ask the LLM, return
        ``(test_file_path, content)`` or ``None``.

        v1 generates ONE test file per LANGUAGE, targeting the
        first source file's symbols. Multiple-file-per-language
        deferred — keeps the LLM prompt bounded and avoids
        per-file prompting cost.
        """
        # Stable choice: the first source file alphabetically. Picks
        # `lib.py` over `widgets.py` if both touched.
        file_groups_sorted = sorted(file_groups, key=lambda fg: fg[0])
        source_rel, symbols = file_groups_sorted[0]
        try:
            source_content = (repo_root / source_rel).read_text(
                errors="replace",
            )
        except OSError:
            return None
        if len(source_content) > _MAX_SOURCE_SNIPPET_CHARS:
            source_content = source_content[: _MAX_SOURCE_SNIPPET_CHARS] + (
                "\n# … (truncated)\n"
            )
        target_test_path = _compose_test_path(source_rel, spec)
        symbol_triples = [
            (s.name, s.kind.value, s.signature) for s in symbols
        ]
        user_prompt = test_gen_user_prompt(
            spec=spec,
            source_file_rel=source_rel,
            source_snippet=source_content,
            symbols_to_test=symbol_triples,
            target_test_path=target_test_path,
        )
        messages = [
            {"role": "system", "content": test_gen_system_prompt()},
            {"role": "user", "content": user_prompt},
        ]
        # ¬B axiom — every LLM call in critic/ must be wrapped in a
        # trace span / emit so silent black-box behaviour is impossible.
        # See test_determinism_pins.py.
        from gitoma.core.trace import current as current_trace
        try:
            raw = self._llm.chat(messages)
            current_trace().emit(
                "test_gen.llm_call",
                language=spec.language,
                source_file=source_rel,
                target_test=target_test_path,
                symbol_count=len(symbols),
                response_chars=len(raw or ""),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            try:
                current_trace().exception("test_gen.llm_failed", exc)
            except Exception:
                pass
            return None
        content = _strip_fences(raw).strip()
        if not content:
            return None
        # Defensive: ensure content has at least 2 non-whitespace lines
        if len(content.splitlines()) < 2:
            return None
        return target_test_path, content + ("\n" if not content.endswith("\n") else "")


# ── Helpers (module-level for testability) ─────────────────────────


def _suffix(rel: str) -> str:
    """Lower-cased final suffix including the dot."""
    p = Path(rel)
    return p.suffix.lower()


def _compose_test_path(source_rel: str, spec: LangSpec) -> str:
    """Build the conventional test-file path for the given source.

    Examples (Python pytest, test_dir_hint=``tests/``):
      ``src/lib.py``                    → ``tests/test_lib.py``
      ``gitoma/cpg/storage.py``         → ``tests/test_storage.py``

    Examples (TypeScript jest, colocated):
      ``src/api.ts``                    → ``src/api.test.ts``

    Examples (Rust cargo, ``tests/`` integration):
      ``src/lib.rs``                    → ``tests/lib_tests.rs``

    Examples (Go, colocated):
      ``internal/handler.go``           → ``internal/handler_test.go``
    """
    src_path = Path(source_rel)
    stem = src_path.stem
    test_filename = spec.test_filename_pattern.format(stem=stem)
    if spec.test_dir_hint:
        # Place under the conventional dir at repo root.
        return f"{spec.test_dir_hint.rstrip('/')}/{test_filename}"
    # Colocate next to the source.
    if src_path.parent == Path():
        return test_filename
    return f"{src_path.parent.as_posix()}/{test_filename}"


def _is_test_path(rel: str) -> bool:
    """Return True when the path looks like an existing test file —
    we don't want to generate tests for tests."""
    p = rel.replace("\\", "/").lower()
    if p.startswith("tests/") or p.startswith("test/"):
        return True
    base = p.rsplit("/", 1)[-1]
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith(("_test.go", ".test.ts", ".test.tsx", ".test.js",
                      ".test.mjs", ".test.cjs", ".spec.ts", ".spec.js")):
        return True
    return False


def _strip_fences(raw: str) -> str:
    """Defensive: even though the system prompt forbids fences, LLMs
    sometimes emit them anyway. Strip a single leading ```lang and
    a trailing ``` if present."""
    lines = raw.splitlines()
    if not lines:
        return raw
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
