"""The staff overview: KPI cards, the overdue banner, the 30-day chart and quick actions, inside the new staff chrome."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import app as app_module  # noqa: E402


def _staff_page(env, wav_bytes, overdue=False):
    o = env.order_from(env.place(wav_bytes))
    env.db.update_order_status(o["id"], "awaiting_review")
    if overdue:
        conn = env.db.get_conn()
        past = time.time() - 5 * 86400
        conn.execute("UPDATE orders SET tat_start_at = ?, deadline_at = ?, internal_deadline_at = ? WHERE id = ?", (past - 86400, past, past, o["id"]))
        conn.commit()
        conn.close()
    env.db.get_or_create_user("staff1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("staff1")), "role": "staff", "display_name": "Godfrey Njoroge"})
    r = env.client.get("/staff")
    assert r.status_code == 200
    return r.text


def test_the_overview_has_the_cards_chart_and_quick_actions(env, wav_bytes):
    page = _staff_page(env, wav_bytes)
    for text in ("so-kpis", "Total Jobs", "In Progress", "In Review", "Completed", "Revenue", "Jobs, last 30 days", "Quick actions", "Review queue",
                 "Manage clients", "Support inbox", "Let's keep things moving.", "staff-ui", "topbar-search-kbd", "Ctrl + K", "account-menu-role"):
        assert text in page, text
    assert "so-alert" not in page                                                      # nothing overdue, no banner


def test_overdue_jobs_get_the_banner_with_a_link_to_them(env, wav_bytes):
    page = _staff_page(env, wav_bytes, overdue=True)
    assert "so-alert" in page and "1 job is overdue" in page and "/staff/jobs?deadline=overdue" in page


def test_the_chart_is_a_smooth_labelled_svg_with_short_dates():
    svg = app_module._daily_trend_svg([{"day": "2026-10-05", "created": 0, "delivered": 0}, {"day": "2026-10-06", "created": 3, "delivered": 1},
                                       {"day": "2026-10-07", "created": 5, "delivered": 4}])
    assert svg.startswith("<svg") and " C" in svg and "trend-fill" in svg and "Oct 5" in svg and "Oct 7" in svg
    assert app_module._daily_trend_svg([]) == ""


# ------------------------------------------------------------------------------------ Production Queue, Reports and CRM
def _staff_get(env, wav_bytes, path):
    _staff_page(env, wav_bytes)
    r = env.client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def test_the_production_queue_has_cards_that_open_their_jobs_and_a_card_view(env, wav_bytes):
    page = _staff_get(env, wav_bytes, "/staff/jobs")
    for text in ("Production Queue", 'data-open-tab="needs_attention"', 'data-open-tab="delivered"', 'id="qp-view-grid"', 'id="filter-client"', 'id="filter-date"',
                 "More filters", "qp-job"):
        assert text in page, text
    assert "Upgrade Plan" not in page and "Upgrade plan" not in page


def test_reports_show_revenue_funnel_chart_and_margin_in_the_new_layout(env, wav_bytes):
    page = _staff_get(env, wav_bytes, "/staff/ops")
    for text in ("Key insights into your business", "All time revenue", "Pending payments", "New leads", "Client funnel", "Revenue over time", "Revenue by provider",
                 "Revenue by type", "AI cost &amp; margin", "Orders created", "Median turnaround", "Failure rate", "/staff/ops/export.csv", "Queue health"):
        assert text in page, text
    csv = env.client.get("/staff/ops/export.csv?days=7")
    assert csv.status_code == 200 and "Revenue (period)" in csv.text and "Day,Revenue,Orders,New clients" in csv.text


def test_the_crm_has_the_six_cards_source_and_category_charts_onboarding_and_the_funnel(env, wav_bytes):
    env.db.create_lead("Amina", "amina@example.org", None, "Acme NGO", "Need subtitles", None, source="website")
    page = _staff_get(env, wav_bytes, "/staff/leads")
    for text in ("Turn conversations into customers", "New leads", "Contacted", "Qualified", "Proposal sent", "Won", "Lost", "Leads by source", "Leads by category",
                 "Client onboarding", "Quick actions", "Conversion funnel", "Recent leads", 'id="crm-search"', "Amina"):
        assert text in page, text
    assert 'id="log-lead-form"' in page and "Upgrade" not in page


def test_the_clients_list_and_a_client_page_follow_the_new_layout(env, wav_bytes):
    page = _staff_get(env, wav_bytes, "/staff/clients")
    for text in ("Manage all your clients in one place", 'id="cl-pills"', 'data-show="inactive"', "More filters", 'id="cl-plan"', "Showing", "cl-table", "Orders", "Spent", "Last seen"):
        assert text in page, text
    assert "Add client" not in page
    detail = env.client.get("/staff/clients/u1")
    assert detail.status_code == 200
    for text in ("cl-hero", "cl-tabs", "Completed orders", "In progress", "Needs action", "Total spent", "Account", "Activity", "Recent orders", 'data-copy="'):
        assert text in detail.text, text
    for tab in ("orders", "team", "billing", "settings"):
        r = env.client.get(f"/staff/clients/u1?tab={tab}")
        assert r.status_code == 200 and "cl-tabs" in r.text, tab
    assert "Internal or test account" in detail.text


def test_the_staff_sidebar_is_grouped_and_keeps_every_page(env, wav_bytes):
    page = _staff_get(env, wav_bytes, "/staff")
    for section in ("Operations", "Insights", "Content", "Finance", "Help"):
        assert f"<span>{section}</span>" in page, section
    side = page.split('<aside class="sidebar"')[1].split("</aside>")[0]
    for href in ("/staff", "/staff/jobs", "/staff/exceptions", "/staff/leads", "/staff/clients", "/staff/engagement", "/staff/winback", "/staff/exit-feedback", "/staff/support",
                 "/staff/ops", "/staff/analytics", "/staff/translations", "/staff/models", "/staff/providers", "/staff/blog", "/staff/blog-audio", "/staff/newsletter",
                 "/staff/billing", "/staff/payment-failures", "/staff/guide", "/staff/handbook", "/staff/resources", "/status", "/settings"):
        assert f'href="{href}"' in side, href
    assert "nav-toggle" in side and 'class="nav-grp' in side
    jobs = env.client.get("/staff/exceptions").text.split('<aside class="sidebar"')[1].split("</aside>")[0]
    assert 'class="nav-grp is-open"' in jobs.split('data-nav-grp="/staff/jobs"')[0][-60:]               # a page inside a group opens its group


def test_the_blog_item_is_not_highlighted_on_the_blog_audio_page(env, wav_bytes):
    _staff_page(env, wav_bytes)
    side = env.client.get("/staff/blog-audio").text.split('<aside class="sidebar"')[1].split("</aside>")[0]
    blog_row = side.split('data-nav-grp="/staff/blog"')[1].split("nav-sub")[0]
    assert 'class="active"' not in blog_row
