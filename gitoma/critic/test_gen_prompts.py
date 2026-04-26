"""Test Gen v1 — per-language LLM prompts.

System + user prompts for the 5 languages CPG-lite covers (Python,
TypeScript, JavaScript, Rust, Go). The user prompt template assembles
the same shape for all languages with language-specific context
(framework name, conventional test layout, idiomatic example).

Output contract: the LLM emits ONE complete test file (no markdown
fences, no prose). The orchestrator strips fences defensively
anyway via the existing `_strip_markdown_fences` helper.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LangSpec",
    "LANG_SPECS",
    "test_gen_system_prompt",
    "test_gen_user_prompt",
]


@dataclass(frozen=True)
class LangSpec:
    """Language-specific knobs for test-gen prompt assembly.

    * ``framework`` — human name shown in the prompt.
    * ``manifest_marker`` — repo-root file whose presence confirms
      the framework is set up (matches
      :mod:`gitoma.analyzers.test_runner` conventions).
    * ``test_dir_hint`` — conventional location for the new test
      file. Empty string when colocation is the convention
      (``foo.test.ts`` next to ``foo.ts``).
    * ``test_filename_pattern`` — Python-style format string with
      `{stem}` substituted from the source file's basename
      (sans extension).
    * ``import_hint`` — short cue showing how the test imports the
      symbol under test.
    * ``minimal_example`` — a tiny one-test example in the
      framework's idiomatic style. Anchors the LLM on the right
      shape without bloating the prompt.
    """

    language: str
    framework: str
    manifest_marker: str
    test_dir_hint: str
    test_filename_pattern: str
    import_hint: str
    minimal_example: str


LANG_SPECS: dict[str, LangSpec] = {
    "python": LangSpec(
        language="Python",
        framework="pytest",
        manifest_marker="pyproject.toml",
        test_dir_hint="tests/",
        test_filename_pattern="test_{stem}.py",
        import_hint=(
            "from <module.path.relative.to.repo.root> import <symbol>"
        ),
        minimal_example=(
            "import pytest\n"
            "from src.lib import helper\n"
            "\n"
            "def test_helper_happy_path():\n"
            "    assert helper(1, 2) == 3\n"
            "\n"
            "def test_helper_zero_args():\n"
            "    with pytest.raises(TypeError):\n"
            "        helper()\n"
        ),
    ),
    "typescript": LangSpec(
        language="TypeScript",
        framework="jest (or vitest — match the project)",
        manifest_marker="package.json",
        test_dir_hint="",  # colocated convention
        test_filename_pattern="{stem}.test.ts",
        import_hint="import { <symbol> } from './<source-stem>';",
        minimal_example=(
            "import { helper } from './lib';\n"
            "\n"
            "describe('helper', () => {\n"
            "  it('returns sum on happy path', () => {\n"
            "    expect(helper(1, 2)).toBe(3);\n"
            "  });\n"
            "});\n"
        ),
    ),
    "javascript": LangSpec(
        language="JavaScript",
        framework="jest (or vitest — match the project)",
        manifest_marker="package.json",
        test_dir_hint="",
        test_filename_pattern="{stem}.test.js",
        import_hint="const { <symbol> } = require('./<source-stem>');",
        minimal_example=(
            "const { helper } = require('./lib');\n"
            "\n"
            "describe('helper', () => {\n"
            "  it('returns sum on happy path', () => {\n"
            "    expect(helper(1, 2)).toBe(3);\n"
            "  });\n"
            "});\n"
        ),
    ),
    "rust": LangSpec(
        language="Rust",
        framework="cargo test",
        manifest_marker="Cargo.toml",
        # Cargo prefers integration tests under tests/ for
        # cross-crate testing. Keep it simple.
        test_dir_hint="tests/",
        test_filename_pattern="{stem}_tests.rs",
        import_hint=(
            "use <crate_name>::<module>::<symbol>; "
            "(integration tests live under tests/ and import via the crate name)"
        ),
        minimal_example=(
            "use mycrate::lib::helper;\n"
            "\n"
            "#[test]\n"
            "fn helper_happy_path() {\n"
            "    assert_eq!(helper(1, 2), 3);\n"
            "}\n"
        ),
    ),
    "go": LangSpec(
        language="Go",
        framework="go test",
        manifest_marker="go.mod",
        test_dir_hint="",  # colocated convention
        test_filename_pattern="{stem}_test.go",
        import_hint=(
            "Same package as the source (no import needed for "
            "exported symbols of the same package)."
        ),
        minimal_example=(
            "package mypkg\n"
            "\n"
            "import \"testing\"\n"
            "\n"
            "func TestHelper_HappyPath(t *testing.T) {\n"
            "    if got := Helper(1, 2); got != 3 {\n"
            "        t.Errorf(\"expected 3, got %d\", got)\n"
            "    }\n"
            "}\n"
        ),
    ),
}


def test_gen_system_prompt() -> str:
    """System prompt for the test-gen LLM call. Strict output
    contract — single file, no fences, no prose."""
    return (
        "You are an expert test engineer. Your single job is to "
        "generate test code for newly-added or changed public symbols "
        "in a software patch.\n\n"
        "STRICT OUTPUT CONTRACT:\n"
        "  * Respond with ONLY the contents of ONE test file.\n"
        "  * NO markdown fences (no ```python or ```ts or any).\n"
        "  * NO prose / explanation / preamble before or after.\n"
        "  * The first character of your response is the first character "
        "of the test file (e.g. `import` / `package` / `use` / `const`).\n\n"
        "QUALITY BAR:\n"
        "  * Cover the happy path AND at least one edge case "
        "(empty / None / zero / negative / large input as relevant).\n"
        "  * Use only stdlib + the framework's standard assertions. "
        "Don't pull in heavy fixtures or mocking libs unless the symbol "
        "obviously needs them.\n"
        "  * Prefer small, named tests over one giant one.\n"
        "  * Imports MUST resolve to symbols you actually see in the "
        "provided source — don't invent helper modules.\n"
    )


def test_gen_user_prompt(
    spec: LangSpec,
    source_file_rel: str,
    source_snippet: str,
    symbols_to_test: list[tuple[str, str, str]],
    target_test_path: str,
) -> str:
    """Assemble the language-specific user prompt.

    Args:
        spec: language spec from :data:`LANG_SPECS`.
        source_file_rel: repo-relative path of the source under test.
        source_snippet: the source content (capped by caller).
        symbols_to_test: list of ``(name, kind, signature)`` triples.
        target_test_path: where the test file will be written.
    """
    sym_lines = []
    for name, kind, signature in symbols_to_test:
        sig = signature or "(no signature captured)"
        sym_lines.append(f"  * {kind} `{name}` with signature `{sig}`")
    sym_block = "\n".join(sym_lines) if sym_lines else "  * (none)"

    return f"""Generate ONE {spec.language} test file using {spec.framework}.

== TARGET TEST FILE ==
Path: {target_test_path}

== SOURCE UNDER TEST ==
File: {source_file_rel}

```
{source_snippet}
```

== SYMBOLS TO TEST (newly added or signature-changed) ==
{sym_block}

== FRAMEWORK CONVENTIONS ==
* Framework: {spec.framework}
* Manifest detected: {spec.manifest_marker}
* Test layout: {spec.test_dir_hint or "colocated next to source"}
* Import shape: {spec.import_hint}

== IDIOMATIC EXAMPLE (style reference) ==
```
{spec.minimal_example}```

== INSTRUCTIONS ==
Write the COMPLETE content of {target_test_path}. Test EVERY symbol
listed above (happy path + edge cases). Imports must reference the
ACTUAL source path / package shown above — do not invent module
names. Output ONLY the file content, starting with the very first
character (e.g. `import`, `package`, `use`). Do NOT wrap in
markdown fences.
"""
