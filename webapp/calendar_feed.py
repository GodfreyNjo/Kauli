"""Connect a Google or Outlook calendar with its private calendar link, see the next meetings that have a video link, and send Kauli to one.

Why a link and not a sign-in: both Google Calendar and Outlook give every calendar a private "secret address" in iCal format. Pasting it
needs no approval process and gives Kauli read-only access to that one calendar's events. The link is a secret (anyone with it can read
the calendar), so it is stored privately, shown only masked, and deleted when the customer disconnects.

Sending Kauli to a meeting uses the same recorder as Meetings (app.client_meeting_create): same plan check, same minutes, same rules.
Nothing joins on its own: the customer presses "Send Kauli" on the meetings they want recorded.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, meeting_bot, rate_limit

router = APIRouter()
A = None
MAX_FEEDS = 3
MAX_ICS_BYTES = 8 * 1024 * 1024
LOOKAHEAD_DAYS = 14
CACHE_SECONDS = 600
# a calendar link may only point at the calendar services themselves
ALLOWED_HOSTS = ("calendar.google.com", ".office365.com", ".outlook.com", ".office.com", ".live.com", ".icloud.com")
_cache: dict = {}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def normalise(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    return url


def host_ok(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith(h) or ("." + host).endswith(h) for h in ALLOWED_HOSTS)


def provider_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("google.com"):
        return "google"
    if host.endswith(("office365.com", "outlook.com", "office.com", "live.com")):
        return "outlook"
    return "other"


def mask(url: str) -> str:
    p = urlparse(url)
    return f"{p.hostname}/...{url[-6:]}"


def _fetch(url: str) -> str:
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(10.0, read=20.0)) as client:
        for _ in range(3):
            r = client.get(url, headers={"Accept": "text/calendar, */*"})
            if r.status_code in (301, 302, 303, 307, 308):
                url = str(httpx.URL(url).join(r.headers.get("location", "")))
                if not host_ok(url):
                    raise ValueError("The calendar link led somewhere that is not a calendar service.")
                continue
            if r.status_code in (401, 403, 404):
                raise ValueError("The calendar service did not accept that link. Copy the private address again.")
            if r.status_code != 200:
                raise ValueError(f"The calendar service answered {r.status_code}.")
            if len(r.content) > MAX_ICS_BYTES:
                raise ValueError("That calendar is too large to read.")
            text = r.text
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("That link is not a calendar (iCal) address.")
            return text
    raise ValueError("Too many redirects.")


_URLS = re.compile(r"https://[^\s<>\"')\]]+")


def meeting_link(*texts: str) -> str | None:
    for text in texts:
        for url in _URLS.findall(text or ""):
            url = url.rstrip(".,;")
            if meeting_bot.platform_for(url):
                return url
    return None


def parse_events(ics_text: str, now: datetime | None = None, days: int = LOOKAHEAD_DAYS) -> list[dict]:
    """The coming meetings in the next `days` days (recurring ones expanded), earliest first, each with a video-meeting link when it has one."""
    import icalendar
    import recurring_ical_events

    now = now or datetime.now(timezone.utc)
    cal = icalendar.Calendar.from_ical(ics_text)
    out = []
    for ev in recurring_ical_events.of(cal).between(now, now + timedelta(days=days)):
        start = ev.get("DTSTART").dt if ev.get("DTSTART") else None
        if start is None or (isinstance(start, date) and not isinstance(start, datetime)):
            continue                                                 # all-day entries are not meetings
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = ev.get("DTEND").dt if ev.get("DTEND") else start + timedelta(hours=1)
        if isinstance(end, datetime) and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(ev.get("SUMMARY", "") or "Meeting").strip()[:120]
        url = meeting_link(str(ev.get("X-GOOGLE-CONFERENCE", "")), str(ev.get("LOCATION", "")), str(ev.get("URL", "")), str(ev.get("DESCRIPTION", "")))
        key = hashlib.sha1(f"{ev.get('UID', '')}|{start.isoformat()}".encode(), usedforsecurity=False).hexdigest()[:16]
        out.append({"key": key, "title": title, "start": start.astimezone(timezone.utc), "end": end.astimezone(timezone.utc) if isinstance(end, datetime) else None,
                    "url": url, "platform": meeting_bot.platform_for(url) if url else None})
    out.sort(key=lambda e: e["start"])
    return out


def events_for(feed, force: bool = False) -> list[dict]:
    hit = _cache.get(feed["id"])
    if hit and not force and hit["at"] > time.time() - CACHE_SECONDS:
        return hit["events"]
    events = parse_events(_fetch(feed["url"]))
    _cache[feed["id"]] = {"at": time.time(), "events": events}
    return events


# ---------------------------------------------------------------------------------------------------- storage
def list_feeds(client_id: str):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM calendar_feeds WHERE client_id = ? ORDER BY created_at", (client_id,)).fetchall()
    conn.close()
    return rows


def get_feed(client_id: str, feed_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM calendar_feeds WHERE id = ? AND client_id = ?", (feed_id, client_id)).fetchone()
    conn.close()
    return row


def _client(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "client" else None


def _back(kind: str, text: str):
    return RedirectResponse(f"/account/calendar?{kind}={quote(text)}", status_code=303)


# ---------------------------------------------------------------------------------------------------- routes
@router.get("/account/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, notice: str | None = None, error: str | None = None, refresh: int = 0):
    user = _client(request)
    if not user:
        return RedirectResponse("/login?next=/account/calendar", status_code=303)
    cid = user["client_scope_id"]
    feeds, problems = [], {}
    for feed in list_feeds(cid):
        try:
            events = events_for(feed, force=bool(refresh))
            problems[feed["id"]] = None
            conn = db.get_conn()
            conn.execute("UPDATE calendar_feeds SET last_ok_at = ?, last_error = NULL WHERE id = ?", (time.time(), feed["id"]))
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001 - one calendar failing must not hide the others
            events, problems[feed["id"]] = [], (str(exc) if isinstance(exc, ValueError) else "Could not read that calendar just now.")
        feeds.append({"row": feed, "events": events, "masked": mask(feed["url"]), "error": problems[feed["id"]]})
    ok, why, minutes = A._meeting_gate(user) if meeting_bot.configured() else (False, "Meetings are not switched on yet.", 0.0)
    return A.templates.TemplateResponse(request, "account_calendar.html", {
        "user": user, "role": "client", "feeds": feeds, "can_send": ok, "why_not": why, "meetings_on": meeting_bot.configured(),
        "can_add": len(feeds) < MAX_FEEDS, "notice": notice, "error": error, "now": datetime.now(timezone.utc)})


@router.post("/account/calendar/connect")
def calendar_connect(request: Request, url: str = Form("")):
    user = _client(request)
    if not user:
        return RedirectResponse("/login?next=/account/calendar", status_code=303)
    cid = user["client_scope_id"]
    url = normalise(url)
    if not host_ok(url):
        return _back("error", "That does not look like a Google Calendar or Outlook calendar link. It should start with https://calendar.google.com or https://outlook.office365.com.")
    allowed, _ = rate_limit.check(f"calfeed:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return _back("error", "Too many tries. Wait a few minutes.")
    if len(list_feeds(cid)) >= MAX_FEEDS:
        return _back("error", f"You can connect up to {MAX_FEEDS} calendars. Disconnect one first.")
    if any(f["url"] == url for f in list_feeds(cid)):
        return _back("notice", "That calendar is already connected.")
    try:
        events = parse_events(_fetch(url))
    except ValueError as exc:
        return _back("error", str(exc))
    except Exception:  # noqa: BLE001
        return _back("error", "Could not read that calendar. Check the link and try again.")
    feed_id = uuid.uuid4().hex[:12]
    conn = db.get_conn()
    conn.execute("INSERT INTO calendar_feeds (id, client_id, provider, url, created_at, last_ok_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (feed_id, cid, provider_of(url), url, time.time(), time.time()))
    conn.commit()
    conn.close()
    _cache[feed_id] = {"at": time.time(), "events": events}
    A.account_hub.track_feature(request, "calendar_connected", provider=provider_of(url))
    return _back("notice", f"Connected. Kauli can see {len(events)} event{'' if len(events) == 1 else 's'} in the next {LOOKAHEAD_DAYS} days.")


@router.post("/account/calendar/{feed_id}/disconnect")
def calendar_disconnect(request: Request, feed_id: str):
    user = _client(request)
    if not user:
        return RedirectResponse("/login?next=/account/calendar", status_code=303)
    conn = db.get_conn()
    conn.execute("DELETE FROM calendar_feeds WHERE id = ? AND client_id = ?", (feed_id, user["client_scope_id"]))
    conn.commit()
    conn.close()
    _cache.pop(feed_id, None)
    return _back("notice", "Disconnected. The private link has been deleted.")


@router.post("/account/calendar/send")
def calendar_send(request: Request, feed_id: str = Form(...), key: str = Form(...), source_lang: str = Form("en")):
    """Send Kauli to one meeting from a connected calendar. Uses the Meetings recorder, so its plan and minutes rules apply."""
    user = _client(request)
    if not user:
        return RedirectResponse("/login?next=/account/calendar", status_code=303)
    feed = get_feed(user["client_scope_id"], feed_id)
    if not feed:
        return _back("error", "That calendar is not connected.")
    try:
        events = events_for(feed)
    except Exception:  # noqa: BLE001
        return _back("error", "Could not read that calendar just now. Try again.")
    ev = next((e for e in events if e["key"] == key), None)
    if not ev or not ev["url"]:
        return _back("error", "That meeting is no longer on the calendar, or it has no Zoom, Meet or Teams link.")
    join_at = ev["start"].strftime("%Y-%m-%dT%H:%M:%S+00:00") if ev["start"] > datetime.now(timezone.utc) else ""
    resp = A.client_meeting_create(request, meeting_url=ev["url"], title=ev["title"], source_lang=source_lang, join_at=join_at)
    loc = resp.headers.get("location", "")
    q = parse_qs(urlparse(loc).query)
    if "error" in q:
        return _back("error", q["error"][0])
    A.account_hub.track_feature(request, "calendar_meeting_sent", platform=ev["platform"] or "")
    return RedirectResponse("/account/meetings?notice=" + quote("Kauli will join " + ev["title"] + ". Tell everyone in it that the meeting is being recorded."), status_code=303)
