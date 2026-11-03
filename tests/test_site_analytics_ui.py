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
