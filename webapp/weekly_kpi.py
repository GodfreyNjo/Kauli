"""The Monday email: the numbers that say whether the business is working, for real customers only, next to the day-30 targets from the growth plan.

Percentages with a handful of customers are noisy, so each row also carries the plain count behind it. Internal and test accounts are left out
(see site_analytics._exclude_internal). The email is sent once a week, on Monday (UTC), to KAULI_KPI_EMAIL or the internal notification address;
staff can also preview it and send it now from the Site analytics page.
"""
from __future__ import annotations

import html
import os
import time

from . import db, onboarding, site_analytics

# (key, label, target at day 30, higher_is_better, unit)
TARGETS = [
    ("visitors", "Visitors in the last 28 days", 700, True, ""),
    ("signup_rate", "Visitor to sign-up", 2.5, True, "%"),
    ("upload_rate", "Sign-up to first upload", 60.0, True, "%"),
    ("pay_rate", "First upload to first payment (within 14 days)", 15.0, True, "%"),
    ("ttfr", "Median time to first result (minutes)", 10.0, False, " min"),
    ("returning", "Returning customers", 25.0, True, "%"),
    ("paying", "Paying customers (real, all time)", 5, True, ""),
    ("revenue", "Revenue in the last 28 days", 100.0, True, "$"),
    ("ratings", "Rated deliveries (last 90 days)", 20, True, ""),
]


def build(now: float | None = None) -> dict:
    now = now or time.time()
    d = site_analytics.dashboard(28, now)
    kp = {k["key"]: k["value"] for k in d["kpis"]}
    conn = db.get_conn()
    try:
        site_analytics._exclude_internal(conn)
        start = now - 28 * 86400
        new_ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE role = 'client' AND created_at >= ?", (start,)).fetchall()]
        uploaded = 0
        if new_ids:
            marks = ",".join("?" for _ in new_ids)
            uploaded = conn.execute(f"SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE client_id IN ({marks})", new_ids).fetchone()["n"]
        cohort = conn.execute("SELECT client_id, MIN(created_at) AS m FROM orders GROUP BY client_id HAVING m BETWEEN ? AND ?", (now - 90 * 86400, now - 14 * 86400)).fetchall()
        paid_fast = 0
        for r in cohort:
            if conn.execute("SELECT 1 FROM payments WHERE user_id = ? AND status = 'completed' AND amount_usd > 0 AND completed_at BETWEEN ? AND ?",
                            (r["client_id"], r["m"], r["m"] + 14 * 86400)).fetchone():
                paid_fast += 1
        paying = conn.execute("SELECT COUNT(DISTINCT user_id) AS n FROM payments WHERE status = 'completed' AND amount_usd > 0").fetchone()["n"]
    finally:
        conn.close()
    ttfv = onboarding.ttfv_minutes(90, now)
    csat = onboarding.csat_summary(90, now)
    visitors, signups = kp.get("visitors", 0), kp.get("signups", 0)
    values = {
        "visitors": (visitors, f"{visitors:,.0f}", None),
        "signup_rate": (100.0 * signups / visitors if visitors else 0.0, f"{(100.0 * signups / visitors) if visitors else 0.0:.1f}%", f"{signups:,.0f} sign-ups"),
        "upload_rate": (100.0 * uploaded / len(new_ids) if new_ids else None, f"{(100.0 * uploaded / len(new_ids)):.0f}%" if new_ids else "-", f"{uploaded} of {len(new_ids)}"),
        "pay_rate": (100.0 * paid_fast / len(cohort) if cohort else None, f"{(100.0 * paid_fast / len(cohort)):.0f}%" if cohort else "-", f"{paid_fast} of {len(cohort)}"),
        "ttfr": (ttfv.get("median"), f"{ttfv['median']:.0f} min" if ttfv.get("median") is not None else "-", f"{ttfv.get('joined', 0)} new customers"),
        "returning": (d["engagement"]["returning"], f"{d['engagement']['returning']:.0f}%", None),
        "paying": (paying, f"{paying}", None),
        "revenue": (d["revenue"]["total"], f"${d['revenue']['total']:,.2f}", None),
        "ratings": (csat.get("n", 0), f"{csat.get('n', 0)}", f"average {csat['avg']}/5" if csat.get("avg") else None),
    }
    rows = []
    for key, label, target, higher, unit in TARGETS:
        value, shown, detail = values[key]
        if value is None:
            status = "none"
        else:
            status = "good" if (value >= target if higher else value <= target) else "behind"
        target_shown = f"${target:,.0f}" if unit == "$" else f"{target:g}{unit}"
        rows.append({"key": key, "label": label, "value": shown, "detail": detail, "target": target_shown, "status": status})
    return {"rows": rows, "generated": time.strftime("%A %d %B %Y", time.gmtime(now)), "real_only": True, "internal_excluded": d.get("internal_excluded", 0)}


def to_html(report: dict, base_url: str) -> str:
    colors = {"good": "#0f7a45", "behind": "#b3261e", "none": "#6b7b94"}
    marks = {"good": "On target", "behind": "Behind", "none": "Not enough data"}
    body = "".join(
        f'<tr><td style="padding:8px 10px;border-bottom:1px solid #e6edf7">{html.escape(r["label"])}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #e6edf7;text-align:right"><strong>{html.escape(r["value"])}</strong>'
        f'{"<br><span style=color:#6b7b94;font-size:12px>" + html.escape(r["detail"]) + "</span>" if r["detail"] else ""}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #e6edf7;text-align:right;color:#6b7b94">{html.escape(r["target"])}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid #e6edf7;color:{colors[r["status"]]};font-weight:700">{marks[r["status"]]}</td></tr>' for r in report["rows"])
    return (f'<h2 style="margin:0 0 4px">Kauli weekly numbers</h2><p style="color:#5a6b85;margin:0 0 14px">{html.escape(report["generated"])}. Real customers only '
            f'({report["internal_excluded"]} internal or test accounts left out). Judge progress by the counts, not the percentages, while the numbers are small.</p>'
            '<table style="border-collapse:collapse;width:100%;font-size:14px"><thead><tr><th align="left" style="padding:8px 10px;border-bottom:2px solid #d7e4fb">Measure</th>'
            '<th align="right" style="padding:8px 10px;border-bottom:2px solid #d7e4fb">This week</th><th align="right" style="padding:8px 10px;border-bottom:2px solid #d7e4fb">Day-30 target</th>'
            f'<th align="left" style="padding:8px 10px;border-bottom:2px solid #d7e4fb">Status</th></tr></thead><tbody>{body}</tbody></table>'
            f'<p style="margin-top:14px"><a href="{base_url.rstrip("/")}/staff/analytics">Open Site analytics</a></p>')


def recipient() -> str:
    return (os.environ.get("KAULI_KPI_EMAIL") or os.environ.get("INTERNAL_NOTIFY_EMAIL") or "").strip()


def week_key(now: float | None = None) -> str:
    import datetime
    year, week, _ = datetime.datetime.utcfromtimestamp(now or time.time()).isocalendar()
    return f"{year}-W{week:02d}"


def send(base_url: str, send_email, wrap_email_html) -> tuple[bool, str]:
    """Builds and sends the email now. send_email and wrap_email_html are the mailer's functions (passed in so tests can fake them)."""
    to = recipient()
    if not to:
        return False, "no KAULI_KPI_EMAIL or INTERNAL_NOTIFY_EMAIL set"
    report = build()
    text = "\n".join(f"{r['label']}: {r['value']} (target {r['target']})" for r in report["rows"])
    ok, detail = send_email(to, "Kauli weekly numbers", wrap_email_html(to_html(report, base_url), cta_text="Open Site analytics", cta_url=f"{base_url.rstrip('/')}/staff/analytics"), text,
                            tags=["weekly-kpi"], sender_name="Kauli Marketing")
    return ok, detail


def send_if_due(base_url: str, send_email, wrap_email_html, now: float | None = None) -> bool:
    """On Monday (UTC), once per week. True when it was sent."""
    now = now or time.time()
    if time.gmtime(now).tm_wday != 0 or db.get_flag("weekly_kpi_week") == week_key(now):
        return False
    ok, _ = send(base_url, send_email, wrap_email_html)
    if ok:
        db.set_flag("weekly_kpi_week", week_key(now))
    return ok
