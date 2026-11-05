"""Request-level protections that sit in front of every route.

* Cross-site form posts. The session cookie is SameSite=Lax, which already keeps it off a form posted from another
  site; this checks the browser's own Origin and Sec-Fetch-Site headers as well, so a forged post from another site
  is refused even for the requests that carry no cookie yet (a login forged onto a victim's browser, for example).
* Private pages are never kept by a browser or a proxy (the back button after logging out must not show an account).
* The signature Calendly puts on its webhooks.
* The security.txt file (RFC 9116) that tells a researcher where to report a problem.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Callers that are programs, not a browser carrying our cookie: each proves itself with a signature or a token of its own.
TOKEN_PREFIXES = ("/webhooks/", "/api/v1/", "/mcp")

# Pages for one signed-in person.
PRIVATE_PREFIXES = ("/client", "/staff", "/settings", "/actor", "/login", "/signup", "/forgot-password", "/reset-password",
                    "/billing/", "/receipts/", "/notifications/", "/auth/")

SECURITY_TXT_EXPIRES = "2027-11-05T00:00:00.000Z"


def _hostname(value: str) -> str:
    try:
        return (urlsplit(value if "://" in value else f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


def _with_and_without_www(host: str) -> set[str]:
    if not host:
        return set()
    return {host, host[4:]} if host.startswith("www.") else {host, f"www.{host}"}


def allowed_hosts(request) -> set[str]:
    """The names this site answers to: the one the request came in on and the public address from the settings."""
    hosts = _with_and_without_www(_hostname(request.headers.get("host", "")))
    hosts |= _with_and_without_www(_hostname(os.environ.get("KAULI_PUBLIC_BASE_URL", "")))
    return hosts


def cross_site_post(request) -> bool:
    """True when a browser is posting to us from a page that is not ours. Programs (no Origin, no Sec-Fetch-Site) and
    the token-protected routes are left alone."""
    if request.method not in UNSAFE_METHODS or request.url.path.startswith(TOKEN_PREFIXES):
        return False
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site == "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin:
        return _hostname(origin) not in allowed_hosts(request)
    return site == "cross-site"


def is_private_path(path: str) -> bool:
    return path.startswith(PRIVATE_PREFIXES)


def keep_private(path: str, response) -> None:
    """No browser or proxy cache for a signed-in page (HTML and JSON only: downloads and media keep their own rules)."""
    kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if is_private_path(path) and kind in ("text/html", "application/json"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")


def calendly_signature_ok(body: bytes, header: str, key: str, tolerance: int = 300, now: float | None = None) -> bool:
    """Calendly sends `Calendly-Webhook-Signature: t=<unix time>,v1=<hex>`, the HMAC-SHA256 of `<t>.<body>` under the signing
    key given when the webhook was created. Nothing is accepted without a configured key, or when the time is far off."""
    if not key or not header:
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    stamp, sent = parts.get("t", ""), parts.get("v1", "")
    if not stamp.isdigit() or not sent:
        return False
    if abs((now if now is not None else time.time()) - int(stamp)) > tolerance:
        return False
    expected = hmac.new(key.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def security_txt(base: str, contact: str) -> str:
    return (f"Contact: mailto:{contact}\n"
            f"Expires: {SECURITY_TXT_EXPIRES}\n"
            "Preferred-Languages: en\n"
            f"Canonical: {base}/.well-known/security.txt\n")
