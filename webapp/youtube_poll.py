"""Read-only YouTube channel/playlist polling - the "detect new uploads for
you" half of automation, not the "process and bill automatically" half.

Deliberately NOT OAuth, NOT a PubSubHubbub/WebSub webhook, and NOT the
captions.insert round-trip postback. All three of those need things this
project doesn't have yet: a Google Cloud OAuth app + consent screen, Google's
app-verification review for restricted YouTube write scopes (can take weeks
and requires demonstrating real usage), and a stable public HTTPS domain for
a webhook callback URL. A single YOUTUBE_API_KEY (no OAuth, no review, just
enabling the API in Google Cloud Console) is enough for the read-only calls
here: resolving a channel's uploads playlist and listing its public videos.

New videos land in youtube_pending_imports (see webapp/db.py) for a human to
turn into a real order - never auto-created as a billable order. That's the
actual safeguard against the "someone uploads a 3-hour raw file and it costs
a fortune before anyone notices" risk, not a content filter.
"""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
import json

API_BASE = "https://www.googleapis.com/youtube/v3"


def youtube_api_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY") or None


def youtube_polling_configured() -> bool:
    return bool(youtube_api_key())


def extract_channel_or_playlist_id(url_or_id: str) -> tuple[str, str] | None:
    """Best-effort parse of what a client actually pastes - a channel URL
    (/channel/UC..., /@handle), a playlist URL (?list=PL...), or a bare ID.
    Returns (kind, id) where kind is 'channel' or 'playlist', or None if it
    doesn't look like any recognizable YouTube reference. Handle-based URLs
    (/@name) need one extra lookup (channels.list?forHandle=) to resolve to
    a real channel id - handled in resolve_uploads_playlist, not here."""
    text = url_or_id.strip()
    list_match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", text)
    if list_match:
        return ("playlist", list_match.group(1))
    channel_match = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", text)
    if channel_match:
        return ("channel", channel_match.group(1))
    handle_match = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", text)
    if handle_match:
        return ("handle", handle_match.group(1))
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", text):
        return ("channel", text)
    if re.fullmatch(r"PL[A-Za-z0-9_-]{16,}", text):
        return ("playlist", text)
    if text.startswith("@"):
        return ("handle", text[1:])
    return None


def _api_get(path: str, params: dict) -> dict:
    api_key = youtube_api_key()
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY not set - youtube_polling_configured() should be checked first.")
    params = {**params, "key": api_key}
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_uploads_playlist(kind: str, value: str) -> str:
    """Every channel has exactly one "uploads" playlist - listing that
    (playlistItems.list, 1 quota unit) is far cheaper than search.list
    (100 units) for "what has this channel published"."""
    if kind == "playlist":
        return value
    if kind == "handle":
        data = _api_get("channels", {"part": "contentDetails", "forHandle": value})
    else:
        data = _api_get("channels", {"part": "contentDetails", "id": value})
    items = data.get("items") or []
    if not items:
        raise ValueError("No YouTube channel found for that link.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_recent_public_videos(playlist_id: str, max_results: int = 10) -> list[dict]:
    """Returns the most recent PUBLIC videos only - private/unlisted ones
    are skipped here (not surfaced as pending imports at all), matching the
    doc's own safeguard against acting on a client's still-private drafts."""
    data = _api_get("playlistItems", {
        "part": "snippet,status", "playlistId": playlist_id, "maxResults": max_results,
    })
    out = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        status = item.get("status", {})
        if status.get("privacyStatus") != "public":
            continue
        video_id = (snippet.get("resourceId") or {}).get("videoId")
        if not video_id:
            continue
        out.append({
            "video_id": video_id,
            "title": snippet.get("title", "(untitled)"),
            "published_at": snippet.get("publishedAt"),
        })
    return out
