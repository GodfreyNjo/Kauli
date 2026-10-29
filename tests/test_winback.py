"""Winning back customers who paid and went quiet: a unique single-use coupon code for one free month of their subscription."""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

DAY = 86400


def _quiet(env, uid, days, paid=True):
    """Make an account look like it was last active `days` ago, optionally with a completed payment."""
    then = time.time() - days * DAY
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET created_at = ?, last_seen_at = ? WHERE id = ?", (then - 100 * DAY, then, uid))
    conn.execute("DELETE FROM orders WHERE client_id = ?", (uid,))
    if paid:
        conn.execute("INSERT INTO payments (id, user_id, plan, amount_usd, currency, provider, status, created_at, completed_at) "
                     "VALUES (?, ?, 'pro', 29, 'USD', 'paystack', 'completed', ?, ?)", (f"p-{uid}", uid, then, then))
    conn.commit()
    conn.close()


def _client(env, uid):
    env.db.get_or_create_user(uid, f"{uid}@example.com", "client")


def _who(env):
    return {r["user"]["id"]: r["segment"] for r in env.app.winback.eligible()}


def _plan(env, uid):
    return env.app.billing.effective_plan(env.db.get_user(uid), env.db.get_subscription_current(uid))


def test_only_people_who_paid_and_went_quiet_qualify(env):
    for uid in ("free1", "paid1", "recent"):
        _client(env, uid)
    _quiet(env, "u1", 40, paid=False)                       # free minutes only, long silent
    _quiet(env, "free1", 90, paid=False)
    _quiet(env, "paid1", 40)                                # paid, silent
    _quiet(env, "recent", 5)                                # paid, but active last week
    assert _who(env) == {"paid1": "went_quiet"}


def test_a_lapsed_subscription_is_the_stopped_paying_group(env):
    _client(env, "lapsed")
    _quiet(env, "lapsed", 60)
    env.db.set_subscription_plan("lapsed", "pro", 30)
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET current_period_end = ? WHERE user_id = 'lapsed'", (time.time() - 30 * DAY,))
    conn.commit()
    conn.close()
    assert _who(env) == {"lapsed": "stopped_paying"}


def test_someone_still_paying_every_month_is_not_offered_a_free_one(env):
    _client(env, "payer")
    _quiet(env, "payer", 60)
    env.db.set_subscription_plan("payer", "pro", 30)
    env.db.set_paystack_subscription("payer", "SUB_9", "tok")
    assert "payer" not in _who(env)


def test_unsubscribed_and_closed_accounts_are_skipped(env):
    for uid in ("unsub", "closed", "fine"):
        _client(env, uid)
        _quiet(env, uid, 50)
    env.db.set_marketing_consent("unsub", False, None)
    env.db.close_account("closed")
    assert _who(env) == {"fine": "went_quiet"}


def test_team_members_are_not_offered_the_owners_deal(env):
    _client(env, "member")
    _quiet(env, "member", 50)
    conn = env.db.get_conn()
    conn.execute("INSERT INTO team_members (id, owner_client_id, member_user_id, invited_email, status, invited_at) "
                 "VALUES ('t1', 'u1', 'member', 'member@example.com', 'accepted', ?)", (time.time(),))
    conn.commit()
    conn.close()
    assert "member" not in _who(env)


def test_everyone_quiet_for_over_a_year_is_left_alone(env):
    _client(env, "old")
    _quiet(env, "old", 500)
    assert "old" not in _who(env)


def test_the_code_is_unique_tied_to_the_email_and_never_repeats(env, monkeypatch):
    sent = []
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("BREVO_FROM_EMAIL", "ops@example.com")
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text, **kw: sent.append((to, subject, html, text)) or (True, "ok"))
    _client(env, "paid1")
    _client(env, "paid2")
    _quiet(env, "paid1", 40)
    _quiet(env, "paid2", 40)
    assert env.app.winback.send_all("https://kauli.test") == 2
    codes = {m[0]: re.search(r"RETURN-[A-Z2-9]{4}-[A-Z2-9]{4}", m[3]).group(0) for m in sent}
    assert len(set(codes.values())) == 2                                                   # a different code for each person
    for to, subject, html, text in sent:
        assert "Kauli Team" in text and "Godfrey" not in text and "%" not in text and "discount" not in text.lower()
        assert "free" in text and codes[to] in html and f"/client/winback/{codes[to]}" in text
    assert env.app.winback.send_all("https://kauli.test") == 0                              # one offer, ever
    assert len(sent) == 2


def test_a_code_works_only_for_its_own_account_and_only_once(env):
    _client(env, "paid1")
    _client(env, "paid2")
    _quiet(env, "paid1", 40)
    w = env.app.winback
    w.send_offer(env.db.get_user("paid1"), "went_quiet", "https://kauli.test")
    code = w.open_offer("paid1")["id"]
    assert w.claim("paid2", code) == "That code is not valid for this account."             # a leaked code is useless to anyone else
    assert w.claim("paid1", code.lower()) is None
    assert w.claim("paid1", code) == "This code has already been used."
    assert w.open_offer("paid1") is None


def test_the_free_month_is_the_plan_they_had_and_ends_by_itself(env):
    _client(env, "paid1")
    _quiet(env, "paid1", 40)
    w = env.app.winback
    w.send_offer(env.db.get_user("paid1"), "went_quiet", "https://kauli.test")
    assert _plan(env, "paid1") == "free"
    w.claim("paid1", w.open_offer("paid1")["id"])
    assert _plan(env, "paid1") == "pro"
    sub = env.db.get_subscription("paid1")
    assert 29 * DAY < sub["current_period_end"] - time.time() <= 30 * DAY and not sub["paystack_subscription_code"]       # no card, no renewal
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET current_period_end = ? WHERE user_id = 'paid1'", (time.time() - 1,))
    conn.commit()
    conn.close()
    assert _plan(env, "paid1") == "free"                                                    # nothing to cancel or take back


def test_a_premium_customer_gets_premium_back(env):
    _client(env, "prem")
    _quiet(env, "prem", 60)
    env.db.set_subscription_plan("prem", "premium", 30)
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET current_period_end = ? WHERE user_id = 'prem'", (time.time() - 30 * DAY,))
    conn.commit()
    conn.close()
    w = env.app.winback
    w.send_offer(env.db.get_user("prem"), "stopped_paying", "https://kauli.test")
    w.claim("prem", w.open_offer("prem")["id"])
    assert _plan(env, "prem") == "premium"


def test_an_old_unused_code_expires(env):
    _client(env, "paid1")
    _quiet(env, "paid1", 40)
    w = env.app.winback
    w.send_offer(env.db.get_user("paid1"), "went_quiet", "https://kauli.test")
    conn = env.db.get_conn()
    conn.execute("UPDATE winback_offers SET sent_at = ?", (time.time() - 31 * DAY,))
    code = conn.execute("SELECT id FROM winback_offers").fetchone()["id"]
    conn.commit()
    conn.close()
    assert w.open_offer("paid1") is None
    assert w.claim("paid1", code) == "This code has expired."


def test_the_customer_uses_the_link_or_types_the_code(env):
    _quiet(env, "u1", 40)
    w = env.app.winback
    w.send_offer(env.db.get_user("u1"), "went_quiet", "https://kauli.test")
    code = w.open_offer("u1")["id"]
    assert "Get my free month" in env.client.get("/client/home").text                       # the banner
    page = env.client.get(f"/client/winback/{code}").text
    assert code in page and "A free month, on us" in page and "nothing to cancel" in page.lower() and "discount" not in page.lower()
    assert "Have a code?" in env.client.get("/client/billing").text
    r = env.client.post("/client/redeem", data={"code": "return-wrong-code"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and _plan(env, "u1") == "free"
    r = env.client.post("/client/redeem", data={"code": code.lower()}, follow_redirects=False)
    assert r.status_code == 303 and _plan(env, "u1") == "pro"
    assert "Your free month is on" in env.client.get(f"/client/winback/{code}").text
    assert "Get my free month" not in env.client.get("/client/home").text


def test_nothing_is_sent_on_its_own_unless_switched_on(env, monkeypatch):
    _client(env, "paid1")
    _quiet(env, "paid1", 40)
    monkeypatch.delenv("KAULI_WINBACK_AUTOSEND", raising=False)
    assert env.app.winback.sweep_once("https://kauli.test")["sent"] == 0
    monkeypatch.setenv("KAULI_WINBACK_AUTOSEND", "1")
    assert env.app.winback.sweep_once("https://kauli.test")["sent"] == 1


def test_staff_see_who_qualifies_and_can_send_one(env):
    _client(env, "paid1")
    _quiet(env, "paid1", 40)
    assert env.client.get("/staff/winback", follow_redirects=False).status_code == 303          # a client is sent away
    assert env.client.post("/staff/winback/send", follow_redirects=False).status_code == 303
    assert not env.app.winback.stats()["sent"]
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/winback").text
    assert "paid1@example.com" in page and "Paid, then went quiet" in page and "OFF" in page
    env.client.post("/staff/winback/send", data={"user_id": "paid1"})
    assert env.app.winback.stats()["sent"] == 1
    assert "RETURN-" in env.client.get("/staff/winback").text


def test_the_code_box_is_on_the_billing_page_even_with_no_payments(env):
    assert "Have a code?" in env.client.get("/client/billing").text
