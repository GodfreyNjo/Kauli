"""Slack notifications against a throwaway database. Nothing here reaches Slack:
httpx.post is replaced by a recorder, and the signed-in user is a stand-in."""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)

GOOD = "https://hooks.slack.com/services/T01ABCDEF/B02GHIJKL/abcDEF123xyz"


@pytest.fixture
def slack(env, wav_bytes):
    sent = []

    def fake_post(url, **kw):
        sent.append({"url": url, **kw})
        return SimpleNamespace(status_code=200, text="ok")

    env.monkeypatch.setattr(env.app.httpx, "post", fake_post)
    env.sent = sent
    env.wav = wav_bytes
    return env


def _order(env):
    return env.order_from(env.place(env.wav))


def _fire(env, order, base="https://kauli.test/"):
    env.app._fire_client_webhook(SimpleNamespace(base_url=base), order, "order.ready")


@pytest.mark.parametrize("url,ok", [
    (GOOD, True),
    ("http://hooks.slack.com/services/T01/B02/abc", False),                  # not https
    ("https://hooks.slack.com.evil.com/services/T01/B02/abc", False),        # lookalike host
    ("https://evil.com/services/T01/B02/abc", False),
    ("https://user@hooks.slack.com/services/T01/B02/abc", False),            # userinfo trick
    ("https://hooks.slack.com/services/T01/B02/abc/extra", False),
    ("https://hooks.slack.com/services/T01/B02", False),
    ("https://hooks.slack.com/services/T01/B02/abc?x=1", False),
    ("https://hooks.slack.com/services/t01/b02/abc", False),                 # ids are upper-case
    ("https://hooks.slack.com/\nservices/T01/B02/abc", False),
    ("", False),
])
def test_only_real_slack_webhook_urls_are_accepted(slack, url, ok):
    assert slack.app._is_slack_webhook_url(url) is ok


def test_saving_validates_and_stores(slack):
    r = slack.client.post("/settings/slack", data={"url": "https://evil.example/hook"})
    assert r.status_code == 303 and "slack_error" in r.headers["location"]
    assert slack.db.get_client_slack("u1") is None
    r = slack.client.post("/settings/slack", data={"url": GOOD, "include_links": "1"})
    assert r.status_code == 303 and "slack_notice" in r.headers["location"]
    row = slack.db.get_client_slack("u1")
    assert row["webhook_url"] == GOOD and row["include_links"] == 1
    # only the checkbox changed: the saved URL is kept
    slack.client.post("/settings/slack", data={"url": ""})
    row = slack.db.get_client_slack("u1")
    assert row["webhook_url"] == GOOD and row["include_links"] == 0


def test_the_page_never_shows_the_full_url(slack):
    slack.client.post("/settings/slack", data={"url": GOOD})
    html = slack.client.get("/settings?tab=integrations").text
    assert "Connected:" in html and GOOD not in html and "xyz" in html          # only the last 4 characters


def test_test_message_and_disconnect(slack):
    assert "slack_error" in slack.client.post("/settings/slack/test").headers["location"]   # not connected yet
    slack.client.post("/settings/slack", data={"url": GOOD})
    r = slack.client.post("/settings/slack/test")
    assert "slack_notice" in r.headers["location"]
    assert slack.sent[-1]["url"] == GOOD and "connected" in slack.sent[-1]["json"]["text"]
    assert slack.sent[-1]["follow_redirects"] is False
    slack.client.post("/settings/slack/delete")
    assert slack.db.get_client_slack("u1") is None


def test_a_refusal_from_slack_is_reported(slack):
    slack.client.post("/settings/slack", data={"url": GOOD})
    slack.app.httpx.post = lambda url, **kw: SimpleNamespace(status_code=404, text="no_service")
    r = slack.client.post("/settings/slack/test")
    assert "slack_error" in r.headers["location"] and "no_service" in r.headers["location"]


def test_order_ready_posts_a_message_without_download_links_by_default(slack):
    order = _order(slack)
    slack.client.post("/settings/slack", data={"url": GOOD})
    _fire(slack, slack.db.get_order(order["id"]))
    assert len(slack.sent) == 1 and slack.sent[0]["url"] == GOOD
    msg = slack.sent[0]["json"]
    assert "clip.wav" in msg["text"] and msg["blocks"][0]["text"]["text"] == "Your Kauli order is ready"
    buttons = msg["blocks"][-1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Open order"]
    assert buttons[0]["url"] == f"https://kauli.test/client/orders/{order['id']}"
    assert any(b["type"] == "context" for b in msg["blocks"])                       # free-minutes note


def test_download_buttons_only_for_a_paid_ready_order_and_only_if_opted_in(slack):
    order = _order(slack)
    slack.db.set_order_free_preview(order["id"], False)
    slack.db.update_order_status(order["id"], "ready_for_delivery")
    slack.client.post("/settings/slack", data={"url": GOOD, "include_links": "1"})
    _fire(slack, slack.db.get_order(order["id"]))
    labels = [b["text"]["text"] for b in slack.sent[-1]["json"]["blocks"][-1]["elements"]]
    assert labels[0] == "Open order" and "Transcript (TXT)" in labels and "Transcript (Word)" in labels
    urls = [b["url"] for b in slack.sent[-1]["json"]["blocks"][-1]["elements"]]
    assert all("sig=" in u for u in urls[1:])                                       # signed, expiring links
    slack.client.post("/settings/slack", data={"url": "", "include_links": ""})
    _fire(slack, slack.db.get_order(order["id"]))
    assert [b["text"]["text"] for b in slack.sent[-1]["json"]["blocks"][-1]["elements"]] == ["Open order"]


def test_file_names_cannot_inject_slack_markup(slack):
    order = _order(slack)
    slack.db.get_conn().close()
    conn = slack.db.get_conn()
    conn.execute("UPDATE orders SET original_filename = ? WHERE id = ?", ("<!channel> a&b <https://x|click>", order["id"]))
    conn.commit()
    slack.client.post("/settings/slack", data={"url": GOOD})
    _fire(slack, slack.db.get_order(order["id"]))
    fields = slack.sent[-1]["json"]["blocks"][1]["fields"][0]["text"]
    assert "<!channel>" not in fields and "&lt;!channel&gt;" in fields and "a&amp;b" in fields


def test_a_slack_outage_never_breaks_delivery_and_is_logged(slack):
    order = _order(slack)
    slack.client.post("/settings/slack", data={"url": GOOD})

    def boom(url, **kw):
        raise httpx.ConnectError("slack unreachable")

    slack.app.httpx.post = boom
    _fire(slack, slack.db.get_order(order["id"]))                                   # must not raise
    log = slack.db.list_webhook_deliveries("u1", limit=5)
    assert log[0]["event"] == "slack.order.ready" and not log[0]["ok"]


def test_no_message_when_slack_is_not_connected(slack):
    order = _order(slack)
    _fire(slack, slack.db.get_order(order["id"]))
    assert slack.sent == []
