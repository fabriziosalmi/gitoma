"""G10 — Semantic config validator (JSON Schema against common tools).

G2 ``validate_post_write_syntax`` catches SYNTACTICAL parse errors
(malformed JSON/TOML/YAML/Python). It doesn't catch CONFIGS that
parse as valid but would fail at runtime because they don't match
the tool's own schema.

Real-world catch on b2v PR #19: an ``.eslintrc.json`` with
``"parser": {"parser": "..."}`` (object instead of string) + invented
options on ``@typescript-eslint/explicit-module-boundary-types``. The
file was valid JSON → G2 silent → G10 would have caught it at write
time via jsonschema validation against the bundled
``eslintrc.json`` schema.

Architecture:
  * Schemas bundled under ``gitoma/worker/schemas/`` (fetched from
    https://www.schemastore.org, pinned for reproducibility, refresh
    via ``gitoma refresh-schemas`` in a future iteration).
  * ``PATH_MATCHERS`` = list of (fnmatch-style pattern, schema file).
    First match wins. Deliberately file-name-based (not MIME-type) so
    a renamed ``my-eslint-config.json`` isn't validated by the ESLint
    schema just because it's JSON.
  * Same revert+retry shape as G2: on validation fail, emit
    ``critic_schema_check.fail`` trace event, revert, retry with the
    parser error injected as feedback.

Out-of-scope for v1 (deferred):
  * JS-native configs (``.eslintrc.js``, ``prettier.config.js``) —
    require a JS parser to extract the config object.
  * ``pyproject.toml [tool.*]`` sub-schemas — each tool (ruff, mypy,
    black, pytest, coverage) has its own; better covered as a v2
    iteration with per-section validation.
  * Very long schemas (tsconfig is 432 KB) are included but slow; if
    this becomes a perf concern, precompile validators once per
    process.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

__all__ = [
    "validate_config_semantics",
    "SCHEMA_DIR",
    "PATH_MATCHERS",
    "load_schema",
]


# Path to the bundled schemas directory. Resolvable from anywhere in
# gitoma since ``Path(__file__)`` is the module's filesystem location.
SCHEMA_DIR = Path(__file__).parent / "schemas"


# (fnmatch pattern, schema filename) — first match wins. Patterns match
# against the REL path's full string, not just the basename, so
# ``.github/workflows/*.yml`` can be distinguished from other ``*.yml``.
#
# Extend by adding a tuple + dropping the corresponding ``<name>.json``
# into ``gitoma/worker/schemas/``. Update tests to cover the new
# pattern.
PATH_MATCHERS: list[tuple[str, str]] = [
    # ESLint — the offender that motivated this guard
    (".eslintrc.json",               "eslintrc.json"),
    ("**/.eslintrc.json",            "eslintrc.json"),
    # Prettier — only the JSON form; .js forms deferred
    (".prettierrc.json",             "prettierrc.json"),
    (".prettierrc",                  "prettierrc.json"),  # also commonly JSON
    ("**/.prettierrc.json",          "prettierrc.json"),
    # npm package
    ("package.json",                 "package.json"),
    # TypeScript
    ("tsconfig.json",                "tsconfig.json"),
    ("tsconfig.*.json",              "tsconfig.json"),  # e.g. tsconfig.build.json
    ("**/tsconfig.json",             "tsconfig.json"),
    ("**/tsconfig.*.json",           "tsconfig.json"),
    # GitHub workflows + dependabot
    (".github/workflows/*.yml",      "github-workflow.json"),
    (".github/workflows/*.yaml",     "github-workflow.json"),
    (".github/dependabot.yml",       "dependabot.json"),
    (".github/dependabot.yaml",      "dependabot.json"),
    # Rust
    ("Cargo.toml",                   "cargo.json"),
    ("**/Cargo.toml",                "cargo.json"),
]


def load_schema(filename: str) -> dict[str, Any] | None:
    """Load a bundled schema by filename. Returns None when the file
    is missing on disk (shouldn't happen in a clean install, but
    tolerated for partial deploys)."""
    path = SCHEMA_DIR / filename
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _match_schema(rel_path: str) -> str | None:
    """Return the schema filename that matches the given rel path, or
    None if no matcher fires. Deterministic: iterates in declaration
    order and returns the FIRST hit."""
    # Normalize to forward-slash for cross-platform matching
    norm = rel_path.replace("\\", "/")
    for pattern, schema_file in PATH_MATCHERS:
        if fnmatch.fnmatch(norm, pattern):
            return schema_file
    return None


def _make_github_workflow_loader():
    """A YAML loader that does NOT treat ``on``/``off``/``yes``/``no``
    as booleans. GitHub Actions workflows use ``on:`` as a trigger
    key; YAML 1.1 resolves that as the boolean ``True``, which then
    doesn't match any property in the github-workflow schema
    (schema expects literal string ``"on"``).

    Strategy: subclass ``SafeLoader`` and override its implicit
    resolver for the ``bool`` tag with a stricter pattern that only
    matches ``true/True/TRUE/false/False/FALSE``.
    """
    import re
    import yaml  # type: ignore[import-not-found]

    class WorkflowSafeLoader(yaml.SafeLoader):
        pass

    # Strip all existing bool resolvers then add a stricter one.
    WorkflowSafeLoader.yaml_implicit_resolvers = {
        k: [(tag, regexp) for (tag, regexp) in v
            if tag != "tag:yaml.org,2002:bool"]
        for k, v in WorkflowSafeLoader.yaml_implicit_resolvers.items()
    }
    WorkflowSafeLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    return WorkflowSafeLoader


def _parse_content(rel_path: str, full_path: Path) -> Any | None:
    """Parse a config file according to its extension. Returns the
    parsed object or None when parsing fails (G2 has already flagged
    it — no point running schema validation on broken content)."""
    suffix = full_path.suffix.lower()
    try:
        if suffix == ".json":
            with open(full_path, encoding="utf-8") as f:
                return json.load(f)
        if suffix in (".yml", ".yaml"):
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError:
                return None
            # GitHub Actions workflows use ``on:`` as a trigger key;
            # YAML 1.1's default loader maps it to Python ``True``
            # which breaks schema validation. Use a stricter loader
            # for workflow files so ``on`` stays a string.
            norm = rel_path.replace("\\", "/")
            if ".github/workflows/" in norm:
                loader = _make_github_workflow_loader()
                with open(full_path, encoding="utf-8") as f:
                    return yaml.load(f, Loader=loader)
            with open(full_path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        if suffix == ".toml":
            try:
                import tomllib
            except ImportError:
                return None
            with open(full_path, "rb") as f:
                return tomllib.load(f)
    except Exception:
        return None
    return None


def _build_offline_registry() -> Any | None:
    """Build a jsonschema Registry that serves bundled schemas from
    disk and returns a permissive empty schema for any external $ref
    we don't bundle.

    Bundled ESLint/Prettier/TSConfig/etc schemas contain ``$ref`` to
    sister schemastore URLs (``https://json.schemastore.org/partial-
    eslint-plugins.json`` etc). Without network access / offline use,
    jsonschema would fail with ``Unresolvable``. The retriever maps
    those URLs back to local files when we have them, else returns
    ``{}`` (matches everything) so validation continues best-effort
    on the schemas we DO have.

    Returns None when the modern ``referencing`` library is not
    available (very old jsonschema versions) — caller falls back to
    direct validate without a registry.
    """
    try:
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource
        from referencing.jsonschema import DRAFT7
    except ImportError:
        return None

    def _retrieve(uri: str):
        # ``https://json.schemastore.org/X.json`` → ``SCHEMA_DIR/X.json``
        # if bundled, else return a permissive {} schema.
        last = uri.rstrip("/").split("/")[-1]
        local = SCHEMA_DIR / last
        if local.is_file():
            try:
                with open(local, encoding="utf-8") as f:
                    return Resource.from_contents(json.load(f))
            except Exception:
                pass
        # Permissive fallback — validation proceeds on what we DO have.
        return Resource(contents={}, specification=DRAFT7)

    return Registry(retrieve=_retrieve)


def validate_config_semantics(
    root: Path, touched: list[str]
) -> tuple[str, str] | None:
    """Validate every touched config file against its bundled schema.

    Returns ``(rel_path, error_message)`` on the FIRST violation,
    ``None`` on clean. Skips files without a matching schema (silent
    pass-through for unrelated files). Errors are formatted to point
    at the offending path within the config + human-readable message
    — e.g. ``"parser: {'parser': '...'} is not of type 'string'"``.

    Silent pass (no error) when:
      * No PATH_MATCHERS fires for the file
      * Schema file missing on disk (partial install)
      * ``jsonschema`` module not importable
      * File doesn't exist on disk (deleted in this commit)
      * Parse fails (G2 already caught it)

    External ``$ref``-to-other-schemastore URLs are resolved against
    bundled schemas when present, and otherwise fall through to a
    permissive empty schema (so validation still catches same-file
    schema violations even when a referenced sub-schema is missing).
    """
    try:
        import jsonschema
    except ImportError:
        return None

    registry = _build_offline_registry()

    for rel in touched:
        schema_file = _match_schema(rel)
        if schema_file is None:
            continue
        full = root / rel
        if not full.is_file():
            continue
        schema = load_schema(schema_file)
        if schema is None:
            continue
        content = _parse_content(rel, full)
        if content is None:
            continue  # G2 will have caught it
        try:
            if registry is not None:
                validator_cls = jsonschema.validators.validator_for(schema)
                validator = validator_cls(schema, registry=registry)
                errors = list(validator.iter_errors(content))
                if errors:
                    # Pick the most specific error via best_match.
                    exc = jsonschema.exceptions.best_match(iter(errors))
                    loc = ".".join(str(p) for p in exc.absolute_path) or "<root>"
                    return rel, f"{loc}: {exc.message}"
            else:
                jsonschema.validate(content, schema)
        except jsonschema.ValidationError as exc:
            loc = ".".join(str(p) for p in exc.absolute_path) or "<root>"
            return rel, f"{loc}: {exc.message}"
        except jsonschema.SchemaError as exc:
            # The SCHEMA itself is broken — shouldn't happen with
            # bundled schemas, but a clear error beats a crash.
            return rel, f"<bundled schema {schema_file} invalid>: {exc.message}"
        except Exception:
            # Any other Referencing/Unresolvable error: silent pass.
            # We prefer false-negatives (miss a violation) to false-
            # positives (block a valid config because a sub-schema
            # couldn't be fetched).
            continue

    return None
