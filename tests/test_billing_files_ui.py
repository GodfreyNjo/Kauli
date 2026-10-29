"""The billing page has no prepaid-credit shop and folds its payment history away; the files page folds each order's files away."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def test_billing_has_no_prepaid_credit_section_and_a_balance_only_when_there_is_one(env):
    html = env.client.get("/client/billing").text
    assert "Prepaid credits" not in html and "wallet-custom" not in html and "/client/billing/wallet" not in html
    assert "Credit balance" not in html                                                   # nothing to show at zero
    env.db.add_wallet_credits("u1", 40)
    html = env.client.get("/client/billing").text
    assert "Credit balance" in html and "Prepaid credits" not in html


def test_payment_history_folds_away(env, wav_bytes):
    env.place(wav_bytes)
    env.db.create_payment("p1", "u1", "pro", 6.72, None, "USD", "paystack", order_id=None)
    html = env.client.get("/client/billing").text
    assert '<details class="card collapsible-card" id="payment-history">' in html and "Payment history" in html


def test_each_orders_files_fold_away_and_the_first_is_open(env, wav_bytes):
    env.place(wav_bytes)
    env.place(wav_bytes)
    html = env.client.get("/client/files").text
    assert html.count('<details class="card collapsible-card"') == 2 and html.count("collapsible-card\" open") == 1
    assert "Open this order" in html
