"""Contract + security-surface pins against the live cockpit.

Fills the remaining gaps the other files don't touch, focusing on
silent-break shapes:

  * **Response headers on /**: CSP + nosniff + Referrer-Policy + a
    restrictive ``X-Request-ID`` echo (observability + anti-MITM)
  * **Keyboard shortcuts a/v/f**: parametrised coverage for the
    remaining command tiles (r is pinned in test_hardening.py)
  * **SSE 404**: stream against an unknown job_id returns 404, not
    an empty-stream that hangs the client
  * **Double-cancel 409**: idempotent cancel must return 409 on the
    second call so the UI can render "already cancelling" instead
    of a phantom success
  * **Bearer whitespace injection**: tokens with CR/LF/tab in them
    are rejected — catches the regression where a clipboard paste
    of a multi-line token is trimmed to something that happens to
    match a prefix
  * **Bearer case-insensitive**: ``authorization:`` (lower-case
    header name) must be accepted — HTTP headers are case-insensitive
  * **/jobs entry shape**: every field the cockpit reads must be
    present (or explicitly nullable) in every entry
  * **No third-party CDN refs in the shell HTML**: the cockpit must
    ship entirely from the same origin — a surprise ``cdnjs`` / ``unpkg``
    / ``googleapis`` reference would violate the CSP ``default-src 'self'``
    pin AND expose a supply-chain attack surface we don't audit
"""

from __future__ import annotations

import re

import pytest
import requests
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e_cockpit


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_TINY_REPO = "https://github.com/fabriziosalmi/b2v"


# ── Security headers on / ─────────────────────────────────────────────────


def test_root_response_carries_all_security_headers(cockpit_url: str):
    """Pin the full header contract the cockpit ships on ``/``. Loss of
    any one is a silent regression: nosniff protects against proxy
    mime-sniff attacks, Referrer-Policy stops leaking the cockpit URL
    to followed links, and X-Request-ID is our one thread for
    correlating a failed request back to server-side logs."""
    resp = requests.get(f"{cockpit_url}/", timeout=10)
    assert resp.status_code == 200
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert headers.get("content-security-policy"), "CSP header missing on /"
    assert headers.get("x-content-type-options", "").lower() == "nosniff", (
        "X-Content-Type-Options: nosniff missing on /"
    )
    assert headers.get("referrer-policy", "").lower() == "no-referrer", (
        "Referrer-Policy: no-referrer missing on /"
    )
    assert headers.get("x-request-id"), (
        "X-Request-ID missing — we lose the one thread that correlates "
        "client errors with server logs"
    )
    # The request-id must look like a short hex id; a UUID-length value
    # is ok too. The guard here is "not empty + not a leaked secret".
    rid = headers["x-request-id"]
    assert re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", rid), (
        f"X-Request-ID has a suspicious shape: {rid!r}"
    )


# ── Keyboard shortcuts: a / v / f ────────────────────────────────────────


@pytest.mark.parametrize(
    "key,dialog_id",
    [
        ("a", "analyze-dialog"),
        ("v", "review-dialog"),
        ("f", "fixci-dialog"),
    ],
)
def test_keyboard_shortcut_opens_expected_dialog(
    authed_page: Page, cockpit_url: str, key: str, dialog_id: str
):
    """Pin the whole {a,v,f} → {analyze,review,fix-ci} map from
    dashboard.js. A refactor that drops any one of these silently
    disables keyboard-only dispatch of that command."""
    authed_page.goto(cockpit_url)
    expect(authed_page.locator("#conn-label")).to_have_text(
        re.compile(r"live", re.I),
        timeout=12_000,
    )
    authed_page.evaluate("document.body.focus();")
    authed_page.keyboard.press(key)
    expect(authed_page.locator(f"#{dialog_id}")).to_have_attribute(
        "open", "", timeout=3000
    )


# ── SSE error surface ────────────────────────────────────────────────────


def test_sse_stream_for_unknown_job_is_404(cockpit_url: str, cockpit_token: str):
    """Streaming against a job_id that doesn't exist must 404
    immediately — NOT open an empty stream that the client then hangs
    on waiting for events that'll never come. This is the kind of
    failure that looks like "the cockpit silently froze" when in
    reality the server is fine."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/stream/00000000-dead-beef-0000-000000000000",
        headers={**_auth(cockpit_token), "Accept": "text/event-stream"},
        timeout=10,
        stream=True,
    )
    try:
        assert resp.status_code == 404, (
            f"SSE for unknown job must 404; got {resp.status_code}. "
            "A 200 empty-stream would silently hang the cockpit."
        )
    finally:
        resp.close()


# ── Double-cancel idempotency ────────────────────────────────────────────


def test_double_cancel_returns_409(cockpit_url: str, cockpit_token: str):
    """Cancel twice: second call must 409 (already terminal), not
    another 200 ack or 500. The cockpit distinguishes "just
    cancelled" from "you already cancelled" via this status code to
    render the right toast ("already cancelling" vs "cancel dispatched")."""
    # Spin up a cheap job to cancel.
    dispatch = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert dispatch.status_code == 202
    job_id = dispatch.json()["job_id"]

    try:
        first = requests.post(
            f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert first.status_code in (200, 409), first.text[:200]

        # Give the runtime a beat so the task transition from
        # "cancelling" → terminal can complete.
        import time
        for _ in range(30):
            status = requests.get(
                f"{cockpit_url}/api/v1/status/{job_id}",
                headers=_auth(cockpit_token),
                timeout=10,
            ).json()["status"]
            if status in ("cancelled", "failed", "completed"):
                break
            time.sleep(0.2)

        # Second cancel on a now-terminal job.
        second = requests.post(
            f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
            headers=_auth(cockpit_token),
            timeout=10,
        )
        assert second.status_code == 409, (
            f"Second cancel on a terminal job must 409; got {second.status_code}. "
            f"Status before: {status!r}. body={second.text[:200]!r}"
        )
    finally:
        # Belt-and-braces cleanup (no-op if already terminal).
        try:
            requests.post(
                f"{cockpit_url}/api/v1/jobs/{job_id}/cancel",
                headers=_auth(cockpit_token),
                timeout=5,
            )
        except Exception:
            pass


# ── Bearer token hygiene ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "poisoned_token",
    [
        "gitoma_valid\ttrailing-tab",
        "  gitoma_valid_leading_space",
        "gitoma_valid_trailing_space  ",
    ],
)
def test_bearer_with_whitespace_is_rejected(cockpit_url: str, poisoned_token: str):
    """A clipboard paste of a poorly-formatted token (copy from a
    terminal that picked up an extra tab, a curl example with leading
    indent, etc.) must 401, not silently match via server-side
    trimming. If the server tolerates whitespace, the token-store on
    the cockpit and the server disagree about what "the token" is —
    a latent footgun.

    CR/LF cases are not included because ``requests`` rejects them
    at the client — defence in depth exists at that layer already.
    Tab + leading/trailing spaces DO traverse the client, so they
    are the meaningful cases to pin server-side."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/run",
        headers={"Authorization": f"Bearer {poisoned_token}"},
        json={"repo_url": _TINY_REPO},
        timeout=10,
    )
    assert resp.status_code in (400, 401, 403), (
        f"Bearer with whitespace ({poisoned_token!r}) must be rejected; "
        f"got {resp.status_code}"
    )


def test_lowercase_authorization_header_is_accepted(
    cockpit_url: str, cockpit_token: str
):
    """HTTP header names are case-insensitive per RFC 7230. If the
    server's token extractor hard-codes ``Authorization`` (title-case)
    the request from a curl/HTTP client that sends ``authorization:``
    will silently fail 401 — and the error message won't hint at the
    cause. Pin case-insensitivity."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/health",
        headers={"authorization": f"Bearer {cockpit_token}"},
        timeout=10,
    )
    assert resp.status_code == 200, (
        f"Lower-case ``authorization:`` header must auth successfully; "
        f"got {resp.status_code}"
    )


# ── /jobs entry shape ────────────────────────────────────────────────────


def test_jobs_entries_have_required_fields(cockpit_url: str, cockpit_token: str):
    """Every value in the ``/api/v1/jobs`` dict must carry the fields
    the cockpit reads directly: ``status``, ``label``, ``lines``,
    ``created_at``. Missing any one silently breaks the table render
    (and silently-broken table = operators lose visibility on
    in-flight work)."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/jobs",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 200
    body = resp.json()
    required = {"status", "label", "lines", "created_at"}
    for jid, info in body.items():
        missing = required - info.keys()
        assert not missing, (
            f"/jobs entry {jid!r} is missing fields {missing!r}. "
            "Cockpit's jobs table row reads these directly."
        )
        # ``lines`` must be a non-negative int — a string or negative
        # number would break the "XX lines" pill that links to the log.
        assert isinstance(info["lines"], int) and info["lines"] >= 0, (
            f"/jobs[{jid!r}].lines must be non-negative int; got {info['lines']!r}"
        )


# ── Shell HTML is self-contained ─────────────────────────────────────────


def test_cockpit_shell_has_no_third_party_cdn_refs(cockpit_url: str):
    """The cockpit must ship entirely from the same origin. A surprise
    reference to cdnjs / unpkg / googleapis / jsdelivr etc. would both
    (a) violate our ``default-src 'self'`` CSP so CSP-respecting
    browsers would blank-screen, AND (b) widen the supply-chain attack
    surface we don't audit. The CSP already enforces this at runtime,
    but pinning at HTML level flags the regression at deploy-time."""
    resp = requests.get(f"{cockpit_url}/", timeout=10)
    assert resp.status_code == 200
    body = resp.text
    banned = [
        "cdnjs.cloudflare.com",
        "unpkg.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "jsdelivr.net",
        "ajax.googleapis.com",
        "stackpath.bootstrapcdn.com",
    ]
    hits = [host for host in banned if host in body]
    assert not hits, (
        f"Cockpit shell references third-party CDN hosts {hits!r} — "
        "these must be inlined or self-hosted. "
        "(Detected in the HTML body on /.)"
    )


def test_openapi_doc_exposes_only_expected_tags(cockpit_url: str):
    """The OpenAPI doc is publicly reachable (by design). That means
    whatever ``tags`` we declare become publicly visible. Pin the set
    so a future endpoint labelled with a leaky tag (e.g.
    ``"admin"``, ``"internal"``, ``"debug"``) flags during CI instead
    of in a security audit."""
    resp = requests.get(f"{cockpit_url}/openapi.json", timeout=10)
    assert resp.status_code == 200
    spec = resp.json()
    tags_found: set[str] = set()
    for _path, methods in spec.get("paths", {}).items():
        for _verb, op in methods.items():
            if isinstance(op, dict):
                for t in op.get("tags") or []:
                    tags_found.add(t)
    # Whitelist: the tags we intentionally advertise.
    allowed = {"system", "agent", "jobs", "state"}
    leaked = tags_found - allowed
    assert not leaked, (
        f"OpenAPI exposes unexpected public tag(s) {leaked!r}. "
        f"Whitelist currently: {allowed!r}. "
        "Update both the whitelist and the review if this is intentional."
    )
