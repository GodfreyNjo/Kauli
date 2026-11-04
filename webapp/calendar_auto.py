"""Kauli joins your meetings on its own: connect Google Calendar or Outlook with one click, and the notetaker is sent to every upcoming
Zoom, Google Meet or Microsoft Teams call on that calendar, the way Otter and Transkriptor do.

Every few minutes this looks a day and a half ahead on each connected calendar. An event that has a meeting link, is not all-day, not
cancelled and not declined by the customer gets a bot scheduled for its start time, through the same recorder as the Meetings page
(app._send_bot: same plan check, same minutes). Each event is remembered, so it is sent once only: cancelling a meeting on the Meetings
page really cancels it. The customer's switches (connections.PROVIDERS["gcal"]["settings"]): turn auto-join off, only join meetings
they organise, and email the notes to everyone invited.

Access is read-only: events only, never the ability to change the calendar.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import calendar_feed, connections, db, meeting_bot

log = logging.getLogger("calendar_auto")
A = None
LOOKAHEAD_HOURS = 36
SCAN_SECONDS = 300
AUTO_CAP = 20                                                             # bots waiting at once for one account
PROVIDERS = ("gcal", "outlook")


def register(app_module) -> None:
    global A
    A = app_module
    if meeting_bot.configured():
        threading.Thread(target=_loop, daemon=True, name="calendar-auto").start()


# ------------------------------------------------------------------------------------------------ reading a calendar
def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _utc(text: str) -> float:
    """Microsoft sends UTC times without a zone ("2026-11-05T10:00:00.0000000")."""
    return datetime.fromisoformat(text.split(".")[0]).replace(tzinfo=timezone.utc).timestamp()


def _google_events(token: str, now: float, until: float) -> list[dict]:
    r = httpx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events", headers={"Authorization": f"Bearer {token}"}, timeout=20, params={
        "timeMin": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeMax": datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": "50"})
    r.raise_for_status()
    out = []
    for item in r.json().get("items", []):
        start, end = item.get("start") or {}, item.get("end") or {}
        if item.get("status") == "cancelled" or "dateTime" not in start:       # cancelled, or an all-day entry
            continue
        points = [e.get("uri") for e in ((item.get("conferenceData") or {}).get("entryPoints") or []) if e.get("entryPointType") == "video"]
        guests = item.get("attendees") or []
        me = next((a for a in guests if a.get("self")), {})
        out.append({"id": item["id"], "title": item.get("summary") or "", "start": _ts(start["dateTime"]), "end": _ts((end or start)["dateTime"]),
                    "url": calendar_feed.meeting_link(item.get("hangoutLink") or "", *points, item.get("location") or "", item.get("description") or ""),
                    "attendees": [a["email"] for a in guests if a.get("email") and not a.get("self") and not a.get("resource")],
                    "declined": me.get("responseStatus") == "declined", "mine": bool((item.get("organizer") or {}).get("self"))})
    return out


def _graph_events(token: str, now: float, until: float, me: str) -> list[dict]:
    r = httpx.get("https://graph.microsoft.com/v1.0/me/calendarView", headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}, timeout=20, params={
        "startDateTime": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "$top": "50", "$orderby": "start/dateTime",
        "$select": "id,subject,start,end,isAllDay,isCancelled,isOrganizer,onlineMeeting,onlineMeetingUrl,location,body,attendees,responseStatus"})
    r.raise_for_status()
    out = []
    for item in r.json().get("value", []):
        if item.get("isAllDay") or item.get("isCancelled"):
            continue
        guests = [(a.get("emailAddress") or {}).get("address") for a in item.get("attendees") or []]
        out.append({"id": item["id"], "title": item.get("subject") or "", "start": _utc(item["start"]["dateTime"]), "end": _utc(item["end"]["dateTime"]),
                    "url": calendar_feed.meeting_link((item.get("onlineMeeting") or {}).get("joinUrl") or "", item.get("onlineMeetingUrl") or "",
                                                      (item.get("location") or {}).get("displayName") or "", (item.get("body") or {}).get("content") or ""),
                    "attendees": [g for g in guests if g and g.lower() != (me or "").lower()],
                    "declined": (item.get("responseStatus") or {}).get("response") == "declined", "mine": bool(item.get("isOrganizer"))})
    return out


def events_for(row, now: float | None = None) -> list[dict]:
    now = now or time.time()
    token = connections._access_token(row)
    if not token:
        raise RuntimeError("the calendar sign-in has expired")
    until = now + LOOKAHEAD_HOURS * 3600
    if row["provider"] == "gcal":
        return _google_events(token, now, until)
    return _graph_events(token, now, until, row["account_label"] or "")


# ------------------------------------------------------------------------------------------------ sending the notetaker
def sync_client(client_id: str, now: float | None = None) -> int:
    """Schedule the notetaker for this account's coming meetings. Returns how many bots were sent."""
    now = now or time.time()
    row_user = db.get_user(client_id)
    if not row_user or not meeting_bot.configured():
        return 0
    user = dict(row_user)
    user["client_scope_id"] = client_id
    lang = user.get("ui_lang") if user.get("ui_lang") in A.LANGUAGE_NAMES else "en"
    sent = 0
    for provider in PROVIDERS:
        row = connections.get_connection(client_id, provider)
        if not row:
            continue
        settings = connections.settings_of(row)
        if not settings.get("auto"):
            continue
        try:
            events = events_for(row, now)
        except Exception as exc:  # noqa: BLE001 - one calendar being unreachable must not stop the others
            log.warning("calendar read failed for %s: %s", provider, exc)
            continue
        for ev in sorted(events, key=lambda e: e["start"]):
            key = f"{provider}:{ev['id']}"
            if not ev["url"] or ev["declined"] or ev["end"] < now + 60 or ev["start"] > now + LOOKAHEAD_HOURS * 3600:
                continue
            if settings.get("only_mine") and not ev["mine"]:
                continue
            if db.calendar_event_seen(client_id, key):
                continue
            ok, _why, minutes = A._meeting_gate(user)
            if not ok or db.count_active_meetings(client_id) >= AUTO_CAP:
                return sent                                              # not enough minutes (or too many waiting): try again next scan
            made = A._send_bot(ev["url"], meeting_bot.platform_for(ev["url"]), ev["title"], lang, client_id, ev["start"], minutes)
            if "error" in made:
                log.warning("auto-join failed for %s: %s", key, made["error"])
                continue
            db.calendar_event_mark(client_id, key, made["id"], ev["attendees"])
            sent += 1
    return sent


def run_once() -> int:
    total = 0
    for client_id in db.list_calendar_clients():
        try:
            total += sync_client(client_id)
        except Exception:  # noqa: BLE001
            log.warning("calendar sync failed for %s", client_id, exc_info=True)
    return total


def _loop() -> None:
    time.sleep(60)                                                       # let the site finish starting first
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            log.warning("calendar loop error", exc_info=True)
        time.sleep(SCAN_SECONDS)
