"""A failed subscription payment: the plan stays open, the customer is told kindly, can fix it in one click or step down, and nothing is deleted."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

DAY = 86400


def _subscribed(env, uid="u1", paid_payments=2):
    """A Pro plan whose period has ended and whose renewal is the one failing."""
    env.db.set_subscription_plan(uid, "pro", 30)
    env.db.set_paystack_subscription(uid, "SUB_1", "tok_1")
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET current_period_end = ? WHERE user_id = ?", (time.time() - 10 * DAY, uid))
    for i in range(paid_payments):
        conn.execute("INSERT INTO payments (id, user_id, plan, amount_usd, currency, provider, status, created_at, completed_at) "
                     "VALUES (?, ?, 'pro', 29, 'USD', 'paystack', 'completed', ?, ?)", (f"pp{i}", uid, time.time() - 50 * DAY, time.time() - 50 * DAY))
    conn.commit()
    conn.close()


def _plan(env, uid="u1"):
    return env.app.billing.effective_plan(env.db.get_user(uid), env.db.get_subscription_current(uid))


def _mail(env, monkeypatch):
    sent = []
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("BREVO_FROM_EMAIL", "ops@example.com")
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text, **kw: sent.append((to, subject, html, text)) or (True, "ok"))
    return sent


def _webhook(env, monkeypatch, event, email):
    monkeypatch.setattr(env.app.billing, "paystack_verify_webhook_signature", lambda raw, sig: True)
    body = {"event": event, "data": {"customer": {"email": email}, "gateway_response": "Insufficient Funds"}}
    return env.client.post("/webhooks/paystack", content=json.dumps(body), headers={"x-paystack-signature": "x"})


def test_a_failed_renewal_keeps_the_plan_open_and_sends_a_kind_email(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    _subscribed(env)
    assert _plan(env) == "free"                                                  # without the grace period the lapsed plan would drop now
    email = env.db.get_user("u1")["email"]
    assert _webhook(env, monkeypatch, "invoice.payment_failed", email).status_code == 200
    assert _plan(env) == "pro"                                                   # the plan stays fully open
    mails = [m for m in sent if m[1] == "Your Kauli payment did not go through"]
    assert len(mails) == 1 and mails[0][0] == email
    for part in mails[0][2:]:
        assert "Kauli Team" in part and "Godfrey" not in part and "/client/billing/update-card" in part
        assert "suspend" not in part.lower() and "off" not in part.lower().split("%")[0][-3:]    # kind wording, no discount bait
    assert "happens all the time" in mails[0][3]


def test_the_banner_and_billing_page_offer_the_ways_out(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    assert "We had trouble processing your latest payment" not in env.client.get("/client/home").text
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    home = env.client.get("/client/home").text
    assert "We had trouble processing your latest payment" in home and "/client/billing/update-card" in home and "Your plan is still active" in home and "by " not in home.split("We had trouble")[1].split("Update payment method")[0]
    billing = env.client.get("/client/billing").text
    assert "Your last payment did not go through" in billing and "Switch to Pay as you go" in billing and "more days" not in billing


def test_update_card_goes_to_a_secure_paystack_form(env, monkeypatch):
    _subscribed(env)
    monkeypatch.setattr(env.app.billing, "paystack_manage_link", lambda code: f"https://paystack.test/manage/{code}")
    r = env.client.get("/client/billing/update-card", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://paystack.test/manage/SUB_1"
    monkeypatch.setattr(env.app.billing, "paystack_manage_link", lambda code: None)
    assert "error=" in env.client.get("/client/billing/update-card", follow_redirects=False).headers["location"]


def test_a_paid_renewal_ends_the_recovery(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    env.db.create_payment("pay-ok", "u1", "pro", 29, None, "USD", "paystack", meta=json.dumps({"kind": "subscription_renewal"}))
    assert env.app._activate_payment(env.db.get_payment("pay-ok"), "ref-1", base_url="https://kauli.test")
    assert env.app.dunning._open("u1") is None
    assert "We had trouble processing" not in env.client.get("/client/home").text


def test_switching_to_pay_as_you_go_stops_the_renewal_and_the_banner(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    calls = []
    monkeypatch.setattr(env.app.billing, "paystack_disable_subscription", lambda code, token: calls.append(code) or {"ok": True})
    r = env.client.post("/client/billing/switch-to-payg", follow_redirects=False)
    assert r.status_code == 303 and calls == ["SUB_1"]
    assert env.app.dunning._open("u1") is None and env.db.get_subscription("u1")["paystack_subscription_code"] is None
    assert env.db.get_user("u1")["account_status"] != "closed"


def test_a_second_failed_retry_sends_one_more_gentle_email_and_only_one(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    _subscribed(env)
    email = env.db.get_user("u1")["email"]
    for _ in range(4):
        _webhook(env, monkeypatch, "invoice.payment_failed", email)
    again = [m for m in sent if m[1] == "We tried your payment again"]
    assert len(again) == 1 and "Pay as you go" in again[0][3] and "Kauli Team" in again[0][3]
    assert env.app.dunning._open("u1")["attempts"] == 4


def test_nothing_waits_on_a_timer_of_ours(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    d = env.app.dunning
    assert not hasattr(d, "sweep_once") and not hasattr(d, "GRACE_DAYS") and not hasattr(d, "FINAL_DAY")
    assert _plan(env) == "pro"                                                          # held open until Paystack settles it, however long that takes
    conn = env.db.get_conn()
    conn.execute("UPDATE payment_failures SET first_failed_at = ?", (time.time() - 20 * DAY,))
    conn.commit()
    conn.close()
    assert d.expire_backstop() == 0 and d._open("u1") is not None                      # still open at three weeks
    conn = env.db.get_conn()
    conn.execute("UPDATE payment_failures SET first_failed_at = ?", (time.time() - 50 * DAY,))
    conn.commit()
    conn.close()
    assert d.expire_backstop() == 1 and d._open("u1") is None                           # only a safety net if Paystack never reports back


def test_paystack_giving_up_on_a_failing_card_pauses_kindly(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    monkeypatch.setattr(env.app.billing, "paystack_disable_subscription", lambda code, token: {"ok": True})
    _subscribed(env)
    email = env.db.get_user("u1")["email"]
    _webhook(env, monkeypatch, "invoice.payment_failed", email)
    _webhook(env, monkeypatch, "subscription.not_renew", email)
    assert any(m[1] == "Your Kauli plan is paused, and your files are safe" for m in sent)
    assert env.app.dunning._open("u1") is None


def test_staff_can_see_the_failed_payments(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    assert env.client.get("/staff/payment-failures", follow_redirects=False).status_code == 303
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/payment-failures").text
    assert env.db.get_user("u1")["email"] in page and "Waiting for payment" in page


def test_the_payment_problem_card_shows_even_with_no_payment_history(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env, paid_payments=0)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    page = env.client.get("/client/billing").text
    assert "Your last payment did not go through" in page and "Switch to Pay as you go" in page


def test_closing_a_failing_plan_is_still_described_as_a_paid_plan(env, monkeypatch):
    _mail(env, monkeypatch)
    _subscribed(env)
    _webhook(env, monkeypatch, "invoice.payment_failed", env.db.get_user("u1")["email"])
    page = env.client.get("/client/close-account").text
    assert "already on Pay as you go" not in page and "stops renewing straight away" in page
