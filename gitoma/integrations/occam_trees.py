"""Occam-Trees HTTP client — deterministic scaffolding knowledge engine.

Occam-Trees (`fabriziosalmi/occam-trees`) is a planning oracle that
maps `(stack, complexity-level)` pairs to canonical file trees.
100 pre-modelled stacks × 10 complexity archetypes = 1000
deterministic scaffolds, every tree node tagged with its semantic
role (manifest, framework-config, root-layout, …). No LLM in the
loop — pure dataset lookup + composition.

This module wraps the bits gitoma needs to consume it:

* `list_stacks(category=None)` → list of {id, name, rank, components, category}
* `list_archetypes()` → list of {id, level, name, complexity, traits, requires, produces}
* `list_categories()` → list of category strings
* `resolve(stack, level)` → ResolvedScaffold dataclass (the
  canonical tree + metadata)

Why gitoma needs this
---------------------
Yesterday's 5-way `gitoma-bench-generation` bench proved gitoma
cannot generate a project from zero — its LLM planner ignores
README intent, spec files, and failing tests. Occam-Trees fills
the upstream gap: gitoma asks "what should a MERN-stack
full-stack-monolith look like?" and gets a deterministic tree
back. The worker (or the new `gitoma scaffold` deterministic
vertical) then materialises the tree on disk + opens a PR. Same
pattern as `gitoma gitignore`.

Design contract (mirrors `gitoma/integrations/occam_gitignore.py`
+ `gitoma/integrations/layer0.py`)
----------------------------------------------------------------
* **Silent fail-open**. If `OCCAM_TREES_URL` is unset OR the
  service is unreachable, every call returns a benign default
  (`None` / `[]`) and the caller skips the feature.
* **Short timeouts**. 5s per request — Occam-Trees is local +
  pure Python + no network IO downstream; if it's slower than
  that something is wrong.
* **No retries**. A failed lookup degrades to "operator runs
  the gitoma scaffold by hand", not a correctness issue.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "OccamTreesConfig",
    "OccamTreesClient",
    "ScaffoldNode",
    "ResolvedScaffold",
    "OccamTreesUnavailable",
]


class OccamTreesUnavailable(RuntimeError):
    """Raised when the operator explicitly asks for an Occam-Trees
    operation but the service is unreachable. The silent-fail-open
    methods on the client never raise this — only the public CLI
    surface should."""


# ── Config (env-driven) ───────────────────────────────────────────


@dataclass(frozen=True)
class OccamTreesConfig:
    base_url: str
    timeout_s: float = 5.0
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "OccamTreesConfig":
        url = (os.environ.get("OCCAM_TREES_URL") or "").strip()
        if not url:
            return cls(base_url="", enabled=False)
        # Normalize: strip trailing slash, ensure scheme present.
        url = url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            timeout_s = float(os.environ.get("OCCAM_TREES_TIMEOUT_S") or "5.0")
        except ValueError:
            timeout_s = 5.0
        return cls(base_url=url, timeout_s=timeout_s, enabled=True)


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ScaffoldNode:
    """One node in a resolved scaffold tree.

    `children=None` ⇒ leaf file. `children=[]` ⇒ empty directory.
    `role` is the semantic tag from the dataset (e.g. "manifest",
    "framework-config", "root-layout") — use it to ID equivalents
    when diffing against an existing repo with non-canonical paths.
    """

    name: str
    role: str = ""
    children: list["ScaffoldNode"] | None = None

    def is_dir(self) -> bool:
        return self.children is not None

    def flatten(self, prefix: str = "") -> list[tuple[str, str]]:
        """Return a flat list of (path, role) tuples for every leaf
        file and every empty-dir-as-marker. Paths are POSIX-style
        relative to the repo root. Used by the scaffold vertical
        to compute "files to create"."""
        out: list[tuple[str, str]] = []
        path = f"{prefix}{self.name}"
        if not self.is_dir():
            out.append((path, self.role))
            return out
        if not (self.children or []):
            # Empty directory marker — represent as the directory path
            # itself with role "directory" so the caller can choose to
            # `mkdir -p` it.
            out.append((path + "/", self.role or "directory"))
            return out
        for child in self.children or []:
            out.extend(child.flatten(prefix=path + "/"))
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScaffoldNode":
        children_raw = d.get("children")
        return cls(
            name=str(d.get("name", "")),
            role=str(d.get("role", "") or ""),
            children=(
                [cls.from_dict(c) for c in children_raw]
                if isinstance(children_raw, list) else None
            ),
        )


@dataclass(frozen=True)
class ResolvedScaffold:
    """The full response of `/v1/resolve?stack=X&level=N`."""

    stack_id: str
    stack_name: str
    stack_components: tuple[str, ...]
    archetype_id: str
    archetype_level: int
    archetype_name: str
    tree: tuple[ScaffoldNode, ...]
    raw: dict[str, Any] = field(default_factory=dict)

    def flatten(self) -> list[tuple[str, str]]:
        """All (path, role) tuples for every leaf file in the tree."""
        out: list[tuple[str, str]] = []
        for node in self.tree:
            out.extend(node.flatten())
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResolvedScaffold":
        stack = d.get("stack") or {}
        archetype = d.get("archetype") or {}
        tree_raw = d.get("tree") or []
        return cls(
            stack_id=str(stack.get("id", "")),
            stack_name=str(stack.get("name", "")),
            stack_components=tuple(stack.get("components") or ()),
            archetype_id=str(archetype.get("id", "")),
            archetype_level=int(archetype.get("level") or 0),
            archetype_name=str(archetype.get("name", "")),
            tree=tuple(ScaffoldNode.from_dict(n) for n in tree_raw),
            raw=d,
        )


# ── Client (silent-fail-open) ─────────────────────────────────────


class OccamTreesClient:
    """Thin httpx wrapper. Every method swallows transport errors
    and returns a benign default. Construction is cheap; the HTTP
    client is built lazily on first call."""

    def __init__(self, config: OccamTreesConfig | None = None) -> None:
        self.config = config or OccamTreesConfig.from_env()
        self._client: httpx.Client | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _get_client(self) -> httpx.Client | None:
        if not self.config.enabled:
            return None
        if self._client is None:
            try:
                self._client = httpx.Client(
                    base_url=self.config.base_url,
                    timeout=self.config.timeout_s,
                )
            except Exception:  # noqa: BLE001
                return None
        return self._client

    # ── list_stacks ───────────────────────────────────────────────

    def list_stacks(self, category: str | None = None) -> list[dict[str, Any]]:
        client = self._get_client()
        if client is None:
            return []
        try:
            params: dict[str, str] = {}
            if category:
                params["category"] = category
            resp = client.get("/v1/stacks", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    # ── list_archetypes ───────────────────────────────────────────

    def list_archetypes(self) -> list[dict[str, Any]]:
        client = self._get_client()
        if client is None:
            return []
        try:
            resp = client.get("/v1/archetypes")
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    # ── list_categories ───────────────────────────────────────────

    def list_categories(self) -> list[str]:
        client = self._get_client()
        if client is None:
            return []
        try:
            resp = client.get("/v1/categories")
            resp.raise_for_status()
            data = resp.json()
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    # ── resolve (the core call) ───────────────────────────────────

    def resolve(self, stack: str, level: int) -> ResolvedScaffold | None:
        """Resolve a (stack, level) pair to a canonical file tree.
        Returns None on any failure (service unreachable, unknown
        stack/level, malformed response). The CLI command is
        responsible for converting None to a user-facing error
        with `OccamTreesUnavailable`."""
        client = self._get_client()
        if client is None:
            return None
        if not stack or level <= 0:
            return None
        try:
            resp = client.get(
                "/v1/resolve", params={"stack": stack, "level": str(level)},
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return None
            # Defensive: dataset version of the API may return null
            # stack/archetype on bad inputs (saw it during recon).
            if not data.get("stack") or not data.get("archetype"):
                return None
            return ResolvedScaffold.from_dict(data)
        except Exception:  # noqa: BLE001
            return None

    # ── teardown ──────────────────────────────────────────────────

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
