"""The staff Site analytics tab: the new layout, the extras (live now, funnel, activity, channel quality, heat map) and the CSV export."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import site_analytics  # noqa: E402


def _staff(env):
    env.db.get_or_create_user("staff1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("staff1")), "role": "staff", "display_name": "Godfrey Njoroge"})


def _seed_hits(env, n=6):
    conn = env.db.get_conn()
    now = time.time()
    for i in range(n):
        ts = now - i * 600
        conn.execute("INSERT INTO site_hits (ts, day, visitor, session, path, is_landing, channel, source, medium, country, device, browser) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, time.strftime("%Y-%m-%d", time.gmtime(ts)), f"v{i}", f"s{i}", "/" if i % 2 else "/login", 1, "Organic Search", "google", "organic", "US", "Desktop", "Chrome"))
    conn.commit()
    conn.close()


def test_the_page_has_every_section_of_the_new_layout(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    _seed_hits(env)
    _staff(env)
    page = env.client.get("/staff/analytics")
    assert page.status_code == 200
    for text in ("Your audience. Your growth.", "Live data", "Traffic sources", "Visitors over time", "Audience by location", "Devices &amp; browsers", "Top pages",
                 "User engagement", "Conversion funnel", "Revenue &amp; business health", "Feature usage", "Audience insights", "Recent activity",
                 "Which channels bring customers", "When visitors arrive", "Cohort retention", "Export CSV", "sx-kpis"):
        assert text in page.text, text
    assert "5 on the site now" in page.text or "6 on the site now" in page.text or "on the site now" in page.text


def test_extras_count_live_visitors_the_funnel_and_recent_events(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    _seed_hits(env, 6)
    ex = site_analytics.extras(28, time.time(), visitors=6, signups=3, paid=1)
    assert ex["live"] >= 1
    labels = [f["label"] for f in ex["funnel"]]
    assert labels == ["Visited the site", "Signed up", "Placed an order", "Became a paying customer"]
    assert ex["funnel"][0]["share"] == 100.0 and ex["funnel"][1]["share"] == 50.0 and ex["funnel"][1]["step"] == 50.0
    assert any(e["kind"] == "order" for e in ex["activity"]) and all("ago" in e for e in ex["activity"])
    assert ex["heat"]["peak"] >= 1 and len(ex["heat"]["grid"]) == 7 and len(ex["heat"]["grid"][0]) == 24 and ex["heat"]["best"]


def test_ago_reads_naturally():
    assert site_analytics.ago(5) == "just now" and site_analytics.ago(300) == "5m ago" and site_analytics.ago(7200) == "2h ago" and site_analytics.ago(3 * 86400) == "3d ago"


def test_the_csv_export_is_for_staff_only_and_has_the_numbers(env, wav_bytes):
    _seed_hits(env)
    assert env.client.get("/staff/analytics/export.csv", follow_redirects=False).status_code in (303, 401, 403)
    _staff(env)
    r = env.client.get("/staff/analytics/export.csv?days=7")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    body = r.text
    for text in ("Metric,Value", "Total website visitors", "Funnel step", "Channel,Sessions", "Day,Visitors,Sessions"):
        assert text in body, text


# ------------------------------------------------------------------------------------------------ real customers only
def test_internal_and_test_accounts_are_left_out_of_every_business_number(env, wav_bytes):
    o = env.order_from(env.place(wav_bytes))
    env.db.create_payment("p1", "u1", "pro", 10.0, None, "USD", "paystack")
    env.db.complete_payment("p1", "ref-1", 30)
    everyone = site_analytics.dashboard(28, include_internal=True)
    real = site_analytics.dashboard(28)
    assert everyone["internal_excluded"] == 0 and everyone["include_internal"]
    k_all = {k["key"]: k["value"] for k in everyone["kpis"]}
    assert k_all["signups"] >= 1 and k_all["paid"] >= 1
    env.db.set_user_internal("u1", True)
    real = site_analytics.dashboard(28)
    k = {x["key"]: x["value"] for x in real["kpis"]}
    assert k["signups"] == 0 and k["paid"] == 0 and k["mrr"] == 0 and real["internal_excluded"] == 1
    assert real["revenue"]["total"] == 0 and not real["activity"] or all(e["kind"] != "payment" for e in real["activity"])
    assert {x["key"]: x["value"] for x in site_analytics.dashboard(28, include_internal=True)["kpis"]}["signups"] >= 1       # the raw view still has them


def test_the_address_lists_mark_accounts_as_internal_automatically(env, monkeypatch):
    monkeypatch.setenv("KAULI_INTERNAL_DOMAINS", "forge-team.test")
    monkeypatch.setenv("KAULI_INTERNAL_EMAILS", "friend@gmail.com")
    assert env.db.is_internal_email("anyone@forge-team.test") and env.db.is_internal_email("Friend@Gmail.com") and not env.db.is_internal_email("customer@gmail.com")
    row, was_new = env.db.get_or_create_user("new1", "ops@forge-team.test", "client")
    assert env.db.get_user("new1")["is_internal"] == 1
    env.db.get_or_create_user("new2", "real@customer.test", "client")
    assert env.db.get_user("new2")["is_internal"] == 0
    env.db.get_or_create_user("new3", "friend@gmail.com", "client")
    env.db.set_user_internal("new3", False)
    assert env.db.flag_internal_by_rule() >= 1 and env.db.get_user("new3")["is_internal"] == 1


def test_staff_can_mark_a_client_internal_and_the_analytics_page_says_so(env, wav_bytes):
    _staff(env)
    r = env.client.post("/staff/clients/u1/internal", data={"internal": "1"}, follow_redirects=False)
    assert r.status_code == 303 and env.db.get_user("u1")["is_internal"] == 1
    page = env.client.get("/staff/analytics").text
    assert "Real customers only: 1 internal or test account left out" in page
    assert "Showing <strong>everyone</strong>" in env.client.get("/staff/analytics?internal=1").text
    env.client.post("/staff/clients/u1/internal", data={}, follow_redirects=False)
    assert env.db.get_user("u1")["is_internal"] == 0


def test_the_page_has_a_link_tagger_for_utm_sources(env):
    _staff(env)
    page = env.client.get("/staff/analytics").text
    assert "Tag a link" in page and 'id="utm-source"' in page and "utm_campaign" in page and 'id="utm-copy"' in page
