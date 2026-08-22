"""Cross-post a Kauli blog post to DEV.to (dev.to, the Forem platform),
with canonical_url pointing back to the real Kauli URL - same backlink
mechanism as medium_publish.py, on a platform that's actually reachable
for free right now (Medium's integration-token self-service was found to
be unavailable when we went looking for one; DEV.to's API is real,
free, and currently active - verified against Forem's own API docs
before this was written, not assumed).

API key: a real human generates one from their own DEV.to account -
Settings -> Extensions -> DEV API Keys - never something this app can
create on someone's behalf. Free account, no approval process, no
waitlist.

One real caveat, stated honestly: DEV.to's API takes body_markdown, and
this sends the post's raw body_html through as-is rather than converting
it. Forem's renderer (like most Markdown processors) passes plain HTML
blocks through untouched, so simple content (<p>, <h2>, <ul>/<li>) should
render fine - but this hasn't been checked against a real posted article
yet. Worth a visual check on DEV.to the first time a real post goes out,
same as the Calendly webhook's payload-shape caveat.
"""
from __future__ import annotations

import os

import httpx

DEVTO_API_BASE = "https://dev.to/api"


def devto_configured() -> bool:
    return bool(os.environ.get("DEV_TO_API_KEY"))


def publish_post(title: str, body_html: str, canonical_url: str, description: str | None = None,
                  tags: list[str] | None = None) -> dict:
    """Returns {"ok": True, "url": "..."} on success, {"ok": False,
    "error": "..."} on failure - never raises for an ordinary API
    failure (missing key, DEV.to rejecting the request, network error)."""
    api_key = os.environ.get("DEV_TO_API_KEY")
    if not api_key:
        return {"ok": False, "error": "DEV_TO_API_KEY isn't set - nothing to publish with."}
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{DEVTO_API_BASE}/articles",
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json={
                    "article": {
                        "title": title,
                        "body_markdown": body_html,
                        "published": True,
                        "canonical_url": canonical_url,
                        "description": (description or "")[:280] or None,
                        "tags": (tags or ["translation", "localization"])[:4],  # DEV.to's own documented cap
                    }
                },
            )
            resp.raise_for_status()
            return {"ok": True, "url": resp.json()["url"]}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        return {"ok": False, "error": f"DEV.to rejected the request ({exc.response.status_code}): {detail}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Couldn't reach DEV.to: {exc}"}
    except (KeyError, ValueError) as exc:
        return {"ok": False, "error": f"Unexpected response shape from DEV.to: {exc}"}
