"""Adversarial tests for security-hardening fixes.

Covers the gaps the draconian audit found:

* patcher — prefix bug, denylist, size cap, symlink TOCTOU, weird input
* API token compare — timing-safe + wrong/missing token rejection
* runtime_token — fail-closed when the FS can't restrict permissions
* heartbeat — exit_clean must reset when a new run starts

If any of these regress, the threat model documented in the patcher module
docstring silently stops holding.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from gitoma.core.config import (
    RUNTIME_TOKEN_FILE,
    ensure_runtime_api_token,
)
from gitoma.worker.patcher import (
    MAX_PATCH_SIZE_BYTES,
    PatchError,
    apply_patches,
)


# ── Patcher: prefix bug (the reason is_relative_to replaced startswith) ─────


def test_patcher_blocks_sibling_prefix_attack(tmp_path: Path):
    """The old ``str(abs).startswith(str(root))`` check accepted
    ``/tmp/foo-evil/x`` when root was ``/tmp/foo``. With ``is_relative_to``
    this must now raise."""
    root = tmp_path / "foo"
    root.mkdir()
    sibling = tmp_path / "foo-evil"
    sibling.mkdir()

    with pytest.raises(PatchError, match="traversal"):
        apply_patches(root, [{
            "action": "create",
            "path": "../foo-evil/pwn.py",
            "content": "print('owned')",
        }])
    assert not (sibling / "pwn.py").exists()


def test_patcher_blocks_classic_dotdot_traversal(tmp_path: Path):
    with pytest.raises(PatchError, match="traversal"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "../../etc/evil.cfg",
            "content": "",
        }])


def test_patcher_rejects_absolute_path(tmp_path: Path):
    with pytest.raises(PatchError, match="Absolute"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "/etc/passwd",
            "content": "root::0:0",
        }])


def test_patcher_rejects_null_byte(tmp_path: Path):
    with pytest.raises(PatchError, match="[Nn]ull"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "ok.py\x00.sh",
            "content": "",
        }])


# ── Patcher: denylist — sensitive files and directories ─────────────────────


@pytest.mark.parametrize("bad_path", [
    ".git/config",
    ".git/hooks/pre-commit",
    "subdir/.git/HEAD",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy.yaml",
    ".github/actions/custom/action.yml",
    ".env",
    ".env.prod",
    ".env.local",
    ".gitmodules",
    ".gitattributes",
    ".netrc",
    ".pypirc",
])
def test_patcher_denies_sensitive_targets(tmp_path: Path, bad_path: str):
    """Supply-chain blast-radius control: even if the LLM is convinced to
    patch these paths, the file must never be written."""
    with pytest.raises(PatchError, match="[Rr]efusing"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": bad_path,
            "content": "poisoned",
        }])
    assert not (tmp_path / bad_path).exists()


def test_patcher_allows_non_workflow_github_dir(tmp_path: Path):
    """`.github/` itself is legitimate (ISSUE_TEMPLATE, FUNDING, README)
    — only actions/workflows are locked down."""
    touched = apply_patches(tmp_path, [{
        "action": "create",
        "path": ".github/ISSUE_TEMPLATE/bug.md",
        "content": "# Bug",
    }])
    assert touched == [".github/ISSUE_TEMPLATE/bug.md"]


def test_patcher_denies_delete_of_sensitive_file(tmp_path: Path):
    """Delete is just as dangerous as create for `.git/config`."""
    with pytest.raises(PatchError, match="[Rr]efusing"):
        apply_patches(tmp_path, [{
            "action": "delete",
            "path": ".git/config",
        }])


# ── Patcher: size cap ───────────────────────────────────────────────────────


def test_patcher_rejects_oversized_content(tmp_path: Path):
    big = "x" * (MAX_PATCH_SIZE_BYTES + 1)
    with pytest.raises(PatchError, match="exceeds"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "big.txt",
            "content": big,
        }])
    assert not (tmp_path / "big.txt").exists()


def test_patcher_accepts_content_at_limit(tmp_path: Path):
    at_limit = "x" * MAX_PATCH_SIZE_BYTES
    touched = apply_patches(tmp_path, [{
        "action": "create",
        "path": "big.txt",
        "content": at_limit,
    }])
    assert touched == ["big.txt"]


def test_patcher_rejects_non_string_content(tmp_path: Path):
    with pytest.raises(PatchError, match="string"):
        apply_patches(tmp_path, [{
            "action": "create",
            "path": "ok.py",
            "content": {"not": "a string"},
        }])


# ── Patcher: symlink TOCTOU ─────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="symlink perms on Windows")
def test_patcher_refuses_to_follow_symlink_out_of_root(tmp_path: Path):
    """Planted before the patch runs: the LLM suggests a legitimate-looking
    relative path, but the target is a symlink pointing outside the repo.
    With O_NOFOLLOW the write must fail rather than clobbering the
    symlink target."""
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")

    repo = tmp_path / "repo"
    repo.mkdir()
    symlink_inside_repo = repo / "innocent.py"
    os.symlink(outside, symlink_inside_repo)

    with pytest.raises(PatchError):
        apply_patches(repo, [{
            "action": "create",
            "path": "innocent.py",
            "content": "pwn",
        }])

    assert outside.read_text() == "untouched"


# ── API token compare: timing-safe + rejection ──────────────────────────────


def test_verify_token_rejects_wrong_token():
    """Hit the dependency directly with a bad token — must raise 403."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from gitoma.api.server import verify_token

    with mock.patch("gitoma.api.server.load_config") as m:
        m.return_value = mock.MagicMock(api_auth_token="the-real-token")
        with pytest.raises(HTTPException) as exc_info:
            verify_token(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong")
            )
    assert exc_info.value.status_code == 403


def test_verify_token_accepts_exact_match():
    from fastapi.security import HTTPAuthorizationCredentials

    from gitoma.api.server import verify_token

    with mock.patch("gitoma.api.server.load_config") as m:
        m.return_value = mock.MagicMock(api_auth_token="the-real-token")
        verify_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="the-real-token")
        )


def test_verify_token_uses_constant_time_compare():
    """Regression guard: the fix replaced ``!=`` with ``secrets.compare_digest``.
    If someone reintroduces ``!=``, this test catches it via a spy."""
    from fastapi.security import HTTPAuthorizationCredentials

    from gitoma.api import server as server_module

    with mock.patch("gitoma.api.server.load_config") as m:
        m.return_value = mock.MagicMock(api_auth_token="target-token")
        with mock.patch.object(
            server_module.secrets, "compare_digest", wraps=server_module.secrets.compare_digest
        ) as spy:
            try:
                server_module.verify_token(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="x")
                )
            except Exception:
                pass
    spy.assert_called_once_with("x", "target-token")


def test_verify_token_503_when_server_has_no_token():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from gitoma.api.server import verify_token

    with mock.patch("gitoma.api.server.load_config") as m:
        m.return_value = mock.MagicMock(api_auth_token="")
        with pytest.raises(HTTPException) as exc_info:
            verify_token(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="anything")
            )
    assert exc_info.value.status_code == 503


# ── runtime_token: fail-closed on FS that can't restrict perms ──────────────


def test_ensure_runtime_token_creates_with_600(tmp_path, monkeypatch):
    """Happy path: file exists with mode 0o600 after generation."""
    token_file = tmp_path / "runtime_token"
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr("gitoma.core.config.load_config",
                        lambda: type("C", (), {"api_auth_token": ""})())

    token, generated = ensure_runtime_api_token()
    assert generated is True
    assert token
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_ensure_runtime_token_fails_closed_when_perms_cannot_be_restricted(
    tmp_path, monkeypatch
):
    """Simulate a filesystem that ignores mode bits (FAT, some NFS setups).
    ``ensure_runtime_api_token`` must delete the file and raise — never
    leave a world-readable token sitting on disk."""
    token_file = tmp_path / "runtime_token"
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr("gitoma.core.config.load_config",
                        lambda: type("C", (), {"api_auth_token": ""})())

    # Pretend every fresh file is world-readable regardless of mode arg.
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)

        class _R:
            # st_mode with o+r,g+r forced on
            st_mode = st.st_mode | 0o044
        return _R()

    monkeypatch.setattr(Path, "stat", fake_stat)

    with pytest.raises(RuntimeError, match="restrict"):
        ensure_runtime_api_token()

    # Crucial: the token file must NOT remain on disk.
    assert not token_file.exists()


def test_ensure_runtime_token_reuses_existing_file(tmp_path, monkeypatch):
    """If a token was already persisted, return it — don't clobber."""
    token_file = tmp_path / "runtime_token"
    monkeypatch.setattr("gitoma.core.config.GITOMA_DIR", tmp_path)
    monkeypatch.setattr("gitoma.core.config.RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr("gitoma.core.config.load_config",
                        lambda: type("C", (), {"api_auth_token": ""})())

    token_file.write_text("already-there")
    token, generated = ensure_runtime_api_token()
    assert generated is False
    assert token == "already-there"


# ── /api/v1/state/{owner}/{name}: path-traversal guard ─────────────────────


@pytest.mark.parametrize("bad_owner,bad_name", [
    ("..",            "passwd"),    # leading-dot rejected
    (".gitconfig",    "x"),
    ("ok",            ".."),
    ("a/b",           "x"),         # raw slash (route matcher catches → 404 anyway, defense-in-depth here)
    ("ok",            "a b"),       # whitespace
    ("ok",            ""),
    ("",              "ok"),
    ("x" * 101,       "ok"),        # over the 100-char cap
])
def test_reset_state_rejects_unsafe_owner_or_name(bad_owner, bad_name, mocker):
    """``/api/v1/state/{owner}/{name}`` interpolates both segments into a
    filesystem path downstream (``STATE_DIR / f"{owner}__{name}.json"``).
    Anything that could escape that directory or smuggle traversal must
    be rejected at the HTTP boundary with 422 — never reach
    ``Path.unlink``. Without this guard a percent-encoded
    ``..%2F..%2Fetc%2Fpasswd``, decoded after route matching, lets an
    authenticated client unlink files outside the state dir."""
    from fastapi.testclient import TestClient
    from gitoma.api.server import app

    cfg = mocker.patch("gitoma.api.server.load_config")
    cfg.return_value.api_auth_token = "TOKEN"

    delete = mocker.patch("gitoma.api.routers._delete_state")
    load = mocker.patch("gitoma.api.routers._load_state")

    c = TestClient(app)
    resp = c.delete(
        f"/api/v1/state/{bad_owner}/{bad_name}",
        headers={"Authorization": "Bearer TOKEN"},
    )
    # 422 is the validator path; 404 is the router-level path-segment
    # rejection (raw slash never matches the {name} converter at all).
    # Either way, the underlying filesystem helpers must NOT be invoked.
    assert resp.status_code in (404, 422), resp.text
    delete.assert_not_called()
    load.assert_not_called()


@pytest.mark.parametrize("bad_value", [
    "..",
    ".env",
    "with/slash",
    "with\\backslash",
    "with\x00null",          # cannot test via httpx (blocks pre-flight) but the regex must reject
    "with space",
    "with\nnewline",
    "x" * 101,
    "",
])
def test_state_slug_regex_rejects_unsafe_inputs(bad_value):
    """Unit test for the validator regex itself — covers cases the
    request-level test can't reach because the HTTP client refuses to
    send them (NUL byte, newline)."""
    from gitoma.api.routers import _STATE_SLUG_RE

    assert _STATE_SLUG_RE.match(bad_value) is None, (
        f"_STATE_SLUG_RE accepted unsafe input {bad_value!r}; the path-traversal "
        "guard on /api/v1/state/{owner}/{name} depends on this regex"
    )


@pytest.mark.parametrize("good_value", [
    "octocat",
    "gitoma",
    "hello-world",
    "my_repo",
    "v1.2.3",
    "a",
    "x" * 100,
])
def test_state_slug_regex_accepts_github_style_names(good_value):
    """Sanity guard: the validator must not over-reject. GitHub's own
    owner/repo allow-set is a strict subset of what we accept."""
    from gitoma.api.routers import _STATE_SLUG_RE

    assert _STATE_SLUG_RE.match(good_value) is not None


# ── heartbeat exit_clean reset ──────────────────────────────────────────────


def test_heartbeat_entry_resets_exit_clean(tmp_path, monkeypatch):
    """If the previous run left exit_clean=True on disk and the next run
    is SIGKILL'd before its finally runs, orphan detection would miss it.
    The entry of the heartbeat context must force exit_clean=False."""
    from gitoma.cli import _heartbeat
    from gitoma.core import state as state_module
    from gitoma.core.state import AgentState

    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path)

    s = AgentState(repo_url="u", owner="o", name="r", branch="b")
    s.exit_clean = True  # leftover from a prior clean run
    state_module.save_state(s)

    # Enter and immediately exit with an exception *before* the finally can
    # flip it back — this simulates the "new run SIGKILL'd early" scenario
    # we care about. Read the on-disk state at the moment after entry.
    captured: dict[str, object] = {}

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with _heartbeat(s):
            reloaded = state_module.load_state("o", "r")
            captured["exit_clean_at_entry"] = reloaded.exit_clean  # type: ignore[union-attr]
            raise _Boom()

    assert captured["exit_clean_at_entry"] is False, (
        "heartbeat context must reset exit_clean at entry, otherwise a "
        "new run inheriting a stale clean flag can't be detected as orphan"
    )


_ = RUNTIME_TOKEN_FILE  # keep import warnable for readers
