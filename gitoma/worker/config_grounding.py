"""G12 — Config-grounding guard against hallucinated package references.

G11 (``content_grounding.py``) catches doc files that name frameworks
absent from the repo. It deliberately doesn't touch JS/TS config
files because the patterns are different: docs cite frameworks by
prose name (``"React"``), configs cite packages by import string
(``require('prettier-plugin-tailwindcss')`` or
``plugins: ['prettier-plugin-tailwindcss']``).

The b2v PR #21 case had BOTH:

  * ``architecture.md`` claimed React+Redux frontend → caught by G11.
  * ``prettier.config.js`` referenced ``prettier-plugin-tailwindcss``
    AND used ``importOrder`` options that imply
    ``@trivago/prettier-plugin-sort-imports`` — neither in npm deps.

That second class is what G12 covers. Same fingerprint, different
file types, different extraction shape.

Architecture:
  * ``CONFIG_FILE_BASENAMES`` — closed set of well-known JS/TS
    config file basenames. Conservative on purpose: greping every
    ``.js`` for package strings would be both expensive AND a
    false-positive minefield (application code legitimately calls
    ``require('lodash')`` even when the file isn't a build config).
    A new config tool gets added here intentionally.
  * Three extractors (``require()``, ``import from``,
    ``plugins/presets: [...]``) collected into a single set per
    file. Each ref is normalised (strip path-suffix, handle
    ``@scope/pkg``), filtered against Node builtins + relative
    paths, then membership-tested against the fingerprint's npm
    deps (lower-cased to match Occam's normalisation).
  * Same revert+retry shape as G2/G7/G10/G11 in the worker apply
    loop: on flag, emit ``critic_config_grounding.fail`` trace
    event, revert, retry with the violation injected as feedback.

Out of scope for v1 (deferred):
  * Option-name → plugin inference (``importOrder`` ⇒ implies
    ``@trivago/prettier-plugin-sort-imports``). Needs per-tool
    option catalogues; useful but a large data-tracking exercise.
  * ``.json`` configs (``.eslintrc.json``, ``tsconfig.json``) — G10
    handles those structurally via JSON Schema; their package
    references live in stringly fields that the schemas don't
    constrain, but extending G12 to JSON is the cleanest follow-up
    if the FP rate stays low here.
  * Source code grounding (does ``require('foo')`` in ``src/app.js``
    resolve?). That's npm's own job at install time; G12 stays
    config-scoped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "validate_config_grounding",
    "CONFIG_FILE_BASENAMES",
    "NODE_BUILTINS",
]


# Closed set of JS/TS config file basenames G12 scans. Extend by
# adding a basename + a test fixture that proves the new file type
# behaves the same as the existing ones.
CONFIG_FILE_BASENAMES: frozenset[str] = frozenset({
    # Prettier
    "prettier.config.js", "prettier.config.mjs", "prettier.config.cjs",
    ".prettierrc.js", ".prettierrc.mjs", ".prettierrc.cjs",
    # ESLint (JS variants — JSON variant covered by G10)
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    ".eslintrc.js", ".eslintrc.cjs",
    # Tailwind
    "tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs",
    "tailwind.config.mjs",
    # Vite / Vitest
    "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "vitest.config.js", "vitest.config.ts", "vitest.config.mjs",
    # Webpack / Rollup / Babel
    "webpack.config.js", "webpack.config.ts",
    "rollup.config.js", "rollup.config.mjs", "rollup.config.ts",
    "babel.config.js", "babel.config.cjs", ".babelrc.js",
    # Test runners (JS configs — Jest/Playwright)
    "jest.config.js", "jest.config.ts", "jest.config.mjs",
    "playwright.config.ts", "playwright.config.js",
    # Meta-frameworks
    "next.config.js", "next.config.mjs", "next.config.ts",
    "nuxt.config.js", "nuxt.config.ts",
    "astro.config.mjs", "astro.config.js", "astro.config.ts",
    "svelte.config.js", "svelte.config.mjs",
    # PostCSS
    "postcss.config.js", "postcss.config.cjs", "postcss.config.mjs",
})


# Node.js core modules — never npm packages so always safe to skip.
# Includes the ``node:`` prefix variants per Node 16+ convention.
_BUILTIN_NAMES = (
    "fs", "path", "os", "crypto", "util", "child_process", "stream",
    "events", "url", "http", "https", "process", "assert", "buffer",
    "querystring", "zlib", "net", "tls", "dns", "dgram", "cluster",
    "readline", "module", "vm", "perf_hooks", "constants", "punycode",
    "string_decoder", "timers", "tty", "v8", "worker_threads",
    "async_hooks", "fs/promises", "stream/promises", "stream/web",
    "timers/promises", "dns/promises", "diagnostics_channel",
    "trace_events", "wasi", "inspector",
)
NODE_BUILTINS: frozenset[str] = frozenset(
    list(_BUILTIN_NAMES) + [f"node:{n}" for n in _BUILTIN_NAMES]
)


# ── Extractors ──────────────────────────────────────────────────────

# require('pkg') / require("pkg")
_REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"\s]+)['"]\s*\)""")

# from 'pkg' (covers both ``import x from 'pkg'`` and re-exports)
# import 'pkg'   (side-effect imports)
_IMPORT_RE = re.compile(r"""(?:from|import)\s+['"]([^'"\s]+)['"]""")

# plugins: ['a', 'b'] or presets: ["x", "y"]. Multiline-tolerant
# (``re.S`` lets ``.`` match newlines so the array body can span
# lines). Captures the array body for second-pass string extraction.
_PLUGIN_ARRAY_RE = re.compile(
    r"""(?:plugins|presets)\s*:\s*\[([^\]]*)\]""",
    flags=re.S,
)
_STRING_LITERAL_RE = re.compile(r"""['"]([^'"\s]+)['"]""")


def _is_config_file(rel_path: str) -> bool:
    """A path matches G12 scope when its basename is in
    ``CONFIG_FILE_BASENAMES``. Fast string membership — no glob."""
    return Path(rel_path).name in CONFIG_FILE_BASENAMES


def _extract_package_refs(text: str) -> set[str]:
    """Return the union of every package-name string referenced in
    the config text — through require(), import-from, or plugin/
    preset array literals. Strings are returned verbatim; caller
    is responsible for filtering builtins / relatives and for
    normalising sub-paths."""
    refs: set[str] = set()
    for m in _REQUIRE_RE.finditer(text):
        refs.add(m.group(1))
    for m in _IMPORT_RE.finditer(text):
        refs.add(m.group(1))
    for arr_match in _PLUGIN_ARRAY_RE.finditer(text):
        body = arr_match.group(1)
        for str_match in _STRING_LITERAL_RE.finditer(body):
            refs.add(str_match.group(1))
    return refs


def _normalise_package(name: str) -> str | None:
    """Reduce a require/import string to its npm package name.

      * ``"./foo"`` / ``"../bar"`` / ``"/abs"`` → None (relative or
        absolute path, not a package).
      * ``"node:fs"`` / ``"fs"`` → None when caller filters via
        ``NODE_BUILTINS``; this function returns the literal so
        the caller can decide.
      * ``"@scope/pkg/sub/x"`` → ``"@scope/pkg"``.
      * ``"pkg/sub/x"`` → ``"pkg"``.

    Returns the normalised name, or ``None`` for non-package refs.
    """
    if not name:
        return None
    if name.startswith((".", "/")):
        return None
    if name.startswith("@"):
        parts = name.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None  # malformed @-only token
    return name.split("/")[0]


def validate_config_grounding(
    root: Path,
    touched: list[str],
    fingerprint: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Validate every touched JS/TS config file's package references
    against the npm dep list in the fingerprint.

    Returns ``(rel_path, message)`` on the FIRST violation, ``None``
    on clean. Silent pass when:

      * fingerprint is None / empty
      * fingerprint reports no ``manifest_files``
      * fingerprint has no ``package.json`` declared (greenfield JS
        repo or non-JS repo — nothing to ground npm refs against;
        avoids false-positives in pure-Rust/Python/Go projects that
        happen to ship a config file)
      * touched file basename not in ``CONFIG_FILE_BASENAMES``
      * file doesn't exist on disk
      * every extracted package ref resolves against npm deps,
        Node builtins, or is a relative/absolute path

    Message format mirrors G11's: cites the literal that failed +
    the resolved package name + a sample of declared deps so the
    worker re-prompt has concrete evidence.
    """
    if not fingerprint:
        return None
    manifests = fingerprint.get("manifest_files") or []
    if "package.json" not in manifests:
        # Repo doesn't declare any npm deps at all → can't ground
        # npm references. Better to silent-pass than punish.
        return None

    npm_deps = {
        d.lower()
        for d in (fingerprint.get("declared_deps") or {}).get("npm") or []
    }

    for rel in touched:
        if not _is_config_file(rel):
            continue
        full = root / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for ref in _extract_package_refs(text):
            pkg = _normalise_package(ref)
            if pkg is None:
                continue
            pkg_lower = pkg.lower()
            if pkg_lower in NODE_BUILTINS:
                continue
            if pkg_lower in npm_deps:
                continue
            sample = sorted(npm_deps)[:5] or ["(no npm deps declared)"]
            return (
                rel,
                f"references npm package {ref!r} (resolved to {pkg!r}) "
                f"but no matching dep in package.json (declared: {sample}…). "
                f"Either install the package and add it to package.json, "
                f"or remove the reference from the config."
            )

    return None
