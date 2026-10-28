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
    assert "Order summary" in page and "Total due" in page and "$1.20" in page and "Human-reviewed" in page
    assert "Processing starts the moment this is confirmed." in page
    for promise in ("Card details go straight to Paystack", "No hidden fees", "Your files stay private"):
        assert promise in page
    assert re.search(r'<button type="submit" id="pay-submit" class="pay-btn">.*Pay \$1\.20</button>', page, re.S)
    if os.environ.get("PAY_DUMP_PATH"):
        Path(os.environ["PAY_DUMP_PATH"]).write_text(page, encoding="utf-8")


def test_payment_methods_are_radio_cards_and_mobile_money_is_only_offered_in_east_africa(env, wav_bytes):
    env.db.set_billing_market("u1", "intl")
    _, intl = _pay_page(env, wav_bytes)
    assert 'type="radio" name="provider" value="paystack"' in intl and 'value="bank"' in intl
    assert "M-Pesa" not in intl and "Airtel" not in intl and "KES" not in intl
    env.db.set_billing_market("u1", "ea")
    _, ea = _pay_page(env, wav_bytes)
    assert "Card, M-Pesa or Airtel Money" in ea and 'value="mpesa"' in ea
