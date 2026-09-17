"""Push a real, generated caption file straight onto a client's own
YouTube video - webapp/app.py's /client/youtube/connect|callback|disconnect
and /client/orders/{id}/push-captions-youtube routes.

A deliberately SEPARATE OAuth grant from the sign-in Google login
(webapp/supabase_auth.py's Google flow): that one only ever asks for
`email profile`, identity-only scopes. Uploading a caption to someone
else's video needs the `youtube.force-ssl` scope, which only the real
channel owner can authorize, and which Google classifies as a
"restricted scope" - real production use beyond a handful of test users
needs Kauli's OAuth client to pass Google's own verification review
first (see GOOGLE_YOUTUBE_CLIENT_ID's own comment in app.py for what
that involves). Nothing here works around that; it's a real external
process on Google's side.

Deliberately NOT routed through Supabase's Google provider (unlike
sign-in) - Supabase's OAuth is scoped to identity/session management,
and asking it to also carry an arbitrary extra scope through to this
app in a way we can refresh independently, months after the original
sign-in, is more fragile than just doing a second, purpose-built OAuth
flow directly against Google for this one real need.
"""
from __future__ import annotations

import json
import os
import time

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def youtube_oauth_configured() -> bool:
    return bool(os.environ.get("GOOGLE_YOUTUBE_CLIENT_ID") and os.environ.get("GOOGLE_YOUTUBE_CLIENT_SECRET"))


def build_authorize_url(redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": os.environ["GOOGLE_YOUTUBE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Forces Google to hand back a refresh_token even if this same
        # client already authorized before - without this, a reconnect
        # after the first one silently returns no refresh_token at all,
        # which would leave a stale, unrefreshable connection.
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Returns {"access_token", "refresh_token", "expires_in"}. Raises
    RuntimeError with Google's own error detail on failure - the caller
    (app.py's callback route) turns that into a real, honest message
    rather than a generic 'something went wrong'."""
    r = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": os.environ["GOOGLE_YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_YOUTUBE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Google rejected the connection: {r.text[:300]}")
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Returns {"access_token", "expires_in"} - Google never re-issues a
    new refresh_token on a refresh call, only the caller's original one
    (from exchange_code_for_tokens) keeps working."""
    r = httpx.post(TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": os.environ["GOOGLE_YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_YOUTUBE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Could not refresh the YouTube connection: {r.text[:300]}")
    return r.json()


def get_channel_info(access_token: str) -> tuple[str | None, str | None]:
    """(channel_id, channel_title) for whichever channel this token's
    owner actually has - real values shown back to the client so they can
    confirm it's the right channel, not just "connected"."""
    r = httpx.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code != 200:
        return None, None
    items = r.json().get("items") or []
    if not items:
        return None, None
    return items[0]["id"], items[0]["snippet"]["title"]


def ensure_fresh_token(connection) -> str:
    """The one function callers actually use - returns a real, currently-
    valid access token, refreshing (and persisting the refresh via
    db.update_youtube_access_token) first if the stored one has expired.
    Imports db locally to avoid a real circular import (db.py doesn't
    import this module, but app.py imports both)."""
    from . import db
    if connection["expires_at"] > time.time() + 60:
        return connection["access_token"]
    tokens = refresh_access_token(connection["refresh_token"])
    expires_at = time.time() + tokens.get("expires_in", 3600)
    db.update_youtube_access_token(connection["user_id"], tokens["access_token"], expires_at)
    return tokens["access_token"]


def upload_caption(access_token: str, video_id: str, language: str, srt_path: str, name: str) -> None:
    """Real captions.insert call - uploads the actual SRT file as a new
    caption track. isDraft=false publishes it immediately (a client
    pushing this has already reviewed the human-verified subtitles in
    their own downloaded file first; this isn't an unreviewed AI draft
    going straight onto their public video). Raises RuntimeError with
    YouTube's own error detail on failure - a permission gap (video not
    owned by this channel, quota exceeded, caption already exists in
    this language) all surface as a real, specific message, not a silent
    no-op."""
    metadata = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": name,
            "isDraft": False,
        }
    }
    with open(srt_path, "rb") as f:
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "file": (os.path.basename(srt_path), f, "application/octet-stream"),
        }
        r = httpx.post(CAPTIONS_URL, params={"part": "snippet"}, headers={"Authorization": f"Bearer {access_token}"},
                       files=files, timeout=120)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YouTube rejected the caption upload: {r.text[:400]}")
