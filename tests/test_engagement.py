"""Customer engagement: every account, new or long-standing, read from what it does, with a next-step card in the account and gentle,
rate-limited emails that staff send (or switch on to send themselves)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_site_dashboard import _order, _pay, _user  # noqa: E402,F401
from webapp import engagement  # noqa: E402

DAY = 86400


def _state(env, uid):
    return next(c for c in engagement.customers() if c["id"] == uid)["state"]


def _mail(env, monkeypatch):
    sent = []
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("BREVO_FROM_EMAIL", "ops@example.com")
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text, **kw: sent.append((to, subject, html, text)) or (True, "ok"))
    return sent


def _clean(env):
    conn = env.db.get_conn()
    conn.execute("DELETE FROM orders WHERE client_id = 'u1'")
    conn.execute("UPDATE users SET created_at = ?, last_seen_at = ? WHERE id = 'u1'", (time.time(), time.time()))
    conn.commit()
    conn.close()


def test_accounts_are_put_in_the_right_state(env):
    _clean(env)
    _user(env, "idle", joined_days_ago=10)                                                # never uploaded
    _user(env, "drift", joined_days_ago=60, seen_days_ago=30)
    _order(env, "drift", 30)
    _user(env, "one", joined_days_ago=30, seen_days_ago=1)                                # only ever transcribes
    _order(env, "one", 20)
    _order(env, "one", 1)
    _user(env, "multi", joined_days_ago=30, seen_days_ago=1)                              # uses several things
    _order(env, "multi", 5, "translate")
    _order(env, "multi", 1, "transcribe")
    _user(env, "payer", joined_days_ago=90, seen_days_ago=12)                             # pays, and slowed down
    _order(env, "payer", 12)
    _pay(env, "payer", 19, days_ago=30)
    env.db.set_subscription_plan("payer", "pro", 30)
    _user(env, "brandnew", joined_days_ago=1)                                             # too early to say anything
    states = {c["id"]: c["state"] for c in engagement.customers()}
    assert states["idle"] == "never_started" and states["drift"] == "drifting" and states["one"] == "underusing"
    assert states["multi"] is None and states["payer"] == "at_risk_paid" and states["brandnew"] is None


def test_people_with_a_failing_payment_or_a_win_back_are_left_to_those_flows(env):
    _user(env, "failing", joined_days_ago=90, seen_days_ago=12)
    _order(env, "failing", 12)
    env.db.set_subscription_plan("failing", "pro", 30)
    conn = env.db.get_conn()
    conn.execute("INSERT INTO payment_failures (user_id, email, first_failed_at, last_failed_at) VALUES ('failing', 'f@example.com', ?, ?)", (time.time(), time.time()))
    conn.commit()
    conn.close()
    assert _state(env, "failing") is None
    _user(env, "gone", joined_days_ago=100, seen_days_ago=40)
    _order(env, "gone", 40)
    env.app.winback.send_offer(env.db.get_user("gone"), "went_quiet", "https://kauli.test")
    assert _state(env, "gone") is None


def test_the_guardrails_protect_people_from_too_many_emails(env):
    _clean(env)
    _user(env, "idle", joined_days_ago=10)
    c = next(x for x in engagement.customers() if x["id"] == "idle")
    assert engagement.can_nudge(c)
    env.db.set_marketing_consent("idle", False, None)
    assert not engagement.can_nudge(next(x for x in engagement.customers() if x["id"] == "idle"))     # unsubscribed
    env.db.set_marketing_consent("idle", True, None)
    engagement.send_nudge(next(x for x in engagement.customers() if x["id"] == "idle"), "https://kauli.test")
    assert not engagement.can_nudge(next(x for x in engagement.customers() if x["id"] == "idle"))     # nudged today: wait two weeks
    conn = env.db.get_conn()
    conn.execute("UPDATE engagement_messages SET sent_at = ?", (time.time() - 15 * DAY,))
    conn.commit()
    conn.close()
    assert engagement.can_nudge(next(x for x in engagement.customers() if x["id"] == "idle"))         # two weeks later it may be nudged again...
    for _ in range(2):
        engagement.send_nudge(next(x for x in engagement.customers() if x["id"] == "idle"), "https://kauli.test")
    conn = env.db.get_conn()
    conn.execute("UPDATE engagement_messages SET sent_at = ?", (time.time() - 60 * DAY,))
    conn.commit()
    conn.close()
    assert not engagement.can_nudge(next(x for x in engagement.customers() if x["id"] == "idle"))     # ...but never more than three of a kind


def test_a_closed_account_and_a_team_member_are_never_nudged(env):
    _user(env, "closed", joined_days_ago=10)
    env.db.close_account("closed")
    _user(env, "member", joined_days_ago=10)
    conn = env.db.get_conn()
    conn.execute("INSERT INTO team_members (id, owner_client_id, member_user_id, invited_email, status, invited_at) VALUES ('t1', 'u1', 'member', 'member@example.com', 'accepted', ?)", (time.time(),))
    conn.commit()
    conn.close()
    ids = {c["id"] for c in engagement.customers()}
    assert "closed" not in ids and "member" not in ids


def test_the_email_is_kind_signed_kauli_team_and_has_a_way_out_and_no_discount(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    _clean(env)
    _user(env, "idle", joined_days_ago=10)
    assert engagement.send_all("https://kauli.test") >= 1
    mail = next(m for m in sent if m[0] == "idle@example.com")
    assert mail[1] == "Your free minutes are waiting"
    for part in mail[2:]:
        assert "Kauli Team" in part and "Godfrey" not in part and "/unsubscribe/idle" in part
        assert "% off" not in part and "discount" not in part.lower() and "coupon" not in part.lower()


def test_nothing_is_sent_by_itself_until_it_is_switched_on(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    _clean(env)
    _user(env, "idle", joined_days_ago=10)
    monkeypatch.delenv("KAULI_ENGAGE_AUTOSEND", raising=False)
    assert engagement.sweep_once("https://kauli.test")["sent"] == 0 and not sent
    monkeypatch.setenv("KAULI_ENGAGE_AUTOSEND", "1")
    assert engagement.sweep_once("https://kauli.test")["sent"] >= 1 and sent


def test_each_state_has_its_own_next_step_in_the_account(env):
    _clean(env)
    _user(env, "idle", joined_days_ago=10)
    card = engagement.next_step("idle")
    assert card["state"] == "never_started" and card["url"] == "/client#new-project" and "free" in card["title"].lower()
    _user(env, "one", joined_days_ago=30, seen_days_ago=1)
    _order(env, "one", 20)
    _order(env, "one", 1)
    assert "translat" in engagement.next_step("one")["title"].lower()
    _user(env, "ok", joined_days_ago=2)
    assert engagement.next_step("ok") is None


def test_the_customer_sees_the_card_on_their_home_page(env):
    _clean(env)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET created_at = ? WHERE id = 'u1'", (time.time() - 10 * DAY,))
    conn.commit()
    conn.close()
    page = env.client.get("/client/home").text
    assert "Your first file is free" in page and 'href="/client#new-project"' in page


def test_staff_see_the_states_and_can_send_one_nudge(env, monkeypatch):
    sent = _mail(env, monkeypatch)
    _clean(env)
    _user(env, "idle", joined_days_ago=10)
    assert env.client.get("/staff/engagement", follow_redirects=False).status_code in (302, 303, 307)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/engagement").text
    assert "Customer engagement" in page and "Signed up, never uploaded" in page and "idle@example.com" in page and "OFF" not in page and "off" in page
    env.client.post("/staff/engagement/send", data={"user_id": "idle"})
    assert [m[0] for m in sent] == ["idle@example.com"]
    env.client.post("/staff/engagement/send", data={"user_id": "idle"})
    assert len(sent) == 1                                                                   # a second press the same day does nothing
