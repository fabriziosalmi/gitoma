"""Patcher — applies LLM-generated file patches to the working tree.

Security model: patches come from the LLM (not the user), so they must be
treated as untrusted. The LLM can be prompt-injected via repo content, so
any path it suggests is a potential path-traversal or supply-chain vector.
We therefore enforce three independent guards before touching the FS:

  1. **Containment**: the resolved path must be strictly inside the repo
     root (``Path.is_relative_to``). A naïve ``str.startswith`` check is
     NOT enough — with root ``/tmp/foo`` it would accept ``/tmp/foo-evil/x``.
  2. **Denylist**: even inside the repo, some paths are off-limits (``.git``,
     GitHub Actions workflows, env files). Writing there would let an
     attacker pivot from "LLM picks a filename" to "steal CI secrets" or
     "rewrite git hooks on the next commit".
  3. **Size cap**: no single file write exceeds ``MAX_PATCH_SIZE_BYTES``, so
     a runaway / malicious patch can't fill the disk.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any


class PatchError(Exception):
    pass


# 2 MiB per file is way beyond any legitimate code file; well under any
# disk-fill attack. Picked empirically — bumps if we ever patch vendored deps.
MAX_PATCH_SIZE_BYTES = 2 * 1024 * 1024

# Paths the LLM must never be allowed to modify. Matched against path parts so
# both ``.git/config`` and nested ``foo/.git/config`` are blocked. ``.github``
# itself is allowed (README badges live there); ``.github/workflows`` is not
# (CI runs with repo secrets — arbitrary workflow = arbitrary secret exfil).
#
# The denylist is split by match shape (path part / prefix / exact filename /
# filename prefix) for efficiency, but its purpose is one thing: stop the LLM
# from pivoting from "edit a Python file" to "exfiltrate cloud creds" or
# "poison the dependency graph". When in doubt, add it here.
_DENY_PATH_PARTS: frozenset[str] = frozenset({
    ".git",
    # Cloud / container creds. Local-only by convention but operators
    # routinely commit them by accident (or store them in a worktree),
    # and the LLM has no reason to touch any of them.
    ".aws", ".ssh", ".docker", ".gcp", ".azure", ".kube",
})
_DENY_PATH_PREFIXES: tuple[tuple[str, ...], ...] = (
    (".github", "workflows"),
    (".github", "actions"),
)
_DENY_FILENAMES: frozenset[str] = frozenset({
    # Env / dotenv variants
    ".env", ".envrc", ".netrc", ".pypirc",
    # Git metadata files (separate from the .git directory itself)
    ".gitmodules", ".gitattributes",
    # Repo-governance files: changing CODEOWNERS would let the LLM
    # bypass review requirements on the very PR it's about to open.
    "CODEOWNERS",
    # Package-manager auth/registry config — leaks publish tokens.
    ".npmrc", ".yarnrc", ".yarnrc.yml",
    # Lockfiles: an LLM-generated lockfile edit is a supply-chain poison
    # vector (silent dependency swap). Lockfiles must only be regenerated
    # by the package manager itself, never by the LLM patching JSON/TOML
    # text directly.
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock", "uv.lock", "Cargo.lock",
    "Gemfile.lock", "composer.lock", "go.sum",
})
_DENY_FILENAME_PREFIXES: tuple[str, ...] = (".env.",)  # .env.prod, .env.local…

# Build-system manifests: files parsed deterministically by a toolchain
# at build time. Any malformed edit here breaks the entire project
# before any unit test can run (caught live rung-1 v2: worker wrote
# Python-style ``# comments`` into go.mod, go refused to parse;
# rung-3 v11: worker over-scoped to pyproject.toml line 19, pytest
# config-parse failed before any test could run).
#
# Default-on rejection (since rung-3 v11): any manifest edit is
# blocked unless EITHER (a) compile_fix_mode=True forces the strict
# block (then NEVER allowed; goal is restore source) OR (b) the
# planner explicitly puts the manifest filename in the subtask's
# file_hints, which the worker passes through as
# ``allowed_manifests`` to ``apply_patches``.
#
# Exposed (no underscore) so worker.py can intersect with
# subtask.file_hints to compute the allow-list.
BUILD_MANIFESTS: frozenset[str] = frozenset({
    "go.mod",
    "Cargo.toml",
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-dev.txt",
    "package.json",
    "Gemfile",
    "composer.json",
    "build.gradle", "build.gradle.kts", "pom.xml",
})


def denylist_summary() -> str:
    """Return a compact, human-readable listing of denied paths.

    Consumed by the planner / worker prompts so the LLM knows which
    paths are off-limits before it generates a patch. Before the b2v
    live run we learned the hard way: the planner happily suggested
    three subtasks targeting ``.github/workflows/deploy-docs.yml`` and
    the patcher had to reject each one — three LLM round-trips burned
    per run on impossible tasks. Telling the LLM up-front is the
    obvious fix.

    The shape intentionally mirrors ``_DENY_*`` so it's maintenance-cheap:
    add an entry there and the prompt updates automatically.
    """
    parts_lines = ", ".join(sorted(_DENY_PATH_PARTS))
    prefix_lines = ", ".join(
        f"{'/'.join(p)}/…" for p in _DENY_PATH_PREFIXES
    )
    filename_lines = ", ".join(sorted(_DENY_FILENAMES))
    prefix_filename_lines = ", ".join(f"{p}*" for p in _DENY_FILENAME_PREFIXES)
    return (
        f"- Any path containing directories named: {parts_lines}\n"
        f"- Any path starting with: {prefix_lines}\n"
        f"- Any file named: {filename_lines}\n"
        f"- Any file whose name starts with: {prefix_filename_lines}"
    )


def _reject_unsafe_relpath(rel_path: str) -> None:
    """Fail fast on obviously-unsafe input before we even touch the FS.

    Catches absolute paths, Windows-drive paths, embedded NULs, and
    directory-style trailing slashes. None of these can be the product
    of any legitimate LLM patch: the first two are attacks, the third
    is an intent mismatch (you cannot write file content to a directory).
    """
    if not rel_path or rel_path.strip() != rel_path:
        raise PatchError(f"Invalid patch path: {rel_path!r}")
    if "\x00" in rel_path:
        raise PatchError("Null byte in patch path")
    # Reject directory-style paths (``./src/tests/``, ``docs\``). The b2v
    # live run surfaced this: the LLM generated ``path="./src/tests/"``
    # intending a directory; the patcher then created the dir via
    # ``parent.mkdir`` and crashed on ``os.open(abs_path, ...)`` with a
    # confusing ``[Errno 17] File exists`` — the target resolved to a
    # directory, not a file. Fail with a clear actionable message so the
    # LLM's next attempt targets a specific file.
    if rel_path.endswith(("/", "\\")):
        raise PatchError(
            f"Patch paths must target a file, not a directory: {rel_path!r}. "
            "Specify an actual filename (e.g. 'src/tests/basic.test.ts' "
            "instead of 'src/tests/')."
        )
    p = PurePosixPath(rel_path.replace("\\", "/"))
    if p.is_absolute() or (len(rel_path) >= 2 and rel_path[1] == ":"):
        raise PatchError(f"Absolute paths are not allowed: {rel_path!r}")


def _reject_if_denied(rel_path: str) -> None:
    """Block writes to sensitive paths (`.git/`, workflows, `.env*`)."""
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if not parts:
        return
    for part in parts:
        if part in _DENY_PATH_PARTS:
            raise PatchError(f"Refusing to touch sensitive path: {rel_path}")
    for prefix in _DENY_PATH_PREFIXES:
        if len(parts) >= len(prefix) and parts[: len(prefix)] == prefix:
            raise PatchError(f"Refusing to touch sensitive path: {rel_path}")
    filename = parts[-1]
    if filename in _DENY_FILENAMES:
        raise PatchError(f"Refusing to touch sensitive file: {rel_path}")
    if any(filename.startswith(pfx) for pfx in _DENY_FILENAME_PREFIXES):
        raise PatchError(f"Refusing to touch sensitive file: {rel_path}")


def _reject_if_build_manifest(rel_path: str) -> None:
    """Unconditional rejection active when compile_fix_mode=True.

    A compile-fix run ("project doesn't build; make it build") has no
    business editing build-system manifests — the compiler's complaint
    is about source, not about dependencies. Letting the LLM rewrite
    a manifest here is how rung-1 v2 ended with ``# Python-style``
    comments in ``go.mod`` and a broken tree."""
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if not parts:
        return
    filename = parts[-1]
    if filename in BUILD_MANIFESTS:
        raise PatchError(
            f"Refusing to edit build manifest {rel_path!r} during compile-fix mode — "
            "the Build Integrity analyzer reported failure; the run's goal is to "
            "restore a compiling state, not to reshape dependencies. "
            "If the compile error genuinely requires a manifest change, surface that "
            "as a separate subtask after the build is green."
        )


def _reject_if_unsanctioned_manifest(
    rel_path: str, allowed_manifests: set[str] | None,
) -> None:
    """Default-on rejection for ``BUILD_MANIFESTS`` filenames outside
    compile-fix mode. The LLM is allowed to edit a manifest ONLY when
    the planner EXPLICITLY put that filename in the subtask's
    ``file_hints`` (the explicit allow-list is then passed in via
    ``allowed_manifests``).

    Caught live on rung-3 v11: worker correctly fixed src/db.py SQLi,
    but ALSO over-scoped to pyproject.toml on the same diff and broke
    TOML syntax at line 19. pytest config-parse failed before any
    test could run — the SQLi fix shipped untestable. Default-block
    closes that collateral-damage path while leaving an explicit door
    open for the legitimate "add a dependency" subtask."""
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if not parts:
        return
    filename = parts[-1]
    if filename not in BUILD_MANIFESTS:
        return
    if allowed_manifests and filename in allowed_manifests:
        return  # Planner explicitly sanctioned this manifest edit
    raise PatchError(
        f"Refusing to edit build manifest {rel_path!r} — "
        "no subtask file_hint explicitly targets it. Build manifests "
        "(pyproject.toml, package.json, Cargo.toml, go.mod, …) are "
        "high-blast-radius files: a single bad token breaks every "
        "subsequent test / build. If editing is genuinely intended, "
        "the planner should put the filename in the subtask's "
        "file_hints; the patcher will then allow it."
    )


def validate_post_write_syntax(
    root: Path, touched: list[str]
) -> tuple[str, str] | None:
    """Per-file syntax check for files we just wrote.

    Routes by extension to a stdlib parser:
      * ``.toml`` → ``tomllib``
      * ``.json`` → ``json``
      * ``.yml`` / ``.yaml`` → ``yaml.safe_load`` (skipped if PyYAML
        absent — yaml is an optional dep, no transitive cost)

    ``.py`` files use ``py_compile.compile`` — same parser as the
    interpreter, no subprocess overhead. Originally skipped here in
    favour of BuildAnalyzer, but rung-3 v16 caught the refiner
    silently corrupting src/db.py (a triple-quote string opener
    truncated to an empty-string-plus-bare-bracket sequence) on its
    own apply path which doesn't go through BuildAnalyzer. A
    patcher-level Python check covers BOTH the worker AND the
    refiner with one helper.

    Other extensions (``.md``, ``.txt``, ``.go``, ``.rs``, ``.js``,
    ``.ts``, …) have no stdlib parser cheap enough to justify a per-
    write call here; they go through ``BuildAnalyzer`` instead.

    Returns ``(rel_path, error_message)`` on the FIRST failure, or
    ``None`` on clean. Errors include the parser's own line/column
    so the worker's retry prompt can point the LLM at the exact
    bad spot.

    Caught live on rung-3 v12: a planner-sanctioned T004 edit added
    ``[tool.coverage.config]`` to ``pyproject.toml`` with
    ``source = src`` (bare identifier instead of quoted string).
    The patcher allowed it (sanction-by-file_hint), the build check
    didn't run TOML through ``tomllib``, pytest config-parse
    failed at runtime → entire test suite uncollectable.
    """
    import json as _json
    try:
        import tomllib as _tomllib
    except ImportError:
        _tomllib = None

    try:
        import yaml as _yaml  # type: ignore[import-not-found]
    except ImportError:
        _yaml = None

    for rel in touched:
        full = root / rel
        if not full.is_file():
            continue
        suffix = full.suffix.lower()
        try:
            if suffix == ".toml" and _tomllib is not None:
                with open(full, "rb") as f:
                    _tomllib.load(f)
            elif suffix == ".json":
                with open(full, encoding="utf-8") as f:
                    _json.load(f)
            elif suffix in (".yml", ".yaml") and _yaml is not None:
                with open(full, encoding="utf-8") as f:
                    _yaml.safe_load(f)
            elif suffix == ".py":
                # Use builtin ``compile()`` rather than
                # ``py_compile.compile`` — same parser, no .pyc side-
                # effect. ``compile`` raises ``SyntaxError`` directly.
                with open(full, "rb") as f:
                    _src = f.read()
                compile(_src, str(full), "exec")
        except Exception as exc:
            return rel, f"{type(exc).__name__}: {exc}"

    return None


def apply_patches(
    root: Path,
    patches: list[dict[str, Any]],
    *,
    compile_fix_mode: bool = False,
    allowed_manifests: set[str] | None = None,
) -> list[str]:
    """Apply a list of file patches to the repo working tree.

    Each patch dict has:
        - action: "create" | "modify" | "delete"
        - path: relative path string (must stay inside ``root``)
        - content: file content (for create/modify, <= MAX_PATCH_SIZE_BYTES)

    Build-manifest hard-block (caught live rung-3 v11: worker over-
    scoped to pyproject.toml on a subtask that wasn't supposed to
    touch it; broke TOML syntax at line 19; pytest collection failed;
    the otherwise-correct src/db.py SQLi fix shipped untestable):

      * If ``compile_fix_mode=True`` → ALWAYS reject any
        ``BUILD_MANIFESTS`` filename (the run's goal is to restore
        compilation; reshaping deps is a separate concern).
      * Else if ``allowed_manifests`` is None → reject any manifest
        (the conservative default — the LLM almost never NEEDS to
        edit pyproject/Cargo/package.json mid-task).
      * Else → reject manifest edits whose filename is NOT in the
        explicit ``allowed_manifests`` allow-list. Pass the set of
        manifest filenames the planner EXPLICITLY put in the
        subtask's file_hints.

    Returns list of touched relative paths.
    """
    touched: list[str] = []
    root_resolved = root.resolve()

    for patch in patches:
        action = patch.get("action", "modify")
        rel_path = patch.get("path", "")
        content = patch.get("content", "")

        if not rel_path:
            continue

        _reject_unsafe_relpath(rel_path)
        _reject_if_denied(rel_path)
        if compile_fix_mode:
            _reject_if_build_manifest(rel_path)
        else:
            _reject_if_unsanctioned_manifest(rel_path, allowed_manifests)

        # Resolve and require strict containment — NOT str.startswith, which
        # would incorrectly accept `/tmp/foo` as a prefix of `/tmp/foo-evil`.
        abs_path = (root / rel_path).resolve()
        if not abs_path.is_relative_to(root_resolved):
            raise PatchError(f"Path traversal attempt blocked: {rel_path}")

        if action in ("create", "modify"):
            if not isinstance(content, str):
                raise PatchError(f"Patch content must be a string: {rel_path}")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_PATCH_SIZE_BYTES:
                raise PatchError(
                    f"Patch exceeds {MAX_PATCH_SIZE_BYTES} bytes: {rel_path} "
                    f"({len(encoded)} bytes)"
                )
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW closes the symlink TOCTOU: if a symlink to
            # /etc/passwd was planted between resolve() and the write, open()
            # refuses instead of following it.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(abs_path, flags, 0o644)
            except OSError as e:
                raise PatchError(f"Cannot open patch target {rel_path}: {e}") from e
            try:
                os.write(fd, encoded)
            finally:
                os.close(fd)
            touched.append(rel_path)

        elif action == "delete":
            if abs_path.exists():
                abs_path.unlink()
                touched.append(rel_path)

        else:
            raise PatchError(f"Unknown patch action: {action}")

    return touched
