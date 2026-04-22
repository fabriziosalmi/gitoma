"""Tests for the compile-fix-mode build-manifest hard block in patcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitoma.worker.patcher import PatchError, apply_patches


def _patch(action: str, path: str, content: str = "") -> dict:
    return {"action": action, "path": path, "content": content}


@pytest.mark.parametrize(
    "manifest",
    [
        "go.mod",
        "Cargo.toml",
        "pyproject.toml",
        "setup.py",
        "package.json",
        "Gemfile",
        "pom.xml",
        "build.gradle",
    ],
)
def test_manifest_edits_rejected_in_compile_fix_mode(
    tmp_path: Path, manifest: str
) -> None:
    """Every build-system manifest must be blocked when compile_fix_mode=True,
    regardless of whether we're creating or modifying it."""
    patches = [_patch("modify", manifest, "module x\nfake-line\n")]
    with pytest.raises(PatchError, match="compile-fix mode"):
        apply_patches(tmp_path, patches, compile_fix_mode=True)


def test_manifest_edits_allowed_when_flag_off(tmp_path: Path) -> None:
    """The manifest block is conditional — when compile_fix_mode=False
    the LLM is free to edit manifests (legitimate "add dependency" tasks)."""
    patches = [_patch("create", "go.mod", "module ok\n\ngo 1.22\n")]
    touched = apply_patches(tmp_path, patches, compile_fix_mode=False)
    assert "go.mod" in touched
    assert (tmp_path / "go.mod").read_text().startswith("module ok")


def test_source_edits_still_allowed_in_compile_fix_mode(tmp_path: Path) -> None:
    """The whole POINT of compile-fix mode is to fix source files.
    Source-file patches must pass through."""
    patches = [_patch("create", "src/main.go", "package main\n\nfunc main() {}\n")]
    touched = apply_patches(tmp_path, patches, compile_fix_mode=True)
    assert "src/main.go" in touched


def test_manifest_rejection_message_is_actionable(tmp_path: Path) -> None:
    """Error message must tell the maintainer (a) what was blocked and
    (b) why. A bare ``PatchError`` would leave them puzzled."""
    patches = [_patch("modify", "go.mod", "module x\n")]
    with pytest.raises(PatchError) as ei:
        apply_patches(tmp_path, patches, compile_fix_mode=True)
    msg = str(ei.value)
    assert "go.mod" in msg
    assert "compile-fix" in msg
    assert "Build Integrity" in msg


def test_compile_fix_mode_does_not_loosen_primary_denylist(tmp_path: Path) -> None:
    """``.github/workflows/ci.yml`` must STILL be rejected even when
    compile_fix_mode=True — compile-fix mode adds a new block, it
    doesn't override the primary security denylist."""
    patches = [_patch("modify", ".github/workflows/ci.yml", "x: y\n")]
    with pytest.raises(PatchError, match="sensitive path"):
        apply_patches(tmp_path, patches, compile_fix_mode=True)


def test_nested_manifest_filename_also_blocked(tmp_path: Path) -> None:
    """A go.mod in a sub-module directory is still a manifest — the
    block matches by filename, not by root-level position."""
    patches = [_patch("modify", "submodule/go.mod", "module sub\n")]
    with pytest.raises(PatchError, match="compile-fix"):
        apply_patches(tmp_path, patches, compile_fix_mode=True)


def test_manifest_deletion_also_blocked(tmp_path: Path) -> None:
    """Deleting a manifest is as destructive as corrupting one — the
    compile-fix guard applies equally to any action on these files."""
    (tmp_path / "go.mod").write_text("module x\n")
    patches = [_patch("delete", "go.mod", "")]
    with pytest.raises(PatchError, match="compile-fix"):
        apply_patches(tmp_path, patches, compile_fix_mode=True)
