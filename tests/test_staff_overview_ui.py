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
