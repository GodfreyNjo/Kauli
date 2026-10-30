"""Do more with a file the customer already sent: a new order from the stored upload, with no second upload."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import repurpose  # noqa: E402


def _ready(env, order_id):
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'ready_for_delivery' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


def _first(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    _ready(env, order["id"])
    return env.db.get_order(order["id"])


def test_a_ready_order_offers_more_without_an_upload(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    order = _first(env, wav_bytes)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert f"/client/orders/{order['id']}/more" in page and "Do more with this file" in page
    more = env.client.get(f"/client/orders/{order['id']}/more")
    assert more.status_code == 200
    for text in ("Translate it", "Dub it", 'name="reuse_order_id"', "About $"):
        assert text in more.text, text
    assert 'name="audio"' not in more.text                                                    # nothing to upload


def test_the_customers_usual_languages_come_first(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    for _ in range(2):
        o = env.order_from(env.place(wav_bytes, service_level="translate", target_lang="sw"))
        _ready(env, o["id"])
    order = _first(env, wav_bytes)
    cards = repurpose.suggestions(order, {**dict(env.db.get_user("u1")), "client_scope_id": "u1"})
    translate = next(c for c in cards if c["key"] == "translate")
    assert translate["languages"][0][0] == "sw"


def test_a_translation_is_made_from_the_stored_file_not_a_new_upload(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    original = _first(env, wav_bytes)
    r = env.client.post("/client/orders", data={
        "reuse_order_id": original["id"], "source_lang": "en", "target_lang": "sw", "service_level": "translate", "review_tier": "ai",
        "idempotency_key": "more-test-1"}, follow_redirects=False)
    new = env.order_from(r)
    assert new["id"] != original["id"] and new["service_level"] == "translate" and new["target_lang"] == "sw"
    assert new["original_filename"] == original["original_filename"]
    assert Path(new["audio_path"]).is_file() and Path(new["audio_path"]).read_bytes() == Path(original["audio_path"]).read_bytes()
    assert str(Path(new["audio_path"]).parent) != str(Path(original["audio_path"]).parent)    # its own folder, so nothing is shared by accident
    again = env.client.post("/client/orders", data={
        "reuse_order_id": original["id"], "source_lang": "en", "target_lang": "sw", "service_level": "translate", "review_tier": "ai",
        "idempotency_key": "more-test-1"}, follow_redirects=False)
    assert again.headers["location"].endswith(new["id"])                                       # a double click does not order twice


def test_one_customer_cannot_reuse_another_customers_file(env, wav_bytes):
    original = _first(env, wav_bytes)
    env.db.get_or_create_user("other", "other@example.com", "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET client_id = 'other' WHERE id = ?", (original["id"],))
    conn.commit()
    conn.close()
    assert env.client.get(f"/client/orders/{original['id']}/more").status_code == 404
    r = env.client.post("/client/orders", data={"reuse_order_id": original["id"], "source_lang": "en", "target_lang": "en",
                                                "service_level": "transcribe", "review_tier": "ai"})
    assert "could not find that earlier order" in r.text


def test_a_file_that_is_gone_says_so_instead_of_failing(env, wav_bytes):
    original = _first(env, wav_bytes)
    os.remove(original["audio_path"])
    assert env.client.get(f"/client/orders/{original['id']}/more", follow_redirects=False).status_code == 303
    r = env.client.post("/client/orders", data={"reuse_order_id": original["id"], "source_lang": "en", "target_lang": "en",
                                                "service_level": "transcribe", "review_tier": "ai"})
    assert "no longer stored" in r.text
