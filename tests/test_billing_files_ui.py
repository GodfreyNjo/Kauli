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


def test_the_order_page_top_folds_away_once_the_order_is_ready(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    html = env.client.get(f"/client/orders/{order['id']}").text
    top = html.split('<details class="od-details"')[1].split("</summary>")[0]
    assert "open" not in top.split(">")[0] and "Order details" in top                         # folded by default when ready
    assert html.index("<h1>") < html.index('class="od-details"') and "Your order is ready" in html.lower() or "ready" in html.lower()
    inside = html.split('class="od-details"')[1]
    assert "order-progress" in inside and "Translate this into another language" in inside and "Your formatting instructions" in inside
    env.db.update_order_status(order["id"], "processing")
    assert '<details class="od-details" open>' in env.client.get(f"/client/orders/{order['id']}").text   # still running: progress stays in view


def test_the_player_and_transcript_come_right_after_the_title_and_captions_can_be_switched_off():
    src = (Path(__file__).parent.parent / "webapp" / "templates" / "order_detail.html").read_text(encoding="utf-8")
    assert src.index("<h1>") < src.index('class="order-workspace-grid"') < src.index('class="od-details"') < src.index('class="downloads"')
    assert 'id="order-cc-toggle"' in src and 'localStorage.getItem("kauliCaptions")' in src and "if (!ccOn)" in src


def test_the_messages_tab_is_gone_and_old_links_land_on_the_dashboard(env):
    page = env.client.get("/client/home").text
    assert 'href="/client/messages"' not in page and 'id="as-open"' in page            # the assistant's inbox is where replies arrive
    r = env.client.get("/client/messages", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/client"
