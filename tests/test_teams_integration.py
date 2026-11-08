"""Microsoft Teams notifications against a throwaway database. Nothing reaches Teams:
httpx.post is replaced by a recorder, and the signed-in user is a stand-in."""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)

GOOD = "https://prod-12.westus.logic.azure.com/workflows/abc123def456/triggers/manual/paths/invoke?sig=Zk9xTEST"


@pytest.fixture
def teams(env, wav_bytes):
    sent = []

    def fake_post(url, **kw):
        sent.append({"url": url, **kw})
        return SimpleNamespace(status_code=202, text="")          # Workflows answer 202 Accepted

    env.monkeypatch.setattr(env.app.httpx, "post", fake_post)
    env.sent, env.wav = sent, wav_bytes
    return env


def _order(env):
    return env.order_from(env.place(env.wav))


def _fire(env, order, base="https://kauli.test/"):
    env.app._fire_client_webhook(SimpleNamespace(base_url=base), order, "order.ready")


def _actions(msg):
    return msg["attachments"][0]["content"].get("actions", [])


@pytest.mark.parametrize("url,ok", [
    (GOOD, True),
    ("https://acme.webhook.office.com/webhookb2/aaa@bbb/IncomingWebhook/ccc/ddd", True),
    ("https://default123.12.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/x/triggers/manual/paths/invoke?sig=s", True),
    ("https://outlook.office.com/webhook/aaa@bbb/IncomingWebhook/ccc/ddd", True),
    ("http://prod-12.westus.logic.azure.com/workflows/abc", False),                # not https
    ("https://logic.azure.com.evil.com/workflows/abc", False),                     # lookalike host
    ("https://evil.com/logic.azure.com/workflows/abc", False),
    ("https://user:pw@prod-12.westus.logic.azure.com/workflows/abc", False),       # credentials in the URL
    ("https://prod-12.westus.logic.azure.com:8443/workflows/abc", False),          # unusual port
    ("https://logic.azure.com/workflows/abc", False),                              # no subdomain: not a webhook host
    ("https://outlook.office.com/mail/inbox", False),                              # same host, not a webhook path
    ("https://prod-12.westus.logic.azure.com/", False),                            # no path
    ("https://prod-12.westus.logic.azure.com/workflows/abc#frag", False),
    ("https://localhost/workflows/abc", False),
    ("https://169.254.169.254/latest/meta-data", False),
    ("", False),
])
def test_only_microsoft_webhook_addresses_are_accepted(teams, url, ok):
    assert teams.app._is_teams_webhook_url(url) is ok


def test_saving_validates_and_stores(teams):
    r = teams.client.post("/settings/teams", data={"url": "https://evil.example/hook"})
    assert r.status_code == 303 and "teams_error" in r.headers["location"]
    assert teams.db.get_client_teams("u1") is None
    r = teams.client.post("/settings/teams", data={"url": GOOD, "include_links": "1"})
    assert "teams_notice" in r.headers["location"]
    row = teams.db.get_client_teams("u1")
    assert row["webhook_url"] == GOOD and row["include_links"] == 1
    teams.client.post("/settings/teams", data={"url": ""})                         # only the checkbox changed
    row = teams.db.get_client_teams("u1")
    assert row["webhook_url"] == GOOD and row["include_links"] == 0


def test_the_page_never_shows_the_full_url(teams):
    teams.client.post("/settings/teams", data={"url": GOOD})
    html = teams.client.get("/settings?tab=integrations").text
    assert "Connected: workflow URL" in html and GOOD not in html and "TEST" in html     # last 4 characters only


def test_test_message_disconnect_and_refusals(teams):
    assert "teams_error" in teams.client.post("/settings/teams/test").headers["location"]   # not connected
    teams.client.post("/settings/teams", data={"url": GOOD})
    r = teams.client.post("/settings/teams/test")
    assert "teams_notice" in r.headers["location"]                                     # 202 counts as success
    assert teams.sent[-1]["url"] == GOOD and teams.sent[-1]["follow_redirects"] is False
    assert teams.sent[-1]["json"]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    teams.app.httpx.post = lambda url, **kw: SimpleNamespace(status_code=400, text="InvalidPayload")
    r = teams.client.post("/settings/teams/test")
    assert "teams_error" in r.headers["location"] and "InvalidPayload" in r.headers["location"]
    teams.client.post("/settings/teams/delete")
    assert teams.db.get_client_teams("u1") is None


def test_order_ready_posts_a_card_without_download_buttons_by_default(teams):
    order = _order(teams)
    teams.client.post("/settings/teams", data={"url": GOOD})
    _fire(teams, order)
    assert len(teams.sent) == 1 and teams.sent[0]["url"] == GOOD
    card = teams.sent[0]["json"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["body"][0]["text"] == "Your Kauli order is ready"
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["File"] == "clip.wav" and facts["Service"] == "Transcription" and facts["Order"] == order["id"]
    actions = _actions(teams.sent[0]["json"])
    assert [a["title"] for a in actions] == ["Open order"]
    assert actions[0]["url"] == f"https://kauli.test/account/orders/{order['id']}"


def test_download_buttons_only_when_opted_in_and_downloads_are_unlocked(teams):
    order = _order(teams)
    teams.db.set_order_free_preview(order["id"], False)
    teams.db.update_order_status(order["id"], "ready_for_delivery")
    teams.client.post("/settings/teams", data={"url": GOOD, "include_links": "1"})
    _fire(teams, teams.db.get_order(order["id"]))
    actions = _actions(teams.sent[-1]["json"])
    assert actions[0]["title"] == "Open order" and "Transcript (TXT)" in [a["title"] for a in actions]
    assert all("sig=" in a["url"] for a in actions[1:]) and len(actions) <= 4          # signed, expiring links
    teams.client.post("/settings/teams", data={"url": "", "include_links": ""})
    _fire(teams, teams.db.get_order(order["id"]))
    assert [a["title"] for a in _actions(teams.sent[-1]["json"])] == ["Open order"]


def test_an_order_still_to_be_paid_for_says_so_on_the_card(teams):
    order = _order(teams)
    conn = teams.db.get_conn()
    conn.execute("UPDATE orders SET is_free_preview = 1, duration_minutes = 10 WHERE id = ?", (order["id"],))
    conn.commit()
    teams.client.post("/settings/teams", data={"url": GOOD, "include_links": "1"})
    _fire(teams, teams.db.get_order(order["id"]))
    body = teams.sent[-1]["json"]["attachments"][0]["content"]["body"]
    assert any("Unlock this order" in b.get("text", "") for b in body)
    assert [a["title"] for a in _actions(teams.sent[-1]["json"])] == ["Open order"]    # no download links yet


def test_file_names_cannot_inject_links_or_mentions(teams):
    order = _order(teams)
    conn = teams.db.get_conn()
    conn.execute("UPDATE orders SET original_filename = ? WHERE id = ?",
                 ("[click](http://evil.example) <at>everyone</at> `x`", order["id"]))
    conn.commit()
    teams.client.post("/settings/teams", data={"url": GOOD})
    _fire(teams, teams.db.get_order(order["id"]))
    value = teams.sent[-1]["json"]["attachments"][0]["content"]["body"][1]["facts"][0]["value"]
    assert not any(ch in value for ch in "[]<>`")


def test_a_teams_outage_never_breaks_delivery_and_is_logged(teams):
    order = _order(teams)
    teams.client.post("/settings/teams", data={"url": GOOD})

    def boom(url, **kw):
        raise httpx.ConnectError("teams unreachable")

    teams.app.httpx.post = boom
    _fire(teams, teams.db.get_order(order["id"]))                                    # must not raise
    log = teams.db.list_webhook_deliveries("u1", limit=5)
    assert log[0]["event"] == "teams.order.ready" and not log[0]["ok"]


def test_nothing_is_sent_when_teams_is_not_connected(teams):
    order = _order(teams)
    _fire(teams, teams.db.get_order(order["id"]))
    assert teams.sent == []
