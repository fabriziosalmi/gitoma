"""Layer0 vector-memory client — gitoma's per-repo cross-run memory substrate.

Layer0 (`fabriziosalmi/layer0`, `supermemoryai/supermemory`) is an
HNSW + Poincaré-ball / Lorentz-hyperboloid memory engine with
multi-tenant namespaces, retention, soft-delete, metadata filters,
TLS, auth, and an MCP bridge. This module wraps the bits gitoma
actually uses.

Why gitoma needs this
---------------------
Without persistent memory across runs, every `gitoma run` rediscovers
the same metrics, re-proposes the same generic boilerplate tasks
(Ruff/CONTRIBUTING/CHANGELOG/SECURITY), and re-trips the same guards.
The 2026-04-28 generation bench made this cost explicit. Layer0
gives gitoma a per-repo append-only ledger of "what we did, what
worked, what failed", queryable by recency + tags before the planner
is invoked.

Design contract (mirrors `gitoma/context/occam_client.py`)
----------------------------------------------------------
* **Silent fail-open**. If `LAYER0_GRPC_URL` is unset OR the server
  is unreachable, every call returns a benign default (None / [])
  and the gitoma pipeline proceeds unchanged. Running gitoma without
  Layer0 must always work.
* **Short timeouts**. 2s per call. Layer0 is local (or LAN); if
  it's slower than that either it's down or the network broke —
  in both cases the LLM call we'd otherwise block is way more
  expensive than losing a memory write or read.
* **No retries**. Missed write = tiny data loss, not a correctness
  bug. The trace JSONL + diary are still authoritative.

Tools wrapped (3 of layer0's 20)
--------------------------------
* `ingest_one(text, namespace, tags, fields)` → memory id
* `search_memory(query, namespace, k, tag_any_of, ...)` → list of hits
* `list_namespaces()` → list of {name, node_count}

Other layer0 tools (delete, restore, retention, compact, …) are
operator-side concerns reachable via the layer0-probe CLI or the
direct MCP bridge — gitoma doesn't need them on the hot path.

Namespacing convention
----------------------
One namespace per gitoma-tracked repo: `{owner}__{name}`. Same shape
as gitoma's existing log directory layout under
`~/.gitoma/logs/{owner}__{name}/`. Layer0's name regex is
`[a-zA-Z0-9_-]{1,64}`; the namespace builder below applies the
same sanitization.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Layer0Config",
    "Layer0Hit",
    "Layer0Group",
    "Layer0Client",
    "namespace_for_repo",
    "dedupe_hits",
]


# ── Config (env-driven, fail-open if unset) ───────────────────────


@dataclass(frozen=True)
class Layer0Config:
    grpc_url: str           # e.g. "127.0.0.1:50051"
    api_key: str = ""       # x-api-key metadata if Layer0 auth enabled
    timeout_s: float = 2.0  # per-call deadline
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "Layer0Config":
        url = (os.environ.get("LAYER0_GRPC_URL") or "").strip()
        if not url:
            return cls(grpc_url="", enabled=False)
        # Strip http:// or https:// prefix — gRPC takes host:port directly.
        if url.startswith("http://"):
            url = url[len("http://"):]
        elif url.startswith("https://"):
            url = url[len("https://"):]
        api_key = (os.environ.get("LAYER0_API_KEY") or "").strip()
        try:
            timeout_s = float(os.environ.get("LAYER0_TIMEOUT_S") or "2.0")
        except ValueError:
            timeout_s = 2.0
        return cls(grpc_url=url, api_key=api_key, timeout_s=timeout_s, enabled=True)


# ── Result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Layer0Hit:
    """One search hit — id + text + distance (lower = closer in the
    Poincaré-warped manifold) + tags pulled from metadata."""

    id: int
    text: str
    distance: float
    tags: tuple[str, ...] = field(default_factory=tuple)
    created_at_ms: int = 0


@dataclass(frozen=True)
class Layer0Group:
    """One bucket of a `search_grouped` response — top-K hits whose
    metadata tags include ``tag``. Empty ``hits`` means no memory in
    the namespace currently carries this tag (or none matched the
    semantic query within the over-fetch window)."""

    tag: str
    hits: tuple[Layer0Hit, ...] = field(default_factory=tuple)


# ── Namespace builder ─────────────────────────────────────────────


_NS_BAD = re.compile(r"[^a-zA-Z0-9_-]+")


def namespace_for_repo(owner: str, name: str) -> str:
    """Stable namespace id for a repo. Matches Layer0 regex
    `[a-zA-Z0-9_-]{1,64}`; truncates if needed."""
    raw = f"{owner}__{name}"
    safe = _NS_BAD.sub("-", raw).strip("-_")
    return safe[:64] or "default"


# ── Dedup utility (pure function, operates on hit lists) ──────────


def dedupe_hits(
    hits: list[Layer0Hit],
    *,
    prefix_len: int = 80,
) -> list[Layer0Hit]:
    """Collapse hits whose ``text`` shares the same first ``prefix_len``
    characters. The hit with the lower ``distance`` (= better match)
    wins; ties broken by earlier position in the input. Order otherwise
    preserved.

    Why this exists: PHASE 1.5 fans out across N tag buckets and can
    inject several near-identical PR-shipped or guard-fail memories
    that pollute the planner prompt with redundancy. A simple prefix
    fold is enough — the first 80 chars of a memory's summary uniquely
    identify it for every gitoma-ingested memory shape today (plan
    summaries, guard firings, PR outcomes all start with a stable
    discriminator). When no two hits collide, the function is a
    no-op (returns hits in input order)."""
    if not hits or prefix_len <= 0:
        return list(hits)
    seen: dict[str, int] = {}  # prefix → index in `out` of best-so-far
    out: list[Layer0Hit] = []
    for h in hits:
        key = (h.text or "")[:prefix_len]
        existing_idx = seen.get(key)
        if existing_idx is None:
            seen[key] = len(out)
            out.append(h)
            continue
        # Replace the existing best if this hit is closer
        if h.distance < out[existing_idx].distance:
            out[existing_idx] = h
    return out


# ── Client (silent-fail-open) ─────────────────────────────────────


class Layer0Client:
    """Thin wrapper over the Layer0 gRPC stub. Every method swallows
    transport errors, returning a benign default. Construction is
    cheap (no connection until first call); the channel is built
    lazily so importing this module costs nothing."""

    def __init__(self, config: Layer0Config | None = None) -> None:
        self.config = config or Layer0Config.from_env()
        self._channel = None
        self._stub = None
        self._stub_init_failed = False

    @property
    def enabled(self) -> bool:
        return self.config.enabled and not self._stub_init_failed

    def _ensure_stub(self) -> bool:
        """Lazy-init gRPC channel + stub. Returns True iff usable.
        Sets ``_stub_init_failed`` permanently on first failure so
        every subsequent call fails fast instead of re-paying the
        connect cost."""
        if not self.config.enabled:
            return False
        if self._stub is not None:
            return True
        if self._stub_init_failed:
            return False
        try:
            import grpc
            from gitoma.integrations._layer0_proto import (
                supermemory_pb2_grpc as _grpc_pb,
            )
            self._channel = grpc.insecure_channel(self.config.grpc_url)
            self._stub = _grpc_pb.CognitiveEngineStub(self._channel)
            return True
        except Exception:  # noqa: BLE001 — silent fail-open
            self._stub_init_failed = True
            self._stub = None
            return False

    def _metadata(self) -> list[tuple[str, str]]:
        if self.config.api_key:
            return [("x-api-key", self.config.api_key)]
        return []

    # ── ingest_one ────────────────────────────────────────────────

    def ingest_one(
        self,
        *,
        text: str,
        namespace: str,
        tags: list[str] | None = None,
        fields: dict[str, str] | None = None,
        pinned: bool = False,
        ttl_ms: int = 0,
    ) -> bool:
        """Ingest a single memory.

        ``pinned=True`` exempts this memory from ALL retention pruning
        (namespace TTL, size-cap, per-memory expires_at_ms). Use for
        architectural facts that must survive months — e.g. "this repo
        uses pytest 8.x with asyncio mode strict".

        ``ttl_ms > 0`` sets per-memory absolute expiry as
        ``now + ttl_ms``. Ignored when ``pinned=True``. Falls back to
        namespace-wide retention when ``ttl_ms=0``.

        Layer0's IngestMemories returns success-only (server assigns
        id internally; gitoma's append-only usage doesn't need it)."""
        if not self._ensure_stub():
            return False
        if not text or not namespace:
            return False
        try:
            from gitoma.integrations._layer0_proto import supermemory_pb2 as pb
            now_ms = int(time.time() * 1000)
            metadata = pb.MemoryMetadata(
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
                tags=list(tags or []),
                fields=dict(fields or {}),
                pinned=bool(pinned),
                expires_at_ms=(
                    now_ms + int(ttl_ms) if (ttl_ms > 0 and not pinned) else 0
                ),
            )
            # IngestMemories has no server-side id assignment —
            # `MemoryNode.id` is taken as the storage slot, passing
            # 0 every time overwrites the same slot. Layer0 reserves
            # ids in `[0, MANUAL_ID_CAP=1024)` for manual ingests
            # (slots [1024..) belong to IngestDocument's chunk
            # allocator). For gitoma's per-repo write pattern (~10
            # memories per run + retention TTL pruning > 30 d), 1024
            # slots is comfortable: collision probability stays below
            # ~0.5 % across the active window. Hash strategy:
            # SHA-256(ns || ts_ns || text) → u64 → mod 1024.
            digest = hashlib.sha256(
                f"{namespace}\0{time.time_ns()}\0{text}".encode("utf-8")
            ).digest()
            node_id = int.from_bytes(digest[:8], "big") % 1024
            req = pb.IngestRequest(
                nodes=[pb.MemoryNode(id=node_id, content=text, metadata=metadata)],
                namespace=namespace,
            )
            resp = self._stub.IngestMemories(
                req, timeout=self.config.timeout_s, metadata=self._metadata(),
            )
            return bool(getattr(resp, "success", False))
        except Exception:  # noqa: BLE001
            return False

    # ── search_memory ─────────────────────────────────────────────

    def search_memory(
        self,
        *,
        query: str,
        namespace: str,
        k: int = 10,
        tag_any_of: list[str] | None = None,
        tag_all_of: list[str] | None = None,
        created_after_ms: int = 0,
    ) -> list[Layer0Hit]:
        """Top-K text search in ``namespace``. Returns [] on failure
        / disabled / empty namespace.

        Tag filters compose: ``tag_any_of`` is OR (hit must carry at
        least one), ``tag_all_of`` is AND (hit must carry every one).
        Both empty ⇒ no tag filtering."""
        if not self._ensure_stub():
            return []
        if not query or not namespace or k <= 0:
            return []
        try:
            from gitoma.integrations._layer0_proto import supermemory_pb2 as pb
            req_kwargs: dict[str, Any] = dict(
                query=query, k=k, namespace=namespace,
            )
            if tag_any_of or tag_all_of or created_after_ms:
                req_kwargs["filter"] = pb.MetadataFilter(
                    tag_any_of=list(tag_any_of or []),
                    tag_all_of=list(tag_all_of or []),
                    created_after_ms=created_after_ms,
                )
            req = pb.SearchByTextRequest(**req_kwargs)
            resp = self._stub.SearchByText(
                req, timeout=self.config.timeout_s, metadata=self._metadata(),
            )
            out: list[Layer0Hit] = []
            for h in resp.hits:
                meta = getattr(h, "metadata", None)
                tags = tuple(getattr(meta, "tags", ()) or ()) if meta else ()
                created = int(getattr(meta, "created_at_ms", 0) or 0) if meta else 0
                out.append(Layer0Hit(
                    id=int(h.id),
                    text=str(h.content),
                    distance=float(h.distance),
                    tags=tags,
                    created_at_ms=created,
                ))
            return out
        except Exception:  # noqa: BLE001
            return []

    # ── search_grouped (top-K per tag bucket, single round-trip) ──

    def search_grouped(
        self,
        *,
        query: str,
        namespace: str,
        group_tags: list[str],
        k_per_group: int = 3,
    ) -> list[Layer0Group]:
        """Single HNSW walk → results bucketised by tag. Returns one
        ``Layer0Group`` per requested tag in the same order. Empty
        ``hits`` on a group means no memory in the namespace currently
        carries that tag (or none matched the semantic query within
        the over-fetch window). Returns [] on failure / disabled /
        empty inputs.

        Designed for PHASE 1.5 prior-runs context where gitoma wants
        e.g. top-3 from each of [plan-shipped, guard-fail, pr-shipped]
        in ONE call instead of three. Layer0 server does the bucketing
        internally with proper over-fetch (k×groups×4) so we never get
        truncation under selective tag filters."""
        if not self._ensure_stub():
            return []
        if not query or not namespace or not group_tags or k_per_group <= 0:
            return []
        try:
            from gitoma.integrations._layer0_proto import supermemory_pb2 as pb
            req = pb.SearchGroupedByTextRequest(
                query=query,
                k_per_group=k_per_group,
                namespace=namespace,
                group_tags=list(group_tags),
            )
            resp = self._stub.SearchGroupedByText(
                req, timeout=self.config.timeout_s, metadata=self._metadata(),
            )
            out: list[Layer0Group] = []
            for g in resp.groups:
                hits: list[Layer0Hit] = []
                for h in g.hits:
                    meta = getattr(h, "metadata", None)
                    tags = tuple(getattr(meta, "tags", ()) or ()) if meta else ()
                    created = (
                        int(getattr(meta, "created_at_ms", 0) or 0) if meta else 0
                    )
                    hits.append(Layer0Hit(
                        id=int(h.id), text=str(h.content),
                        distance=float(h.distance),
                        tags=tags, created_at_ms=created,
                    ))
                out.append(Layer0Group(tag=str(g.tag), hits=tuple(hits)))
            return out
        except Exception:  # noqa: BLE001
            return []

    # ── get_by_id (point lookup, audit / replay) ──────────────────

    def get_by_id(
        self,
        *,
        id: int,
        namespace: str,
    ) -> Layer0Hit | None:
        """Fetch a single memory by its slot id. Returns ``None`` when
        the id doesn't exist in ``namespace``, when the client is
        disabled, or on transport error.

        Note the hit's ``distance`` is always 0.0 (point lookup, no
        semantic ranking). Tags + created_at_ms come back from the
        server-side metadata."""
        if not self._ensure_stub():
            return None
        if id < 0 or not namespace:
            return None
        try:
            from gitoma.integrations._layer0_proto import supermemory_pb2 as pb
            req = pb.GetMemoryByIdRequest(id=int(id), namespace=namespace)
            resp = self._stub.GetMemoryById(
                req, timeout=self.config.timeout_s, metadata=self._metadata(),
            )
            # Server returns id=0 + empty content when the slot is
            # unused. Treat as "not found".
            if not resp.content:
                return None
            meta = getattr(resp, "metadata", None)
            tags = tuple(getattr(meta, "tags", ()) or ()) if meta else ()
            created = int(getattr(meta, "created_at_ms", 0) or 0) if meta else 0
            return Layer0Hit(
                id=int(resp.id), text=str(resp.content), distance=0.0,
                tags=tags, created_at_ms=created,
            )
        except Exception:  # noqa: BLE001
            return None

    # ── list_namespaces (ops / debug) ─────────────────────────────

    def list_namespaces(self) -> list[dict[str, Any]]:
        """Return [{name, node_count}, …]. Empty list on failure."""
        if not self._ensure_stub():
            return []
        try:
            from gitoma.integrations._layer0_proto import supermemory_pb2 as pb
            resp = self._stub.ListNamespaces(
                pb.ListNamespacesRequest(),
                timeout=self.config.timeout_s,
                metadata=self._metadata(),
            )
            return [
                {"name": ns.name, "node_count": int(ns.node_count)}
                for ns in resp.namespaces
            ]
        except Exception:  # noqa: BLE001
            return []

    # ── teardown ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close the gRPC channel. Safe to call multiple times."""
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001
                pass
            self._channel = None
            self._stub = None
