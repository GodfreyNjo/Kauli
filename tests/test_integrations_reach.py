"""What a customer can connect today with nothing registered on Kauli's side (Discord by pasting a link), the landing page badges that follow
what really works, the Slack one-link setup for the owner, and the honest notice the meeting bot posts."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_connections_more import Reply, _order  # noqa: E402,F401
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import connections, meeting_bot  # noqa: E402

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz0123456789"


def _clean(monkeypatch):
    for k in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET",
              "ONEDRIVE_CLIENT_ID", "ONEDRIVE_CLIENT_SECRET", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_discord_connects_by_pasting_a_link_and_proves_it_with_a_first_message(env, monkeypatch, wav_bytes):
    _clean(monkeypatch)
    card = env.client.get("/account/integrations").text.split("<h3>Discord</h3>")[1].split("</article>")[0]
    assert 'action="/account/integrations/discord/paste"' in card and "Copy Webhook URL" in card and "Coming soon" not in card
    sent = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: sent.append((url, k)) or Reply({}, 204))
    r = env.client.post("/account/integrations/discord/paste", data={"url": HOOK}, follow_redirects=False)
    assert "Discord+is+connected" in r.headers["location"] or "Discord%20is%20connected" in r.headers["location"]
    assert sent[0][0] == HOOK and "Kauli is connected" in sent[0][1]["json"]["content"]                    # a first message proves the link works
    row = connections.get_connection("u1", "discord")
    assert connections._dec(row["target_enc"]) == HOOK and HOOK not in row["target_enc"]
    page = env.client.get("/account/integrations").text.split("<h3>Discord</h3>")[1].split("</article>")[0]
    assert 'data-settings="discord"' in page and 'data-disconnect="discord"' in page and "ig-paste" not in page
    sent.clear()                                                                                             # and finished orders arrive there
    connections.deliver_order_ready(_order(env, wav_bytes), "https://kauli.test")
    assert sent and sent[0][0] == HOOK and "Weekly sync.mp4" in sent[0][1]["json"]["content"]


def test_a_pasted_discord_link_must_really_be_a_discord_webhook_that_answers(env, monkeypatch):
    _clean(monkeypatch)
    sent = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: sent.append(url) or Reply({}, 204))
    for bad in ("https://evil.example.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz", "http://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz",
                "https://discord.com.evil.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz", "https://discord.com/api/webhooks/1/x", "not a link", ""):
        r = env.client.post("/account/integrations/discord/paste", data={"url": bad}, follow_redirects=False)
        assert "notice=" in r.headers["location"] and "does+not+look" in r.headers["location"].replace("%20", "+"), bad
    assert sent == [] and connections.get_connection("u1", "discord") is None                                # nothing was contacted or saved
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: Reply({}, 404))                          # a link Discord no longer knows
    r = env.client.post("/account/integrations/discord/paste", data={"url": HOOK}, follow_redirects=False)
    assert "did+not+accept" in r.headers["location"].replace("%20", "+") and connections.get_connection("u1", "discord") is None


def test_the_landing_page_badges_follow_what_really_works(env, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)

    def state(html, name):
        return re.search(rf'<span class="int-name">{re.escape(name)}</span>\s*<span class="int-badge (\w+)">', html).group(1)

    page = env.client.get("/").text
    assert [state(page, n) for n in ("Discord", "Google Calendar", "Outlook Calendar", "Slack", "Notion")] == ["live"] * 5       # these need nothing from Kauli
    assert [state(page, n) for n in ("Google Drive", "Dropbox", "OneDrive", "Google Docs", "HubSpot", "Zoom")] == ["soon"] * 6
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DROPBOX_APP_KEY", "k")
    monkeypatch.setenv("DROPBOX_APP_SECRET", "s")
    monkeypatch.setenv("RECALL_API_KEY", "r")
    page = env.client.get("/").text
    assert [state(page, n) for n in ("Google Drive", "Google Docs", "Dropbox", "Zoom", "Google Meet")] == ["live"] * 5
    assert state(page, "OneDrive") == "soon"


def test_the_owner_can_create_the_slack_app_from_one_prefilled_link(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/integrations").text
    link = re.search(r'href="(https://api\.slack\.com/apps\?new_app=1&amp;manifest_json=[^"]+)"', page).group(1).replace("&amp;", "&")
    manifest = json.loads(parse_qs(urlparse(link).query)["manifest_json"][0])
    assert manifest["oauth_config"]["scopes"]["bot"] == ["incoming-webhook"] and manifest["display_information"]["name"] == "Kauli"
    assert manifest["oauth_config"]["redirect_urls"][0].endswith("/client/integrations/slack/callback")
    assert "Create the Slack app in one click" in page


def test_the_recording_notice_promises_only_what_kauli_can_do():
    text = meeting_bot.JOIN_MESSAGE.lower()
    assert "recording" in text and "tell the host" in text and "remove me" in text
    assert "i will stop" not in text and "i'll stop" not in text and "rather i did not" not in text            # nothing reads the chat, so no promise to react to it
