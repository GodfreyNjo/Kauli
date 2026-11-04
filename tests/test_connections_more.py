"""The other one-click services: Google Sheets and Drive, Discord, Notion, Dropbox, OneDrive and the two calendars."""
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import connections  # noqa: E402

KEYS = {"gsheets": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"), "gdrive": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
        "gcal": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"), "slack": ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"),
        "discord": ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"), "notion": ("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET"),
        "dropbox": ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"), "onedrive": ("ONEDRIVE_CLIENT_ID", "ONEDRIVE_CLIENT_SECRET"),
        "outlook": ("ONEDRIVE_CLIENT_ID", "ONEDRIVE_CLIENT_SECRET")}
HOSTS = {"gsheets": "accounts.google.com", "gdrive": "accounts.google.com", "gcal": "accounts.google.com", "slack": "slack.com", "discord": "discord.com",
         "notion": "api.notion.com", "dropbox": "www.dropbox.com", "onedrive": "login.microsoftonline.com", "outlook": "login.microsoftonline.com"}


class Reply:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _on(monkeypatch, provider):
    for k in KEYS[provider]:
        monkeypatch.setenv(k, "x-" + k.lower())
    monkeypatch.setenv("KAULI_PUBLIC_BASE_URL", "https://kauli.test")


def _link(env, monkeypatch, provider, reply):
    _on(monkeypatch, provider)
    r = env.client.get(f"/client/integrations/{provider}/connect", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    monkeypatch.setattr(connections.httpx, "post", lambda *a, **k: Reply(reply))
    for name in ("_google_email", "_dropbox_label", "_graph_label"):
        monkeypatch.setattr(connections, name, lambda t: "me@example.com")
    monkeypatch.setattr(connections, "_notion_first_page", lambda t: "page-1")
    return env.client.get(f"/client/integrations/{provider}/callback", params={"code": "c", "state": q["state"][0]})


def test_every_service_sends_the_customer_to_its_own_sign_in_with_the_right_address(env, monkeypatch):
    for provider, host in HOSTS.items():
        _on(monkeypatch, provider)
        r = env.client.get(f"/client/integrations/{provider}/connect", follow_redirects=False)
        url = urlparse(r.headers["location"])
        q = parse_qs(url.query)
        assert url.netloc == host, provider
        assert q["redirect_uri"] == [f"https://kauli.test/client/integrations/{provider}/callback"], provider
        assert q["client_id"] == ["x-" + KEYS[provider][0].lower()] and q["state"], provider
    cal = parse_qs(urlparse(env.client.get("/client/integrations/gcal/connect", follow_redirects=False).headers["location"]).query)
    assert "calendar.events.readonly" in cal["scope"][0] and "auth/calendar " not in cal["scope"][0] + " "          # read-only, events only
    assert "scope" not in parse_qs(urlparse(env.client.get("/client/integrations/notion/connect", follow_redirects=False).headers["location"]).query)


def test_each_service_stores_what_it_needs_to_send_later(env, monkeypatch):
    got = _link(env, monkeypatch, "discord", {"access_token": "a", "webhook": {"url": "https://discord.com/api/webhooks/1/abc", "name": "general"}})
    assert "Connection Successful!" in got.text
    row = connections.get_connection("u1", "discord")
    assert connections._dec(row["target_enc"]) == "https://discord.com/api/webhooks/1/abc" and row["account_label"] == "general" and row["token_enc"] is None
    _link(env, monkeypatch, "notion", {"access_token": "ntn-secret", "workspace_name": "Acme"})
    row = connections.get_connection("u1", "notion")
    assert connections._dec(row["token_enc"]) == "ntn-secret" and connections._dec(row["target_enc"]) == "page-1" and row["account_label"] == "Acme" and row["expires_at"] is None
    _link(env, monkeypatch, "dropbox", {"access_token": "dbx", "refresh_token": "dbx-r", "expires_in": 14400})
    row = connections.get_connection("u1", "dropbox")
    assert connections._dec(row["refresh_enc"]) == "dbx-r" and row["expires_at"] > time.time() + 14000 and row["account_label"] == "me@example.com"
    bad = _link(env, monkeypatch, "slack", {"ok": False, "error": "invalid_code"})
    assert "Could not connect" in bad.text and connections.get_connection("u1", "slack") is None
    page = env.client.get("/client/integrations").text
    assert 'data-settings="discord"' in page and 'data-settings="notion"' in page and 'data-settings="dropbox"' in page


def _order(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET ai_summary_text = ?, original_filename = ? WHERE id = ?", ("We agreed to ship on Friday.", "Weekly sync.mp4", order["id"]))
    conn.commit()
    conn.close()
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: None)
    return env.db.get_order(order["id"])


def test_a_finished_order_reaches_discord_dropbox_onedrive_drive_and_sheets(env, wav_bytes, monkeypatch):
    order = _order(env, wav_bytes)
    for provider in ("discord", "dropbox", "onedrive", "gdrive", "gsheets"):
        _link(env, monkeypatch, provider, {"access_token": "t", "refresh_token": "r", "expires_in": 3600, "webhook": {"url": "https://discord.com/api/webhooks/1/abc", "name": "g"}})
    sent = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: sent.append(("POST", url, k)) or Reply({"id": "new-id"}))
    monkeypatch.setattr(connections.httpx, "put", lambda url, **k: sent.append(("PUT", url, k)) or Reply({}))
    connections.deliver_order_ready(order, "https://kauli.test")
    urls = [u for _m, u, _k in sent]
    discord = next(k for _m, u, k in sent if u.startswith("https://discord.com/api/webhooks"))
    assert "Weekly sync.mp4" in discord["json"]["content"] and "We agreed to ship on Friday." in discord["json"]["content"]
    dropbox = next(k for _m, u, k in sent if "dropboxapi.com/2/files/upload" in u)
    assert json.loads(dropbox["headers"]["Dropbox-API-Arg"])["path"] == "/Weekly sync.mp4 (Kauli).txt" and b"We agreed to ship on Friday." in dropbox["content"]
    onedrive = next((u, k) for m, u, k in sent if m == "PUT")
    assert "graph.microsoft.com/v1.0/me/drive/special/approot:/" in onedrive[0] and onedrive[0].endswith(":/content") and b"We agreed" in onedrive[1]["content"]
    assert any("drive/v3/files?fields=id" in u for u in urls)                                   # the Kauli folder, then the file inside it
    assert any("uploadType=multipart" in u for u in urls)
    sheet = next(k for _m, u, k in sent if "A1:append" in u)["json"]["values"]
    assert sheet[0][:2] == ["Date", "Title"] and sheet[1][1] == "Weekly sync.mp4" and "We agreed" in sheet[1][3]       # a header the first time, then the row
    conn = env.db.get_conn()
    bad = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE order_id = ? AND ok = 0", (order["id"],)).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE order_id = ? AND ok = 1", (order["id"],)).fetchone()[0]
    conn.close()
    assert bad == 0 and done == 5
    sent.clear()
    connections.deliver_order_ready(order, "https://kauli.test")                                # the folder and the sheet are reused, not made again
    assert not any("drive/v3/files?fields=id" in u for _m, u, _k in sent)
    assert [k["json"]["values"] for _m, u, k in sent if "A1:append" in u][0].__len__() == 1


def test_an_expired_token_is_refreshed_before_sending(env, monkeypatch):
    _link(env, monkeypatch, "dropbox", {"access_token": "old", "refresh_token": "r1", "expires_in": 3600})
    conn = env.db.get_conn()
    conn.execute("UPDATE integration_connections SET expires_at = 1 WHERE provider = 'dropbox'")
    conn.commit()
    conn.close()
    seen = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: seen.append((url, k)) or Reply({"access_token": "new", "expires_in": 3600}))
    assert connections._access_token(connections.get_connection("u1", "dropbox")) == "new"
    assert seen[0][0] == "https://api.dropboxapi.com/oauth2/token" and seen[0][1]["data"]["grant_type"] == "refresh_token" and seen[0][1]["data"]["refresh_token"] == "r1"
    assert connections._dec(connections.get_connection("u1", "dropbox")["refresh_enc"]) == "r1"       # kept when the provider sends no new one


def test_calendars_are_connections_but_never_a_place_to_send_transcripts(env, wav_bytes, monkeypatch):
    _link(env, monkeypatch, "gcal", {"access_token": "t", "refresh_token": "r", "expires_in": 3600})
    d = env.client.get("/client/integrations/gcal/settings").json()
    assert {o["key"] for o in d["options"]} == {"auto", "only_mine", "tell_attendees"}
    sent = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: sent.append(url) or Reply({}))
    connections.deliver_order_ready(_order(env, wav_bytes), "https://kauli.test")
    assert sent == []


def test_cards_that_are_not_built_offer_a_one_click_tell_me_instead_of_developer_docs(env):
    page = env.client.get("/client/integrations").text
    cards = page.split('id="ig-grid"')[1].split("</article>")
    hubspot = next(c for c in cards if "HubSpot" in c)
    assert "Coming soon" in hubspot and "Please tell me when HubSpot connects" in hubspot and "/docs/api" not in hubspot
    assert "/docs/api" in next(c for c in cards if "<h3>Zapier</h3>" in c)                  # the developer tools still point to their docs
    assert "background: #1a73e8" in page                                                    # each card has its coloured initial


def test_staff_see_exactly_what_to_register_with_each_service(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/integrations").text
    assert "Customer one-click connections" in page and "Needs setup" in page
    for provider in ("gdocs", "gsheets", "gdrive", "gcal", "slack", "discord", "notion", "dropbox", "onedrive", "outlook"):
        assert f"/client/integrations/{provider}/callback" in page, provider
    assert "GOOGLE_OAUTH_CLIENT_ID" in page and "DISCORD_CLIENT_SECRET" in page and "NOTION_CLIENT_ID" in page
