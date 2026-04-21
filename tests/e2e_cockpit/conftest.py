"""Browser-driven e2e tests against a live Gitoma cockpit.

These tests drive the cockpit with chromium (via ``pytest-playwright``)
and exercise paths that an in-process ``TestClient`` cannot reach: the
real CSP enforcement, sessionStorage-based bearer injection, the
WS-subprotocol auth handshake, and the live Pipeline strip updates.

They are **opt-in**: excluded from the default ``pytest`` run because
they hit a real runtime over the network. Missing env vars ⇒ every
test is skipped (not errored) so ``pytest tests/`` still passes
cleanly on a fresh checkout.

Run:

    export GITOMA_COCKPIT_URL=http://100.98.112.23:8000
    export GITOMA_COCKPIT_TOKEN=gitoma_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
    pytest tests/e2e_cockpit -m e2e_cockpit -v

The default ``pytest -q`` continues to skip these thanks to the
``addopts = -m 'not e2e_cockpit'`` pin in pyproject.toml.

Design notes
────────────
* ``cockpit_url`` / ``cockpit_token`` are module-scoped env reads so a
  missing value skips loudly with one message instead of repeating it
  per-test.
* ``authed_page`` uses ``add_init_script`` to seed sessionStorage
  **before** the SPA's bootstrap runs — the app reads the token on
  module load and any later injection races with the first WS attempt.
* We consume ``playwright-pytest``'s built-in ``page`` fixture rather
  than spawning our own browser. Its default viewport + lifecycle are
  adequate for cockpit coverage and keep the suite hermetic-ish.
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import pytest

pytestmark = pytest.mark.e2e_cockpit


# ── Browser launch: bypass system proxy ───────────────────────────────────
# On macOS dev boxes, chromium inherits the system SOCKS/HTTP proxy. Many
# devs here run Proxymate/mitmproxy which MITMs localhost and tailnet
# traffic — the cockpit's ``ws://`` upgrade then dies inside the proxy's
# tunnel handler with "Establishing a tunnel via proxy server failed".
# REST still works (HTTP CONNECT-ignores), but the WS test goes red on a
# machine that otherwise looks fine. ``--no-proxy-server`` proved
# insufficient (chromium still honours the system PAC for WS CONNECT);
# the reliable fix is the explicit ``--proxy-server=direct://``, which
# short-circuits the proxy resolver entirely. Playwright's ``proxy=None``
# means "use system", not "no proxy" — so we cannot rely on it either.


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    existing_args = list(browser_type_launch_args.get("args") or [])
    return {
        **browser_type_launch_args,
        "args": [*existing_args, "--proxy-server=direct://"],
    }


# ── Environment resolution ────────────────────────────────────────────────


def _env_or_skip(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        pytest.skip(
            f"{name} is unset. Export it to a live cockpit URL/token "
            "(see tests/e2e_cockpit/conftest.py module docstring).",
            allow_module_level=False,
        )
    return val


@pytest.fixture(scope="session")
def cockpit_url() -> str:
    """Base URL of the cockpit under test (no trailing slash)."""
    return _env_or_skip("GITOMA_COCKPIT_URL").rstrip("/")


@pytest.fixture(scope="session")
def cockpit_token() -> str:
    """Bearer token matching the cockpit's ``GITOMA_API_TOKEN``."""
    return _env_or_skip("GITOMA_COCKPIT_TOKEN")


# ── Browser fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def authed_page(page, cockpit_url: str, cockpit_token: str) -> Iterator:
    """A ``page`` with the bearer token pre-seeded into sessionStorage.

    Using ``add_init_script`` ensures the token is present before the
    dashboard JS bootstraps — otherwise we'd race the first WS connect
    and the initial SPA state (no token → Settings dialog).
    """
    # JSON-encode to bulletproof against unusual characters in real tokens.
    token_literal = json.dumps(cockpit_token)
    script = (
        "try { sessionStorage.setItem('gitoma.api_token.v2', "
        + token_literal
        + "); } catch (e) {}"
    )
    page.add_init_script(script)
    yield page


@pytest.fixture
def fresh_page(page) -> Iterator:
    """A ``page`` with sessionStorage explicitly empty — no token.

    Used by tests that verify the no-token UX (Settings dialog auto-open,
    WS rejection, etc.)."""
    page.add_init_script(
        "try { sessionStorage.removeItem('gitoma.api_token.v2'); } catch (e) {}"
    )
    yield page
