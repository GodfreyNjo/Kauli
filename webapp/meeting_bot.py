"""Kauli in your meetings: a bot joins a Zoom, Google Meet or Microsoft Teams call, records it, and the recording becomes
an ordinary Instant AI order (transcript, preview player, downloads) when the meeting ends.

The bot itself is provided by Recall.ai. This module is the only place that knows its API, so if their request or response
shape differs from what is written here, this file is the one to adjust. Inert until RECALL_API_KEY is set: nothing in the
site offers meetings, and every route refuses, until then.

    RECALL_API_KEY          a Recall.ai API token
    RECALL_API_BASE         their regional base URL (default https://us-east-1.recall.ai)
    RECALL_WEBHOOK_SECRET   the signing secret of the webhook that points at /webhooks/recall (whsec_...)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import urlparse

import httpx

BOT_NAME = "Kauli Notetaker"
JOIN_MESSAGE = ("Hi, I'm Kauli Notetaker. I'm recording and transcribing this meeting so notes and action items can be shared afterwards. "
                "If anyone would rather not be recorded, please tell the host, who can remove me from the meeting at any time.")
MIN_MEETING_MINUTES = 30.0          # minutes a client must have left (plan allowance plus credits) before a bot is sent
MAX_ACTIVE_MEETINGS = 3
MAX_SCHEDULE_DAYS = 30
MAX_RECORDING_BYTES = 2 * 1024 * 1024 * 1024
ACTIVE = ("scheduled", "joining", "recording", "processing")

_PLATFORMS = (
    ("zoom", ("zoom.us",)),
    ("meet", ("meet.google.com",)),
    ("teams", ("teams.microsoft.com", "teams.live.com")),
)
PLATFORM_NAMES = {"zoom": "Zoom", "meet": "Google Meet", "teams": "Microsoft Teams"}


def configured() -> bool:
    return bool(os.environ.get("RECALL_API_KEY", "").strip())


def _base() -> str:
    return (os.environ.get("RECALL_API_BASE") or "https://us-east-1.recall.ai").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Token {os.environ.get('RECALL_API_KEY', '').strip()}", "Content-Type": "application/json", "Accept": "application/json"}


def platform_for(url: str) -> str | None:
    """'zoom', 'meet' or 'teams' for a real meeting link of one of those services over https, otherwise None."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    for name, suffixes in _PLATFORMS:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    return None


# ------------------------------------------------------------------ the provider
def create_bot(meeting_url: str, join_at_iso: str | None = None) -> dict:
    """{"id": ...} on success, {"error": ...} on any failure."""
    body = {"meeting_url": meeting_url, "bot_name": BOT_NAME}
    if join_at_iso:
        body["join_at"] = join_at_iso
    try:
        # Kauli tells the room it is there and recording, the way Otter and Fireflies do: nobody is recorded without being told.
        # If the meeting service does not accept that field the bot is sent without it rather than not at all.
        r = httpx.post(f"{_base()}/api/v1/bot/", headers=_headers(), json={**body, "chat": {"on_bot_join": {"send_to": "everyone", "message": JOIN_MESSAGE}}}, timeout=30)
        if r.status_code >= 400:
            r = httpx.post(f"{_base()}/api/v1/bot/", headers=_headers(), json=body, timeout=30)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not reach the meeting service: {exc}"}
    if r.status_code >= 300 or not data.get("id"):
        return {"error": str(data.get("detail") or data.get("message") or "The meeting service refused the request.")[:300]}
    return {"id": data["id"]}


def get_bot(bot_id: str) -> dict:
    try:
        r = httpx.get(f"{_base()}/api/v1/bot/{bot_id}/", headers=_headers(), timeout=30)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not reach the meeting service: {exc}"}
    if r.status_code >= 300:
        return {"error": str(data.get("detail") or "The meeting service refused the request.")[:300]}
    return data


def leave_bot(bot_id: str) -> bool:
    try:
        r = httpx.post(f"{_base()}/api/v1/bot/{bot_id}/leave_call/", headers=_headers(), timeout=30)
        return r.status_code < 300
    except Exception:  # noqa: BLE001
        return False


_CODE_TO_STATUS = {
    "ready": "scheduled", "joining_call": "joining", "in_waiting_room": "joining", "in_call_not_recording": "joining",
    "recording_permission_allowed": "joining", "in_call_recording": "recording", "call_ended": "processing",
    "done": "done", "analysis_done": "done", "recording_permission_denied": "failed", "fatal": "failed",
}


def status_from_bot(bot: dict) -> tuple[str, str | None]:
    """(our status, a plain-words problem or None) from the provider's bot object."""
    changes = bot.get("status_changes") or []
    last = changes[-1] if changes else {}
    code = str(last.get("code") or (bot.get("status") or {}).get("code") or "")
    status = _CODE_TO_STATUS.get(code, "joining" if code else "scheduled")
    problem = None
    if status == "failed":
        sub = str(last.get("sub_code") or "")
        problem = {"recording_permission_denied": "The host did not allow Kauli to record.",
                   "timeout_exceeded_waiting_room": "Kauli waited in the waiting room and was never admitted.",
                   "bot_kicked_from_call": "Kauli was removed from the meeting.",
                   "meeting_not_found": "The meeting link did not lead to a meeting."}.get(sub) or "Kauli could not join or record this meeting."
    return status, problem


def recording_url(bot: dict) -> str | None:
    """A download link for the finished recording, from whichever of the provider's known shapes carries one."""
    if bot.get("video_url"):
        return str(bot["video_url"])
    for rec in bot.get("recordings") or []:
        shortcuts = (rec or {}).get("media_shortcuts") or {}
        for kind in ("video_mixed", "audio_mixed"):
            link = ((shortcuts.get(kind) or {}).get("data") or {}).get("download_url")
            if link:
                return str(link)
    return None


def download(url: str, dest_path: str) -> int:
    """Stream the recording to disk, refusing anything over the upload limit. Returns the size in bytes."""
    total = 0
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in r.iter_bytes(1024 * 256):
                total += len(chunk)
                if total > MAX_RECORDING_BYTES:
                    raise ValueError("The recording is larger than the upload limit.")
                fh.write(chunk)
    return total


# ------------------------------------------------------------------ webhooks (Svix signatures)
def verify_webhook(headers, body: bytes, tolerance_s: int = 300) -> bool:
    secret = os.environ.get("RECALL_WEBHOOK_SECRET", "").strip()
    if not secret:
        return False
    msg_id = headers.get("svix-id") or headers.get("webhook-id") or ""
    timestamp = headers.get("svix-timestamp") or headers.get("webhook-timestamp") or ""
    signatures = headers.get("svix-signature") or headers.get("webhook-signature") or ""
    if not (msg_id and timestamp and signatures):
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance_s:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, IndexError):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[1]) for part in signatures.split() if "," in part)
