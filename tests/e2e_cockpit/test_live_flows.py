"""Live dispatch + route-interception flows.

Three higher-risk tests that still stay safe for the live cockpit:

  * **SSE round-trip** — dispatches a read-only ``analyze`` on
    ``fabriziosalmi/b2v`` (user's existing test repo), streams via
    ``/api/v1/stream/{job_id}``, then cancels the job before it
    finishes heavy work. Reaches the entire job lifecycle
    (spawn → SSE push → cancel → terminal status) without mutating
    any repo state.

  * **Rate-limit payload-first ordering** — 21 requests with a
    malformed payload must ALL come back 422 and never flip to 429.
    This pins that pydantic validation runs BEFORE
    ``_enforce_dispatch_rate_limit``, which is what prevents an
    attacker from burning legitimate users' quota with pre-validation
    garbage.

  * **Connection-down banner** — intercepts ``/api/v1/*`` at the
    browser layer to force 503s and verifies the cockpit's Banner
    surfaces with a Retry action. Simulates the real "I deployed
    while you had the page open" scenario without touching the
    running service.
"""

from __future__ import annotations

import re
import time

import pytest
import requests
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e_cockpit


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── SSE round-trip on a read-only analyze ────────────────────────────────


# Any public repo small enough to clone quickly would work. The user's
# existing test repo is the defensible choice: we already know it's
# reachable, has no branch-protection oddities, and is the one the
# cockpit screenshots reference — so a breakage here is a real breakage.
_TINY_REPO = "https://github.com/fabriziosalmi/b2v"


def test_sse_dispatch_stream_cancel_roundtrip(cockpit_url: str, cockpit_token: str):
    """End-to-end job lifecycle with zero state mutation:

      1. POST ``/api/v1/analyze`` → 202 with ``job_id``
      2. Open SSE at ``/api/v1/stream/{job_id}`` → at least one
         non-empty frame arrives inside a short window (confirms the
         subprocess is spawned and stdout pumping works)
      3. POST ``/api/v1/jobs/{job_id}/cancel`` → 200 with
         ``status=cancelling``
      4. Poll ``/api/v1/status/{job_id}`` until terminal → must be in
         a terminal status ("cancelled" / "failed" / "completed";
         never stuck "running")

    ``analyze`` is read-only — no state.json mutation, no commits,
    no PR. The worst a cancel-race can do is leave a tempdir on the
    server for a few minutes, which the OS reaps.
    """
    # Step 1: dispatch
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert resp.status_code == 202, (
        f"analyze dispatch should be 202; got {resp.status_code}. "
        f"body={resp.text[:300]!r}"
    )
    body = resp.json()
    job_id = body["job_id"]
    assert len(job_id) >= 8, f"job_id unexpectedly short: {job_id!r}"

    # Step 2: SSE stream — consume for a short bounded window
    got_any_data = False
    stream_resp = requests.get(
        f"{cockpit_url}/api/v1/stream/{job_id}",
        headers={**_auth(cockpit_token), "Accept": "text/event-stream"},
        timeout=15,
        stream=True,
    )
    try:
        assert stream_resp.status_code == 200, (
            f"SSE stream should be 200; got {stream_resp.status_code}"
        )
        ct = stream_resp.headers.get("content-type", "")
        assert "event-stream" in ct, f"SSE content-type wrong: {ct!r}"

        # Read a few bytes; any non-empty line from the event-stream is
        # enough to confirm the pipe is alive. 5s is ample: gitoma's
        # banner prints at startup.
        deadline = time.monotonic() + 5.0
        for raw in stream_resp.iter_lines(decode_unicode=True):
            if raw:
                got_any_data = True
                break
            if time.monotonic() > deadline:
                break
    finally:
        stream_resp.close()

    assert got_any_data, (
        f"SSE /stream/{job_id} did not emit any data in 5s — either the "
        "subprocess never spawned or stdout isn't being pumped"
    )

    # Step 3: cancel
    cancel_resp = requests.post(
        f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    # 200 (cancelling) is the happy path; 409 (already terminal) is also
    # acceptable if the SSE read was slow and the job finished quickly.
    assert cancel_resp.status_code in (200, 409), (
        f"cancel should be 200 or 409; got {cancel_resp.status_code}. "
        f"body={cancel_resp.text[:200]!r}"
    )

    # Step 4: poll until terminal
    terminal_statuses = {"cancelled", "completed", "failed"}
    final_status = None
    deadline = time.monotonic() + 30.0  # generous — a real analyze is ~10s
    while time.monotonic() < deadline:
        status_resp = requests.get(
            f"{cockpit_url}/api/v1/status/{job_id}",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert status_resp.status_code == 200, status_resp.text[:200]
        final_status = status_resp.json().get("status")
        if final_status in terminal_statuses:
            break
        time.sleep(0.5)

    assert final_status in terminal_statuses, (
        f"Job {job_id} never reached a terminal status within 30s "
        f"(last: {final_status!r}). Either cancel is broken or the "
        "subprocess is stuck."
    )


# ── Rate-limit: pydantic validation runs before the limiter ───────────────


def test_rate_limit_does_not_fire_on_invalid_payloads(
    cockpit_url: str, cockpit_token: str
):
    """Security-critical ordering: 21 requests with a malformed
    ``repo_url`` must all come back 422, NOT one 422 × 20 then a 429.

    This pins that ``_enforce_dispatch_rate_limit`` runs AFTER pydantic's
    ``field_validator`` — if the order ever flips, a malicious client
    could burn a legitimate operator's 20/min quota with bogus payloads
    and lock them out of dispatching real jobs for a minute.

    21 was chosen because the limit is exactly 20 per 60s; the 21st
    would be the one to flip to 429 if the ordering is wrong."""
    codes: list[int] = []
    for i in range(21):
        resp = requests.post(
            f"{cockpit_url}/api/v1/analyze",
            headers=_auth(cockpit_token),
            json={"repo_url": f"garbage-{i}-not-a-url"},
            timeout=10,
        )
        codes.append(resp.status_code)

    # Every response must be 422 (validation failure) — not a single 429.
    assert all(c == 422 for c in codes), (
        "Invalid-payload requests leaked past pydantic into the rate "
        f"limiter. Codes: {codes}. If any are 429, the rate limiter is "
        "running BEFORE validation — regression."
    )
    # Sanity: the one right after (with a valid payload) must also be
    # 202, proving we never polluted the bucket. Uses a distinct valid
    # URL + immediate cancel to stay zero-impact.
    valid_resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert valid_resp.status_code == 202, (
        f"After 21 malformed POSTs the bucket must still be empty; "
        f"got {valid_resp.status_code}. Body: {valid_resp.text[:200]!r}"
    )
    # Clean up by cancelling the job immediately so we don't leak work.
    job_id = valid_resp.json().get("job_id")
    if job_id:
        requests.post(
            f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
            headers=_auth(cockpit_token),
            timeout=5,
        )


# ── "Server down" banner via route interception ──────────────────────────


def test_connection_banner_surfaces_when_api_returns_503(
    authed_page: Page, cockpit_url: str
):
    """Simulates "the cockpit was running, I redeployed the backend"
    scenario without actually killing the service. Route every
    ``/api/v1/jobs`` call to 503 — the cockpit's job poller will fail,
    the Banner subsystem must surface with a Retry action.

    This is the UX contract: no matter what caused the poll failure,
    the operator gets an actionable banner, not a silent UI freeze."""

    # Start intercepting BEFORE navigation so the very first poll fails.
    def _fail_jobs(route: Route) -> None:
        req = route.request
        if "/api/v1/jobs" in req.url:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail": "simulated: server not configured"}',
            )
        else:
            route.continue_()

    authed_page.route("**/api/v1/**", _fail_jobs)

    authed_page.goto(cockpit_url)

    # The banner may take a poll cycle or two to appear (initial page
    # render races against the first jobs poll). 8s is ample.
    banner = authed_page.locator("#banner")
    # The banner element is present in the DOM always; it toggles via
    # ``hidden`` / ``aria-hidden``. Prefer visibility assertion.
    expect(banner).to_be_visible(timeout=8000)

    # Must contain a Retry action — the point of the banner is that
    # operators have a one-click recovery path.
    banner_text = banner.text_content() or ""
    assert re.search(r"retry", banner_text, re.I), (
        f"Connection banner must expose a Retry action; got: {banner_text!r}"
    )


def test_connection_banner_hides_when_api_recovers(
    authed_page: Page, cockpit_url: str
):
    """Companion to the test above: once the API is reachable again
    (simulated by un-routing after the banner is up), the next poll
    must hide the banner automatically. No user action should be
    required — the cockpit recovers visibly."""

    fail_enabled = {"on": True}

    def _conditional(route: Route) -> None:
        if fail_enabled["on"] and "/api/v1/jobs" in route.request.url:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail": "simulated"}',
            )
        else:
            route.continue_()

    authed_page.route("**/api/v1/**", _conditional)
    authed_page.goto(cockpit_url)
    banner = authed_page.locator("#banner")
    expect(banner).to_be_visible(timeout=8000)

    # Flip the switch: subsequent polls reach the real server.
    fail_enabled["on"] = False
    # Click Retry in the banner to force a fresh poll instead of
    # waiting for the next natural tick. Selector is scoped to the
    # banner to avoid clicking any other button with "Retry" text.
    retry_btn = banner.get_by_role("button", name=re.compile(r"retry", re.I))
    if retry_btn.count():
        retry_btn.first.click()

    # Banner must hide within a couple of poll cycles.
    expect(banner).to_be_hidden(timeout=8000)
    # And the Conn pill must settle on live (confirms the WS wasn't
    # disturbed by the REST route interception — different transport).
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=10_000,
    )
