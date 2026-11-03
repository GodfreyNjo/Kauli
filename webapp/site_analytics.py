"""Kauli's own analytics, alongside Google Analytics: a cookie-free visit counter, and the engines that turn visits, orders and payments into
the numbers on the staff Site analytics tab (traffic, audience, retention, product use, revenue) plus two models: who is likely to buy, and
who is likely to leave.

Privacy by design: no cookie, no IP address and no raw user-agent is stored. A visitor is a hash of the address and browser mixed with a
secret and the date, so it cannot be followed from one day to the next or turned back into a person. Visits by signed-in customers also
carry their account id (so the app side can be measured); people who send Do Not Track or Global Privacy Control are not counted.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from . import billing, db, onboarding

DAY = 86400
SESSION_GAP = 30 * 60
SKIP_PREFIXES = ("/static/", "/api/", "/staff", "/webhooks", "/lang/", "/mcp", "/login/google", "/billing/callback", "/receipts", "/media",
                 "/download", "/healthz", "/status", "/sitemap", "/robots", "/llms", "/feed", "/podcast", "/.well-known", "/favicon", "/sw.js")
_BOT = re.compile(r"bot|crawl|spider|slurp|headless|monitor|uptime|curl|wget|python-requests|httpx|preview|facebookexternalhit|lighthouse|pingdom|go-http", re.I)
SEARCH_HOSTS = ("google.", "bing.", "duckduckgo.", "yahoo.", "baidu.", "ecosia.", "yandex.", "brave.", "startpage.", "ask.com")
SOCIAL_HOSTS = ("facebook.", "fb.com", "instagram.", "t.co", "twitter.", "x.com", "linkedin.", "lnkd.in", "youtube.", "youtu.be", "reddit.",
                "tiktok.", "pinterest.", "whatsapp.", "wa.me", "telegram.", "t.me", "medium.com", "quora.")
CHANNELS = ["Organic Search", "Direct", "Social Media", "Paid Search", "Referral", "Email"]
PAID_CHANNELS = ("Paid Search", "Paid Social")


# ------------------------------------------------------------------------------------------------------------- the counter
def is_bot(ua: str) -> bool:
    return not ua or bool(_BOT.search(ua))


def device_of(ua: str) -> str:
    if re.search(r"ipad|tablet|kindle|playbook", ua, re.I):
        return "Tablet"
    if re.search(r"mobi|android|iphone|ipod", ua, re.I):
        return "Mobile"
    return "Desktop"


def browser_of(ua: str) -> str:
    if "Edg/" in ua or "EdgA/" in ua:
        return "Edge"
    if "OPR/" in ua or "Opera" in ua:
        return "Opera"
    if "Firefox/" in ua or "FxiOS" in ua:
        return "Firefox"
    if "Chrome/" in ua or "CriOS" in ua:
        return "Chrome"
    if "Safari/" in ua:
        return "Safari"
    return "Others"


def classify(referrer: str, own_host: str, query: dict) -> tuple[str, str, str]:
    """(channel, source, medium) for a visit that starts a session."""
    medium = (query.get("utm_medium") or [""])[0].lower()
    source = (query.get("utm_source") or [""])[0].lower()
    host = (urlparse(referrer).hostname or "").lower() if referrer else ""
    if host.startswith("www."):
        host = host[4:]
    if medium in ("cpc", "ppc", "paid", "paidsearch", "paid_search", "paidsocial", "paid_social", "display"):
        social = any(s in source for s in ("facebook", "instagram", "linkedin", "tiktok", "twitter", "x", "reddit", "youtube")) or "social" in medium
        return ("Paid Social" if social else "Paid Search"), source or host or "paid", medium
    if medium in ("email", "e-mail", "newsletter") or source in ("email", "newsletter", "brevo"):
        return "Email", source or "email", medium or "email"
    if host and own_host and (host == own_host or host.endswith("." + own_host)):
        return "Direct", "direct", "none"
    if host and any(h in host for h in SEARCH_HOSTS):
        return "Organic Search", host, "organic"
    if host and any(h in host for h in SOCIAL_HOSTS):
        return "Social Media", host, "social"
    if host:
        return "Referral", host, "referral"
    if source:
        return "Referral", source, medium or "referral"
    return "Direct", "direct", "none"


def _visitor_hash(ip: str, ua: str, day: str) -> str:
    salt = os.environ.get("KAULI_SESSION_SECRET") or os.environ.get("SECRET_KEY") or "kauli"
    return hashlib.sha256(f"{salt}|{day}|{ip}|{ua}".encode()).hexdigest()[:16]


def should_count(method: str, path: str, status: int, content_type: str, ua: str, dnt: bool) -> bool:
    if method != "GET" or status != 200 or dnt or is_bot(ua):
        return False
    if not content_type.lower().startswith("text/html"):
        return False
    return not any(path.startswith(p) for p in SKIP_PREFIXES)


def record_hit(path: str, query_string: str, referrer: str, host: str, ip: str, ua: str, country: str | None, user_id: str | None,
               now: float | None = None) -> None:
    """Count one page view. Never raises: a failure here must not break a page."""
    try:
        now = now or time.time()
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        visitor = _visitor_hash(ip, ua, day)
        query = parse_qs(query_string or "")
        conn = db.get_conn()
        try:
            last = conn.execute("SELECT session, ts FROM site_hits WHERE visitor = ? ORDER BY ts DESC LIMIT 1", (visitor,)).fetchone()
            new_session = last is None or now - last["ts"] > SESSION_GAP
            session = uuid.uuid4().hex[:12] if new_session else last["session"]
            channel, source, medium = classify(referrer, host.split(":")[0].lower().lstrip("www."), query) if new_session else ("", "", "")
            conn.execute(
                "INSERT INTO site_hits (ts, day, visitor, session, user_id, path, is_landing, channel, source, medium, country, device, browser) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, day, visitor, session, user_id, path[:200], 1 if new_session else 0, channel, source[:80], medium[:40],
                 (country or "").upper()[:2] or None, device_of(ua), browser_of(ua)))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def attribute_signup(user_id: str, ip: str, ua: str, now: float | None = None) -> None:
    """Remember which channel a new customer arrived through: today's landing visit from the same browser."""
    try:
        now = now or time.time()
        visitor = _visitor_hash(ip, ua, _day(now))
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT channel FROM site_hits WHERE visitor = ? AND is_landing = 1 AND channel != '' ORDER BY ts LIMIT 1", (visitor,)).fetchone()
        finally:
            conn.close()
        db.set_signup_channel(user_id, row["channel"] if row else "Direct")
    except Exception:  # noqa: BLE001
        pass


def prune(keep_days: int = 400) -> int:
    conn = db.get_conn()
    cur = conn.execute("DELETE FROM site_hits WHERE ts < ?", (time.time() - keep_days * DAY,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


# ----------------------------------------------------------------------------------------------------------------- helpers
def _rows(conn, sql: str, params: tuple = ()) -> list:
    return conn.execute(sql, params).fetchall()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _days_list(end: float, n: int) -> list[str]:
    return [_day(end - (n - 1 - i) * DAY) for i in range(n)]


def _pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def _change(now: float, before: float) -> float | None:
    if not before:
        return None if not now else 100.0
    return round((now - before) / before * 100.0, 1)


def _owners(conn) -> list:
    """Customers: client accounts that are not team members of someone else's account and are not closed."""
    return _rows(conn, "SELECT * FROM users WHERE role = 'client' AND account_status != 'closed' "
                       "AND id NOT IN (SELECT member_user_id FROM team_members WHERE status = 'accepted' AND member_user_id IS NOT NULL)")


def user_activity(conn, since: float) -> dict[str, set[int]]:
    """{user id: the days (as day numbers) the customer did something} from orders, payments, app page views and last seen."""
    out: dict[str, set[int]] = {}

    def add(uid, ts):
        if uid and ts and ts >= since:
            out.setdefault(uid, set()).add(int(ts // DAY))
    for r in _rows(conn, "SELECT client_id AS u, created_at AS t FROM orders WHERE created_at >= ?", (since,)):
        add(r["u"], r["t"])
    for r in _rows(conn, "SELECT user_id AS u, COALESCE(completed_at, created_at) AS t FROM payments WHERE status = 'completed' AND COALESCE(completed_at, created_at) >= ?", (since,)):
        add(r["u"], r["t"])
    for r in _rows(conn, "SELECT user_id AS u, ts AS t FROM site_hits WHERE user_id IS NOT NULL AND ts >= ?", (since,)):
        add(r["u"], r["t"])
    for r in _rows(conn, "SELECT id AS u, last_seen_at AS t FROM users WHERE last_seen_at IS NOT NULL AND last_seen_at >= ?", (since,)):
        add(r["u"], r["t"])
    return out


# ------------------------------------------------------------------------------------------------------------------ models
PURCHASE_PRIOR = {"bias": -2.2, "uploaded": 1.6, "translated": 0.7, "viewed_plans": 0.9, "returning": 0.8, "recent": 0.5, "orders": 0.4, "free_used": 0.8}
FEATURES = [k for k in PURCHASE_PRIOR if k != "bias"]


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def purchase_features(conn, now: float | None = None) -> dict[str, dict]:
    """What each customer has done that predicts buying: has uploaded, has translated, looked at plans, comes back, is recent, how much."""
    now = now or time.time()
    since = now - 120 * DAY
    act = user_activity(conn, since)
    orders = {r["u"]: r for r in _rows(
        conn, "SELECT client_id AS u, COUNT(*) AS n, SUM(CASE WHEN service_level IN ('translate','dub') THEN 1 ELSE 0 END) AS tr, "
              "COALESCE(SUM(duration_minutes), 0) AS mins FROM orders GROUP BY client_id")}
    plans_view = {r["u"] for r in _rows(conn, "SELECT DISTINCT user_id AS u FROM site_hits WHERE user_id IS NOT NULL AND ts >= ? AND "
                                                "(path LIKE '/client/plans%' OR path LIKE '/client/billing%')", (since,))}
    paid = {r["u"] for r in _rows(conn, "SELECT DISTINCT user_id AS u FROM payments WHERE status = 'completed' AND amount_usd > 0")}
    out = {}
    for u in _owners(conn):
        uid = u["id"]
        o = orders.get(uid)
        days = act.get(uid, set())
        last_day = max(days) if days else None
        out[uid] = {
            "uploaded": 1.0 if o and o["n"] else 0.0, "translated": 1.0 if o and (o["tr"] or 0) else 0.0,
            "viewed_plans": 1.0 if uid in plans_view else 0.0, "returning": 1.0 if len(days) >= 2 else 0.0,
            "recent": 1.0 if last_day is not None and now // DAY - last_day <= 7 else 0.0,
            "orders": math.log1p(o["n"]) if o else 0.0,
            "free_used": 1.0 if o and (o["mins"] or 0) >= billing.FREE_MINUTES_PER_MONTH * 0.5 else 0.0,
            "paid": uid in paid, "active_days": len(days), "last_day": last_day,
        }
    return out


def purchase_model(feats: dict[str, dict]) -> dict:
    """A small logistic model of who becomes a paying customer. It starts from sensible weights; once there are enough customers (20, at
    least 4 of them paying) it learns from them, pulled gently back towards the starting weights so a small sample cannot swing wild."""
    weights = dict(PURCHASE_PRIOR)
    rows = list(feats.values())
    positives = sum(1 for r in rows if r["paid"])
    trained = len(rows) >= 20 and positives >= 4 and len(rows) - positives >= 4
    if trained:
        lam, lr = 0.05, 0.25
        for _ in range(300):
            grad = {k: 0.0 for k in weights}
            for r in rows:
                z = weights["bias"] + sum(weights[k] * r[k] for k in FEATURES)
                err = _sigmoid(z) - (1.0 if r["paid"] else 0.0)
                grad["bias"] += err
                for k in FEATURES:
                    grad[k] += err * r[k]
            for k in weights:
                weights[k] -= lr * (grad[k] / len(rows) + lam * (weights[k] - PURCHASE_PRIOR[k]))
    scores = {uid: _sigmoid(weights["bias"] + sum(weights[k] * r[k] for k in FEATURES)) for uid, r in feats.items()}
    return {"weights": weights, "trained": trained, "scores": scores, "customers": len(rows), "paying": positives}


def churn_scores(conn, now: float | None = None) -> dict[str, dict]:
    """0-1 risk of leaving for every customer who was active lately or pays: how long since they did anything, whether they are fading,
    a failing card, or a plan that will not renew."""
    now = now or time.time()
    today = int(now // DAY)
    act = user_activity(conn, now - 120 * DAY)
    subs = {r["user_id"]: r for r in _rows(conn, "SELECT * FROM subscriptions")}
    failing = {r["user_id"] for r in _rows(conn, "SELECT user_id FROM payment_failures WHERE resolved_at IS NULL")}
    out = {}
    for u in _owners(conn):
        uid = u["id"]
        days = sorted(act.get(uid, set()))
        sub = subs.get(uid)
        paying = bool(sub and sub["plan"] != "free" and (sub["current_period_end"] or 0) > now - 3 * DAY)
        if not days and not paying:
            continue
        quiet = (today - days[-1]) if days else 999
        if not paying and quiet > 60:
            continue                                                       # long gone, not "at risk": that is the win-back's job
        risk = 0.0 if quiet <= 3 else 0.25 if quiet <= 7 else 0.5 if quiet <= 14 else 0.8 if quiet <= 30 else 1.0
        recent = sum(1 for d in days if today - d <= 14)
        before = sum(1 for d in days if 14 < today - d <= 28)
        if before and not recent:
            risk += 0.2
        if paying and not sub["paystack_subscription_code"] and (sub["current_period_end"] or 0) < now + 7 * DAY:
            risk += 0.2
        if uid in failing:
            risk += 0.3
        out[uid] = {"risk": min(1.0, risk), "quiet_days": quiet if quiet < 999 else None, "paying": paying, "recent": recent, "before": before,
                    "failing": uid in failing, "active_days": len(days)}
    return out


# ---------------------------------------------------------------------------------------------------------------- the data
def _traffic_block(conn, start: float, end: float, days: int) -> dict:
    s, e = _day(start), _day(end)
    daily = {r["day"]: r for r in _rows(
        conn, "SELECT day, COUNT(DISTINCT visitor) AS visitors, COUNT(DISTINCT session) AS sessions, COUNT(*) AS views FROM site_hits "
              "WHERE day >= ? AND day <= ? GROUP BY day", (s, e))}
    labels = _days_list(end, days)
    return {"labels": labels, "visitors": [daily[d]["visitors"] if d in daily else 0 for d in labels],
            "sessions": [daily[d]["sessions"] if d in daily else 0 for d in labels]}


def dashboard(days: int = 28, now: float | None = None) -> dict:
    now = now or time.time()
    end = now
    start = now - (days - 1) * DAY
    prev_end, prev_start = start - DAY, start - days * DAY
    conn = db.get_conn()
    try:
        owners = _owners(conn)
        cur, prev = _traffic_block(conn, start, end, days), _traffic_block(conn, prev_start, prev_end, days)
        first_hit = _rows(conn, "SELECT MIN(ts) AS t, COUNT(*) AS n FROM site_hits")[0]
        visitors, visitors_prev = sum(cur["visitors"]), sum(prev["visitors"])
        sessions, sessions_prev = sum(cur["sessions"]), sum(prev["sessions"])

        def signups(a, b):
            return _rows(conn, "SELECT COUNT(*) AS n FROM users WHERE role = 'client' AND created_at >= ? AND created_at < ?", (a, b))[0]["n"]
        signups_now, signups_prev = signups(start, end + DAY), signups(prev_start, start)
        sign_daily = {r["d"]: r["n"] for r in _rows(conn, "SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS n FROM users WHERE role = 'client' "
                                                          "AND created_at >= ? GROUP BY d", (start,))}
        act = user_activity(conn, prev_start)
        s_day, e_day, ps_day, pe_day = int(start // DAY), int(end // DAY), int(prev_start // DAY), int(prev_end // DAY)
        active_now = sum(1 for d in act.values() if any(s_day <= x <= e_day for x in d))
        active_prev = sum(1 for d in act.values() if any(ps_day <= x <= pe_day for x in d))
        active_daily = [sum(1 for d in act.values() if (int(start // DAY) + i) in d) for i in range(days)]

        def paid_customers(a, b):
            return _rows(conn, "SELECT COUNT(DISTINCT user_id) AS n FROM payments WHERE status = 'completed' AND amount_usd > 0 AND completed_at >= ? AND completed_at < ?", (a, b))[0]["n"]
        paid_now, paid_prev = paid_customers(start, end + DAY), paid_customers(prev_start, start)
        revenue_rows = _rows(conn, "SELECT date(completed_at, 'unixepoch') AS d, SUM(amount_usd) AS usd FROM payments WHERE status = 'completed' "
                                   "AND completed_at >= ? GROUP BY d", (start,))
        revenue_daily_map = {r["d"]: r["usd"] for r in revenue_rows}
        revenue_now = sum(revenue_daily_map.values())
        revenue_prev = _rows(conn, "SELECT COALESCE(SUM(amount_usd), 0) AS usd FROM payments WHERE status = 'completed' AND completed_at >= ? AND completed_at < ?", (prev_start, start))[0]["usd"]

        # recurring revenue now (and as it stood at the start of the period) from the subscriptions that are in force
        subs = _rows(conn, "SELECT s.*, u.email FROM subscriptions s JOIN users u ON u.id = s.user_id")
        in_force = [r for r in subs if r["plan"] in billing.PLANS and r["plan"] != "free" and (r["current_period_end"] or 0) > now - 3 * DAY and r["status"] == "active"]
        mrr = sum(billing.PLANS[r["plan"]]["price_usd"] for r in in_force)
        at_start = [r for r in subs if r["plan"] in billing.PLANS and r["plan"] != "free" and (r["current_period_end"] or 0) >= start]
        lost = [r for r in at_start if r not in in_force]
        churn_rate = _pct(len(lost), len(at_start))
        by_plan: dict[str, float] = {}
        for r in _rows(conn, "SELECT plan, order_id, SUM(amount_usd) AS usd FROM payments WHERE status = 'completed' AND amount_usd > 0 AND completed_at >= ? "
                             "GROUP BY plan, order_id IS NULL", (start,)):
            key = billing.PLANS[r["plan"]]["name"] if r["order_id"] is None and r["plan"] in billing.PLANS else "Pay as you go"
            by_plan[key] = by_plan.get(key, 0.0) + r["usd"]

        # acquisition
        landings = _rows(conn, "SELECT channel, source, COUNT(*) AS n FROM site_hits WHERE is_landing = 1 AND day >= ? AND day <= ? GROUP BY channel, source", (_day(start), _day(end)))
        by_channel: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for r in landings:
            by_channel[r["channel"] or "Direct"] = by_channel.get(r["channel"] or "Direct", 0) + r["n"]
            label = f"{r['source']} / {r['channel']}" if r["source"] else r["channel"]
            by_source[label] = by_source.get(label, 0) + r["n"]
        channels = [{"name": c, "sessions": by_channel.get(c, 0), "share": _pct(by_channel.get(c, 0), sum(by_channel.values()))}
                    for c in CHANNELS + [c for c in by_channel if c not in CHANNELS]]
        channels = [c for c in channels if c["sessions"]] or [{"name": c, "sessions": 0, "share": 0.0} for c in CHANNELS[:3]]
        pages = _rows(conn, "SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor) AS people FROM site_hits WHERE day >= ? AND day <= ? GROUP BY path ORDER BY views DESC LIMIT 8", (_day(start), _day(end)))
        sess = {r["session"]: r for r in _rows(conn, "SELECT session, COUNT(*) AS n, MIN(ts) AS t0, MAX(ts) AS t1 FROM site_hits WHERE day >= ? AND day <= ? GROUP BY session", (_day(start), _day(end)))}
        bounce_sessions = sum(1 for r in sess.values() if r["n"] == 1)
        multi = [r for r in sess.values() if r["n"] > 1]
        avg_session = sum(r["t1"] - r["t0"] for r in multi) / len(multi) if multi else 0.0
        landing_rows = _rows(conn, "SELECT path, COUNT(*) AS views, SUM(CASE WHEN (SELECT COUNT(*) FROM site_hits h2 WHERE h2.session = h.session) = 1 THEN 1 ELSE 0 END) AS bounced "
                                   "FROM site_hits h WHERE is_landing = 1 AND day >= ? AND day <= ? GROUP BY path ORDER BY views DESC LIMIT 6", (_day(start), _day(end)))
        # time on page: the gap to the next view in the same session
        top_paths = [p["path"] for p in pages]
        time_on = {}
        for p in top_paths:
            r = _rows(conn, "SELECT AVG(nxt - ts) AS secs FROM (SELECT ts, (SELECT MIN(h2.ts) FROM site_hits h2 WHERE h2.session = h.session AND h2.ts > h.ts) AS nxt "
                            "FROM site_hits h WHERE path = ? AND day >= ? AND day <= ?) WHERE nxt IS NOT NULL", (p, _day(start), _day(end)))[0]["secs"]
            time_on[p] = r or 0.0
        landing_bounce = {r["path"]: _pct(r["bounced"] or 0, r["views"]) for r in landing_rows}
        devices = {r["device"]: r["n"] for r in _rows(conn, "SELECT device, COUNT(DISTINCT session) AS n FROM site_hits WHERE day >= ? AND day <= ? GROUP BY device", (_day(start), _day(end)))}
        browsers = {r["browser"]: r["n"] for r in _rows(conn, "SELECT browser, COUNT(DISTINCT session) AS n FROM site_hits WHERE day >= ? AND day <= ? GROUP BY browser ORDER BY n DESC", (_day(start), _day(end)))}
        countries = _rows(conn, "SELECT country, COUNT(DISTINCT session) AS sessions FROM site_hits WHERE country IS NOT NULL AND day >= ? AND day <= ? GROUP BY country ORDER BY sessions DESC LIMIT 12", (_day(start), _day(end)))
        paid_signups = _rows(conn, "SELECT COUNT(*) AS n FROM users WHERE role = 'client' AND created_at >= ? AND signup_channel IN ('Paid Search', 'Paid Social')", (start,))[0]["n"]

        # models
        feats = purchase_features(conn, now)
        model = purchase_model(feats)
        user_country = {r["user_id"]: r["country"] for r in _rows(conn, "SELECT user_id, country FROM site_hits WHERE user_id IS NOT NULL AND country IS NOT NULL GROUP BY user_id HAVING ts = MAX(ts)")}
        today = int(now // DAY)
        active_ids = [uid for uid, f in feats.items() if f["last_day"] is not None and today - f["last_day"] <= days]
        prospects = [uid for uid in active_ids if not feats[uid]["paid"]]
        high = [uid for uid in prospects if model["scores"][uid] >= 0.5]
        likelihood = _pct(len(high), len(prospects))
        by_country: dict[str, list] = {}
        for uid in prospects:
            by_country.setdefault(user_country.get(uid, ""), []).append(model["scores"][uid] >= 0.5)
        country_rows = []
        for r in countries:
            xs = by_country.get(r["country"], [])
            country_rows.append({"code": r["country"], "sessions": r["sessions"], "likelihood": _pct(sum(xs), len(xs)) if xs else None})
        activities = []
        for label, key in (("Completed first upload", "uploaded"), ("Used translation feature", "translated"), ("Viewed plans or billing page", "viewed_plans"),
                           ("Came back on another day", "returning"), ("Used more than half the free minutes", "free_used")):
            buyers = [f for f in feats.values() if f["paid"]]
            activities.append({"label": label, "share": _pct(sum(1 for f in buyers if f[key]), len(buyers)) if buyers else _pct(sum(1 for f in feats.values() if f[key]), len(feats))})
        activities.sort(key=lambda a: -a["share"])

        churn = churn_scores(conn, now)
        pop = list(churn.values())
        at_risk = [c for c in pop if c["risk"] >= 0.5]
        churn_risk = _pct(len(at_risk), len(pop))
        indicators = [
            {"label": "No activity for 7+ days", "share": _pct(sum(1 for c in pop if (c["quiet_days"] or 999) >= 7), len(pop))},
            {"label": "Only viewed (didn't use)", "share": _pct(sum(1 for uid in churn if not feats.get(uid, {}).get("uploaded")), len(pop))},
            {"label": "Plan not set to renew", "share": _pct(sum(1 for c in pop if c["paying"] and c["risk"] >= 0.2 and not c["failing"] and (c["quiet_days"] or 0) > 7), len(pop))},
            {"label": "Low feature usage", "share": _pct(sum(1 for uid in churn if feats.get(uid, {}).get("orders", 0) <= math.log1p(1) and feats.get(uid, {}).get("translated", 0) == 0), len(pop))},
            {"label": "No return visits (30 days)", "share": _pct(sum(1 for c in pop if c["active_days"] <= 1), len(pop))},
        ]

        # engagement and retention
        ret_total = sum(1 for uid, f in feats.items() if f["returning"] and f["last_day"] is not None and today - f["last_day"] <= days)
        returning = _pct(ret_total, len(active_ids))
        dau = sum(1 for f in feats.values() if f["last_day"] is not None and today - f["last_day"] <= 0)
        wau = sum(1 for f in feats.values() if f["last_day"] is not None and today - f["last_day"] <= 6)
        mau = sum(1 for f in feats.values() if f["last_day"] is not None and today - f["last_day"] <= 29)
        inactive = sum(1 for f in feats.values() if f["last_day"] is None or today - f["last_day"] > 14) if feats else 0
        cohorts = _cohorts(owners, act_full := user_activity(conn, now - 90 * DAY), now)

        # product use
        used = {r["k"]: r["n"] for r in _rows(conn, "SELECT service_level AS k, COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ? GROUP BY service_level", (start,))}
        summary_users = _rows(conn, "SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ? AND ai_summary_text IS NOT NULL", (start,))[0]["n"]
        human_users = _rows(conn, "SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ? AND review_tier = 'human'", (start,))[0]["n"]
        order_users = _rows(conn, "SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ?", (start,))[0]["n"]
        feature_rows = [("Transcription", order_users), ("Translation", used.get("translate", 0) + used.get("dub", 0)), ("Dubbing", used.get("dub", 0)),
                        ("Human review", human_users), ("AI summary", summary_users)]
        base = max(active_now, order_users, 1)
        features = sorted([{"name": n, "users": c, "share": _pct(c, base)} for n, c in feature_rows], key=lambda r: -r["users"])

        # funnel: first-party conversion
        trial_users = _rows(conn, "SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ?", (prev_start,))[0]["n"]
        paid_of_trial = _rows(conn, "SELECT COUNT(DISTINCT p.user_id) AS n FROM payments p WHERE p.status = 'completed' AND p.amount_usd > 0 AND p.completed_at >= ? "
                                    "AND p.user_id IN (SELECT client_id FROM orders)", (prev_start,))[0]["n"]
        trial_to_paid = _pct(paid_of_trial, trial_users)
        arpu = round(mrr / len(in_force), 2) if in_force else (round(revenue_now / paid_now, 2) if paid_now else 0.0)
        spend = float(db.get_flag("ad_spend_usd_month", "0") or 0)
        spend_period = spend * days / 30.0
        cac = round(spend_period / paid_signups, 2) if spend and paid_signups else None
        paid_signup_total = paid_signups
        mrr_series = []
        running = 0.0
        for d in cur["labels"]:
            running += revenue_daily_map.get(d, 0.0)
            mrr_series.append(round(running, 2))
    finally:
        conn.close()

    insights = _insights(channels, country_rows, churn_rate, _change(churn_rate, 0) if False else None, devices, likelihood, high, prospects, features, inactive)
    more = extras(days, now, visitors, signups_now, paid_now)
    return {
        **more,
        "days": days, "labels": cur["labels"], "first_hit": first_hit["t"], "hits": first_hit["n"],
        "kpis": [
            {"key": "visitors", "label": "Total website visitors", "value": visitors, "change": _change(visitors, visitors_prev), "series": cur["visitors"], "fmt": "int"},
            {"key": "signups", "label": "New sign-ups", "value": signups_now, "change": _change(signups_now, signups_prev), "series": [sign_daily.get(d, 0) for d in cur["labels"]], "fmt": "int"},
            {"key": "active", "label": "Active users (app)", "value": active_now, "change": _change(active_now, active_prev), "series": active_daily, "fmt": "int"},
            {"key": "paid", "label": "Paid customers", "value": paid_now, "change": _change(paid_now, paid_prev), "series": [_cum(revenue_daily_map, cur["labels"], i) for i in range(days)], "fmt": "int"},
            {"key": "mrr", "label": "Monthly recurring revenue", "value": mrr, "change": _change(revenue_now, revenue_prev), "series": mrr_series, "fmt": "usd"},
            {"key": "churn", "label": "Churn rate", "value": churn_rate, "change": None, "series": [], "fmt": "pct", "invert": True},
        ],
        "traffic": {"sessions": sessions, "sessions_change": _change(sessions, sessions_prev), "visitors": cur["visitors"], "sessions_series": cur["sessions"],
                    "channels": channels, "sources": sorted(({"name": k, "sessions": v} for k, v in by_source.items()), key=lambda r: -r["sessions"])[:8],
                    "landing": [{"path": r["path"], "views": r["views"], "bounce": landing_bounce.get(r["path"], 0.0)} for r in landing_rows],
                    "signup_rate": _pct(signups_now, visitors), "signup_rate_prev": _pct(signups_prev, visitors_prev)},
        "audience": {"countries": country_rows, "likelihood": likelihood, "high_intent": len(high), "prospects": len(prospects), "trained": model["trained"],
                     "activities": activities, "model_customers": model["customers"], "model_paying": model["paying"]},
        "churn": {"risk": churn_risk, "at_risk": len(at_risk), "watched": len(pop), "indicators": indicators},
        "devices": {"sessions": sum(devices.values()), "rows": [{"name": k, "sessions": devices.get(k, 0), "share": _pct(devices.get(k, 0), sum(devices.values()))} for k in ("Desktop", "Mobile", "Tablet")],
                    "browsers": [{"name": k, "share": _pct(v, sum(browsers.values()))} for k, v in list(browsers.items())[:5]]},
        "engagement": {"avg_session": avg_session, "returning": returning, "active": active_now, "dau": dau, "wau": wau, "mau": mau,
                       "inactive": inactive, "cohorts": cohorts, "bounce": _pct(bounce_sessions, len(sess))},
        "features": features,
        "pages": [{"path": r["path"], "views": r["views"], "people": r["people"], "time_on": time_on.get(r["path"], 0.0)} for r in pages],
        "revenue": {"trial_to_paid": trial_to_paid, "arpu": arpu, "cac": cac, "ad_spend": spend, "paid_signups": paid_signup_total, "series": mrr_series,
                    "by_plan": sorted(({"name": k, "usd": round(v, 2)} for k, v in by_plan.items()), key=lambda r: -r["usd"]), "total": round(revenue_now, 2), "mrr": mrr},
        "insights": insights, "model": {"trained": model["trained"], "customers": model["customers"], "paying": model["paying"]},
        "onboarding": {"ttfv": onboarding.ttfv_minutes(90, now), "csat": onboarding.csat_summary(90, now), "stuck": len(onboarding.first_orders_needing_help(now))},
    }


def ago(seconds: float) -> str:
    """"just now", "5m ago", "3h ago", "2d ago"."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def extras(days: int, now: float, visitors: int, signups: int, paid: int) -> dict:
    """What the first dashboard did not have: visitors on the site right now, the visit-to-customer funnel, what just happened (sign-ups,
    payments, orders, contact requests, posts), how well each channel turns visitors into customers, and when visitors arrive."""
    start = now - (days - 1) * DAY
    conn = db.get_conn()
    try:
        live = _rows(conn, "SELECT COUNT(DISTINCT visitor) AS n FROM site_hits WHERE ts >= ?", (now - 300,))[0]["n"]
        ordering = _rows(conn, "SELECT COUNT(DISTINCT client_id) AS n FROM orders WHERE created_at >= ?", (start,))[0]["n"]
        steps = [("Visited the site", visitors), ("Signed up", signups), ("Placed an order", ordering), ("Became a paying customer", paid)]
        funnel, prior = [], None
        top = steps[0][1] or max((c for _, c in steps), default=1) or 1
        for name, count in steps:
            funnel.append({"label": name, "count": count, "share": _pct(count, top), "step": _pct(count, prior) if prior else None})
            prior = count or None

        events: list[dict] = []
        for r in _rows(conn, "SELECT display_name, created_at FROM users WHERE role = 'client' AND created_at >= ? ORDER BY created_at DESC LIMIT 6", (start,)):
            events.append({"kind": "signup", "title": "New sign-up", "detail": "A new user created an account", "ts": r["created_at"]})
        for r in _rows(conn, "SELECT amount_usd, completed_at FROM payments WHERE status = 'completed' AND amount_usd > 0 AND completed_at >= ? ORDER BY completed_at DESC LIMIT 6", (start,)):
            events.append({"kind": "payment", "title": "New payment", "detail": f"${r['amount_usd']:,.2f} received", "ts": r["completed_at"]})
        for r in _rows(conn, "SELECT created_at FROM orders WHERE created_at >= ? ORDER BY created_at DESC LIMIT 6", (start,)):
            events.append({"kind": "order", "title": "New order", "detail": "A file was submitted", "ts": r["created_at"]})
        for r in _rows(conn, "SELECT created_at FROM leads WHERE created_at >= ? ORDER BY created_at DESC LIMIT 4", (start,)):
            events.append({"kind": "lead", "title": "Contact request", "detail": "New message from the website", "ts": r["created_at"]})
        for r in _rows(conn, "SELECT title, published_at FROM blog_posts WHERE status = 'published' AND published_at >= ? ORDER BY published_at DESC LIMIT 3", (start,)):
            events.append({"kind": "post", "title": "Blog post published", "detail": r["title"], "ts": r["published_at"]})
        events.sort(key=lambda e: -e["ts"])
        activity = [{**e, "ago": ago(now - e["ts"])} for e in events[:8]]

        quality = []
        for r in _rows(conn, "SELECT COALESCE(NULLIF(signup_channel, ''), 'Direct') AS ch, COUNT(*) AS n, "
                             "SUM(EXISTS(SELECT 1 FROM payments p WHERE p.user_id = users.id AND p.status = 'completed' AND p.amount_usd > 0)) AS paid "
                             "FROM users WHERE role = 'client' AND created_at >= ? GROUP BY ch ORDER BY n DESC", (start,)):
            quality.append({"name": r["ch"], "signups": r["n"], "paid": r["paid"] or 0, "rate": _pct(r["paid"] or 0, r["n"])})

        grid = [[0] * 24 for _ in range(7)]
        for r in _rows(conn, "SELECT CAST(strftime('%w', ts, 'unixepoch') AS INTEGER) AS dow, CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS hr, "
                             "COUNT(DISTINCT session) AS n FROM site_hits WHERE ts >= ? GROUP BY dow, hr", (start,)):
            grid[r["dow"]][r["hr"]] = r["n"]
    finally:
        conn.close()
    peak = max((v for row in grid for v in row), default=0)
    slots = sorted(((v, d, h) for d, row in enumerate(grid) for h, v in enumerate(row) if v), reverse=True)[:3]
    return {"live": live, "funnel": funnel, "activity": activity, "quality": quality,
            "heat": {"grid": grid, "peak": peak, "best": [f"{WEEKDAYS[d]} {h:02d}:00" for _, d, h in slots]}}


def _cum(daily_map: dict, labels: list[str], i: int) -> float:
    return sum(1 for d in labels[: i + 1] if daily_map.get(d))


def _cohorts(owners: list, act: dict, now: float) -> list[dict]:
    """Weekly sign-up cohorts: the share of each week's new customers who did something in week 1, 2, 3 and 4 after joining."""
    today = int(now // DAY)
    by_week: dict[int, list] = {}
    for u in owners:
        if not u["created_at"]:
            continue
        joined = int(u["created_at"] // DAY)
        age = today - joined
        if age < 0 or age > 70:
            continue
        by_week.setdefault(age // 7, []).append((u["id"], joined))
    rows = []
    for w in sorted(by_week)[:6]:
        members = by_week[w]
        start = datetime.fromtimestamp((today - (w + 1) * 7 + 1) * DAY, timezone.utc)
        cells = []
        for k in range(4):
            if k > w:                                                 # that week of their life has not happened yet
                cells.append(None)
                continue
            n = sum(1 for uid, joined in members if any(joined + 7 * k <= d < joined + 7 * (k + 1) for d in act.get(uid, ())))
            cells.append(_pct(n, len(members)))
        rows.append({"label": f"{start.strftime('%b %d')} - {(start + timedelta(days=6)).strftime('%b %d')}", "size": len(members), "cells": cells})
    return rows


def _insights(channels, countries, churn_rate, _unused, devices, likelihood, high, prospects, features, inactive) -> list[str]:
    out = []
    top_c = next((c for c in countries if c["likelihood"] is not None and c["likelihood"] > 0), None)
    if top_c:
        out.append(f"{top_c['code']} has the highest purchase likelihood ({top_c['likelihood']:.0f}%): consider targeted campaigns there.")
    real = [c for c in channels if c["sessions"]]
    if real:
        out.append(f"{real[0]['name']} brings the most visits ({real[0]['share']:.0f}% of sessions).")
    mobile = next((d for d in devices.items() if d[0] == "Mobile"), None) if isinstance(devices, dict) else None
    if features and features[0]["users"]:
        out.append(f"{features[0]['name']} is the most used feature ({features[0]['share']:.0f}% of active customers).")
    if inactive:
        out.append(f"{inactive} accounts have been quiet for 14+ days: the Engagement page shows how to bring them back.")
    if prospects and not out:
        out.append(f"{len(high)} of {len(prospects)} active free users look ready to buy.")
    return out[:4] or ["Insights appear here as the site collects more data."]


# ------------------------------------------------------------------------------------------------------------------ charts
def spark(values: list, w: int = 200, h: int = 34, color: str = "#2b7be4") -> str:
    vals = [float(v) for v in values] or [0.0]
    if len(vals) == 1:
        vals = vals * 2
    hi, lo = max(vals), min(vals)
    span = (hi - lo) or 1.0
    pts = [(i * (w - 2) / (len(vals) - 1) + 1, h - 3 - (v - lo) / span * (h - 8)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"1,{h} " + line + f" {w - 1},{h}"
    return (f'<svg class="sa-spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Trend"><polygon points="{area}" fill="{color}" opacity="0.12"/>'
            f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>')


PALETTE = ["#2b7be4", "#8b5cf6", "#f59e0b", "#10b981", "#ef4444", "#06b6d4", "#94a3b8"]


def donut(items: list[tuple[str, float]], size: int = 150, stroke: int = 26, center_top: str = "", center_bottom: str = "") -> str:
    total = sum(v for _, v in items) or 1.0
    r = (size - stroke) / 2
    circ = 2 * math.pi * r
    parts, offset = [], 0.0
    for i, (_, v) in enumerate(items):
        frac = v / total
        parts.append(f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" stroke-width="{stroke}" '
                     f'stroke-dasharray="{frac * circ:.2f} {circ:.2f}" stroke-dashoffset="{-offset * circ:.2f}" transform="rotate(-90 {size / 2} {size / 2})"/>')
        offset += frac
    if not sum(v for _, v in items):
        parts = [f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="#e6edf7" stroke-width="{stroke}"/>']
    return (f'<svg class="sa-donut" viewBox="0 0 {size} {size}" role="img" aria-label="Breakdown">{"".join(parts)}'
            f'<text x="50%" y="47%" text-anchor="middle" class="sa-donut-top">{center_top}</text>'
            f'<text x="50%" y="62%" text-anchor="middle" class="sa-donut-bottom">{center_bottom}</text></svg>')


def ring(percent: float, color: str = "#10b981", size: int = 86, label: str = "") -> str:
    r = (size - 12) / 2
    circ = 2 * math.pi * r
    return (f'<svg class="sa-ring" viewBox="0 0 {size} {size}" role="img" aria-label="{label}"><circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="#e6edf7" stroke-width="9"/>'
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round" '
            f'stroke-dasharray="{min(percent, 100) / 100 * circ:.1f} {circ:.1f}" transform="rotate(-90 {size / 2} {size / 2})"/>'
            f'<text x="50%" y="56%" text-anchor="middle" class="sa-ring-text">{percent:.0f}%</text></svg>')


def lines(labels: list[str], series: list[tuple[str, list]], w: int = 520, h: int = 170) -> str:
    """A small line chart with a soft grid; each series is (name, values)."""
    top = max([max(v) if v else 0 for _, v in series] + [1])
    top = math.ceil(top / (10 ** max(len(str(int(top))) - 1, 0))) * 10 ** max(len(str(int(top))) - 1, 0) or 1
    pad_l, pad_b, pad_t = 34, 22, 8
    iw, ih = w - pad_l - 6, h - pad_b - pad_t
    grid = "".join(f'<line x1="{pad_l}" x2="{w - 6}" y1="{pad_t + ih * i / 4:.1f}" y2="{pad_t + ih * i / 4:.1f}" stroke="#e9eff8"/>'
                   f'<text x="{pad_l - 6}" y="{pad_t + ih * i / 4 + 3:.1f}" text-anchor="end" class="sa-axis">{_short(top * (4 - i) / 4)}</text>' for i in range(5))
    out = [grid]
    n = max(len(labels), 2)
    for i in range(0, len(labels), max(1, len(labels) // 6)):
        out.append(f'<text x="{pad_l + iw * i / (n - 1):.1f}" y="{h - 6}" text-anchor="middle" class="sa-axis">{labels[i][5:]}</text>')
    for idx, (_, vals) in enumerate(series):
        if not vals:
            continue
        pts = [(pad_l + iw * i / (n - 1), pad_t + ih - ih * (v / top)) for i, v in enumerate(vals)]
        color = PALETTE[idx % 2]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if idx == 0:
            out.append(f'<polygon points="{pad_l},{pad_t + ih} {path} {pad_l + iw},{pad_t + ih}" fill="{color}" opacity="0.1"/>')
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
    return f'<svg class="sa-lines" viewBox="0 0 {w} {h}" role="img" aria-label="Sessions and users over time">{"".join(out)}</svg>'


def pairs(rows: list[dict], name_key: str, value_key: str) -> list[tuple[str, float]]:
    return [(r[name_key], r[value_key]) for r in rows]


def _short(v: float) -> str:
    return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"


# a dotted world map: each character is a 5 x 5 degree cell, 72 columns from 180W, 28 rows from 80N to 55S
_LAND = {
    0: [(18, 32)], 1: [(14, 32), (39, 41), (47, 49), (54, 62)], 2: [(3, 23), (24, 31), (40, 71)], 3: [(3, 17), (21, 25), (31, 33), (38, 71)],
    4: [(4, 17), (21, 24), (26, 28), (36, 71)], 5: [(6, 24), (35, 36), (37, 70)], 6: [(8, 25), (34, 69)], 7: [(10, 25), (34, 49), (52, 67)],
    8: [(11, 23), (34, 49), (50, 66)], 9: [(11, 22), (35, 48), (51, 65)], 10: [(12, 22), (32, 45), (46, 64)], 11: [(13, 20), (32, 48), (49, 62)],
    12: [(13, 20), (32, 47), (49, 62)], 13: [(14, 19), (32, 47), (49, 52), (55, 58)], 14: [(18, 22), (32, 47), (50, 51), (55, 58), (61, 62)],
    15: [(19, 26), (34, 46), (55, 64)], 16: [(19, 27), (34, 46), (55, 64)], 17: [(19, 29), (35, 46), (55, 67)], 18: [(19, 29), (35, 46), (60, 66)],
    19: [(19, 29), (36, 46), (59, 66)], 20: [(20, 28), (36, 46), (58, 66)], 21: [(20, 27), (36, 45), (58, 66)], 22: [(21, 26), (37, 44), (59, 66)],
    23: [(21, 25), (38, 43), (60, 66), (69, 70)], 24: [(21, 24), (70, 71)], 25: [(21, 24), (69, 70)], 26: [(21, 23)], 27: [(22, 23)],
}
COUNTRY_XY = {  # (longitude, latitude)
    "US": (-98, 39), "CA": (-100, 58), "MX": (-102, 23), "BR": (-52, -10), "AR": (-64, -34), "CO": (-74, 4), "CL": (-71, -33), "PE": (-75, -10),
    "GB": (-2, 54), "IE": (-8, 53), "FR": (2, 46), "DE": (10, 51), "ES": (-4, 40), "PT": (-8, 39), "IT": (12, 42), "NL": (5, 52), "BE": (4, 50),
    "CH": (8, 47), "SE": (15, 62), "NO": (9, 61), "PL": (19, 52), "UA": (31, 49), "RU": (60, 60), "TR": (35, 39), "GR": (22, 39),
    "EG": (30, 27), "NG": (8, 9), "GH": (-1, 8), "KE": (38, 0), "TZ": (35, -6), "UG": (32, 1), "ET": (40, 8), "ZA": (24, -29), "MA": (-6, 32),
    "RW": (30, -2), "CD": (24, -3), "SN": (-14, 14), "CI": (-5, 7), "ZM": (28, -14), "ZW": (30, -19),
    "SA": (45, 24), "AE": (54, 24), "IL": (35, 31), "IR": (53, 32), "PK": (69, 30), "IN": (78, 22), "BD": (90, 24), "LK": (81, 8), "NP": (84, 28),
    "CN": (104, 35), "JP": (138, 36), "KR": (127, 36), "TW": (121, 24), "HK": (114, 22), "SG": (104, 1), "MY": (102, 4), "TH": (101, 15),
    "VN": (108, 14), "PH": (122, 13), "ID": (113, -2), "AU": (134, -25), "NZ": (172, -41),
}


def world_dots() -> str:
    dots = []
    for row, spans in _LAND.items():
        for a, b in spans:
            for c in range(a, b + 1):
                dots.append(f'<circle cx="{c * 10 + 5}" cy="{row * 10 + 5}" r="2.5"/>')
    return "".join(dots)


def map_pins(rows: list[dict]) -> str:
    top = max([r["sessions"] for r in rows] + [1])
    out = []
    for r in rows:
        xy = COUNTRY_XY.get(r["code"])
        if not xy:
            continue
        x, y = (xy[0] + 180) / 5 * 10, (80 - xy[1]) / 5 * 10
        rad = 5 + 11 * math.sqrt(r["sessions"] / top)
        out.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{rad:.0f}" class="sa-pin"><title>{r["code"]}: {r["sessions"]} sessions</title></circle>')
    return "".join(out)


COUNTRY_NAMES = {"US": "United States", "KE": "Kenya", "GB": "United Kingdom", "CA": "Canada", "ZA": "South Africa", "DE": "Germany", "NG": "Nigeria",
                 "TZ": "Tanzania", "UG": "Uganda", "IN": "India", "FR": "France", "ES": "Spain", "MX": "Mexico", "BR": "Brazil", "AU": "Australia",
                 "AE": "United Arab Emirates", "NL": "Netherlands", "IT": "Italy", "GH": "Ghana", "EG": "Egypt", "CN": "China", "JP": "Japan",
                 "PH": "Philippines", "PK": "Pakistan", "ET": "Ethiopia", "RW": "Rwanda", "SG": "Singapore", "IE": "Ireland", "SE": "Sweden"}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code or "Unknown")


def flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper()) if len(code or "") == 2 and code.isalpha() else "🌐"
