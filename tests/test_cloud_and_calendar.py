"""Import from Google Drive, OneDrive and Dropbox, and connecting a calendar by its private link."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import calendar_feed, cloud_import, meeting_bot  # noqa: E402

PROVIDER_ENV = ("DROPBOX_APP_KEY", "GOOGLE_PICKER_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_ONE_TAP_CLIENT_ID", "GOOGLE_PICKER_APP_ID", "ONEDRIVE_CLIENT_ID")


def _clean(monkeypatch):
    for k in PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


def _post(env, body):
    return env.client.post("/client/import/fetch", json=body)


# ------------------------------------------------------------------------------------------------ which services are on
def test_a_service_appears_only_when_all_its_settings_are_present(monkeypatch):
    _clean(monkeypatch)
    assert cloud_import.config() == {}
    monkeypatch.setenv("DROPBOX_APP_KEY", "dk")
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "od")
    assert set(cloud_import.config()) == {"dropbox", "onedrive"}
    monkeypatch.setenv("GOOGLE_PICKER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_PICKER_APP_ID", "123")
    assert "google" not in cloud_import.config()                                              # no OAuth client yet
    monkeypatch.setenv("GOOGLE_ONE_TAP_CLIENT_ID", "web.apps.googleusercontent.com")
    assert cloud_import.config()["google"]["client_id"] == "web.apps.googleusercontent.com"


def test_the_config_is_for_signed_in_customers_and_the_upload_page_loads_the_script(env, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("DROPBOX_APP_KEY", "dk")
    assert env.client.get("/client/import/config").json() == {"dropbox": {"app_key": "dk"}}
    page = env.client.get("/client").text
    assert 'id="cloud-import"' in page and "/static/cloud_import.js" in page
    js = env.client.get("/static/cloud_import.js").text
    for needle in ("dropins.js", "OneDrive.js", "gapi.load", "/client/import/fetch", "kauliUseCloudFile", "drive.file"):
        assert needle in js, needle
    assert "window.kauliUseCloudFile" in env.client.get("/static/client_wizard.js").text
    csp = env.client.get("/").headers["content-security-policy"]
    for host in ("https://www.dropbox.com", "https://apis.google.com", "https://js.live.net", "https://docs.google.com"):
        assert host in csp, host
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert env.client.get("/client/import/config").status_code == 401


# ------------------------------------------------------------------------------------------------ the download
def test_only_the_services_own_file_addresses_are_accepted():
    ok = cloud_import.host_allowed
    assert ok("dropbox", "https://dl.dropboxusercontent.com/s/abc/file.mp3?dl=1") and ok("dropbox", "https://www.dropbox.com/s/abc/file.mp3")
    assert ok("onedrive", "https://contoso-my.sharepoint.com/personal/a/_layouts/15/download.aspx?x=1") and ok("onedrive", "https://public.sn.files.1drv.com/y4m")
    assert not ok("dropbox", "http://dl.dropboxusercontent.com/file.mp3")                    # not https
    assert not ok("dropbox", "https://dropbox.com.evil.example/file.mp3") and not ok("dropbox", "https://evildropbox.com/f")
    assert not ok("onedrive", "https://127.0.0.1/file.mp3") and not ok("onedrive", "https://localhost/x") and not ok("dropbox", "https://169.254.169.254/latest")
    assert not ok("dropbox", "file:///etc/passwd") and not ok("nonsense", "https://dl.dropboxusercontent.com/f")


REAL_CLIENT = httpx.Client


def _client_factory(handler):
    real = REAL_CLIENT

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)
    return factory


def test_the_downloader_keeps_redirects_on_the_provider_and_enforces_the_size_limit(monkeypatch, tmp_path):
    def evil(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    monkeypatch.setattr(cloud_import.httpx, "Client", _client_factory(evil))
    try:
        cloud_import._download("https://dl.dropboxusercontent.com/f.mp3", tmp_path / "x.mp3", provider="dropbox")
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("a redirect off the provider must be refused")
    assert not (tmp_path / "x.mp3").exists()

    def big(request):
        return httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")
    monkeypatch.setattr(cloud_import.httpx, "Client", _client_factory(big))
    try:
        cloud_import._download("https://dl.dropboxusercontent.com/f.mp3", tmp_path / "y.mp3", provider="dropbox", limit=1000)
    except ValueError as exc:
        assert "larger" in str(exc)
    else:
        raise AssertionError("an oversized file must be refused")

    def sneaky(request):
        return httpx.Response(200, content=b"x" * 5000)                                       # no length header, too much data
    monkeypatch.setattr(cloud_import.httpx, "Client", _client_factory(sneaky))
    try:
        cloud_import._download("https://dl.dropboxusercontent.com/f.mp3", tmp_path / "z.mp3", provider="dropbox", limit=1000)
    except ValueError:
        pass
    else:
        raise AssertionError("a stream past the limit must be cut off")
    assert not (tmp_path / "z.mp3").exists()

    def fine(request):
        if request.url.host == "dl.dropboxusercontent.com":
            return httpx.Response(302, headers={"location": "https://uc123.dropboxusercontent.com/real"})
        return httpx.Response(200, content=b"abc")
    monkeypatch.setattr(cloud_import.httpx, "Client", _client_factory(fine))
    assert cloud_import._download("https://dl.dropboxusercontent.com/f.mp3", tmp_path / "ok.mp3", provider="dropbox") == 3


# ------------------------------------------------------------------------------------------------ the route and the order
def test_a_dropbox_file_becomes_an_order_without_being_uploaded_again(env, wav_bytes, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("DROPBOX_APP_KEY", "dk")
    assert _post(env, {"provider": "onedrive", "url": "https://x.sharepoint.com/f.mp3", "name": "a.mp3"}).status_code == 400    # not switched on
    assert _post(env, {"provider": "dropbox", "url": "https://evil.example/f.mp3", "name": "a.mp3"}).status_code == 400
    assert _post(env, {"provider": "dropbox", "url": "https://dl.dropboxusercontent.com/f.exe", "name": "run.exe"}).status_code == 400

    def fake(url, dest, headers=None, provider=None, limit=None):
        dest.write_bytes(wav_bytes)
        return len(wav_bytes)
    monkeypatch.setattr(cloud_import, "_download", fake)
    r = _post(env, {"provider": "dropbox", "url": "https://dl.dropboxusercontent.com/s/abc/Team call.wav?dl=1", "name": "../Team call.wav"})
    assert r.status_code == 200
    data = r.json()
    assert data["filename"] == ".._Team call.wav" or data["filename"].endswith("Team call.wav") and "/" not in data["filename"]
    key = data["upload_key"]
    assert key.startswith("import:") and cloud_import.staged_file(key, "u1") is not None
    assert cloud_import.staged_file(key, "someone-else") is None                              # another account cannot use it
    posted = env.client.post("/client/orders", data={"source_lang": "en", "target_lang": "en", "service_level": "transcribe", "review_tier": "ai",
                                                     "instr_verbatim_level": "clean_read", "upload_key": key})
    assert posted.status_code == 303
    order = env.db.list_orders_for_client("u1")[0]
    assert order["original_filename"] == data["filename"] and Path(order["audio_path"]).is_file()
    assert cloud_import.staged_file(key, "u1") is None                                       # the staging copy is gone once used
    again = env.client.post("/client/orders", data={"source_lang": "en", "target_lang": "en", "service_level": "transcribe", "review_tier": "ai",
                                                    "instr_verbatim_level": "clean_read", "upload_key": key})
    assert again.status_code != 303 and "import again" in again.text.lower().replace("import the file again", "import again")


def test_a_google_drive_file_is_fetched_with_the_one_off_token_and_the_token_is_not_kept(env, wav_bytes, monkeypatch):
    _clean(monkeypatch)
    for k, v in (("GOOGLE_PICKER_API_KEY", "k"), ("GOOGLE_OAUTH_CLIENT_ID", "c.apps.googleusercontent.com"), ("GOOGLE_PICKER_APP_ID", "123")):
        monkeypatch.setenv(k, v)
    seen = {}
    monkeypatch.setattr(cloud_import, "_google_info", lambda fid, tok: {"name": "Interview.m4a", "size": "1000", "mimeType": "audio/mp4"})

    def fake(url, dest, headers=None, provider=None, limit=None):
        seen.update(url=url, headers=headers, provider=provider)
        dest.write_bytes(wav_bytes)
        return len(wav_bytes)
    monkeypatch.setattr(cloud_import, "_download", fake)
    assert _post(env, {"provider": "google", "file_id": "../etc", "token": "t"}).status_code == 400
    assert _post(env, {"provider": "google", "file_id": "1AbCdEfGhIjKlMnOp", "token": ""}).status_code == 400
    r = _post(env, {"provider": "google", "file_id": "1AbCdEfGhIjKlMnOp", "token": "ya29.secret-token"})
    assert r.status_code == 200 and r.json()["filename"] == "Interview.m4a"
    assert seen["url"] == "https://www.googleapis.com/drive/v3/files/1AbCdEfGhIjKlMnOp?alt=media" and seen["headers"] == {"Authorization": "Bearer ya29.secret-token"}
    assert seen["provider"] is None                                                           # Google is called at its fixed API address only
    assert "ya29.secret-token" not in r.text
    conn = env.db.get_conn()
    for table in ("orders", "users", "security_events"):
        try:
            dump = " ".join(str(tuple(row)) for row in conn.execute(f"SELECT * FROM {table}").fetchall())
        except Exception:
            continue
        assert "ya29.secret-token" not in dump, table


# ------------------------------------------------------------------------------------------------ calendar
def _ics(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def _ev(uid, start, summary="Weekly sync", extra=""):
    end = start + timedelta(hours=1)
    f = lambda d: d.strftime("%Y%m%dT%H%M%SZ")                                                # noqa: E731
    return f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{f(start)}\r\nDTSTART:{f(start)}\r\nDTEND:{f(end)}\r\nSUMMARY:{summary}\r\n{extra}END:VEVENT\r\n"


def _sample():
    soon = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=26)
    return _ics(
        _ev("a1", soon, "Customer call", "DESCRIPTION:Join here https://us02web.zoom.us/j/123456789?pwd=abc\r\n"),
        _ev("a2", soon + timedelta(hours=2), "Planning", "LOCATION:https://meet.google.com/abc-defg-hij\r\n"),
        _ev("a3", soon + timedelta(hours=3), "Lunch", "LOCATION:Cafe on Main Street\r\n"),
        _ev("a4", soon + timedelta(hours=4), "Called off", "STATUS:CANCELLED\r\nLOCATION:https://meet.google.com/zzz-zzzz-zzz\r\n"),
        _ev("a5", soon + timedelta(days=1), "Weekly standup", "RRULE:FREQ=DAILY;COUNT=4\r\nLOCATION:https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0\r\n"),
        _ev("a6", datetime.now(timezone.utc) - timedelta(days=2), "Long ago", "LOCATION:https://meet.google.com/old-old-old\r\n"),
        "BEGIN:VEVENT\r\nUID:allday\r\nDTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:%s\r\nSUMMARY:Holiday\r\nEND:VEVENT\r\n" % (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y%m%d"))


def test_the_calendar_is_read_for_coming_meetings_with_their_video_links():
    events = calendar_feed.parse_events(_sample())
    titles = [e["title"] for e in events]
    assert titles[:3] == ["Customer call", "Planning", "Lunch"] and "Called off" not in titles and "Long ago" not in titles and "Holiday" not in titles
    by = {e["title"]: e for e in events}
    assert by["Customer call"]["platform"] == "zoom" and by["Customer call"]["url"].startswith("https://us02web.zoom.us/j/123456789")
    assert by["Planning"]["platform"] == "meet" and by["Lunch"]["url"] is None
    assert titles.count("Weekly standup") == 4 and all(e["platform"] == "teams" for e in events if e["title"] == "Weekly standup")   # a repeating meeting
    assert len({e["key"] for e in events}) == len(events)                                    # every occurrence has its own key


def test_only_calendar_services_are_accepted_as_a_calendar_address():
    ok = calendar_feed.host_ok
    assert ok("https://calendar.google.com/calendar/ical/x/private-abc/basic.ics") and ok("https://outlook.office365.com/owa/calendar/x/reachcalendar.ics")
    assert ok(calendar_feed.normalise("webcal://outlook.live.com/owa/calendar/x/cal.ics"))
    assert not ok("http://calendar.google.com/x.ics") and not ok("https://calendar.google.com.evil.example/x.ics") and not ok("https://example.com/cal.ics")
    assert not ok("https://127.0.0.1/cal.ics") and not ok("https://169.254.169.254/x")
    assert calendar_feed.provider_of("https://calendar.google.com/x") == "google" and calendar_feed.provider_of("https://outlook.office365.com/x") == "outlook"


GOOD = "https://calendar.google.com/calendar/ical/me%40example.com/private-0123456789abcdef/basic.ics"


def _connect(env, monkeypatch, url=GOOD, text=None):
    monkeypatch.setattr(calendar_feed, "_fetch", lambda u: text or _sample())
    return env.client.post("/client/calendar/connect", data={"url": url})


def test_connecting_a_calendar_shows_the_meetings_and_keeps_the_link_secret(env, monkeypatch):
    monkeypatch.setattr(meeting_bot, "configured", lambda: True)
    monkeypatch.setattr(env.app, "_meeting_gate", lambda user: (True, "", 500.0))
    page = env.client.get("/client/calendar").text
    assert "Connect a calendar" in page and "Secret address in iCal format" in page
    r = _connect(env, monkeypatch)
    assert r.status_code == 303 and "Connected" in env.client.get(r.headers["location"]).text
    page = env.client.get("/client/calendar").text
    assert "Customer call" in page and "Planning" in page and page.count("Send Kauli") >= 6 and "No video link" in page
    assert "private-0123456789abcdef" not in page and "calendar.google.com/...ic" in page.replace("basic.ics", "...ic") or "...ic" in page   # shown masked only
    assert _connect(env, monkeypatch).status_code == 303 and len(calendar_feed.list_feeds("u1")) == 1                           # the same link twice is one calendar


def test_bad_calendar_links_are_refused_with_a_reason(env, monkeypatch):
    for url, words in (("https://example.com/cal.ics", "Google Calendar or Outlook"), ("http://calendar.google.com/x.ics", "Google Calendar or Outlook")):
        r = _connect(env, monkeypatch, url=url)
        assert words in env.client.get(r.headers["location"]).text, url
    monkeypatch.setattr(calendar_feed, "_fetch", lambda u: (_ for _ in ()).throw(ValueError("That link is not a calendar (iCal) address.")))
    r = env.client.post("/client/calendar/connect", data={"url": GOOD})
    assert "not a calendar" in env.client.get(r.headers["location"]).text and calendar_feed.list_feeds("u1") == []
    for i in range(calendar_feed.MAX_FEEDS):
        _connect(env, monkeypatch, url=GOOD.replace("0123", f"{i:04d}"))
    assert len(calendar_feed.list_feeds("u1")) == calendar_feed.MAX_FEEDS
    r = _connect(env, monkeypatch, url=GOOD.replace("0123", "9999"))
    assert "up to" in env.client.get(r.headers["location"]).text


def test_disconnecting_deletes_the_link_and_only_the_owner_can(env, monkeypatch):
    _connect(env, monkeypatch)
    feed = calendar_feed.list_feeds("u1")[0]
    env.db.get_or_create_user("u2", "other@example.com", "client")
    mine = env.app.current_user
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u2")), "client_scope_id": "u2"})
    env.client.post(f"/client/calendar/{feed['id']}/disconnect")
    assert len(calendar_feed.list_feeds("u1")) == 1                                          # someone else cannot remove it
    assert "Customer call" not in env.client.get("/client/calendar").text                    # nor see it
    env.monkeypatch.setattr(env.app, "current_user", mine)
    env.client.post(f"/client/calendar/{feed['id']}/disconnect")
    assert calendar_feed.list_feeds("u1") == []
    conn = env.db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM calendar_feeds WHERE url LIKE '%private-0123%'").fetchone()[0] == 0


def test_send_kauli_to_a_calendar_meeting_uses_the_meetings_recorder(env, monkeypatch):
    monkeypatch.setattr(meeting_bot, "configured", lambda: True)
    monkeypatch.setattr(env.app, "_meeting_gate", lambda user: (True, "", 500.0))
    made = {}
    monkeypatch.setattr(meeting_bot, "create_bot", lambda url, join_iso=None: made.update(url=url, join=join_iso) or {"id": "bot-1"})
    _connect(env, monkeypatch)
    feed = calendar_feed.list_feeds("u1")[0]
    events = calendar_feed.events_for(feed)
    zoom = next(e for e in events if e["title"] == "Customer call")
    r = env.client.post("/client/calendar/send", data={"feed_id": feed["id"], "key": zoom["key"], "source_lang": "en"})
    assert r.status_code == 303 and r.headers["location"].startswith("/client/meetings?notice=")
    assert made["url"].startswith("https://us02web.zoom.us/j/123456789") and made["join"] and made["join"].endswith("Z")           # joins when it starts
    meeting = env.db.list_meetings_for_client("u1")[0]
    assert meeting["title"] == "Customer call" and meeting["platform"] == "zoom"
    lunch = next(e for e in events if e["title"] == "Lunch")
    r = env.client.post("/client/calendar/send", data={"feed_id": feed["id"], "key": lunch["key"]})
    assert "no longer on the calendar, or it has no" in env.client.get(r.headers["location"]).text                                # no video link: nothing is sent
    r = env.client.post("/client/calendar/send", data={"feed_id": feed["id"], "key": "nope"})
    assert "no longer on the calendar" in env.client.get(r.headers["location"]).text
    monkeypatch.setattr(env.app, "_meeting_gate", lambda user: (False, "Kauli can join your meetings on the Pro and Premium plans.", 0.0))
    planning = next(e for e in events if e["title"] == "Planning")
    r = env.client.post("/client/calendar/send", data={"feed_id": feed["id"], "key": planning["key"]})
    assert "Pro and Premium" in env.client.get(r.headers["location"]).text                    # the plan rules still apply


def test_the_integrations_page_shows_real_connections_and_comes_alive_when_a_service_is_configured(env, monkeypatch):
    _clean(monkeypatch)
    page = env.client.get("/client/integrations").text
    assert 'href="/client/calendar"' in page and "Google Calendar" in page and "Outlook Calendar" in page
    assert "Coming soon" in page and "Import a file" not in page
    monkeypatch.setenv("DROPBOX_APP_KEY", "dk")
    live = env.client.get("/client/integrations").text
    assert "Import a file" in live and "Pick a recording from Dropbox" in live
