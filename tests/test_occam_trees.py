"""Tests for the Occam-Trees integration module + scaffold CLI surface.

Pure-function tests + silent-fail-open invariant. The HTTP roundtrip
is exercised against a mock httpx Client; we don't hit the live
:8420 server in unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from gitoma.integrations.occam_trees import (
    OccamTreesClient,
    OccamTreesConfig,
    ResolvedScaffold,
    ScaffoldNode,
)


# ── OccamTreesConfig.from_env ─────────────────────────────────────


def test_from_env_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCCAM_TREES_URL", raising=False)
    cfg = OccamTreesConfig.from_env()
    assert cfg.enabled is False
    assert cfg.base_url == ""


def test_from_env_enabled_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    cfg = OccamTreesConfig.from_env()
    assert cfg.enabled is True
    assert cfg.base_url == "http://localhost:8420"


def test_from_env_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420/")
    cfg = OccamTreesConfig.from_env()
    assert cfg.base_url == "http://localhost:8420"


def test_from_env_adds_http_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """If user provides bare host:port, scheme is added."""
    monkeypatch.setenv("OCCAM_TREES_URL", "trees.local:8420")
    cfg = OccamTreesConfig.from_env()
    assert cfg.base_url == "http://trees.local:8420"


def test_from_env_invalid_timeout_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://x:8420")
    monkeypatch.setenv("OCCAM_TREES_TIMEOUT_S", "garbage")
    cfg = OccamTreesConfig.from_env()
    assert cfg.timeout_s == 5.0


# ── ScaffoldNode.from_dict + flatten ──────────────────────────────


def test_scaffold_node_leaf_round_trip() -> None:
    n = ScaffoldNode.from_dict(
        {"name": "foo.py", "role": "manifest", "children": None}
    )
    assert n.name == "foo.py"
    assert n.role == "manifest"
    assert n.is_dir() is False
    assert n.flatten() == [("foo.py", "manifest")]


def test_scaffold_node_nested_flatten() -> None:
    n = ScaffoldNode.from_dict({
        "name": "src",
        "role": "",
        "children": [
            {
                "name": "main.py",
                "role": "entry-point",
                "children": None,
            },
            {
                "name": "lib",
                "role": "",
                "children": [
                    {"name": "util.py", "role": "util", "children": None},
                ],
            },
        ],
    })
    assert n.is_dir()
    paths = n.flatten()
    assert ("src/main.py", "entry-point") in paths
    assert ("src/lib/util.py", "util") in paths


def test_scaffold_node_empty_dir_marker() -> None:
    """An empty directory should surface as a path ending in '/'."""
    n = ScaffoldNode.from_dict({"name": "empty", "role": "", "children": []})
    flat = n.flatten()
    assert len(flat) == 1
    assert flat[0][0].endswith("/")


# ── ResolvedScaffold.from_dict ────────────────────────────────────


def _stub_resolve_response() -> dict:
    return {
        "stack": {
            "id": "mern", "name": "MERN", "rank": 1,
            "components": ["MongoDB", "Express.js", "React", "Node.js"],
            "category": "fullstack-js",
        },
        "archetype": {
            "id": "fullstack-monolith", "level": 4,
            "name": "Full-Stack Monolith",
        },
        "tree": [
            {"name": "package.json", "role": "manifest", "children": None},
            {
                "name": "app",
                "role": "",
                "children": [
                    {"name": "page.jsx", "role": "home-page", "children": None},
                ],
            },
        ],
    }


def test_resolved_scaffold_round_trip() -> None:
    r = ResolvedScaffold.from_dict(_stub_resolve_response())
    assert r.stack_id == "mern"
    assert r.stack_name == "MERN"
    assert r.archetype_level == 4
    assert r.archetype_id == "fullstack-monolith"
    assert len(r.stack_components) == 4
    flat = r.flatten()
    paths = {p for p, _ in flat}
    assert "package.json" in paths
    assert "app/page.jsx" in paths


def test_resolved_scaffold_keeps_raw() -> None:
    """Raw response is preserved for callers that want fields the
    typed wrapper doesn't expose."""
    raw = _stub_resolve_response()
    r = ResolvedScaffold.from_dict(raw)
    assert r.raw == raw


# ── Silent fail-open contract ─────────────────────────────────────


def test_disabled_client_methods_return_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OCCAM_TREES_URL", raising=False)
    c = OccamTreesClient()
    assert c.enabled is False
    assert c.list_stacks() == []
    assert c.list_archetypes() == []
    assert c.list_categories() == []
    assert c.resolve("mern", 4) is None


def test_resolve_invalid_inputs_return_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    assert c.resolve("", 4) is None
    assert c.resolve("mern", 0) is None
    assert c.resolve("mern", -1) is None


def test_list_stacks_swallows_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.side_effect = httpx.ConnectError("nope")
    c._client = fake  # bypass lazy init
    assert c.list_stacks() == []


def test_resolve_swallows_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.side_effect = httpx.HTTPError("boom")
    c._client = fake
    assert c.resolve("mern", 4) is None


def test_resolve_handles_null_stack_archetype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saw this during recon: bad stack name returns 200 with stack=null."""
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.return_value = MagicMock(
        json=lambda: {"stack": None, "archetype": None, "tree": []},
        raise_for_status=lambda: None,
    )
    c._client = fake
    assert c.resolve("garbage", 4) is None


# ── Successful HTTP roundtrip (mocked) ────────────────────────────


def test_resolve_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.return_value = MagicMock(
        json=lambda: _stub_resolve_response(),
        raise_for_status=lambda: None,
    )
    c._client = fake
    r = c.resolve("mern", 4)
    assert r is not None
    assert r.stack_id == "mern"
    assert r.archetype_level == 4
    assert len(r.tree) == 2


def test_list_stacks_with_category_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.return_value = MagicMock(
        json=lambda: [{"id": "mern", "category": "fullstack-js"}],
        raise_for_status=lambda: None,
    )
    c._client = fake
    out = c.list_stacks(category="fullstack-js")
    assert len(out) == 1
    fake.get.assert_called_once_with(
        "/v1/stacks", params={"category": "fullstack-js"},
    )


def test_list_categories_returns_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCCAM_TREES_URL", "http://localhost:8420")
    c = OccamTreesClient()
    fake = MagicMock(spec=httpx.Client)
    fake.get.return_value = MagicMock(
        json=lambda: ["frontend", "fullstack-js", "ai-agents"],
        raise_for_status=lambda: None,
    )
    c._client = fake
    cats = c.list_categories()
    assert cats == ["frontend", "fullstack-js", "ai-agents"]
    assert all(isinstance(x, str) for x in cats)


def test_close_safe_when_not_connected() -> None:
    c = OccamTreesClient(OccamTreesConfig(base_url="", enabled=False))
    c.close()  # No raise
    c.close()  # Idempotent


# ── CLI registration smoke ────────────────────────────────────────


def test_scaffold_command_registered() -> None:
    """Verify `scaffold` is reachable via the global Typer app."""
    import gitoma.cli.commands  # noqa: F401
    from gitoma.cli._app import app
    names: set[str] = set()
    for cmd in app.registered_commands:
        if cmd.name:
            names.add(cmd.name)
        elif cmd.callback is not None:
            names.add(cmd.callback.__name__)
    assert "scaffold" in names
