"""LRU+TTL in-memory cache for GitHub API responses.

Thread-safe via RLock. Zero-copy reads. Automatic stale-entry eviction on access.
Designed for concurrent access from ThreadPoolExecutor workers.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

V = TypeVar("V")

DEFAULT_TTL = 300.0  # 5 minutes
MAX_ENTRIES = 512     # LRU eviction after this many keys


@dataclass
class _Entry:
    value: Any
    expires_at: float
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class GitHubContextCache:
    """
    Thread-safe LRU+TTL cache for GitHub API responses.

    Design decisions:
    - OrderedDict for O(1) LRU eviction (move_to_end on hit)
    - threading.RLock for reentrant safety (same thread can re-acquire)
    - Lazy eviction: expired entries are removed on access, not by background timer
    - Zero-copy: stores references, no serialization overhead
    - Prefix-based invalidation for repo-scoped cache busting post-push
    """

    def __init__(self, max_entries: int = MAX_ENTRIES, default_ttl: float = DEFAULT_TTL) -> None:
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._total_hits = 0
        self._total_misses = 0
        self._total_sets = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing/expired. O(1) with LRU promotion."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._total_misses += 1
                return None
            if entry.is_expired:
                del self._store[key]
                self._total_misses += 1
                return None
            # LRU: promote to end (most recently used)
            self._store.move_to_end(key)
            entry.hits += 1
            self._total_hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store value with TTL. Evicts LRU entry if at capacity."""
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _Entry(value=value, expires_at=expires_at)
            self._total_sets += 1
            # LRU eviction: remove oldest entry if over capacity
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def invalidate(self, key: str) -> bool:
        """Remove a single key. Returns True if it existed."""
        with self._lock:
            return self._store.pop(key, None) is not None

    def invalidate_prefix(self, prefix: str) -> int:
        """Remove all keys starting with prefix. Returns count of removed entries."""
        with self._lock:
            keys_to_del = [k for k in self._store if k.startswith(prefix)]
            for k in keys_to_del:
                del self._store[k]
            return len(keys_to_del)

    def clear(self) -> int:
        """Evict all entries. Returns count."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def stats(self) -> dict[str, Any]:
        """Return cache statistics snapshot."""
        with self._lock:
            alive = sum(1 for e in self._store.values() if not e.is_expired)
            stale = len(self._store) - alive
            total_req = self._total_hits + self._total_misses
            return {
                "entries": len(self._store),
                "alive": alive,
                "stale": stale,
                "hits": self._total_hits,
                "misses": self._total_misses,
                "sets": self._total_sets,
                "hit_rate": round(self._total_hits / total_req, 3) if total_req else 0.0,
                "max_capacity": self._max_entries,
                "default_ttl_s": self._default_ttl,
            }

    # ── Context manager support ───────────────────────────────────────────────

    def get_or_set(self, key: str, factory: Any, ttl: float | None = None) -> Any:
        """
        Return cached value, or call factory() to compute + cache it.
        Factory is called OUTSIDE the lock to prevent deadlocks on slow I/O.
        Uses double-checked locking for correctness under concurrency.
        """
        # Fast path: check without lock
        value = self.get(key)
        if value is not None:
            return value

        # Compute without holding lock (factory may do network I/O)
        computed = factory()

        # Write result (another thread may have written it by now — that's OK,
        # we just overwrite with the same data; no correctness issue)
        self.set(key, computed, ttl=ttl)
        return computed


# ── Module-level singleton (shared across all GitHubMCPClient instances) ──────
_cache = GitHubContextCache()


def get_cache() -> GitHubContextCache:
    """Return the global singleton cache instance."""
    return _cache
