"""The order payment page: clear summary, radio-card payment methods, safety promises."""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _pay_page(env, wav, **overrides):
    order = env.order_from(env.place(wav, review_tier="human", **overrides))
    return order, env.client.get(f"/client/orders/{order['id']}/pay").text


def test_payment_page_shows_the_order_the_price_and_why_it_is_safe(env, wav_bytes):
    order, page = _pay_page(env, wav_bytes)
    assert 'class="pay-file"' in page and "clip.wav" in page
    assert "Order summary" in page and "Total due" in page and "$1.79" in page and "Human-reviewed" in page
    assert "Processing starts the moment this is confirmed." in page
    for promise in ("Card details go straight to Paystack", "No hidden fees", "Your files stay private"):
        assert promise in page
    assert re.search(r'<button type="submit" id="pay-submit" class="pay-btn">.*Pay \$1\.79</button>', page, re.S)
    if os.environ.get("PAY_DUMP_PATH"):
        Path(os.environ["PAY_DUMP_PATH"]).write_text(page, encoding="utf-8")


def _set_created(env, ts):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET created_at = ? WHERE id = 'u1'", (ts,))
    conn.commit()
    conn.close()


def test_mobile_money_is_only_offered_in_kenya_and_to_the_older_kenyan_accounts(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    url = f"/client/orders/{order['id']}/pay"
    env.db.set_billing_market("u1", "intl")
    elsewhere = env.client.get(url, headers={"cf-ipcountry": "DE"}).text
    assert 'type="radio" name="provider" value="paystack"' in elsewhere
    assert "M-Pesa" not in elsewhere and "Airtel" not in elsewhere and 'value="mpesa"' not in elsewhere
    kenya = env.client.get(url, headers={"cf-ipcountry": "KE"}).text
    assert "Card, M-Pesa or Airtel Money" in kenya and 'value="mpesa"' in kenya
    env.db.set_billing_market("u1", "ea")                           # an East African account outside Kenya is not Kenya
    assert "M-Pesa" not in env.client.get(url, headers={"cf-ipcountry": "UG"}).text
    env.db.set_billing_market("u1", "legacy")                       # the original Kenyan clients keep it
    assert "M-Pesa" in env.client.get(url, headers={"cf-ipcountry": "DE"}).text


def test_bank_transfer_is_kept_for_existing_clients_and_hidden_for_new_ones(env, wav_bytes):
    from webapp import app as app_mod
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    url = f"/client/orders/{order['id']}/pay"
    _set_created(env, app_mod.BANK_TRANSFER_LEGACY_BEFORE - 86400)           # joined before the change
    assert 'value="bank"' in env.client.get(url).text
    _set_created(env, app_mod.BANK_TRANSFER_LEGACY_BEFORE + 86400)           # joined after it
    assert 'value="bank"' not in env.client.get(url).text and "Bank transfer" not in env.client.get(url).text
    billing = env.client.get("/client/billing").text
    assert 'value="bank"' not in billing and "Bank transfer</option>" not in billing


def test_a_new_client_cannot_pay_by_bank_or_mpesa_by_posting_directly(env, wav_bytes):
    from webapp import app as app_mod
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    _set_created(env, app_mod.BANK_TRANSFER_LEGACY_BEFORE + 86400)
    env.db.set_billing_market("u1", "intl")
    r = env.client.post(f"/client/orders/{order['id']}/pay", data={"provider": "bank"})
    assert r.status_code == 303 and "not+available" in r.headers["location"]
    r2 = env.client.post(f"/client/orders/{order['id']}/pay", data={"provider": "mpesa", "phone": "254700000000"}, headers={"cf-ipcountry": "DE"})
    assert r2.status_code == 303 and "only+available+in+Kenya" in r2.headers["location"]
    assert env.db.get_order(order["id"])["status"] == "pending_payment"
