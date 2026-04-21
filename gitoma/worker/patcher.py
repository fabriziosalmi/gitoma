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


def apply_patches(root: Path, patches: list[dict[str, Any]]) -> list[str]:
    """Apply a list of file patches to the repo working tree.

    Each patch dict has:
        - action: "create" | "modify" | "delete"
        - path: relative path string (must stay inside ``root``)
        - content: file content (for create/modify, <= MAX_PATCH_SIZE_BYTES)

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
