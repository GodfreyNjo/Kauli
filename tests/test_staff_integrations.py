"""Integrations for staff accounts: a staff member sends their alerts on to their own Slack, Teams or signed webhook."""
import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import staff_integrations  # noqa: E402

SLACK = "https://hooks.slack.com/services/T01ABCDEF/B02GHIJKL/abcdefghij1234567890"
TEAMS = "https://prod-12.westeurope.logic.azure.com/workflows/abc123/triggers/manual/paths/invoke?api-version=2016-06-01&sig=xyz"
HOOK = "https://hooks.example.com/kauli"


def _staff(env, user_id="s1", email="staff@example.com"):
    env.db.get_or_create_user(user_id, email, "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user(user_id)), "client_scope_id": user_id})


def _capture(env):
    sent = []

    def fake_post(url, **kw):
        sent.append({"url": url, **kw})
        return SimpleNamespace(status_code=200, text="ok")

    env.monkeypatch.setattr(staff_integrations.httpx, "post", fake_post)
    env.monkeypatch.setattr(env.app, "_is_safe_webhook_url", lambda url: url.startswith("https://hooks.example.com"))
    return sent


def test_only_staff_can_see_or_change_integrations_and_it_is_in_their_menu(env):
    assert env.client.get("/staff/integrations", follow_redirects=False).status_code in (302, 303, 307)
    assert env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK}, follow_redirects=False).status_code in (302, 303, 307)
    assert not env.db.get_staff_integrations("u1")
    _staff(env)
    page = env.client.get("/staff/integrations").text
    assert "Integrations" in page and 'id="slack"' in page and 'id="teams"' in page and 'id="webhook"' in page and 'href="/staff/integrations"' in page
    assert "A job is ready for review" in page and "A job failed and needs attention" in page


def test_a_url_must_be_the_real_shape_for_its_provider(env):
    _staff(env)
    for provider, url in (("slack", "https://evil.example.com/x"), ("teams", "https://evil.example.com/x"), ("webhook", "http://127.0.0.1/hook")):
        r = env.client.post("/staff/integrations/save", data={"provider": provider, "url": url, "events": "needs_review"}, follow_redirects=False)
        assert "error=" in r.headers["location"], provider
    assert not env.db.get_staff_integrations("s1")
    assert "error=" in env.client.post("/staff/integrations/save", data={"provider": "bogus", "url": SLACK}, follow_redirects=False).headers["location"]


def test_each_staff_member_connects_their_own_and_the_url_is_shown_masked(env):
    _capture(env)
    _staff(env)
    r = env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["needs_review", "dead_letter"], "enabled": "1"}, follow_redirects=False)
    assert "notice=" in r.headers["location"]
    row = env.db.get_staff_integrations("s1")["slack"]
    assert row["url"] == SLACK and json.loads(row["events"]) == ["needs_review", "dead_letter"]
    page = env.client.get("/staff/integrations").text
    assert SLACK not in page and "hooks.slack.com/serv..." in page and "Connected" in page                 # masked on the page
    _staff(env, "s2", "other@example.com")
    assert not env.db.get_staff_integrations("s2") and "Not connected" in env.client.get("/staff/integrations").text.split('id="slack"')[1].split("</section>")[0]


def test_an_alert_goes_only_where_it_was_asked_for_and_a_webhook_is_signed(env):
    sent = _capture(env)
    _staff(env)
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["needs_review"], "enabled": "1"})
    env.client.post("/staff/integrations/save", data={"provider": "teams", "url": TEAMS, "events": ["dead_letter"], "enabled": "1"})
    env.client.post("/staff/integrations/save", data={"provider": "webhook", "url": HOOK, "events": ["needs_review", "dead_letter"], "enabled": "1"})
    secret = env.db.get_staff_integrations("s1")["webhook"]["secret"]
    assert secret and len(secret) >= 32
    assert staff_integrations.deliver("needs_review", "clip.wav is ready for review", "/staff/orders/abc") == 2          # Slack and the webhook, not Teams
    urls = [s["url"] for s in sent]
    assert SLACK in urls and HOOK in urls and TEAMS not in urls
    slack = next(s for s in sent if s["url"] == SLACK)["json"]["text"]
    assert "clip.wav is ready for review" in slack and "/staff/orders/abc" in slack
    hook = next(s for s in sent if s["url"] == HOOK)
    expected = "sha256=" + hmac.new(secret.encode(), hook["content"], hashlib.sha256).hexdigest()
    assert hook["headers"]["X-Kauli-Signature"] == expected and hook["headers"]["X-Kauli-Event"] == "needs_review"
    body = json.loads(hook["content"])
    assert body["event"] == "needs_review" and body["title"] == "clip.wav is ready for review" and body["link"].endswith("/staff/orders/abc")
    sent.clear()
    assert staff_integrations.deliver("dead_letter", "clip.wav failed and needs attention", "/staff/orders/abc") == 2         # Teams and the webhook
    assert TEAMS in [s["url"] for s in sent] and SLACK not in [s["url"] for s in sent]
    card = next(s for s in sent if s["url"] == TEAMS)["json"]
    assert card["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_the_real_staff_alert_reaches_the_integration_and_a_paused_or_failing_one_never_blocks_it(env):
    sent = _capture(env)
    _staff(env)
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["dead_letter"], "enabled": "1"})
    env.monkeypatch.setattr(staff_integrations, "deliver_in_background", lambda kind, title, link=None: staff_integrations.deliver(kind, title, link))
    env.db.notify_all_staff("dead_letter", "clip.wav failed and needs attention", "/staff/orders/abc")
    assert [s["url"] for s in sent] == [SLACK]
    assert env.db.get_staff_integrations("s1")["slack"]["last_status"] == "Delivered"
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["dead_letter"]})                # no "enabled": paused
    sent.clear()
    env.db.notify_all_staff("dead_letter", "again", "/staff")
    assert sent == []
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["dead_letter"], "enabled": "1"})

    def boom(url, **kw):
        raise staff_integrations.httpx.ConnectError("no route")

    env.monkeypatch.setattr(staff_integrations.httpx, "post", boom)
    env.db.notify_all_staff("dead_letter", "still goes to the bell", "/staff")                                                        # no exception
    assert "no route" in env.db.get_staff_integrations("s1")["slack"]["last_status"]
    assert any(n["title"] == "still goes to the bell" for n in env.db.list_recent_notifications("s1", limit=5))


def test_test_and_remove_work_on_my_own_integration_only(env):
    sent = _capture(env)
    _staff(env)
    assert "error=" in env.client.post("/staff/integrations/test", data={"provider": "slack"}, follow_redirects=False).headers["location"]       # not saved yet
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["needs_review"], "enabled": "1"})
    r = env.client.post("/staff/integrations/test", data={"provider": "slack"}, follow_redirects=False)
    assert "notice=" in r.headers["location"] and sent and "Test alert from Kauli" in sent[-1]["json"]["text"]
    _staff(env, "s2", "other@example.com")
    assert "error=" in env.client.post("/staff/integrations/test", data={"provider": "slack"}, follow_redirects=False).headers["location"]       # not theirs
    _staff(env)
    env.client.post("/staff/integrations/remove", data={"provider": "slack"})
    assert not env.db.get_staff_integrations("s1")


def test_a_demoted_account_stops_receiving_alerts(env):
    sent = _capture(env)
    _staff(env)
    env.client.post("/staff/integrations/save", data={"provider": "slack", "url": SLACK, "events": ["needs_review"], "enabled": "1"})
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET role = 'client' WHERE id = 's1'")
    conn.commit()
    conn.close()
    assert staff_integrations.deliver("needs_review", "x", "/staff") == 0 and sent == []
