"""Notion transcript export against a throwaway database and a fake Notion API.
Nothing here reaches Notion: httpx.request is replaced by a recorder."""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)
from webapp import notion_export  # noqa: E402

TOKEN = "ntn_" + "A1b2C3" * 5
PARENT = "8a1b2c3d4e5f6789abcdef0123456789"
LINK = f"https://www.notion.so/Workspace/Transcripts-{PARENT}?v=abc123"


def _resp(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {}, text="")


class FakeNotion:
    def __init__(self, kind="database", fail_create=False, bad_token=False):
        self.kind, self.fail_create, self.bad_token, self.calls = kind, fail_create, bad_token, []

    def __call__(self, method, url, headers=None, json=None, timeout=None, follow_redirects=None):
        path = url.replace("https://api.notion.com", "")
        self.calls.append(SimpleNamespace(method=method, path=path, headers=headers, json=json,
                                          follow_redirects=follow_redirects, url=url))
        if self.bad_token or headers["Authorization"] != f"Bearer {TOKEN}":
            return _resp(401)
        if path == "/v1/users/me":
            return _resp(200, {"name": "Kauli transcripts"})
        if path.startswith("/v1/databases/"):
            if self.kind == "database":
                return _resp(200, {"properties": {"Tags": {"type": "multi_select"}, "Doc name": {"type": "title"}}})
            return _resp(400, {"message": "is a page, not a database"})
        if method == "GET" and path.startswith("/v1/pages/"):
            return _resp(200, {}) if self.kind == "page" else _resp(404)
        if method == "POST" and path == "/v1/pages":
            return _resp(500) if self.fail_create else _resp(200, {"id": "page-1", "url": "https://notion.so/page-1"})
        if method == "PATCH":
            return _resp(200, {})
        return _resp(404)

    def created(self):
        return [c for c in self.calls if c.method == "POST" and c.path == "/v1/pages"]

    def appended(self):
        return [c for c in self.calls if c.method == "PATCH"]


# --------------------------------------------------------------- pure module
@pytest.mark.parametrize("link,expected", [
    (LINK, PARENT),
    (f"https://www.notion.so/{PARENT}", PARENT),
    (f"https://www.notion.so/My-Page-{PARENT}#section", PARENT),
    ("8a1b2c3d-4e5f-6789-abcd-ef0123456789", PARENT),
    (PARENT.upper(), PARENT),
    ("https://example.com/nothing-here", None),
    ("", None),
])
def test_parse_notion_id(link, expected):
    assert notion_export.parse_notion_id(link) == expected


def test_long_text_is_split_under_notions_limit_on_word_boundaries():
    text = ("word " * 1200).strip()
    pieces = notion_export._split_text(text)
    assert len(pieces) >= 3 and all(len(p) <= notion_export.MAX_TEXT for p in pieces)
    assert " ".join(pieces) == text                                   # nothing lost, no cut words
    assert len(notion_export._split_text("x" * 5000)) == 3            # an unbroken run is still cut


def test_blocks_carry_the_ai_note_and_never_exceed_text_limits():
    blocks = notion_export.build_blocks("a.wav | Transcription | English", "https://kauli.test/o/1",
                                        ["[00:00] " + "long " * 800, "short line"], ai_note=True)
    texts = [r["text"]["content"] for b in blocks for r in b.get(b["type"], {}).get("rich_text", [])]
    assert any("not reviewed by a person" in t for t in texts)
    assert all(len(t) <= 2000 for t in texts)
    assert blocks[0]["paragraph"]["rich_text"][1]["text"]["link"]["url"] == "https://kauli.test/o/1"
    assert not any("not reviewed" in t for b in notion_export.build_blocks("m", "u", ["x"], ai_note=False)
                   for t in [r["text"]["content"] for r in b.get(b["type"], {}).get("rich_text", [])])


def test_validate_finds_the_title_property_of_a_database_and_accepts_pages():
    info = notion_export.validate_connection(TOKEN, PARENT, http=FakeNotion("database"))
    assert info == {"kind": "database", "title_prop": "Doc name", "bot_name": "Kauli transcripts"}
    info = notion_export.validate_connection(TOKEN, PARENT, http=FakeNotion("page"))
    assert info["kind"] == "page" and info["title_prop"] == "title"


def test_validate_gives_actionable_errors():
    with pytest.raises(notion_export.NotionError, match="rejected that token"):
        notion_export.validate_connection(TOKEN, PARENT, http=FakeNotion(bad_token=True))

    def nothing_shared(method, url, headers=None, **kw):
        return _resp(200, {"name": "bot"}) if url.endswith("/users/me") else _resp(404)
    with pytest.raises(notion_export.NotionError, match="Connections"):
        notion_export.validate_connection(TOKEN, PARENT, http=nothing_shared)

    def offline(*a, **k):
        raise httpx.ConnectError("down")
    with pytest.raises(notion_export.NotionError, match="Could not reach Notion"):
        notion_export.validate_connection(TOKEN, PARENT, http=offline)


def test_create_page_sends_100_blocks_then_appends_the_rest_in_order():
    fake, naps = FakeNotion("page"), []
    blocks = notion_export.build_blocks("m", "https://k/o", [f"line {i}" for i in range(250)], ai_note=False)
    page = notion_export.create_page(TOKEN, PARENT, "page", "title", "Transcript - a.wav", blocks,
                                     http=fake, sleep=naps.append)
    assert page == {"id": "page-1", "url": "https://notion.so/page-1"}
    created = fake.created()[0].json
    assert created["parent"] == {"page_id": PARENT} and len(created["children"]) == 100
    assert created["properties"]["title"]["title"][0]["text"]["content"] == "Transcript - a.wav"
    sent = created["children"] + [b for c in fake.appended() for b in c.json["children"]]
    assert sent == blocks                                              # nothing lost, same order
    assert all(len(c.json["children"]) <= 100 for c in fake.appended()) and len(naps) == len(fake.appended())
    assert all(c.follow_redirects is False and c.url.startswith("https://api.notion.com/") for c in fake.calls)


def test_database_pages_use_the_databases_own_title_property():
    fake = FakeNotion("database")
    notion_export.create_page(TOKEN, PARENT, "database", "Doc name", "T", [], http=fake, sleep=lambda s: None)
    body = fake.created()[0].json
    assert body["parent"] == {"database_id": PARENT} and "Doc name" in body["properties"]


# ----------------------------------------------------------------- the app side
LINES = [f"Speaker line number {i} about the irrigation project." for i in range(6)]


@pytest.fixture
def notion(env, wav_bytes, monkeypatch):
    fake = FakeNotion("database")
    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(env.app, "_NOTION_RUN_IN_BACKGROUND", False)
    env.fake, env.wav = fake, wav_bytes
    env.set_lines = lambda lines: monkeypatch.setattr(env.app, "_load_job", lambda o: SimpleNamespace(segments=[
        SimpleNamespace(segment_type="speech", source_final_text=t, source_transcript=t, start_ms=i * 4000, speaker_id=None)
        for i, t in enumerate(lines)]))
    env.set_lines(LINES)
    return env


def _connect(env, **extra):
    return env.client.post("/settings/notion", data={"token": TOKEN, "url": LINK, "auto_send": "1", **extra})


def _ready(env):
    order = env.order_from(env.place(env.wav))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    env.db.set_order_free_preview(order["id"], False)
    return env.db.get_order(order["id"])


def _fire(env, order):
    env.app._fire_client_webhook(SimpleNamespace(base_url="https://kauli.test/"), order, "order.ready")


def test_saving_validates_the_token_and_the_link(notion):
    for data, text in (({"token": "short", "url": LINK}, "doesn%27t+look+like+a+Notion+integration"),
                       ({"token": TOKEN, "url": "https://example.com/x"}, "page+or+database+link"),
                       ({"token": "", "url": LINK}, "Paste+your+Notion+integration+secret"),
                       ({"token": TOKEN, "url": ""}, "Paste+the+link")):
        r = notion.client.post("/settings/notion", data=data)
        assert r.status_code == 303 and "notion_error" in r.headers["location"] and text in r.headers["location"], data
    assert notion.db.get_client_notion("u1") is None
    notion.fake.bad_token = True
    r = _connect(notion)
    assert "notion_error" in r.headers["location"] and "rejected+that+token" in r.headers["location"]
    assert notion.db.get_client_notion("u1") is None


def test_the_token_is_stored_encrypted_and_never_shown(notion):
    r = _connect(notion)
    assert "notion_notice" in r.headers["location"]
    row = notion.db.get_client_notion("u1")
    assert row["parent_kind"] == "database" and row["title_prop"] == "Doc name" and row["parent_id"] == PARENT
    assert TOKEN not in row["token_enc"] and row["token_hint"] == TOKEN[-4:]
    assert notion.app._notion_decrypt(row["token_enc"]) == TOKEN
    page = notion.client.get("/settings?tab=integrations").text
    assert TOKEN not in page and row["token_enc"] not in page and f"&hellip;{TOKEN[-4:]}" in page


def test_updating_without_repasting_keeps_the_saved_token(notion):
    _connect(notion)
    r = notion.client.post("/settings/notion", data={"token": "", "url": "", "auto_send": ""})
    assert "notion_notice" in r.headers["location"]
    row = notion.db.get_client_notion("u1")
    assert row["auto_send"] == 0 and notion.app._notion_decrypt(row["token_enc"]) == TOKEN


def test_disconnect(notion):
    _connect(notion)
    notion.client.post("/settings/notion/delete")
    assert notion.db.get_client_notion("u1") is None


def test_a_ready_order_becomes_a_notion_page_once(notion):
    _connect(notion)
    order = _ready(notion)
    _fire(notion, order)
    _fire(notion, order)                                              # a webhook retry or double approve
    assert len(notion.fake.created()) == 1
    body = notion.fake.created()[0].json
    assert body["parent"] == {"database_id": PARENT}
    assert body["properties"]["Doc name"]["title"][0]["text"]["content"] == "Transcript - clip.wav"
    texts = [r["text"]["content"] for b in body["children"] for r in b.get(b["type"], {}).get("rich_text", [])]
    assert "[00:00] Speaker line number 0 about the irrigation project." in texts
    assert any("not reviewed by a person" in t for t in texts)         # Instant AI stays labelled as such
    export = notion.db.get_notion_export(order["id"])
    assert export["page_url"] == "https://notion.so/page-1"
    assert notion.db.list_webhook_deliveries("u1", limit=3)[0]["event"] == "notion.order.ready"


def test_nothing_is_sent_before_a_plan_is_chosen(notion):
    _connect(notion)
    order = _ready(notion)
    conn = notion.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL WHERE id = 'u1'")
    conn.commit()
    _fire(notion, order)
    assert notion.fake.created() == []
    r = notion.client.post(f"/account/orders/{order['id']}/send-to-notion")
    assert "Unlock+this+order" in r.headers["location"] and notion.fake.created() == []


def test_auto_send_off_means_manual_only(notion):
    _connect(notion, auto_send="")
    order = _ready(notion)
    _fire(notion, order)
    assert notion.fake.created() == []
    page = notion.client.get(f"/account/orders/{order['id']}").text
    assert "Send to Notion" in page and "Open in Notion" not in page
    r = notion.client.post(f"/account/orders/{order['id']}/send-to-notion")
    assert r.status_code == 303 and "notice=Sent+to+Notion" in r.headers["location"]
    assert len(notion.fake.created()) == 1
    page = notion.client.get(f"/account/orders/{order['id']}").text
    assert "Open in Notion" in page and 'href="https://notion.so/page-1"' in page


def test_a_long_transcript_is_chunked(notion):
    _connect(notion)
    notion.set_lines([f"line {i}" for i in range(150)])
    order = _ready(notion)
    notion.app.notion_export.create_page.__defaults__  # (documented: sleeps 0.35s between chunks)
    _fire(notion, order)
    assert len(notion.fake.created()) == 1 and len(notion.fake.appended()) == 1
    assert len(notion.fake.created()[0].json["children"]) == 100


def test_a_notion_failure_is_reported_logged_and_can_be_retried(notion):
    _connect(notion)
    order = _ready(notion)
    notion.fake.fail_create = True
    _fire(notion, order)                                              # must not raise
    assert notion.db.get_notion_export(order["id"]) is None            # the claim was released
    assert not notion.db.list_webhook_deliveries("u1", limit=3)[0]["ok"]
    r = notion.client.post(f"/account/orders/{order['id']}/send-to-notion")
    assert "error=" in r.headers["location"]
    notion.fake.fail_create = False
    r = notion.client.post(f"/account/orders/{order['id']}/send-to-notion")
    assert "notice=Sent+to+Notion" in r.headers["location"]


def test_an_unreadable_saved_token_asks_to_reconnect(notion, monkeypatch):
    _connect(notion)
    order = _ready(notion)
    monkeypatch.setattr(notion.app, "_session_secret", "a-different-secret")
    r = notion.client.post(f"/account/orders/{order['id']}/send-to-notion")
    assert "Reconnect+Notion" in r.headers["location"] and notion.fake.created() == []


def test_unknown_orders_are_not_found(notion):
    _connect(notion)
    assert notion.client.post("/account/orders/doesnotexist/send-to-notion").status_code == 404


def test_a_stuck_claim_can_be_retaken_but_a_fresh_one_cannot(notion):
    assert notion.db.claim_notion_export("o1", "u1") is True
    assert notion.db.claim_notion_export("o1", "u1") is False          # in flight
    assert notion.db.claim_notion_export("o1", "u1", stale_after_s=-1) is True   # an old, unfinished claim
    notion.db.finish_notion_export("o1", "p", "https://notion.so/p")
    assert notion.db.claim_notion_export("o1", "u1", stale_after_s=-1) is False  # finished: never again
