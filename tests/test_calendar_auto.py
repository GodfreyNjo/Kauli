"""Kauli joins the meetings on a connected calendar by itself, and writes the notes afterwards. The calendar and the meeting service are faked."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_meetings import Resp, meet, _send  # noqa: E402,F401
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import calendar_auto, connections, meeting_bot, meeting_notes  # noqa: E402


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(env, provider="gcal", settings=None):
    conn = env.db.get_conn()
    conn.execute("DELETE FROM integration_connections WHERE client_id = 'u1' AND provider = ?", (provider,))
    conn.execute("INSERT INTO integration_connections (client_id, provider, token_enc, refresh_enc, expires_at, account_label, target_enc, settings_json, connected_at) "
                 "VALUES ('u1', ?, ?, NULL, ?, 'me@example.com', NULL, ?, ?)", (provider, connections._enc("tok"), time.time() + 3000, json.dumps(settings or {}), time.time()))
    conn.commit()
    conn.close()


def _google(monkeypatch, items):
    monkeypatch.setattr(calendar_auto.httpx, "get", lambda url, **k: Resp(200, {"items": items}) if "calendar/v3" in url else Resp(200, {}))
    monkeypatch.setattr(Resp, "raise_for_status", lambda self: None, raising=False)


def _event(eid, hours=2, **extra):
    start = time.time() + hours * 3600
    base = {"id": eid, "summary": f"Meeting {eid}", "start": {"dateTime": _iso(start)}, "end": {"dateTime": _iso(start + 1800)},
            "hangoutLink": "https://meet.google.com/abc-defg-hij", "organizer": {"self": True},
            "attendees": [{"email": "me@example.com", "self": True}, {"email": "bob@acme.com"}]}
    base.update(extra)
    return base


def test_every_upcoming_call_with_a_link_gets_the_notetaker_once(env, meet, monkeypatch):
    _connect(env)
    _google(monkeypatch, [
        _event("a"),
        _event("nolink", hangoutLink=""),                                                         # no video link: nothing to join
        _event("declined", attendees=[{"email": "me@example.com", "self": True, "responseStatus": "declined"}]),
        _event("allday", start={"date": "2026-11-06"}, end={"date": "2026-11-07"}),
        _event("gone", status="cancelled"),
        _event("zoom", hangoutLink="", location="Join: https://zoom.us/j/123456789"),
        _event("far", hours=90),                                                                  # beyond the day-and-a-half window
    ])
    assert calendar_auto.sync_client("u1") == 2
    mine = env.db.list_meetings_for_client("u1")
    assert sorted(m["title"] for m in mine) == ["Meeting a", "Meeting zoom"] and {m["status"] for m in mine} == {"scheduled"}
    assert all(m["join_at"] and m["join_at"] > time.time() for m in mine)                       # scheduled for the start time
    assert calendar_auto.sync_client("u1") == 0                                                  # the same events are never sent again
    env.db.update_meeting(next(m for m in mine if m["title"] == "Meeting a")["id"], status="cancelled")
    assert calendar_auto.sync_client("u1") == 0                                                  # and a meeting the customer cancelled stays cancelled


def test_the_customers_switches_decide_which_meetings_are_joined(env, meet, monkeypatch):
    _google(monkeypatch, [_event("mine"), _event("theirs", organizer={"email": "boss@acme.com"})])
    _connect(env, settings={"auto": False})
    assert calendar_auto.sync_client("u1") == 0                                                  # auto-join switched off
    _connect(env, settings={"auto": True, "only_mine": True})
    assert calendar_auto.sync_client("u1") == 1 and [m["title"] for m in env.db.list_meetings_for_client("u1")] == ["Meeting mine"]


def test_nothing_is_sent_without_a_paid_plan_or_minutes_and_it_is_tried_again_later(env, meet, monkeypatch):
    _connect(env)
    _google(monkeypatch, [_event("a")])
    env.db.set_subscription_plan("u1", "free", 0)
    assert calendar_auto.sync_client("u1") == 0 and not env.db.list_meetings_for_client("u1") and not env.db.calendar_event_seen("u1", "gcal:a")
    env.db.set_subscription_plan("u1", "pro", 30)
    assert calendar_auto.sync_client("u1") == 1                                                  # they upgraded: the next scan picks it up


def test_outlook_events_are_read_the_same_way(env, meet, monkeypatch):
    _connect(env, "outlook")
    start = time.time() + 7200
    item = {"id": "o1", "subject": "Board call", "start": {"dateTime": _iso(start)[:-1] + ".0000000"}, "end": {"dateTime": _iso(start + 1800)[:-1] + ".0000000"},
            "isAllDay": False, "isCancelled": False, "isOrganizer": True, "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/xyz"},
            "attendees": [{"emailAddress": {"address": "me@example.com"}}, {"emailAddress": {"address": "ceo@acme.com"}}], "responseStatus": {"response": "organizer"}}
    skip = {**item, "id": "o2", "isCancelled": True}
    monkeypatch.setattr(calendar_auto.httpx, "get", lambda url, **k: Resp(200, {"value": [item, skip]}))
    monkeypatch.setattr(Resp, "raise_for_status", lambda self: None, raising=False)
    assert calendar_auto.sync_client("u1") == 1
    m = env.db.list_meetings_for_client("u1")[0]
    assert m["platform"] == "teams" and m["title"] == "Board call"
    assert json.loads(env.db.calendar_event_for_meeting(m["id"])["attendees"]) == ["ceo@acme.com"]   # not the customer themselves


def test_the_notetaker_tells_the_room_it_is_recording_and_still_joins_if_the_service_refuses_that(meet):
    meeting_bot.create_bot("https://zoom.us/j/1")
    assert "recording" in meet.state.posts[0]["chat"]["on_bot_join"]["message"] and meet.state.posts[0]["chat"]["on_bot_join"]["send_to"] == "everyone"
    calls = []

    def picky(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return Resp(400, {"detail": "unknown field chat"}) if "chat" in json else Resp(201, {"id": "bot-2"})

    meeting_bot.httpx.post = picky
    assert meeting_bot.create_bot("https://zoom.us/j/2") == {"id": "bot-2"} and "chat" in calls[0] and "chat" not in calls[1]


def _meeting_order(env, wav_bytes, tell):
    _connect(env, settings={"tell_attendees": tell})
    order = env.order_from(env.place(wav_bytes))
    env.db.create_meeting("m1", "u1", "https://zoom.us/j/1", "zoom", "Weekly sync", "en", "bot-1", None, 60)
    env.db.update_meeting("m1", order_id=order["id"], status="done")
    env.db.calendar_event_mark("u1", "gcal:e1", "m1", ["bob@acme.com", "amy@acme.com"])
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: None)
    env.monkeypatch.setattr(env.app, "_transcript_text_for_order", lambda job, level: "We agreed to ship on Friday.")
    return order


def test_a_finished_meeting_gets_notes_and_they_are_emailed_only_when_the_customer_asked(env, wav_bytes, monkeypatch):
    summaries, mails = [], []
    monkeypatch.setattr(meeting_notes.order_ai_assist, "generate_templated_summary",
                        lambda text, tpl: summaries.append(tpl["id"]) or {"ok": True, "summary": "**Decisions**\nShip on Friday"})
    monkeypatch.setattr(meeting_notes.mailer, "send_email", lambda to, subject, html, text=None, **k: mails.append((to, subject, text, k)) or (True, "id"))
    order = _meeting_order(env, wav_bytes, tell=False)
    got = meeting_notes.prepare(order)
    assert summaries == ["general-meeting"] and "Ship on Friday" in got["ai_summary_text"] and mails == []          # notes made, nobody else emailed
    env.db.get_conn().execute  # (the saved summary is also in the order's summaries)
    assert any(r["template_id"] == "general-meeting" for r in env.db.list_order_summaries(order["id"]))
    _connect(env, settings={"tell_attendees": True})
    meeting_notes.prepare(env.db.get_order(order["id"]))
    assert sorted(m[0] for m in mails) == ["amy@acme.com", "bob@acme.com"] and mails[0][1] == "Notes from Weekly sync"
    assert "Ship on Friday" in mails[0][2] and mails[0][2].rstrip().endswith("The Kauli Team") and mails[0][3]["sender_name"] == "Kauli Team"
    assert len(summaries) == 1                                                                                       # the summary is made once


def test_an_order_that_is_not_from_a_meeting_is_left_alone(env, wav_bytes, monkeypatch):
    called = []
    monkeypatch.setattr(meeting_notes.order_ai_assist, "generate_templated_summary", lambda *a: called.append(1) or {"ok": True, "summary": "x"})
    monkeypatch.setattr(meeting_notes.time, "sleep", lambda s: None)
    order = env.order_from(env.place(wav_bytes))
    assert meeting_notes.prepare(order)["id"] == order["id"] and called == []


def test_the_calendar_cards_and_the_meetings_page_offer_one_click(env, monkeypatch):
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "ONEDRIVE_CLIENT_ID", "ONEDRIVE_CLIENT_SECRET",
                "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET", "RECALL_API_KEY"):
        monkeypatch.delenv(key, raising=False)                                                  # a page that depends on what is set up starts from nothing set up
    monkeypatch.setenv("RECALL_API_KEY", "k")
    page = env.client.get("/account/integrations").text
    assert 'data-connect="gcal"' not in page and 'href="/account/calendar"' in page                 # no keys yet: the private-link way still works
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    assert 'data-connect="gcal"' in env.client.get("/account/integrations").text
    assert "Connect my calendar" in env.client.get("/account/meetings").text
    _connect(env)
    page = env.client.get("/account/integrations").text
    assert 'data-settings="gcal"' in page and 'data-disconnect="gcal"' in page
    assert "Kauli Notetaker is on your calendar (Google Calendar (me@example.com))" in env.client.get("/account/meetings").text
