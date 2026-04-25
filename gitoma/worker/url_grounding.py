"""G14 — URL/path grounding against fabricated link targets.

The closing piece of the content-grounding trilogy (G11 frameworks,
G12 npm package refs, G13 code-block preservation, G14 link
targets). Catches the failure mode where a worker MODIFY on a doc
file ADDS a Markdown link or raw URL pointing to:

  * an external domain that doesn't exist (b2v PR #24:
    `https://b2v.github.io/...` — the hostname has no DNS record;
    the project never set up GitHub Pages);
  * a relative file path that's not in the repo (b2v PR #27:
    `docs/guide/code/encoder.md`, `docs/guide/code/decoder.md` —
    invented paths that look plausible but don't exist).

Both shapes survived G10/G11/G12/G13 because the link targets are
SYNTACTICALLY valid Markdown — they just point at nothing real.

Two deterministic checks:

  1. **External-URL DNS resolve** — for every `https?://` URL ADDED
     in a MODIFY (in NEW but not in ORIGINAL), resolve the
     hostname. Failure = flag. Path-level 404s are NOT checked
     (full HTTP would be too slow + flaky); we accept the
     trade-off that a real-domain-with-fake-path slips by.

  2. **Relative-path filesystem existence** — for every Markdown
     link `[text](path)` where the target is NOT http/mailto/anchor,
     check that the file exists either relative to the doc OR
     relative to repo root (covers Markdown vs vitepress/gitbook
     conventions).

Architecture mirrors G13:
  * Reads the BEFORE-write content from the ``originals`` dict
    captured by ``read_modify_originals``. Only MODIFY ops get
    checked — diff against original surfaces what's NEW.
  * Carry-over targets (already in the original) are exempt:
    not the worker's invention, not the worker's responsibility.
  * Fail-open per design: DNS lookup timeouts / network errors do
    NOT flag (we don't punish the user for being offline).
  * Opt-out via ``GITOMA_URL_GROUNDING_OFFLINE=true`` for runs
    behind a no-network firewall (test envs, CI sandboxes).

Scope decisions:
  * Doc files only (`.md`/`.mdx`/`.rst`/`.txt`). Code-file URLs
    (e.g. in Rust doc-comments) are out of scope; those should be
    caught by code reviewers.
  * Path-level HTTP checks NOT performed — only DNS. Catches
    invented hostnames cheaply, accepts that fabricated paths on
    real hosts (e.g. ``github.com/<invented>/<repo>``) slip by.
  * ``localhost``/``127.0.0.1``/private RFC1918 ranges always pass
    DNS check (legit dev docs). ``mailto:`` and bare ``#anchor``
    links always skipped.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "validate_url_grounding",
    "DOC_EXTENSIONS",
    "_url_resolves",
    "_extract_external_urls",
    "_extract_link_targets",
    "_is_skippable_link",
    "_resolve_doc_link",
]


DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})


# Raw URLs in prose: `https://example.com/page`. Stops at common
# closing chars (paren, bracket, whitespace, comma, period at end).
_URL_RE = re.compile(r"https?://[^\s)\]<>]+?(?=[.,;:!?\"']*(?:[\s)\]<>]|$))")

# Markdown link: `[text](target)`. Captures the target only.
# Tolerates whitespace around the target.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)\s*\)")

# Hosts that always pass DNS check — legit local-dev URLs.
_LOCAL_HOSTS: frozenset[str] = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
})


def _is_private_ipv4(host: str) -> bool:
    """Cheap RFC1918 check for ``10.x``/``172.16-31.x``/``192.168.x``
    without spinning up ipaddress just for this."""
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def _extract_external_urls(text: str) -> set[str]:
    """All raw https:// URLs in the text. Set semantics — duplicate
    URLs are counted once (the diff between sets reveals genuine
    additions, not multi-occurrences of the same URL)."""
    return set(_URL_RE.findall(text))


def _extract_link_targets(text: str) -> set[str]:
    """All Markdown link targets (the part inside the parens of
    ``[text](target)``). Includes both http URLs AND relative
    paths AND anchors — caller filters."""
    return set(_MD_LINK_RE.findall(text))


def _url_resolves(url: str, timeout: float = 3.0) -> bool:
    """Two-tier check on whether a URL points at a real resource.

    1. **DNS** — fastest. Failure (``gaierror``) → False.
    2. **HEAD with status check** — needed because wildcard-DNS hosts
       (``*.github.io``, ``*.netlify.app``, ``*.vercel.app``,
       ``*.pages.dev``) resolve EVERY subdomain regardless of whether
       a site is published there. b2v PR #24's ``b2v.github.io`` DNS-
       resolves but HEAD returns 404. HTTP 404 from a HEAD →
       confirmed bad URL → False. Anything else (5xx, 405 method-not-
       allowed, timeout, SSL error) → True (fail-open).

    Returns:
      * ``True`` for localhost / private IPs (no checks needed);
      * ``True`` on resolution + non-404 HEAD;
      * ``False`` on definitive "URL does not exist" (DNS fail OR
        HTTP 404);
      * ``True`` for every transient / ambiguous error.
    """
    try:
        host = urlparse(url).hostname
    except Exception:
        return True
    if not host:
        return True
    if host in _LOCAL_HOSTS:
        return True
    if _is_private_ipv4(host):
        return True
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        try:
            socket.gethostbyname(host)
        except socket.gaierror:
            return False
        except Exception:
            return True  # transient — fail-open
        # DNS resolved. HEAD to catch wildcard-host-with-no-content.
        try:
            import urllib.error
            import urllib.request
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "gitoma-g14/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status < 400
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            # 401/403/405/5xx — server reachable, definitive 404 not
            # asserted; fail-open so we don't punish protected URLs.
            return True
        except Exception:
            # SSL, timeout, redirect loop, IPv6 issue — fail-open.
            return True
    finally:
        socket.setdefaulttimeout(old)


def _is_skippable_link(target: str) -> bool:
    """Markdown link targets that aren't filesystem paths and
    shouldn't be existence-checked."""
    if not target:
        return True
    if target.startswith(("http://", "https://", "mailto:", "tel:", "#", "<")):
        return True
    return False


def _resolve_doc_link(
    root: Path, doc_full: Path, target: str
) -> bool:
    """Try both interpretations of a Markdown link target:
    relative-to-the-doc-file (CommonMark default) AND
    relative-to-repo-root (vitepress / docusaurus / gitbook).
    Strips any ``?query`` and ``#fragment``. Returns True if
    either interpretation lands on an existing path.
    """
    raw = target.split("?")[0].split("#")[0]
    if not raw:
        return True
    candidates = [
        (doc_full.parent / raw).resolve(),
        (root / raw.lstrip("/")).resolve(),
    ]
    return any(p.exists() for p in candidates)


def validate_url_grounding(
    root: Path,
    touched: list[str],
    originals: dict[str, str],
) -> tuple[str, str] | None:
    """Validate every touched doc file's NEWLY-ADDED links against
    DNS (external) + filesystem (relative). Returns ``(rel_path,
    message)`` on first violation, ``None`` on clean. Silent pass
    when:

      * file extension not in ``DOC_EXTENSIONS``
      * no original content (CREATE / DELETE / not captured) — the
        guard is preservation-style, only validates what changed
      * file doesn't exist on disk
      * unreadable
      * env var ``GITOMA_URL_GROUNDING_OFFLINE`` is truthy
      * every added URL resolves AND every added path exists

    Carry-over URLs/paths (present in the original) are exempt —
    not the worker's invention, not the worker's bug.
    """
    if (os.environ.get("GITOMA_URL_GROUNDING_OFFLINE") or "").lower() in (
        "1", "true", "yes"
    ):
        return None

    for rel in touched:
        full = root / rel
        if full.suffix.lower() not in DOC_EXTENSIONS:
            continue
        original = originals.get(rel)
        if original is None:
            continue
        if not full.is_file():
            continue
        try:
            new = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # ── External URLs: DNS only ──
        orig_urls = _extract_external_urls(original)
        new_urls = _extract_external_urls(new)
        added_urls = new_urls - orig_urls
        for url in sorted(added_urls):
            if not _url_resolves(url):
                host = urlparse(url).hostname or "?"
                return (
                    rel,
                    f"added URL {url!r} — hostname {host!r} does not resolve "
                    f"OR returns HTTP 404. Likely a fabricated link target. "
                    f"Either remove the link or replace with a real URL."
                )

        # ── Relative paths: filesystem existence ──
        orig_targets = _extract_link_targets(original)
        new_targets = _extract_link_targets(new)
        for target in sorted(new_targets - orig_targets):
            if _is_skippable_link(target):
                continue
            if _resolve_doc_link(root, full, target):
                continue
            return (
                rel,
                f"added Markdown link points to {target!r} which does not "
                f"exist (checked relative to the doc AND relative to repo "
                f"root). Either create the file in the same patch or remove "
                f"the link."
            )

    return None
