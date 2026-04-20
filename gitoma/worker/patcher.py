"""Patcher — applies LLM-generated file patches to the working tree."""

from __future__ import annotations

from pathlib import Path


class PatchError(Exception):
    pass


def apply_patches(root: Path, patches: list[dict]) -> list[str]:
    """
    Apply a list of file patches to the repo working tree.

    Each patch dict has:
        - action: "create" | "modify" | "delete"
        - path: relative path string
        - content: file content (for create/modify)

    Returns list of touched relative paths.
    """
    touched: list[str] = []

    for patch in patches:
        action = patch.get("action", "modify")
        rel_path = patch.get("path", "")
        content = patch.get("content", "")

        if not rel_path:
            continue

        # Security: prevent path traversal
        abs_path = (root / rel_path).resolve()
        if not str(abs_path).startswith(str(root.resolve())):
            raise PatchError(f"Path traversal attempt blocked: {rel_path}")

        if action in ("create", "modify"):
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(content, encoding="utf-8")
            touched.append(rel_path)

        elif action == "delete":
            if abs_path.exists():
                abs_path.unlink()
                touched.append(rel_path)

        else:
            raise PatchError(f"Unknown patch action: {action}")

    return touched
