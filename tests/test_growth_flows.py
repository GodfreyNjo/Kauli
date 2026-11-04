"""Try before signing up, magic-link sign-in, "Not ready? Tell us why", free-minute prompts, "a plan would have cost less", and the Monday numbers."""
import calendar
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import savings, usage_prompts, weekly_kpi  # noqa: E402


def _real_limits(env):
    """The suite switches the rate limits off; these tests are about the limits."""
    counts = {}

    def check(key, limit, window_s):
        counts[key] = counts.get(key, 0) + 1
        return counts[key] <= limit, 60
    env.monkeypatch.setattr(env.app.rate_limit, "check", check)


def _real_sessions(env):
    """Use the real cookie session instead of the test's always-signed-in customer."""
    def current_user(request):
        api = getattr(request.state, "api_user", None)
        if api is not None:
            return api
        uid = request.session.get("user_id")
        row = env.db.get_user(uid) if uid else None
        return {**dict(row), "client_scope_id": uid} if row else None
    env.monkeypatch.setattr(env.app, "current_user", current_user)


# ------------------------------------------------------------------------------------------------------ try before signing up
def test_a_visitor_can_send_up_to_the_free_minutes_and_only_sees_the_result_inside_an_account(env, wav_bytes):
    _real_sessions(env)
    assert env.app.GUEST_TRIAL_MAX_MINUTES == 30.0
    r = env.client.post("/try/start", files={"audio": ("talk.wav", wav_bytes, "audio/wav")}, data={"source_lang": "en"})
    assert r.status_code == 200, r.text
    link = r.json()["redirect"]
    assert link.startswith("/client/orders/")
    order = env.db.get_order(link.rsplit("/", 1)[1])
    guest = env.db.get_user(order["client_id"])
    assert guest["is_guest"] == 1 and guest["is_internal"] == 1                                  # never counted as a sign-up
    page = env.client.get(link).text
    assert "See your transcript in your free account" in page and "Email me the link" in page
    assert "order-transcript-table" not in page and "download/docx" not in page                  # the transcript is not shown outside an account
    for path in (f"{link}/download/txt", f"{link}/export?format=txt"):
        assert env.client.get(path, follow_redirects=False).status_code != 200, path


def test_a_recording_longer_than_the_free_minutes_is_cut_to_them(env, wav_bytes, monkeypatch):
    _real_sessions(env)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda p: 45.0)
    seen = []

    real_run = env.app.subprocess.run

    def fake_run(cmd, **kw):
        if cmd and cmd[0] == "ffmpeg" and "-t" in cmd:                                           # the cut to the free minutes
            seen.append(cmd)
            shutil.copy(cmd[cmd.index("-i") + 1], cmd[-1])
            return None
        return real_run(cmd, **kw)
    monkeypatch.setattr(env.app.subprocess, "run", fake_run)
    r = env.client.post("/try/start", files={"audio": ("long.wav", wav_bytes, "audio/wav")}, data={"source_lang": "en"})
    assert r.status_code == 200 and r.json()["trimmed"] is True
    assert "-t" in seen[0] and seen[0][seen[0].index("-t") + 1] == "1800"


def test_trials_are_limited_per_address(env, wav_bytes):
    _real_sessions(env)
    _real_limits(env)
    codes = [env.client.post("/try/start", files={"audio": ("t.wav", wav_bytes, "audio/wav")}, data={"source_lang": "en"}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    assert "limit" in env.client.post("/try/start", files={"audio": ("t.wav", wav_bytes, "audio/wav")}).json()["error"]


def test_giving_an_email_sends_a_link_and_the_order_moves_to_the_real_account_when_it_is_opened(env, wav_bytes, monkeypatch):
    _real_sessions(env)
    sent = []
    monkeypatch.setattr(env.app.supabase_auth, "send_magic_link", lambda email, redirect_to: sent.append((email, redirect_to)))
    link = env.client.post("/try/start", files={"audio": ("talk.wav", wav_bytes, "audio/wav")}, data={"source_lang": "en"}).json()["redirect"]
    order_id = link.rsplit("/", 1)[1]
    r = env.client.post("/client/save-guest", data={"email": "New.Person@Example.com", "next": link}, follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert sent and sent[0][0] == "new.person@example.com" and "/auth/google/callback?via=magic" in sent[0][1]
    env.db.get_or_create_user("real1", "new.person@example.com", "client")
    env.db.claim_guest_orders("new.person@example.com", "real1")                                 # what signing in with that email does
    assert env.db.get_order(order_id)["client_id"] == "real1"
    assert env.db.get_user("real1")["is_guest"] == 0


def test_a_bad_email_is_refused_kindly(env, wav_bytes):
    _real_sessions(env)
    link = env.client.post("/try/start", files={"audio": ("t.wav", wav_bytes, "audio/wav")}, data={"source_lang": "en"}).json()["redirect"]
    r = env.client.post("/client/save-guest", data={"email": "nope", "next": link}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


# ------------------------------------------------------------------------------------------------------------- magic link
def test_the_sign_in_page_offers_a_link_by_email_and_never_reveals_which_addresses_have_accounts(env, monkeypatch):
    _real_sessions(env)
    _real_limits(env)
    page = env.client.get("/login").text
    assert 'formaction="/login/magic"' in page and "Email me a sign-in link instead" in page
    signin = page.split('id="panel-signin"')[1].split('id="panel-signup"')[0]
    assert signin.count('name="email"') == 1                                                       # one email box per form, not two
    sent = []
    monkeypatch.setattr(env.app.supabase_auth, "send_magic_link", lambda email, redirect_to: sent.append(email))
    good = env.client.post("/login/magic", data={"email": "anyone@example.com", "next": "/client"})
    bad = env.client.post("/login/magic", data={"email": "not an email"})
    assert good.status_code == 200 and "sign-in link is on its way" in good.text and "sign-in link is on its way" in bad.text
    assert sent == ["anyone@example.com"]
    for _ in range(4):
        last = env.client.post("/login/magic", data={"email": "anyone@example.com"})
    assert last.status_code == 429


# ------------------------------------------------------------------------------------------------------- "Not ready? Tell us why"
def test_the_review_step_has_the_prompt_and_the_answers_are_kept(env):
    assert "Not ready? Tell us why" in env.client.get("/client").text
    ok = env.client.post("/client/exit-reason", json={"reason": "price", "note": "Too much for one file", "step": "review"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert env.client.post("/client/exit-reason", json={"reason": "bogus"}).status_code == 400
    counts = env.db.exit_reason_counts(0)
    assert counts == [{"reason": "price", "n": 1}] and env.db.recent_exit_notes()[0]["note"] == "Too much for one file"


# ------------------------------------------------------------------------------------------------- the 50% and 80% free minutes
def _used(env, minutes):
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET minutes_used_this_period = ? WHERE user_id = 'u1'", (minutes,))
    conn.commit()
    conn.close()


def test_a_kind_word_goes_out_once_at_half_and_once_at_most_of_the_free_minutes(env):
    sent = []
    send = lambda user, kind, subject, body, **kw: sent.append((kind, subject, body))      # noqa: E731
    _used(env, 10)
    assert usage_prompts.check("u1", "http://t/", send) == [] and usage_prompts.banner({"role": "client", "client_scope_id": "u1"}) is None
    _used(env, 16)
    assert usage_prompts.check("u1", "http://t/", send) == [50]
    assert usage_prompts.check("u1", "http://t/", send) == []                                # once only
    assert "half of your free minutes" in sent[0][1] and "pay as you go" in sent[0][2] and "discount" not in sent[0][2].lower()
    _used(env, 25)
    assert usage_prompts.check("u1", "http://t/", send) == [80] and "free minutes left" in sent[1][1]
    banner = usage_prompts.banner({"role": "client", "client_scope_id": "u1"})
    assert banner["level"] == 80 and banner["left"] == 5.0
    page = env.client.get("/client/home").text
    assert "free minutes left this month" in page


def test_a_paid_plan_gets_no_free_minute_prompts(env):
    env.db.set_subscription_plan("u1", "pro", 30)
    _used(env, 25)
    assert usage_prompts.status("u1") is None


# ---------------------------------------------------------------------------------------------------- a plan would have cost less
def _paid_order(env, wav_bytes, pid, usd, minutes, level="transcribe", tier="ai", extra=b""):
    order = env.order_from(env.place(wav_bytes + extra))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET duration_minutes = ?, service_level = ?, review_tier = ? WHERE id = ?", (minutes, level, tier, order["id"]))
    conn.commit()
    conn.close()
    env.db.create_payment(pid, "u1", "free", usd, None, "USD", "paystack", order_id=order["id"])
    env.db.complete_payment(pid, "ref-" + pid, 0)


def test_two_costly_orders_show_what_the_monthly_plan_would_have_saved(env, wav_bytes):
    _paid_order(env, wav_bytes, "pa", 15.0, 20, extra=b"\x01" * 8)
    assert savings.pro_savings("u1") is None                                                  # one order is not a pattern
    _paid_order(env, wav_bytes, "pb", 15.0, 20, extra=b"\x02" * 8)
    s = savings.pro_savings("u1")
    assert s == {"paid": 30.0, "saved": 11.0, "minutes": 40.0, "pro_price": 19, "pro_minutes": s["pro_minutes"], "orders": 2}
    assert "A monthly plan would have cost you less" in env.client.get("/client/billing").text


def test_dubbing_and_human_review_are_left_out_because_a_plan_does_not_cover_them(env, wav_bytes):
    _paid_order(env, wav_bytes, "pc", 15.0, 20, level="dub", extra=b"\x03" * 8)
    _paid_order(env, wav_bytes, "pd", 15.0, 20, tier="human", extra=b"\x04" * 8)
    assert savings.pro_savings("u1") is None


def test_someone_already_on_a_plan_is_not_told_about_it(env, wav_bytes):
    _paid_order(env, wav_bytes, "pe", 15.0, 20, extra=b"\x05" * 8)
    _paid_order(env, wav_bytes, "pf", 15.0, 20, extra=b"\x06" * 8)
    env.db.set_subscription_plan("u1", "pro", 30)
    assert savings.pro_savings("u1") is None


# ------------------------------------------------------------------------------------------------------------- the Monday email
def test_the_weekly_numbers_cover_the_plans_measures_with_targets(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    report = weekly_kpi.build()
    labels = [r["label"] for r in report["rows"]]
    assert len(labels) == 9 and "Visitor to sign-up" in labels and "Median time to first result (minutes)" in labels
    assert all(r["status"] in ("good", "behind", "none") and r["target"] for r in report["rows"])
    assert "Real customers only" in weekly_kpi.to_html(report, "https://kauli.test")


def test_it_is_sent_once_on_monday_to_the_chosen_address_and_staff_can_send_it_now(env, monkeypatch):
    monkeypatch.setenv("KAULI_KPI_EMAIL", "owner@example.com")
    mails = []
    send = lambda to, subject, html, text=None, **kw: (mails.append((to, subject, html)), (True, "id"))[1]      # noqa: E731
    wrap = lambda inner, **kw: inner                                                                              # noqa: E731
    monday = calendar.timegm((2026, 11, 2, 9, 0, 0))
    tuesday = calendar.timegm((2026, 11, 3, 9, 0, 0))
    assert weekly_kpi.send_if_due("https://kauli.test", send, wrap, now=tuesday) is False
    assert weekly_kpi.send_if_due("https://kauli.test", send, wrap, now=monday) is True
    assert weekly_kpi.send_if_due("https://kauli.test", send, wrap, now=monday + 3600) is False        # not twice in a week
    assert mails[0][0] == "owner@example.com" and mails[0][1] == "Kauli weekly numbers" and "Day-30 target" in mails[0][2]
    env.db.get_or_create_user("staff1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("staff1")), "role": "staff"})
    assert "Weekly numbers" in env.client.get("/staff/analytics/kpi").text
    monkeypatch.setattr(env.app.mailer, "send_email", send)
    r = env.client.post("/staff/analytics/kpi/send", follow_redirects=False)
    assert r.status_code == 303 and len(mails) == 2
