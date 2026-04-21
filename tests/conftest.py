"""Test-wide fixtures for gitoma.

Only thing lives here for now: a **token-cache reset** that runs before
every test. ``gitoma.api.server`` caches the Bearer token after the first
successful ``verify_token`` so production deployments don't pay the
disk-I/O tax of re-reading ``~/.gitoma/config.toml`` on every request.

Tests that monkey-patch ``load_config`` need that cache cleared or they
end up validating a stale token from a previous test run — a source of
extremely confusing cross-test pollution. A single autouse fixture is
cheap and bulletproof.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_api_token_cache() -> None:
    """Invalidate the ``gitoma.api.server`` Bearer-token cache.

    Runs before every test (autouse). The reset is idempotent and very
    cheap, so it's fine to pay unconditionally instead of only for tests
    that mock ``load_config``.

    Also clears the per-token dispatch rate-limiter buckets — without this,
    a test that fires many dispatches (e.g. a parametrized validator
    sweep) bleeds into the next test and trips 429s for unrelated cases.
    """
    from gitoma.api.server import _reset_token_cache
    from gitoma.api.routers import _reset_dispatch_rate_limiter

    _reset_token_cache()
    _reset_dispatch_rate_limiter()
