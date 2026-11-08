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
    page = env.client.get(f"/account/orders/{order['id']}").text
    assert f"/account/orders/{order['id']}/more" in page and "Do more with this file" in page
    more = env.client.get(f"/account/orders/{order['id']}/more")
    assert more.status_code == 200
    for text in ("Translate it", "Dub it", 'name="reuse_order_id"', "Your plan", "Pay as you go"):
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
    r = env.client.post("/account/orders", data={
        "reuse_order_id": original["id"], "source_lang": "en", "target_lang": "sw", "service_level": "translate", "review_tier": "ai",
        "idempotency_key": "more-test-1"}, follow_redirects=False)
    new = env.order_from(r)
    assert new["id"] != original["id"] and new["service_level"] == "translate" and new["target_lang"] == "sw"
    assert new["original_filename"] == original["original_filename"]
    assert Path(new["audio_path"]).is_file() and Path(new["audio_path"]).read_bytes() == Path(original["audio_path"]).read_bytes()
    assert str(Path(new["audio_path"]).parent) != str(Path(original["audio_path"]).parent)    # its own folder, so nothing is shared by accident
    again = env.client.post("/account/orders", data={
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
    assert env.client.get(f"/account/orders/{original['id']}/more").status_code == 404
    r = env.client.post("/account/orders", data={"reuse_order_id": original["id"], "source_lang": "en", "target_lang": "en",
                                                "service_level": "transcribe", "review_tier": "ai"})
    assert "could not find that earlier order" in r.text


def test_a_file_that_is_gone_says_so_instead_of_failing(env, wav_bytes):
    original = _first(env, wav_bytes)
    os.remove(original["audio_path"])
    assert env.client.get(f"/account/orders/{original['id']}/more", follow_redirects=False).status_code == 303
    r = env.client.post("/account/orders", data={"reuse_order_id": original["id"], "source_lang": "en", "target_lang": "en",
                                                "service_level": "transcribe", "review_tier": "ai"})
    assert "no longer stored" in r.text


def _long(env, order, minutes):
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET duration_minutes = ? WHERE id = ?", (minutes, order["id"]))
    conn.commit()
    conn.close()
    return env.db.get_order(order["id"])


def _cards(env, order):
    return {c["key"]: c for c in repurpose.suggestions(order, {**dict(env.db.get_user("u1")), "client_scope_id": "u1"})}


def test_a_customer_on_pay_as_you_go_sees_the_full_rate_and_where_a_plan_would_cover_it(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    order = _long(env, _first(env, wav_bytes), 20)
    card = _cards(env, order)["translate"]
    q = card["quote"]
    assert not card["included"] and q["usd"] == round(20 * q["rate"], 2) and q["included_minutes"] == 0       # free minutes never pay for a translation
    assert q["hint"] and q["hint"]["plan"] == "Pro"                                                              # Pro would include it
    page = env.client.get(f"/account/orders/{order['id']}/more").text
    assert f"${q['usd']:.2f}" in page and "On Pro" in page and "20.0 min at $" in page


def test_a_paid_plan_covers_translation_from_its_included_minutes_and_only_part_when_they_run_low(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    env.db.set_subscription_plan("u1", "pro", 30)
    order = _long(env, _first(env, wav_bytes), 20)
    card = _cards(env, order)["translate"]
    assert card["included"] and card["usd"] == 0 and card["quote"]["included_minutes"] == 20
    page = env.client.get(f"/account/orders/{order['id']}/more").text
    assert "Included in your plan" in page and "Uses 20.0 of your included minutes" in page
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET minutes_used_this_period = ? WHERE user_id = 'u1'", (env.app.billing.plan_included_minutes("pro") - 12,))
    conn.commit()
    conn.close()
    q = _cards(env, order)["translate"]["quote"]
    assert q["included_minutes"] == 12 and q["billable_minutes"] == 8 and q["usd"] == round(8 * q["rate"], 2)      # 12 included, the other 8 at the rate
    assert "12.0 min included in your Pro plan" in env.client.get(f"/account/orders/{order['id']}/more").text


def test_a_dub_is_always_priced_on_its_own_and_prepaid_credit_comes_off_it(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    env.db.set_subscription_plan("u1", "premium", 30)
    order = _long(env, _first(env, wav_bytes), 10)
    q = _cards(env, order)["dub"]["quote"]
    assert q["included_minutes"] == 0 and q["usd"] == round(10 * q["rate"], 2)
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET wallet_credits = 100 WHERE user_id = 'u1'")
    conn.commit()
    conn.close()
    q2 = _cards(env, order)["dub"]["quote"]
    assert q2["credits_usd"] > 0 and q2["usd"] < q["usd"]
