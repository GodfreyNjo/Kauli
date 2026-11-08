"""Staff > Site analytics: the Google Analytics reader, the plain-language findings, the visitor-side events and the product-use events."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402
from webapp import ga_reports  # noqa: E402


def _row(dims, mets, dim_names, met_names):
    return {"dimensionValues": [{"value": d} for d in dims], "metricValues": [{"value": str(m)} for m in mets]}


def _report(dim_names, met_names, rows):
    return {"dimensionHeaders": [{"name": n} for n in dim_names], "metricHeaders": [{"name": n} for n in met_names],
            "rows": [_row(d, m, dim_names, met_names) for d, m in rows]}


def _fake_data(**over):
    base = {"days": 28,
            "totals": {"this": {"activeUsers": 1200.0, "newUsers": 900.0, "sessions": 1500.0, "engagementRate": 0.62, "averageSessionDuration": 95.0, "screenPageViews": 3000.0, "keyEvents": 40.0},
                       "previous": {"activeUsers": 1000.0, "newUsers": 700.0, "sessions": 1200.0, "engagementRate": 0.6, "averageSessionDuration": 90.0, "screenPageViews": 2500.0, "keyEvents": 30.0}},
            "daily": [{"date": "20261020", "activeUsers": 40.0, "sessions": 50.0, "keyEvents": 1.0}, {"date": "20261021", "activeUsers": 55.0, "sessions": 60.0, "keyEvents": 2.0}],
            "channels": [{"sessionDefaultChannelGroup": "Organic Search", "sessions": 800.0, "activeUsers": 700.0, "engagementRate": 0.7, "keyEvents": 30.0}],
            "pages": [{"pagePath": "/", "screenPageViews": 900.0, "activeUsers": 800.0, "userEngagementDuration": 40000.0, "keyEvents": 20.0}],
            "countries": [{"country": "United States", "activeUsers": 700.0, "sessions": 800.0, "engagementRate": 0.65, "keyEvents": 30.0}],
            "devices": [{"deviceCategory": "desktop", "sessions": 800.0, "engagementRate": 0.7, "keyEvents": 30.0}, {"deviceCategory": "mobile", "sessions": 700.0, "engagementRate": 0.55, "keyEvents": 10.0}],
            "sources": [{"sessionSourceMedium": "google / organic", "sessions": 800.0, "engagementRate": 0.7, "keyEvents": 30.0}],
            "events": [{"eventName": "cta_click", "eventCount": 200.0, "totalUsers": 150.0}, {"eventName": "sign_up", "eventCount": 40.0, "totalUsers": 40.0},
                       {"eventName": "order_submitted", "eventCount": 25.0, "totalUsers": 20.0}, {"eventName": "purchase", "eventCount": 8.0, "totalUsers": 8.0}],
            "landing": [{"landingPage": "/", "sessions": 600.0, "engagementRate": 0.6, "keyEvents": 20.0}], "fetched_at": time.time()}
    base.update(over)
    return base


# ------------------------------------------------------------------------------------------------ the reader
def test_it_is_inert_until_every_setting_is_there(monkeypatch, tmp_path):
    for k in ("GA4_PROPERTY_ID", "GA4_SERVICE_ACCOUNT_FILE", "GA4_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(k, raising=False)
    assert ga_reports.configured() is False
    monkeypatch.setenv("GA4_PROPERTY_ID", "properties/123456")
    assert ga_reports.property_id() == "123456" and ga_reports.configured() is False
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "a@b.iam", "private_key": "k"}))
    assert ga_reports.configured() is True
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_JSON", "not json")
    assert ga_reports.configured() is False
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"client_email": "a@b.iam", "private_key": "k"}), encoding="utf-8")
    monkeypatch.delenv("GA4_SERVICE_ACCOUNT_JSON")
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_FILE", str(path))
    assert ga_reports.configured() is True


def test_report_rows_become_plain_dicts():
    rep = _report(["country"], ["activeUsers", "keyEvents"], [(["United States"], [10, 2]), (["Kenya"], [4, 0])])
    assert ga_reports.table(rep) == [{"country": "United States", "activeUsers": 10.0, "keyEvents": 2.0}, {"country": "Kenya", "activeUsers": 4.0, "keyEvents": 0.0}]
    assert ga_reports.table({}) == []


def test_the_overview_asks_google_in_two_batches_and_caches_the_answer(monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "99")
    ga_reports._cache.clear()
    calls = []
    t_dims, t_mets = ["dateRange"], ["activeUsers", "keyEvents"]

    def fake_batch(requests):
        calls.append(requests)
        if len(requests) == 5:
            return [_report(["dateRange"], ["activeUsers", "newUsers", "sessions", "engagementRate", "averageSessionDuration", "screenPageViews", "keyEvents"],
                            [(["this"], [10, 5, 12, 0.5, 60, 30, 3]), (["previous"], [8, 4, 9, 0.4, 50, 20, 1])]),
                    _report(["date"], ["activeUsers", "sessions", "keyEvents"], [(["20261020"], [3, 4, 1])]),
                    _report(["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "engagementRate", "keyEvents"], [(["Direct"], [5, 4, 0.5, 1])]),
                    _report(["pagePath"], ["screenPageViews", "activeUsers", "userEngagementDuration", "keyEvents"], [(["/"], [20, 8, 400, 2])]),
                    _report(["country"], ["activeUsers", "sessions", "engagementRate", "keyEvents"], [(["United States"], [8, 9, 0.5, 2])])]
        return [_report(["deviceCategory"], ["sessions", "engagementRate", "keyEvents"], [(["desktop"], [9, 0.5, 2])]),
                _report(["sessionSourceMedium"], ["sessions", "engagementRate", "keyEvents"], [(["google / organic"], [6, 0.6, 2])]),
                _report(["eventName"], ["eventCount", "totalUsers"], [(["sign_up"], [3, 3])]),
                _report(["landingPage"], ["sessions", "engagementRate", "keyEvents"], [(["/"], [9, 0.5, 2])])]

    monkeypatch.setattr(ga_reports, "_batch", fake_batch)
    data = ga_reports.overview(28)
    assert data["totals"]["this"]["activeUsers"] == 10 and data["totals"]["previous"]["activeUsers"] == 8
    assert data["channels"][0]["sessionDefaultChannelGroup"] == "Direct" and data["events"][0]["eventName"] == "sign_up"
    assert len(calls) == 2 and len(calls[0]) == 5 and len(calls[1]) == 4
    assert calls[0][0]["dateRanges"][1]["name"] == "previous"
    ga_reports.overview(28)
    assert len(calls) == 2                                                                   # the second look is served from the cache
    ga_reports.overview(28, force=True)
    assert len(calls) == 4


def test_google_errors_are_explained_in_plain_words(monkeypatch):
    class R:
        def __init__(self, code, text="x"):
            self.status_code, self.text = code, text

        def json(self):
            return {}
    monkeypatch.setenv("GA4_PROPERTY_ID", "99")
    monkeypatch.setattr(ga_reports, "_access_token", lambda: "t")
    for code, words in ((403, "Viewer"), (404, "Property ID"), (500, "500")):
        monkeypatch.setattr(ga_reports.httpx, "post", lambda *a, **k: R(code))
        try:
            ga_reports._batch([{}])
        except ga_reports.GAError as exc:
            assert words in str(exc), (code, str(exc))
        else:
            raise AssertionError(code)


def test_the_token_request_is_signed_with_the_service_account_key(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = key.public_key()
    monkeypatch.setenv("GA4_PROPERTY_ID", "1")
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": pem}))
    ga_reports._token.update(value=None, expires=0)
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    def fake_post(url, data=None, **k):
        seen.update(url=url, data=data)
        return R()
    monkeypatch.setattr(ga_reports.httpx, "post", fake_post)
    assert ga_reports._access_token() == "tok"
    claims = jwt.decode(seen["data"]["assertion"], pub, algorithms=["RS256"], audience=ga_reports.TOKEN_URL)
    assert claims["iss"] == "svc@proj.iam.gserviceaccount.com" and claims["scope"] == ga_reports.SCOPE and "readonly" in claims["scope"]
    assert seen["data"]["grant_type"].endswith("jwt-bearer")
    assert ga_reports._access_token() == "tok"                                              # kept until it nearly expires


# ------------------------------------------------------------------------------------------------ the findings
def test_good_numbers_give_a_calm_summary_and_growth_is_noticed():
    out = ga_reports.insights(_fake_data())
    assert any(i["level"] == "good" and "up 20%" in i["title"] for i in out)


def test_each_rule_fires_on_its_own_problem():
    d = _fake_data()
    d["totals"]["this"]["activeUsers"] = 500.0
    assert any("down 50%" in i["title"] for i in ga_reports.insights(d))
    d = _fake_data()
    d["totals"]["this"]["keyEvents"] = 0.0
    assert any("key events" in i["title"].lower() for i in ga_reports.insights(d))
    d = _fake_data()
    d["channels"] = [{"sessionDefaultChannelGroup": "Paid Social", "sessions": 120.0, "activeUsers": 100.0, "engagementRate": 0.3, "keyEvents": 0.0}]
    assert any("Paid Social" in i["title"] and "no sign-ups" in i["title"] for i in ga_reports.insights(d))
    d = _fake_data()
    d["pages"] = [{"pagePath": "/solutions/ngos", "screenPageViews": 80.0, "activeUsers": 70.0, "userEngagementDuration": 350.0, "keyEvents": 0.0}]
    assert any("/solutions/ngos" in i["title"] for i in ga_reports.insights(d))
    d = _fake_data()
    d["devices"] = [{"deviceCategory": "desktop", "sessions": 500.0, "engagementRate": 0.8, "keyEvents": 20.0}, {"deviceCategory": "mobile", "sessions": 500.0, "engagementRate": 0.4, "keyEvents": 2.0}]
    assert any("Mobile visitors" in i["title"] for i in ga_reports.insights(d))
    d = _fake_data()
    d["events"][0]["eventCount"], d["events"][1]["eventCount"] = 300.0, 12.0
    assert any("click a start button" in i["title"] for i in ga_reports.insights(d))
    d = _fake_data()
    d["countries"] = [{"country": "India", "activeUsers": 600.0, "sessions": 700.0, "engagementRate": 0.5, "keyEvents": 3.0}, {"country": "United States", "activeUsers": 100.0, "sessions": 120.0, "engagementRate": 0.6, "keyEvents": 5.0}]
    assert any("United States" in i["title"] for i in ga_reports.insights(d))
    empty = _fake_data()
    empty["totals"]["this"] = {}
    assert ga_reports.insights(empty)[0]["title"].startswith("No visits")


def test_the_funnel_runs_from_visitors_to_payment():
    steps = ga_reports.funnel(_fake_data())
    assert [s["label"] for s in steps] == ["Visitors", "Clicked a start button", "Created an account", "Placed an order", "Paid"]
    assert steps[0]["share"] == 100.0 and steps[4]["count"] == 8.0 and round(steps[2]["share"], 1) == 3.3


# ------------------------------------------------------------------------------------------------ the page
def _staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


def test_the_page_is_staff_only(env):
    r = env.client.get("/staff/analytics")
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_without_google_connected_it_explains_the_setup_and_still_shows_kauli_numbers(env, wav_bytes, monkeypatch):
    for k in ("GA4_PROPERTY_ID", "GA4_SERVICE_ACCOUNT_FILE", "GA4_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(k, raising=False)
    _finished(env, wav_bytes)
    _staff(env)
    page = env.client.get("/staff/analytics").text
    assert "Connect Google Analytics" in page and "GA4_PROPERTY_ID" in page and "Property access management" in page
    assert "Inside Kauli" in page and "Orders placed" in page and "Blog readers" in page
    assert env.client.get("/staff/analytics?days=7").status_code == 200 and env.client.get("/staff/analytics?days=9999").status_code == 200
    assert 'href="/staff/analytics"' in page


def test_with_google_connected_the_numbers_findings_and_funnel_are_shown(env, monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "99")
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "a@b.iam", "private_key": "k"}))
    monkeypatch.setattr(ga_reports, "overview", lambda days, force=False: _fake_data(days=days))
    _staff(env)
    page = env.client.get("/staff/analytics").text
    for needle in ("Visitors", "1,200", "+20%", "What to look at", "Organic Search", "google / organic", "United States", "Desktop", "cta_click",
                   "From visit to payment", "Created an account", "Most viewed pages", "<svg"):
        assert needle in page, needle
    assert "Connect Google Analytics" not in page


def test_a_google_problem_is_shown_in_words_not_as_a_crash(env, monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "99")
    monkeypatch.setenv("GA4_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "a@b.iam", "private_key": "k"}))

    def boom(days, force=False):
        raise ga_reports.GAError("The service account can read nothing here. Add its email as a Viewer.")
    monkeypatch.setattr(ga_reports, "overview", boom)
    _staff(env)
    r = env.client.get("/staff/analytics")
    assert r.status_code == 200 and "Add its email as a Viewer" in r.text and "Inside Kauli" in r.text


def test_the_internal_numbers_count_real_orders_and_readers(env, wav_bytes):
    from webapp import account_hub
    _finished(env, wav_bytes)
    env.db.get_or_create_user("u9", "n@example.com", "client")
    stats = account_hub.internal_site_stats(28)
    assert stats["orders"] >= 1 and stats["new_clients_ordering"] >= 1
    assert set(stats) >= {"orders", "paid", "revenue", "leads", "blog_views", "posts", "countries", "wizard", "orders_by_day"}


# ------------------------------------------------------------------------------------------------ events
def test_visitor_side_events_are_defined_once_and_only_fire_after_consent(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    html = env.client.get("/").text
    for needle in ("window.kauliTrack", "'scroll_depth'", "'view_pricing'", "'video_play'", "'video_complete'", "'form_start'", "'contact_click'", "'cta_click'"):
        assert needle in html, needle
    assert "typeof window.gtag === 'function'" in html                                        # nothing is sent before analytics is loaded
    js = env.client.get("/static/hear_demo.js").text
    for needle in ('track("demo_play"', 'track("demo_scenario"', 'track("demo_language"'):
        assert needle in js, needle


def test_product_use_events_carry_kinds_and_counts_never_personal_data(env, wav_bytes, monkeypatch):
    sent = []
    monkeypatch.setattr(env.app.ga_events, "send_event", lambda client_id, name, params=None: sent.append((name, params or {})))
    order = _finished(env, wav_bytes)
    env.client.get(f"/account/orders/{order['id']}/export?format=csv")
    env.client.post(f"/account/orders/{order['id']}/share", json={"action": "create"})
    env.client.post("/account/notes/new")
    env.client.post(f"/account/orders/{order['id']}/notes?as=transcript")
    env.client.post("/account/vocabulary/add", data={"entries": "Secret Name | sekrit"})
    env.client.post("/account/assistant/message", json={"text": "how do I download?"})
    env.client.post("/account/calendar/interest", data={"provider": "google_calendar"})
    names = [n for n, _ in sent]
    for expected in ("transcript_download", "transcript_share", "note_created", "vocabulary_added", "assistant_question", "calendar_interest"):
        assert expected in names, (expected, names)
    flat = json.dumps(sent)
    for private in ("Secret Name", "sekrit", "clip.wav", "client@example.com", "how do I download", "Hello there"):
        assert private not in flat, private
    assert dict(sent)["transcript_download"] == {"format": "csv", "side": "source", "split": "line"}


def test_measuring_can_never_break_the_feature(env, wav_bytes, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ga down")
    order = _finished(env, wav_bytes)
    monkeypatch.setattr(env.app.ga_events, "send_event", boom)
    assert env.client.get(f"/account/orders/{order['id']}/export?format=txt").status_code == 200
    assert env.client.post("/account/notes/new").status_code == 303
