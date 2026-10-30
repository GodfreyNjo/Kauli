"""The staff site analytics dashboard: our own cookie-free visit counter, the engines behind it, the two models, and the page."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import site_analytics as sa  # noqa: E402

DAY = 86400
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


def _hits(env):
    conn = env.db.get_conn()
    rows = conn.execute("SELECT * FROM site_hits ORDER BY ts").fetchall()
    conn.close()
    return rows


def _order(env, uid, days_ago, service="transcribe", summary=False):
    oid = f"o-{uid}-{days_ago}-{service}"
    env.db.create_order(order_id=oid, client_id=uid, original_filename="a.wav", audio_path="/tmp/a.wav", source_lang="en", target_lang="en",
                        tier="free", asr="faster-whisper", mt="stub", tts="stub", outdir="/tmp/o")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET created_at = ?, service_level = ?, ai_summary_text = ? WHERE id = ?",
                 (time.time() - days_ago * DAY, service, "s" if summary else None, oid))
    conn.commit()
    conn.close()


def _user(env, uid, joined_days_ago=10, seen_days_ago=None):
    env.db.get_or_create_user(uid, f"{uid}@example.com", "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET created_at = ?, last_seen_at = ? WHERE id = ?",
                 (time.time() - joined_days_ago * DAY, None if seen_days_ago is None else time.time() - seen_days_ago * DAY, uid))
    conn.commit()
    conn.close()


def _pay(env, uid, usd, days_ago=1, plan="pro", order_id=None):
    conn = env.db.get_conn()
    conn.execute("INSERT INTO payments (id, user_id, plan, order_id, amount_usd, currency, provider, status, created_at, completed_at) "
                 "VALUES (?, ?, ?, ?, ?, 'USD', 'paystack', 'completed', ?, ?)", (f"p-{uid}-{days_ago}-{usd}", uid, plan, order_id, usd, time.time() - days_ago * DAY, time.time() - days_ago * DAY))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------------------------------------ the counter
def test_channels_are_told_apart_from_the_referrer_and_the_campaign_tags():
    c = lambda ref, q="": sa.classify(ref, "kauli.test", q and __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(q) or {})[0]  # noqa: E731
    assert c("https://www.google.com/search?q=x") == "Organic Search" and c("https://duckduckgo.com/") == "Organic Search"
    assert c("https://l.facebook.com/l.php") == "Social Media" and c("https://t.co/abc") == "Social Media"
    assert c("https://someblog.org/post") == "Referral"
    assert c("") == "Direct" and c("https://kauli.test/pricing") == "Direct"
    assert c("https://google.com", "utm_medium=cpc&utm_source=google") == "Paid Search"
    assert c("", "utm_medium=cpc&utm_source=facebook") == "Paid Social"
    assert c("", "utm_medium=email&utm_source=newsletter") == "Email"


def test_devices_and_browsers_are_read_from_the_user_agent():
    assert sa.device_of(CHROME) == "Desktop" and sa.device_of(IPHONE) == "Mobile" and sa.device_of("Mozilla/5.0 (iPad; CPU OS 17)") == "Tablet"
    assert sa.browser_of(CHROME) == "Chrome" and sa.browser_of(IPHONE) == "Safari" and sa.browser_of(CHROME + " Edg/120") == "Edge"
    assert sa.browser_of("Mozilla/5.0 Firefox/121.0") == "Firefox" and sa.browser_of("something") == "Others"


def test_only_real_page_views_by_real_people_are_counted():
    ok = lambda **kw: sa.should_count(**{"method": "GET", "path": "/", "status": 200, "content_type": "text/html; charset=utf-8", "ua": CHROME, "dnt": False, **kw})  # noqa: E731
    assert ok()
    assert not ok(method="POST") and not ok(status=404) and not ok(dnt=True)
    assert not ok(ua="Googlebot/2.1") and not ok(ua="python-requests/2.31") and not ok(ua="")
    assert not ok(content_type="application/json") and not ok(path="/static/style.css") and not ok(path="/staff/analytics") and not ok(path="/api/v1/orders")


def test_a_visit_is_stored_without_any_address_and_sessions_follow_a_30_minute_gap(env):
    now = time.time()
    sa.record_hit("/", "utm_source=x", "https://google.com/", "kauli.test", "203.0.113.9", CHROME, "ke", None, now=now)
    sa.record_hit("/pricing", "", "", "kauli.test", "203.0.113.9", CHROME, "ke", None, now=now + 60)
    sa.record_hit("/", "", "", "kauli.test", "203.0.113.9", CHROME, "ke", "u1", now=now + 60 + 31 * 60)
    a, b, c = _hits(env)
    assert a["session"] == b["session"] != c["session"] and a["is_landing"] == 1 and b["is_landing"] == 0 and c["is_landing"] == 1
    assert a["channel"] == "Organic Search" and a["country"] == "KE" and a["device"] == "Desktop" and c["user_id"] == "u1"
    conn = env.db.get_conn()
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(site_hits)")}
    conn.close()
    assert not ({"ip", "ip_address", "user_agent", "ua", "email"} & columns)                      # nothing that identifies a person is kept
    assert "203.0.113.9" not in str(dict(a))


def test_the_same_browser_is_a_different_visitor_each_day(env):
    now = time.time()
    sa.record_hit("/", "", "", "kauli.test", "198.51.100.4", CHROME, "us", None, now=now)
    sa.record_hit("/", "", "", "kauli.test", "198.51.100.4", CHROME, "us", None, now=now + DAY)
    first, second = _hits(env)
    assert first["visitor"] != second["visitor"]


def test_real_page_views_are_counted_by_the_site_itself_but_not_when_do_not_track_is_on(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    env.client.get("/", headers={"user-agent": CHROME})
    env.client.get("/try", headers={"user-agent": CHROME, "dnt": "1"})
    env.client.get("/", headers={"user-agent": "Googlebot/2.1"})
    for _ in range(40):                                                                   # the counter runs beside the request, not in front of it
        if _hits(env):
            break
        time.sleep(0.05)
    paths = [h["path"] for h in _hits(env)]
    assert paths == ["/"]


# ----------------------------------------------------------------------------------------------------------------- models
def test_the_purchase_model_scores_engaged_customers_above_idle_ones(env):
    _user(env, "keen", seen_days_ago=0)
    _user(env, "idle", joined_days_ago=40, seen_days_ago=40)
    for d in (1, 3):
        _order(env, "keen", d, "translate")
    sa.record_hit("/client/plans", "", "", "kauli.test", "1.1.1.1", CHROME, "us", "keen")
    conn = env.db.get_conn()
    model = sa.purchase_model(sa.purchase_features(conn))
    conn.close()
    assert model["scores"]["keen"] > 0.5 > model["scores"]["idle"] and model["scores"]["idle"] < 0.2
    assert model["trained"] is False                                                      # too few customers to learn from: it uses the starting weights


def test_the_purchase_model_learns_when_there_are_enough_customers(env):
    for i in range(24):
        _user(env, f"c{i}", seen_days_ago=1)
        if i % 3 == 0:
            _order(env, f"c{i}", 2, "translate")
            _pay(env, f"c{i}", 19)
        elif i % 3 == 1:
            _order(env, f"c{i}", 2, "transcribe")
    conn = env.db.get_conn()
    model = sa.purchase_model(sa.purchase_features(conn))
    conn.close()
    assert model["trained"] is True and model["paying"] == 8
    assert model["weights"]["translated"] > sa.PURCHASE_PRIOR["translated"]               # translating is what the buyers did


def test_churn_risk_rises_with_silence_and_a_failing_card(env):
    _user(env, "fresh", seen_days_ago=0)
    _user(env, "quiet", seen_days_ago=20)
    _user(env, "failing", seen_days_ago=2)
    env.db.set_subscription_plan("failing", "pro", 30)
    conn = env.db.get_conn()
    conn.execute("INSERT INTO payment_failures (user_id, email, first_failed_at, last_failed_at) VALUES ('failing', 'failing@example.com', ?, ?)", (time.time(), time.time()))
    conn.commit()
    scores = sa.churn_scores(conn)
    conn.close()
    assert scores["fresh"]["risk"] == 0 and scores["quiet"]["risk"] >= 0.8 and scores["failing"]["risk"] >= 0.3 and scores["failing"]["failing"]


# --------------------------------------------------------------------------------------------------------------- the data
def test_the_dashboard_numbers_come_from_the_records(env):
    now = time.time()
    for i in range(3):
        sa.record_hit("/", "utm_medium=cpc&utm_source=google" if i == 0 else "", "https://google.com/" if i == 1 else "", "kauli.test", f"10.0.0.{i}",
                      IPHONE if i == 2 else CHROME, "us" if i < 2 else "ke", None, now=now - i * 3600)
    _user(env, "a", joined_days_ago=2, seen_days_ago=0)
    _user(env, "b", joined_days_ago=5, seen_days_ago=1)
    _order(env, "a", 1, "translate", summary=True)
    _order(env, "b", 2, "dub")
    _pay(env, "a", 19, plan="pro")
    env.db.set_subscription_plan("a", "pro", 30)
    d = sa.dashboard(28)
    k = {x["key"]: x for x in d["kpis"]}
    assert k["visitors"]["value"] == 3 and k["signups"]["value"] >= 2 and k["paid"]["value"] == 1 and k["mrr"]["value"] == 19
    assert {c["name"] for c in d["traffic"]["channels"]} >= {"Paid Search", "Organic Search", "Direct"}
    assert abs(sum(c["share"] for c in d["traffic"]["channels"]) - 100) < 0.2
    assert {r["name"]: r["sessions"] for r in d["devices"]["rows"]}["Mobile"] == 1
    assert {c["code"] for c in d["audience"]["countries"]} == {"US", "KE"}
    names = {f["name"]: f["users"] for f in d["features"]}
    assert names["Translation"] >= 2 and names["Dubbing"] == 1 and names["AI summary"] == 1
    assert d["revenue"]["by_plan"][0]["name"] == "Pro" and d["revenue"]["total"] == 19
    assert len(d["labels"]) == 28 and len(d["kpis"][0]["series"]) == 28


def test_cohorts_show_who_came_back(env):
    for i in range(4):
        _user(env, f"w{i}", joined_days_ago=12, seen_days_ago=0 if i < 2 else None)
        _order(env, f"w{i}", 12, "transcribe")                                             # everyone did something in week one
        if i < 2:
            _order(env, f"w{i}", 3, "transcribe")                                          # half of them came back a week and a half later
    d = sa.dashboard(28)
    row = next(r for r in d["engagement"]["cohorts"] if r["size"] >= 4)
    assert row["cells"][0] == 100.0 and row["cells"][1] == 50.0


def test_signups_are_attributed_to_the_channel_they_arrived_through(env):
    now = time.time()
    sa.record_hit("/", "", "https://www.google.com/", "kauli.test", "9.9.9.9", CHROME, "us", None, now=now)
    _user(env, "newbie")
    sa.attribute_signup("newbie", "9.9.9.9", CHROME, now=now + 30)
    assert env.db.get_user("newbie")["signup_channel"] == "Organic Search"


# ------------------------------------------------------------------------------------------------------------------ the page
def _staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


def test_the_staff_dashboard_has_every_section_and_only_staff_see_it(env):
    assert env.client.get("/staff/analytics", follow_redirects=False).status_code in (302, 303, 307)
    _staff(env)
    for n in (7, 28, 90):
        assert env.client.get(f"/staff/analytics?days={n}").status_code == 200
    page = env.client.get("/staff/analytics").text
    for section in ("Total website visitors", "New sign-ups", "Active users (app)", "Paid customers", "Monthly recurring revenue", "Churn rate",
                    "Acquisition &amp; Traffic", "Global Audience", "Audience Insights", "Purchase Likelihood", "Churn Risk", "Quick Insights",
                    "Devices &amp; Browsers", "User Engagement &amp; Retention", "Cohort retention", "Inactive accounts", "Product Usage",
                    "Website Performance &amp; Top Pages", "Revenue &amp; Business Health", "Trial &rarr; Paid conversion", "ARPU", "CAC (paid channels)",
                    "Revenue by plan", "Google Analytics 4"):
        assert section in page, section
    assert "<svg" in page and 'class="sa-map"' in page


def test_ad_spend_gives_the_cost_of_a_paid_customer(env):
    _staff(env)
    _user(env, "ad1", joined_days_ago=3)
    _user(env, "ad2", joined_days_ago=3)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET signup_channel = 'Paid Search' WHERE id IN ('ad1', 'ad2')")
    conn.commit()
    conn.close()
    assert sa.dashboard(30)["revenue"]["cac"] is None
    env.client.post("/staff/analytics/ad-spend", data={"spend": "$600"})
    assert sa.dashboard(30)["revenue"]["cac"] == 300.0                                      # $600 over a month / 2 customers
    env.client.post("/staff/analytics/ad-spend", data={"spend": "abc"})
    assert sa.dashboard(30)["revenue"]["ad_spend"] == 600.0


def test_the_privacy_page_says_how_visits_are_counted(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    text = env.client.get("/privacy").text
    assert "Our own visit counter" in text and "Do Not Track" in text


def test_the_signed_out_message_reads_correctly(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    page = env.client.get("/login?notice=session_expired").text
    assert "You were signed out after a period of inactivity. Please sign in again to continue." in page
    assert "a while inactive" not in page
