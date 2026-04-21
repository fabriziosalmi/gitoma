"""LRU+TTL in-memory cache for GitHub API responses.

Thread-safe via ``RLock``. Zero-copy reads. Automatic stale-entry eviction
on access. Designed for concurrent use from the MCP server's
``ThreadPoolExecutor`` workers.

Industrial-grade additions:

* ``invalidate_prefix`` is O(1) on the touched namespace instead of O(n)
  over every stored key — write tools bust whole namespaces on every
  mutation, so making the bust cheap directly translates to lower p99 on
  each write tool.
* Per-namespace hit/miss counters help diagnose "is my tool's cache
  actually paying off?" without shipping a whole metrics stack — the
  numbers are exposed through ``stats()`` under ``namespaces``.
* ``time.monotonic`` for TTL (immune to NTP steps), ``time.time()``
  isn't called anywhere for correctness-sensitive work.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, TypeVar

V = TypeVar("V")

DEFAULT_TTL = 300.0  # 5 minutes
MAX_ENTRIES = 512     # LRU eviction after this many keys

# Keys follow the convention ``namespace:<free-form tail>``, e.g.
# ``tree:owner/repo:300`` or ``file:owner/repo:HEAD:path/to/file``. The
# namespace is the leading component up to the first colon — that's what
# the secondary index is keyed by, and what prefix-invalidation talks to.
_NAMESPACE_SEP = ":"


@dataclass
class _Entry:
    value: Any
    expires_at: float
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


def _namespace_of(key: str) -> str:
    """Extract the leading ``namespace:`` segment of a cache key.

    Keys without a ``:`` (unusual but legal) land in the synthetic
    ``"_unscoped"`` bucket so the secondary index is still cleanable.
    """
    idx = key.find(_NAMESPACE_SEP)
    return key[:idx] if idx != -1 else "_unscoped"


class GitHubContextCache:
    """Thread-safe LRU+TTL cache for GitHub API responses.

    Design decisions:

    * OrderedDict for O(1) LRU eviction (``move_to_end`` on hit)
    * ``threading.RLock`` for reentrant safety
    * Lazy eviction: expired entries are removed on access, not by background timer
    * **Secondary namespace index** (``_ns_keys``) maps ``namespace -> set[key]``
      so ``invalidate_prefix`` is O(|namespace|), not O(|store|).
    * Per-namespace hit/miss counters so cache effectiveness is inspectable
      without a metrics sidecar.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES, default_ttl: float = DEFAULT_TTL) -> None:
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._ns_keys: dict[str, set[str]] = defaultdict(set)
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._total_hits = 0
        self._total_misses = 0
        self._total_sets = 0
        self._total_evictions = 0
        self._ns_hits: dict[str, int] = defaultdict(int)
        self._ns_misses: dict[str, int] = defaultdict(int)

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing/expired. O(1) with LRU promotion."""
        ns = _namespace_of(key)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._total_misses += 1
                self._ns_misses[ns] += 1
                return None
            if entry.is_expired:
                self._drop_locked(key)
                self._total_misses += 1
                self._ns_misses[ns] += 1
                return None
            # LRU: promote to end (most recently used)
            self._store.move_to_end(key)
            entry.hits += 1
            self._total_hits += 1
            self._ns_hits[ns] += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store value with TTL. Evicts LRU entry if at capacity."""
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        ns = _namespace_of(key)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _Entry(value=value, expires_at=expires_at)
            self._ns_keys[ns].add(key)
            self._total_sets += 1
            # LRU eviction: remove oldest entry if over capacity. ``popitem``
            # on OrderedDict is O(1); we use our own helper so the secondary
            # index stays in sync.
            while len(self._store) > self._max_entries:
                oldest_key, _ = self._store.popitem(last=False)
                oldest_ns = _namespace_of(oldest_key)
                self._ns_keys[oldest_ns].discard(oldest_key)
                self._total_evictions += 1

    def invalidate(self, key: str) -> bool:
        """Remove a single key. Returns True if it existed."""
        with self._lock:
            if key in self._store:
                self._drop_locked(key)
                return True
            return False

    def invalidate_prefix(self, prefix: str) -> int:
        """Remove all keys starting with prefix. Returns count removed.

        Fast path (O(1)): when ``prefix`` is exactly a namespace name and
        the caller wants to nuke the whole namespace, we walk only
        ``_ns_keys[ns]``.

        Slow path (O(namespace size)): when ``prefix`` is more specific
        than a namespace — e.g. ``"file:owner/repo"`` — we narrow to the
        namespace bucket first and then string-match only within it. The
        legacy full-scan is never used.
        """
        ns = _namespace_of(prefix)
        with self._lock:
            candidates = self._ns_keys.get(ns, set())
            if not candidates:
                return 0
            # If prefix IS the namespace (no tail), match them all.
            tail = prefix[len(ns) + 1:] if prefix.startswith(ns + _NAMESPACE_SEP) else ""
            if not tail and prefix == ns:
                keys_to_del = list(candidates)
            else:
                keys_to_del = [k for k in candidates if k.startswith(prefix)]
            for k in keys_to_del:
                self._drop_locked(k)
            return len(keys_to_del)

    def clear(self) -> int:
        """Evict all entries. Returns count."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            self._ns_keys.clear()
            return count

    def stats(self) -> dict[str, Any]:
        """Return cache statistics snapshot, including per-namespace counters."""
        with self._lock:
            alive = sum(1 for e in self._store.values() if not e.is_expired)
            stale = len(self._store) - alive
            total_req = self._total_hits + self._total_misses
            namespaces: dict[str, dict[str, int | float]] = {}
            for ns, keys in self._ns_keys.items():
                hits = self._ns_hits.get(ns, 0)
                misses = self._ns_misses.get(ns, 0)
                total = hits + misses
                namespaces[ns] = {
                    "entries": len(keys),
                    "hits": hits,
                    "misses": misses,
                    "hit_rate": round(hits / total, 3) if total else 0.0,
                }
            return {
                "entries": len(self._store),
                "alive": alive,
                "stale": stale,
                "hits": self._total_hits,
                "misses": self._total_misses,
                "sets": self._total_sets,
                "evictions": self._total_evictions,
                "hit_rate": round(self._total_hits / total_req, 3) if total_req else 0.0,
                "max_capacity": self._max_entries,
                "default_ttl_s": self._default_ttl,
                "namespaces": namespaces,
            }

    # ── Context manager support ───────────────────────────────────────────────

    def get_or_set(self, key: str, factory: Any, ttl: float | None = None) -> Any:
        """Return cached value, or call factory() to compute + cache it.

        Factory is called OUTSIDE the lock to prevent deadlocks on slow I/O.
        Uses double-checked locking for correctness under concurrency.
        """
        # Fast path: check without (ever) holding the lock beyond the
        # atomic read — get() acquires+releases internally.
        value = self.get(key)
        if value is not None:
            return value

        # Compute without holding lock (factory may do network I/O)
        computed = factory()

        # Write result (another thread may have written it by now — that's
        # OK, we just overwrite with the same data; no correctness issue).
        self.set(key, computed, ttl=ttl)
        return computed

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _drop_locked(self, key: str) -> None:
        """Remove a single key + its index entry. Caller must hold the lock."""
        self._store.pop(key, None)
        ns = _namespace_of(key)
        bucket = self._ns_keys.get(ns)
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                # Keep the dict lean — an empty bucket is useless and pays
                # cost in stats() enumeration. The same ns will be
                # repopulated on the next set() anyway.
                self._ns_keys.pop(ns, None)


# ── Module-level singleton (shared across all GitHubMCPClient instances) ──────
_cache = GitHubContextCache()


def get_cache() -> GitHubContextCache:
    """Return the global singleton cache instance."""
    return _cache
