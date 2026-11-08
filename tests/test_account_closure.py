"""Closing an account: self-serve, an optional survey with a tailored alternative, a confirmation email, and the feedback reaching staff."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _surveys(env):
    conn = env.db.get_conn()
    rows = conn.execute("SELECT * FROM account_exit_surveys").fetchall()
    conn.close()
    return rows


def _status(env, uid="u1"):
    return env.db.get_user(uid)["account_status"]


def test_the_close_page_explains_what_happens_and_offers_a_way_to_stay(env):
    page = env.client.get("/account/close-account").text
    assert "Close your account" in page and "What happens when you close" in page
    for label in ("It costs too much", "A feature I need is missing", "My project is finished", "I need a break for now"):
        assert label in page
    assert "Keep my account" in page and "Changed your mind?" in page
    assert "% off" not in page and "discount" not in page.lower()                           # no discount bargaining, pricing is simple
    assert "disabled" not in page.split('id="close-form"')[1].split("</form>")[0]            # the close button is never disabled


def test_closing_needs_the_email_typed_exactly(env):
    r = env.client.post("/account/close-account", data={"confirm_text": "someone@else.com"}, follow_redirects=False)
    assert r.status_code == 303 and "/account/close-account?error=" in r.headers["location"]
    assert _status(env) != "closed" and not _surveys(env)


def test_closing_saves_the_survey_tells_staff_and_signs_out(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    email = env.db.get_user("u1")["email"]
    r = env.client.post("/account/close-account", data={"confirm_text": email.upper(), "reason": "switching", "detail": "Needed Arabic dubbing.", "competitor": "Rev"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?notice=account_closed"
    assert _status(env) == "closed"
    rows = _surveys(env)
    assert len(rows) == 1 and rows[0]["reason"] == "switching" and "Rev" in rows[0]["detail"] and "Arabic dubbing" in rows[0]["detail"]
    conn = env.db.get_conn()
    note = conn.execute("SELECT * FROM notifications WHERE kind = 'account_closed'").fetchone()
    conn.close()
    assert note is not None and "/staff/exit-feedback" in note["link"]


def test_the_survey_is_optional_and_unknown_reasons_are_ignored(env):
    email = env.db.get_user("u1")["email"]
    env.client.post("/account/close-account", data={"confirm_text": email, "reason": "<script>"},
                    follow_redirects=False)
    assert _status(env) == "closed" and _surveys(env)[0]["reason"] == ""


def test_the_confirmation_email_is_signed_kauli_team(env, monkeypatch):
    sent = []
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("BREVO_FROM_EMAIL", "ops@example.com")
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text, **kw: sent.append((to, subject, html, text)) or (True, "ok"))
    email = env.db.get_user("u1")["email"]
    env.client.post("/account/close-account", data={"confirm_text": email}, follow_redirects=False)
    assert len(sent) == 1 and sent[0][0] == email and sent[0][1] == "Your Kauli account is closed"
    for part in sent[0][2:]:
        assert "Kauli Team" in part and "Godfrey" not in part
        assert "reopen your account" in part and "nothing more to pay" in part


def test_a_failed_subscription_stop_keeps_the_account_open(env, monkeypatch):
    env.db.set_subscription_plan("u1", "pro", 30)
    env.db.set_paystack_subscription("u1", "SUB_1", "tok_1")
    monkeypatch.setattr(env.app.billing, "paystack_disable_subscription", lambda code, token: {"ok": False, "error": "down"})
    email = env.db.get_user("u1")["email"]
    r = env.client.post("/account/close-account", data={"confirm_text": email}, follow_redirects=False)
    assert "nothing+was+closed" in r.headers["location"] or "nothing%20was%20closed" in r.headers["location"]
    assert _status(env) != "closed" and not _surveys(env)


def test_a_recurring_plan_is_stopped_before_the_account_closes(env, monkeypatch):
    env.db.set_subscription_plan("u1", "pro", 30)
    env.db.set_paystack_subscription("u1", "SUB_1", "tok_1")
    calls = []
    monkeypatch.setattr(env.app.billing, "paystack_disable_subscription", lambda code, token: calls.append((code, token)) or {"ok": True})
    page = env.client.get("/account/close-account").text
    assert "Stop my plan renewing, keep my account" in page
    email = env.db.get_user("u1")["email"]
    env.client.post("/account/close-account", data={"confirm_text": email, "reason": "too_expensive"}, follow_redirects=False)
    assert calls == [("SUB_1", "tok_1")] and _status(env) == "closed"
    assert _surveys(env)[0]["plan"] == "pro"


def test_staff_see_what_leavers_said_and_clients_do_not(env):
    email = env.db.get_user("u1")["email"]
    env.client.post("/account/close-account", data={"confirm_text": email, "reason": "missing_feature", "detail": "No Zoom import"}, follow_redirects=False)
    assert env.client.get("/staff/exit-feedback", follow_redirects=False).status_code == 303   # a client is sent away
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/exit-feedback").text
    assert "A feature I need is missing" in page and "No Zoom import" in page and "1 closure" in page


def test_the_plans_page_and_settings_link_to_the_close_page(env):
    assert 'href="/account/close-account"' in env.client.get("/account/billing").text
    assert 'href="/account/close-account"' in env.client.get("/settings?tab=security").text
