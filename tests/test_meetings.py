"""Kauli in your meetings: the gate (paid plan plus enough minutes), the bot lifecycle, signed webhooks, and the
recording becoming an ordinary Instant AI order. The meeting service is faked; nothing here reaches the network."""
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import meeting_bot  # noqa: E402

SECRET = "whsec_" + base64.b64encode(b"unit-test-secret").decode()


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _signed(body: bytes, ts=None, mid="msg_1"):
    ts = str(ts or int(time.time()))
    key = base64.b64decode(SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": mid, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"}


@pytest.fixture
def meet(env, monkeypatch, wav_bytes):
    monkeypatch.setenv("RECALL_API_KEY", "test-key")
    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", SECRET)
    state = SimpleNamespace(posts=[], leaves=[], bot={"id": "bot-1", "status_changes": [{"code": "in_call_recording"}]})

    def fake_post(url, headers=None, json=None, timeout=None):
        if url.endswith("/leave_call/"):
            state.leaves.append(url)
            return Resp(200, {})
        state.posts.append(json)
        return Resp(201, {"id": "bot-1"})

    monkeypatch.setattr(meeting_bot.httpx, "post", fake_post)
    monkeypatch.setattr(meeting_bot.httpx, "get", lambda url, headers=None, timeout=None: Resp(200, state.bot))
    monkeypatch.setattr(meeting_bot, "download", lambda url, dest: (Path(dest).write_bytes(wav_bytes), len(wav_bytes))[1])
    env.db.set_subscription_plan("u1", "pro", 30)
    return SimpleNamespace(env=env, state=state, wav=wav_bytes)


def _send(meet, url="https://zoom.us/j/123456789", **extra):
    data = {"meeting_url": url, "title": "Weekly planning", "source_lang": "en", "join_at": ""}
    data.update(extra)
    return meet.env.client.post("/account/meetings", data=data)


def test_only_real_meeting_links_are_accepted():
    p = meeting_bot.platform_for
    assert p("https://zoom.us/j/123") == "zoom" and p("https://us02web.zoom.us/j/123?pwd=x") == "zoom"
    assert p("https://meet.google.com/abc-defg-hij") == "meet"
    assert p("https://teams.microsoft.com/l/meetup-join/xyz") == "teams" and p("https://teams.live.com/meet/123") == "teams"
    for bad in ("http://zoom.us/j/123", "https://zoom.us.evil.com/j/1", "https://evilzoom.us/j/1", "https://example.com", "zoom.us/j/1", "", "javascript:alert(1)"):
        assert p(bad) is None, bad


def test_the_bot_status_is_read_in_plain_words():
    s = meeting_bot.status_from_bot
    assert s({"status_changes": [{"code": "joining_call"}]}) == ("joining", None)
    assert s({"status_changes": [{"code": "in_call_recording"}]}) == ("recording", None)
    assert s({"status_changes": [{"code": "call_ended"}]})[0] == "processing"
    status, why = s({"status_changes": [{"code": "fatal", "sub_code": "timeout_exceeded_waiting_room"}]})
    assert status == "failed" and "waiting room" in why
    assert meeting_bot.recording_url({"recordings": [{"media_shortcuts": {"video_mixed": {"data": {"download_url": "https://x/y.mp4"}}}}]}) == "https://x/y.mp4"
    assert meeting_bot.recording_url({"video_url": "https://x/z.mp4"}) == "https://x/z.mp4" and meeting_bot.recording_url({}) is None


def test_nothing_is_offered_until_the_meeting_service_is_switched_on(env, monkeypatch):
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    assert "coming soon" in env.client.get("/account/meetings").text
    assert 'href="/account/meetings"' not in env.client.get("/account/home").text
    r = env.client.post("/account/meetings", data={"meeting_url": "https://zoom.us/j/1", "source_lang": "en"})
    assert r.status_code == 303 and "not+switched+on" in r.headers["location"].replace("%20", "+")
    assert env.client.post("/webhooks/recall", content=b"{}", headers=_signed(b"{}")).status_code == 401      # no secret, so no webhook


def test_a_client_on_the_free_plan_is_pointed_to_a_plan_and_nothing_is_sent(meet):
    meet.env.db.set_subscription_plan("u1", "free", None)
    r = _send(meet)
    assert r.status_code == 303 and "Pro+and+Premium" in r.headers["location"].replace("%20", "+")
    assert meet.state.posts == []
    page = meet.env.client.get("/account/meetings").text
    assert "Pro and Premium plans" in page and "disabled" in page


def test_a_client_who_is_nearly_out_of_minutes_cannot_send_the_bot(meet):
    conn = meet.env.db.get_conn()
    conn.execute("UPDATE subscriptions SET minutes_used_this_period = 2490 WHERE user_id = 'u1'")      # 10 of 2,500 left
    conn.commit()
    conn.close()
    r = _send(meet)
    assert r.status_code == 303 and "at+least+30+minutes" in r.headers["location"].replace("%20", "+")
    assert meet.state.posts == []


def test_sending_the_bot_records_the_meeting_and_lists_it(meet):
    r = _send(meet)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert len(meet.state.posts) == 1 and meet.state.posts[0]["meeting_url"] == "https://zoom.us/j/123456789" and meet.state.posts[0]["bot_name"] == "Kauli Notetaker"
    assert "recording" in meet.state.posts[0]["chat"]["on_bot_join"]["message"]                    # the room is told
    rows = meet.env.db.list_meetings_for_client("u1")
    assert len(rows) == 1 and rows[0]["platform"] == "zoom" and rows[0]["provider_bot_id"] == "bot-1"
    page = meet.env.client.get("/account/meetings").text
    assert "Weekly planning" in page and "Zoom" in page and "Recording" in page          # the fake service says it is recording
    assert 'href="/account/meetings"' in meet.env.client.get("/account/home").text         # and the sidebar now shows the tab
    bad = _send(meet, url="https://example.com/meeting")
    assert "Zoom%2C+Google+Meet" in bad.headers["location"] or "Zoom" in bad.headers["location"]
    assert len(meet.state.posts) == 1


def test_a_scheduled_meeting_passes_its_start_time_and_is_limited_to_the_next_30_days(meet):
    soon = time.time() + 3600
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(soon))
    assert _send(meet, join_at=iso).status_code == 303
    assert meet.state.posts[-1]["join_at"].startswith(iso[:13])
    far = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 90 * 86400))
    n = len(meet.state.posts)
    r = _send(meet, url="https://meet.google.com/abc-defg-hij", join_at=far)
    assert "error=" in r.headers["location"] and len(meet.state.posts) == n


def test_no_more_than_three_meetings_at_once(meet):
    for i in range(3):
        assert "notice=" in _send(meet, url=f"https://zoom.us/j/{i}00000000").headers["location"]
    r = _send(meet, url="https://zoom.us/j/999999999")
    assert "error=" in r.headers["location"] and len(meet.state.posts) == 3


def test_webhooks_must_be_signed(meet):
    body = json.dumps({"event": "bot.status_change", "data": {"bot": {"id": "bot-1"}}}).encode()
    assert meet.env.client.post("/webhooks/recall", content=body, headers={"content-type": "application/json"}).status_code == 401
    assert meet.env.client.post("/webhooks/recall", content=body, headers=_signed(body, ts=int(time.time()) - 3600)).status_code == 401
    forged = _signed(body)
    forged["svix-signature"] = "v1," + base64.b64encode(b"x" * 32).decode()
    assert meet.env.client.post("/webhooks/recall", content=body, headers=forged).status_code == 401
    assert meet.env.client.post("/webhooks/recall", content=body, headers=_signed(body)).status_code == 200        # signed, bot unknown: ignored


def test_a_finished_meeting_becomes_one_instant_ai_order_even_if_the_webhook_repeats(meet):
    _send(meet)
    meet.state.bot = {"id": "bot-1", "status_changes": [{"code": "done"}], "video_url": "https://recordings.example/m.mp4"}
    body = json.dumps({"event": "bot.status_change", "data": {"bot": {"id": "bot-1"}, "status": {"code": "done"}}}).encode()
    assert meet.env.client.post("/webhooks/recall", content=body, headers=_signed(body, mid="msg_a")).status_code == 200
    assert meet.env.client.post("/webhooks/recall", content=body, headers=_signed(body, mid="msg_b")).status_code == 200
    m = meet.env.db.list_meetings_for_client("u1")[0]
    assert m["status"] == "done" and m["order_id"]
    orders = meet.env.db.list_orders_for_client("u1")
    assert len(orders) == 1 and orders[0]["id"] == m["order_id"]
    o = orders[0]
    assert o["review_tier"] == "ai" and o["service_level"] == "transcribe" and o["source_lang"] == "en" and o["folder_name"] == "Meetings"
    assert o["status"] in ("queued", "processing") and o["cost_usd"] == 0                # covered by the plan's own minutes, so it just runs
    assert "Transcript ready" in meet.env.client.get("/account/meetings").text


def test_a_meeting_that_could_not_be_recorded_says_why(meet):
    _send(meet)
    meet.state.bot = {"id": "bot-1", "status_changes": [{"code": "fatal", "sub_code": "recording_permission_denied"}]}
    page = meet.env.client.get("/account/meetings").text                          # opening the page also refreshes
    m = meet.env.db.list_meetings_for_client("u1")[0]
    assert m["status"] == "failed" and "did not allow Kauli to record" in page
    assert meet.env.db.list_orders_for_client("u1") == []


def test_cancelling_sends_the_bot_away_and_only_for_its_owner(meet):
    _send(meet)
    m = meet.env.db.list_meetings_for_client("u1")[0]
    r = meet.env.client.post(f"/account/meetings/{m['id']}/cancel")
    assert r.status_code == 303 and meet.state.leaves and meet.env.db.get_meeting(m["id"])["status"] == "cancelled"
    assert meet.env.client.post("/account/meetings/not-a-meeting/cancel").status_code == 404


def test_the_home_page_lists_meeting_platforms_as_available_only_when_switched_on(meet, monkeypatch):
    import re
    def states(html):
        sec = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
        return {n: s for n, s in re.findall(r'<span class="int-name">(Zoom|Teams meetings|Google Meet)</span>\s*<span class="int-badge (\w+)">', sec)}
    monkeypatch.setattr(meet.env.app, "current_user", lambda request: None)
    assert states(meet.env.client.get("/").text) == {"Zoom": "live", "Teams meetings": "live", "Google Meet": "live"}
    monkeypatch.delenv("RECALL_API_KEY")
    assert states(meet.env.client.get("/").text) == {"Zoom": "soon", "Teams meetings": "soon", "Google Meet": "soon"}
