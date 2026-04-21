"""Authenticated REST coverage against the live cockpit.

These tests exercise the ``/api/v1/*`` surface without triggering any
mutating dispatch (no real ``run``, no real ``analyze``, no real PR
operations). Every request here is read-only or targets a deliberately
invalid payload/path so the live state on the Mac Mini is never
touched. If we ever need a mutating round-trip it gets its own file
with a very loud marker.

Why this is a separate file from ``test_smoke.py``:
  * smoke = every cockpit deploy must pass (shell, CSP, WS handshake)
  * this file = API contract — "shape of the REST surface as seen from
    the outside", which can evolve without breaking the UI shell.
  * splitting lets a future CI run them in parallel, and makes grep-
    driven debugging less noisy.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.e2e_cockpit


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Health endpoint ───────────────────────────────────────────────────────


def test_health_requires_bearer(cockpit_url: str):
    """``/api/v1/health`` must sit behind the same bearer gate as
    everything else. A regression that flips it public would be a
    pretext for an unauthenticated attacker to fingerprint the LM
    Studio status + available model list (leaking which models the
    operator has licensed locally)."""
    resp = requests.get(f"{cockpit_url}/api/v1/health", timeout=10)
    assert resp.status_code == 401, (
        f"/api/v1/health should be 401 without bearer; got {resp.status_code}"
    )


def test_health_returns_model_info_with_bearer(cockpit_url: str, cockpit_token: str):
    """Authenticated health response must carry ``status`` + ``lm_studio``
    fields. This is what the cockpit's bootstrap poll consumes to decide
    whether to flag 'LM Studio down' in the UI; a silent schema break
    would leave the banner stuck or absent."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/health",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body.get("status") == "ok", f"health.status != ok: {body!r}"
    assert "lm_studio" in body, "health payload must expose lm_studio block"
    assert "level" in body["lm_studio"], "lm_studio.level missing"


# ── Jobs endpoint ─────────────────────────────────────────────────────────


def test_jobs_list_requires_bearer(cockpit_url: str):
    resp = requests.get(f"{cockpit_url}/api/v1/jobs", timeout=10)
    assert resp.status_code == 401


def test_jobs_list_returns_dict_with_bearer(cockpit_url: str, cockpit_token: str):
    """``/api/v1/jobs`` must return a JSON object keyed by job_id. The
    cockpit iterates over this shape directly; a regression to list-form
    would silently break the 'Jobs total' pill in the header."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/jobs",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict), (
        f"/api/v1/jobs must return a dict (keyed by job_id); got {type(body).__name__}"
    )
    # If any jobs exist, each value must carry the contract fields the
    # cockpit displays. Empty-dict is fine.
    for jid, info in body.items():
        assert isinstance(jid, str) and len(jid) >= 8
        for required in ("status", "label", "lines", "created_at"):
            assert required in info, (
                f"jobs[{jid!r}] missing '{required}' — cockpit expects this "
                f"shape. Got: {info!r}"
            )


# ── Status / cancel: 404 surface ──────────────────────────────────────────


def test_status_unknown_job_is_404(cockpit_url: str, cockpit_token: str):
    """The cockpit may poll a stale job_id after a restart (jobs live
    in memory only). That must surface as a clean 404 so the cockpit
    can evict the row instead of spinning on a phantom."""
    resp = requests.get(
        f"{cockpit_url}/api/v1/status/00000000-dead-beef-0000-000000000000",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


def test_cancel_unknown_job_is_404(cockpit_url: str, cockpit_token: str):
    """Cancel must 404 on a job_id that doesn't exist — not 409
    (which means "terminal") and not 500. Distinguishing the two is
    what lets the cockpit's Cancel button stay idempotent."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/jobs/00000000-dead-beef-0000-000000000000/cancel",
        headers=_auth(cockpit_token),
        timeout=10,
    )
    assert resp.status_code == 404, f"cancel unknown → expected 404, got {resp.status_code}"


# ── Payload validation ────────────────────────────────────────────────────


def test_invalid_repo_url_returns_422(cockpit_url: str, cockpit_token: str):
    """``repo_url`` is regex-validated by pydantic. A malformed URL must
    422 BEFORE the dispatch rate limiter even runs — this is the shield
    that prevents a malicious caller from burning the quota with
    pre-validation-failure requests."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/analyze",
        headers=_auth(cockpit_token),
        json={"repo_url": "not-a-real-url-ok"},
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"malformed repo_url must 422; got {resp.status_code}. body={resp.text[:200]!r}"
    )


def test_run_resume_and_reset_are_mutually_exclusive(cockpit_url: str, cockpit_token: str):
    """A request with BOTH ``resume=true`` and ``reset=true`` must 422.
    Accepting both would be ambiguous — does the run reset and then
    resume from nothing? The field-validator rejects it so the cockpit
    cannot dispatch a nonsense combination even if the operator chains
    the buttons in a weird order."""
    resp = requests.post(
        f"{cockpit_url}/api/v1/run",
        headers=_auth(cockpit_token),
        json={
            "repo_url": "https://github.com/octocat/Hello-World",
            "resume": True,
            "reset": True,
        },
        timeout=10,
    )
    assert resp.status_code == 422, (
        f"resume+reset must 422 (mutually exclusive); got {resp.status_code}"
    )
    # The error body must mention the conflict so operators can debug.
    assert "mutually exclusive" in resp.text.lower()


# ── OpenAPI discoverability ──────────────────────────────────────────────


def test_openapi_json_accessible(cockpit_url: str):
    """``/openapi.json`` must be public — it's how /docs renders and how
    external integrations introspect the API. Pinning the route here
    flags the regression where a future refactor accidentally moves it
    under the bearer-gated ``/api/v1/*`` prefix (where it obviously
    can't be consumed by an unauthenticated /docs page)."""
    resp = requests.get(f"{cockpit_url}/openapi.json", timeout=10)
    assert resp.status_code == 200, (
        f"/openapi.json must be public (200); got {resp.status_code}"
    )
    body = resp.json()
    assert body.get("openapi", "").startswith("3."), (
        f"openapi version unexpected: {body.get('openapi')!r}"
    )
    # A handful of core paths must exist — catches the regression where
    # the router is silently dropped from app.include_router().
    for required_path in ("/api/v1/health", "/api/v1/run", "/api/v1/jobs"):
        assert required_path in body.get("paths", {}), (
            f"/openapi.json missing path {required_path!r}"
        )


def test_docs_page_accessible(cockpit_url: str):
    """The Swagger UI at ``/docs`` must be publicly reachable so
    operators can explore the API. Requires the static Swagger JS
    bundle to load correctly, which we only assert on status here
    (content is FastAPI-provided)."""
    resp = requests.get(f"{cockpit_url}/docs", timeout=10)
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()
