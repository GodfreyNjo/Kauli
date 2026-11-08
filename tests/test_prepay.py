"""Pay first, then upload: a large Instant AI order that needs payment is paid for before the file goes up, the price is explained in plain words,
free-minute notices only follow real work, and anyone who has paid can download and edit."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _unchoose(env):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    conn.close()


def _quote(env, minutes, **form):
    data = {"minutes": str(minutes), "service_level": "transcribe", "review_tier": "ai", "source_lang": "sw", "target_lang": "same", "name": "Site 1 Women_mixdown.mp3"}
    data.update(form)
    return env.client.post("/client/orders/quote", data=data).json()


def test_the_price_is_the_length_minus_the_free_minutes_times_the_rate_and_it_is_said_in_words(env):
    q = _quote(env, 191.7532)
    assert q["can_prepay"] and q["needs_payment"] and q["total_usd"] == 19.41 and q["charge_usd"] == 19.41
    assert q["explain"] == "191.8 min - 30.0 free = 161.8 min x $0.12 = $19.41"           # 191.75 - 30 = 161.75 minutes, at $0.12
    assert q["prepay_url"] == "/client/pay-first"
    assert abs(q["credits"] - 194.1) < 1e-6                                                    # $19.41 of prepaid credits, to the cent
    small = _quote(env, 25)
    assert small["can_prepay"] and small["needs_payment"] is False and "prepay_url" not in small   # inside the free minutes: upload and process as usual


def test_only_plain_instant_ai_orders_are_paid_first(env):
    assert _quote(env, 191, review_tier="human")["can_prepay"] is False
    assert _quote(env, 191, service_level="dub", target_lang="en")["can_prepay"] is False
    assert _quote(env, 191, addon_video_deliverables="1")["can_prepay"] is False
    assert _quote(env, 191, rush="1")["can_prepay"] is False
    assert _quote(env, 0)["can_prepay"] is False                                               # the length could not be read: carry on as before
    translated = _quote(env, 100, service_level="transcribe", target_lang="en")
    assert translated["can_prepay"] and translated["service_level"] == "translate"             # a different target language makes it a translation


def test_a_new_customers_free_first_order_is_not_asked_for_money(env):
    _unchoose(env)                                                                              # nobody has chosen a plan and there is no earlier order
    assert _quote(env, 80)["needs_payment"] is False                                           # the first Instant AI order is processed free, as create_order does
    assert _quote(env, 300)["needs_payment"] is True                                           # but a very long file is not


def test_the_pay_first_page_checkout_and_the_way_back(env, monkeypatch):
    assert env.client.get("/client/pay-first").status_code == 303                          # nothing to pay first: back to the order form
    _quote(env, 191.7532)
    page = env.client.get("/client/pay-first")
    assert page.status_code == 200 and "Pay first, then we upload" in page.text and "$19.41" in page.text
    assert "191.8 min - 30.0 free = 161.8 min x $0.12 = $19.41" in page.text and "not uploaded until your payment goes through" in page.text
    monkeypatch.setattr(env.app.billing, "paystack_configured", lambda: True)
    monkeypatch.setattr(env.app.billing, "paystack_initialize", lambda *a, **k: {"authorization_url": "https://paystack.test/pay/abc"})
    go = env.client.post("/client/pay-first", data={"provider": "paystack"})
    assert go.status_code == 303 and go.headers["location"] == "https://paystack.test/pay/abc"
    conn = env.db.get_conn()
    pay = conn.execute("SELECT * FROM payments WHERE user_id = 'u1' ORDER BY created_at DESC LIMIT 1").fetchone()
    conn.close()
    meta = json.loads(pay["meta"])
    assert abs(pay["amount_usd"] - 19.41) < 1e-9 and meta["kind"].startswith("credits_topup:194.1") and meta["return_to"] == "/client?prepaid=1" and pay["order_id"] is None
    # Paystack sends the customer back: the payment is confirmed and they land on the order form, where the file goes up
    monkeypatch.setattr(env.app.billing, "paystack_verify", lambda pid: {"status": "success", "reference": "ref-1"})
    back = env.client.get(f"/billing/callback/paystack?payment_id={pay['id']}")
    assert back.status_code == 303 and back.headers["location"].startswith("/client?prepaid=1&notice=")
    assert abs(env.db.wallet_credits_remaining("u1") - 194.1) < 1e-6
    # The credits now cover the order, so the order form uploads it and it starts at once
    assert _quote(env, 191.7532)["needs_payment"] is False
    assert env.client.get("/client/pay-first/status").json() == {"covered": True}
    cost = env.app.billing.order_cost_usd(191.7532, "transcribe", "free", 30.0, wallet_credits_available=194.1, market="intl", review_tier="ai")
    assert cost["total_usd"] == 0.0 and cost["credits_applied_usd"] == 19.41


def test_a_failed_payment_goes_back_to_the_pay_first_page_not_the_order_form(env, monkeypatch):
    _quote(env, 191.7532)
    monkeypatch.setattr(env.app.billing, "paystack_configured", lambda: True)
    monkeypatch.setattr(env.app.billing, "paystack_initialize", lambda *a, **k: {"authorization_url": "https://paystack.test/pay/abc"})
    env.client.post("/client/pay-first", data={"provider": "paystack"})
    conn = env.db.get_conn()
    pid = conn.execute("SELECT id FROM payments WHERE user_id = 'u1' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    conn.close()
    monkeypatch.setattr(env.app.billing, "paystack_verify", lambda p: {"status": "failed"})
    back = env.client.get(f"/billing/callback/paystack?payment_id={pid}")
    assert back.status_code == 303 and back.headers["location"].startswith("/client/pay-first?error=")


def test_the_pay_page_for_an_order_shows_how_the_price_is_worked_out(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 191.7532)
    order = env.order_from(env.place(wav_bytes))
    assert order["status"] == "pending_payment" and abs(order["cost_usd"] - 19.41) < 1e-9
    page = env.client.get(f"/client/orders/{order['id']}/pay").text
    assert "How this price is worked out" in page and "Your free minutes cover 30.0 of them" in page and "191.8 min - 30.0 free = 161.8 min x $0.12 = $19.41" in page


def test_nobody_is_told_their_free_minutes_are_gone_while_an_order_waits_for_payment(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 191.7532)
    order = env.order_from(env.place(wav_bytes))
    assert order["status"] == "pending_payment" and not env.submitted                          # not started, nothing processed
    kinds = [n["kind"] for n in env.db.list_recent_notifications("u1", 50)]
    assert not any(k.startswith("usage_") for k in kinds)                                       # so no "0 free minutes left"
    env.db.create_payment("pay-1", "u1", "free", order["cost_usd"], None, "USD", "paystack", meta='{"kind": "order"}', order_id=order["id"])
    assert env.app._activate_payment(env.db.get_payment("pay-1"), "ref-1", base_url="http://test/")
    assert env.submitted == [order["id"]]                                                       # paid: the file is processed
    kinds = [n["kind"] for n in env.db.list_recent_notifications("u1", 50)]
    assert any(k.startswith("usage_") for k in kinds)                                           # and now the free minutes really have been used


def test_anyone_who_has_paid_can_download_and_edit_everything(env, wav_bytes, monkeypatch):
    # an order processed with free minutes is a preview, locked until the customer pays
    first = env.order_from(env.place(wav_bytes))
    assert first["is_free_preview"] == 1 and env.app._order_locked(env.db.get_order(first["id"]))
    # any real payment (here a wallet top-up) makes them a paying customer: that order, and the next ones, open
    env.db.create_payment("pay-top", "u1", "wallet", 5.0, None, "USD", "paystack", meta='{"kind": "credits_topup:50"}')
    assert env.app._activate_payment(env.db.get_payment("pay-top"), "ref-top", base_url="http://test/")
    assert env.db.client_has_paid("u1")
    assert not env.app._order_locked(env.db.get_order(first["id"]))
    second = env.order_from(env.place(wav_bytes))
    assert second["is_free_preview"] == 0 and not env.app._order_locked(env.db.get_order(second["id"]))


def test_the_one_time_migration_opens_the_old_previews_of_customers_who_already_paid(env, wav_bytes):
    first = env.order_from(env.place(wav_bytes))
    assert first["is_free_preview"] == 1
    conn = env.db.get_conn()
    conn.execute("INSERT INTO payments (id, user_id, plan, amount_usd, currency, provider, status, created_at) VALUES ('old', 'u1', 'free', 4, 'USD', 'paystack', 'completed', 1)")
    conn.execute("DELETE FROM app_flags WHERE key = 'migrated_payers_unlocked_2026_11'")
    conn.commit()
    conn.close()
    env.db.init_db()
    assert env.db.get_order(first["id"])["is_free_preview"] == 0
