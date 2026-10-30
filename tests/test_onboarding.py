"""Getting a new customer's first order ready with little friction, while the platform stays protected."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import onboarding  # noqa: E402

DAY = 86400


def _reset(env, email=None, ip=None, datacenter=None):
    conn = env.db.get_conn()
    conn.execute("DELETE FROM orders WHERE client_id = 'u1'")
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL, created_at = ? WHERE id = 'u1'", (time.time(),))
    if email:
        conn.execute("UPDATE users SET email = ? WHERE id = 'u1'", (email,))
    conn.execute("UPDATE users SET signup_ip = ?, signup_ip_is_datacenter = ? WHERE id = 'u1'", (ip, datacenter))
    conn.commit()
    conn.close()


def _risk(env):
    return onboarding.risk(env.db.get_user("u1"))


def test_an_ordinary_newcomer_is_low_risk(env):
    _reset(env, "amina@gmail.com", "198.51.100.7", 0)
    r = _risk(env)
    assert r["level"] == "low" and r["score"] == 0 and r["reasons"] == []
    assert onboarding.first_order_cap(env.db.get_user("u1")) == 120


def test_each_warning_sign_is_a_readable_reason(env):
    _reset(env, "x1@mailinator.com", "198.51.100.8", 0)
    r = _risk(env)
    assert r["level"] == "high" and "throw-away email address" in r["reasons"]
    _reset(env, "amina@gmail.com", "198.51.100.9", 1)
    r = _risk(env)
    assert r["level"] == "low" and r["reasons"] == ["signed up from a cloud server"] and r["score"] == 30


def test_several_accounts_from_one_address_in_a_day_raise_the_risk(env):
    _reset(env, "amina@gmail.com", "203.0.113.50", 1)
    for i in range(3):
        env.db.get_or_create_user(f"farm{i}", f"farm{i}@gmail.com", "client")
        conn = env.db.get_conn()
        conn.execute("UPDATE users SET signup_ip = '203.0.113.50' WHERE id = ?", (f"farm{i}",))
        conn.commit()
        conn.close()
    r = _risk(env)
    assert r["level"] == "high" and any("3 other accounts" in x for x in r["reasons"])
    assert onboarding.first_order_cap(env.db.get_user("u1")) == 0


def test_signing_in_with_google_counts_in_their_favour(env):
    _reset(env, "amina@gmail.com", "198.51.100.10", 1)
    before = _risk(env)["score"]
    env.db.log_security_event("signup", user_id="u1", email="amina@gmail.com", detail="google_oauth")
    after = _risk(env)
    assert after["score"] < before and "signed in with Google (verified identity)" in after["reasons"]


def test_the_first_order_is_as_generous_as_the_account_deserves(env, wav_bytes, monkeypatch):
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 79.8)
    _reset(env, "amina@gmail.com", "198.51.100.11", 0)                                       # low risk: 120 minutes
    assert env.order_from(env.place(wav_bytes))["cost_usd"] == 0
    _reset(env, "amina@gmail.com", "198.51.100.12", 1)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET signup_ip = '198.51.100.13' WHERE id = 'u1'")
    conn.commit()
    conn.close()
    env.db.get_or_create_user("twin", "twin@gmail.com", "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET signup_ip = '198.51.100.13' WHERE id = 'twin'")
    conn.commit()
    conn.close()
    assert _risk(env)["level"] == "medium"                                                   # a cloud server plus another account: 45 minutes
    assert env.order_from(env.place(wav_bytes))["status"] == "pending_payment"               # 79.8 is over that
    _reset(env, "amina@gmail.com", "198.51.100.14", 1)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 40.0)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET signup_ip = '198.51.100.13' WHERE id IN ('u1', 'twin')")
    conn.commit()
    conn.close()
    assert env.order_from(env.place(wav_bytes))["cost_usd"] == 0                              # 40 is under it
    _reset(env, "burner@mailinator.com", "198.51.100.15", 0)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 40.0)
    assert env.order_from(env.place(wav_bytes))["status"] == "pending_payment"               # high risk: no free first order, nothing blocked


# --------------------------------------------------------------------------------------------- follow-through
def _stuck_order(env, hours, status="pending_payment"):
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = ?, created_at = ? WHERE client_id = 'u1'", (status, time.time() - hours * 3600))
    conn.commit()
    conn.close()


def test_a_first_order_that_is_not_moving_is_reported_once_a_day(env, wav_bytes):
    _reset(env)
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    _stuck_order(env, 1)
    assert onboarding.first_orders_needing_help() == []                                       # an hour is nothing
    _stuck_order(env, 5)
    found = onboarding.first_orders_needing_help()
    assert [f["order_id"] for f in found] == [order["id"]] and found[0]["hours"] == 5
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    assert onboarding.alert_stuck_first_orders() == 1 and onboarding.alert_stuck_first_orders() == 0     # staff are told once
    conn = env.db.get_conn()
    note = conn.execute("SELECT * FROM notifications WHERE kind = 'first_order_stuck'").fetchone()
    conn.close()
    assert note and "5h" in note["title"] and note["link"] == f"/staff/orders/{order['id']}"
    _stuck_order(env, 5, "ready_for_delivery")
    assert onboarding.first_orders_needing_help() == []                                       # once they have a result they are not "stuck"


def _ready(env, wav_bytes, n):
    ids = []
    for i in range(n):
        o = env.order_from(env.place(wav_bytes))
        conn = env.db.get_conn()
        conn.execute("UPDATE orders SET status = 'ready_for_delivery', created_at = ?, status_changed_at = ? WHERE id = ?", (time.time() + i, time.time() + i, o["id"]))
        conn.commit()
        conn.close()
        ids.append(o["id"])
    return ids


def test_the_satisfaction_question_comes_after_the_first_third_and_sixth_result_once_each(env, wav_bytes):
    _reset(env)
    env.db.set_plan_chosen("u1")
    ids = _ready(env, wav_bytes, 4)
    assert onboarding.csat_due("u1", ids[0]) and not onboarding.csat_due("u1", ids[1]) and onboarding.csat_due("u1", ids[2]) and not onboarding.csat_due("u1", ids[3])
    page = env.client.get(f"/client/orders/{ids[0]}").text
    assert "How easy was it to get this result?" in page and f'/client/orders/{ids[0]}/rate' in page
    assert "How easy was it" not in env.client.get(f"/client/orders/{ids[1]}").text
    r = env.client.post(f"/client/orders/{ids[0]}/rate", data={"score": "5", "comment": "Quick and clear"}, follow_redirects=False)
    assert r.status_code == 303 and not onboarding.csat_due("u1", ids[0])
    env.client.post(f"/client/orders/{ids[0]}/rate", data={"score": "1"})                       # a second answer for the same order is ignored
    env.client.post(f"/client/orders/{ids[2]}/rate", data={"score": "9"})                       # and so is a score that is not 1 to 5
    s = onboarding.csat_summary()
    assert s["n"] == 1 and s["avg"] == 5 and s["recent"][0]["comment"] == "Quick and clear" and onboarding.csat_due("u1", ids[2])


def test_time_to_first_result_is_measured(env, wav_bytes):
    _reset(env)
    ids = _ready(env, wav_bytes, 1)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET created_at = ? WHERE id = 'u1'", (time.time() - 600,))
    conn.execute("UPDATE orders SET status_changed_at = ? WHERE id = ?", (time.time() - 300, ids[0]))
    conn.commit()
    conn.close()
    t = onboarding.ttfv_minutes()
    assert t["got_result"] == 1 and 4 < t["median"] < 6 and t["within_day"] == 1


def test_the_dashboard_and_the_staff_page_show_these_numbers(env, wav_bytes):
    _reset(env)
    _ready(env, wav_bytes, 1)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    dash = env.client.get("/staff/analytics").text
    for text in ("Time to first result", "Got a result in a day", "Satisfaction", "First orders stuck"):
        assert text in dash, text
    page = env.client.get("/staff/engagement").text
    assert "New customers whose first order needs a hand" in page and "Time to first result" in page
