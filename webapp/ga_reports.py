"""Reads this site's Google Analytics 4 numbers through the GA4 Data API so staff can see how the website is doing inside Kauli
(Staff > Site analytics), next to what Kauli itself knows (orders, payments, the upload wizard, blog readers).

Inert until configured, like every other integration here. Needed in .env:
    GA4_PROPERTY_ID            the numeric property id (GA4 Admin > Property details), not the G-XXXX measurement id
    GA4_SERVICE_ACCOUNT_FILE   path to a service-account JSON key, or
    GA4_SERVICE_ACCOUNT_JSON   the same JSON pasted in as one line
and that service account's email must be added as a Viewer in GA4 Admin > Property access management. Setup steps are in
marketing/google-analytics-guide.md. Only read-only access is ever requested.

What comes back is aggregated numbers only (no individual visitors). Results are cached for ten minutes.
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
API = "https://analyticsdata.googleapis.com/v1beta/properties/{pid}:batchRunReports"
CACHE_SECONDS = 600

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict = {}


class GAError(Exception):
    """Something a person can act on, worded for them."""


def property_id() -> str:
    return os.environ.get("GA4_PROPERTY_ID", "").strip().replace("properties/", "")


def _credentials() -> dict | None:
    raw = os.environ.get("GA4_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.environ.get("GA4_SERVICE_ACCOUNT_FILE", "").strip()
    try:
        if raw:
            return json.loads(raw)
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, ValueError):
        return None
    return None


def configured() -> bool:
    creds = _credentials()
    return bool(property_id() and creds and creds.get("client_email") and creds.get("private_key"))


def _access_token() -> str:
    import jwt          # PyJWT with the cryptography backend, for the RS256 signature Google asks for

    with _lock:
        if _token["value"] and _token["expires"] > time.time() + 60:
            return _token["value"]
    creds = _credentials()
    if not creds:
        raise GAError("Google Analytics is not connected yet.")
    now = int(time.time())
    assertion = jwt.encode({"iss": creds["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3000},
                           creds["private_key"], algorithm="RS256")
    r = httpx.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}, timeout=20)
    if r.status_code != 200:
        raise GAError("Google did not accept the service-account key (" + (r.json().get("error_description") or r.text[:120]) + ").")
    body = r.json()
    with _lock:
        _token.update(value=body["access_token"], expires=time.time() + int(body.get("expires_in", 3000)))
    return body["access_token"]


def _batch(requests: list[dict]) -> list[dict]:
    r = httpx.post(API.format(pid=property_id()), headers={"Authorization": f"Bearer {_access_token()}"}, json={"requests": requests}, timeout=45)
    if r.status_code == 403:
        raise GAError("The service account can read nothing here. Add its email as a Viewer under GA4 Admin > Property access management.")
    if r.status_code == 404 or r.status_code == 400 and "property" in r.text.lower():
        raise GAError("GA4 does not know that property id. Use the numeric Property ID from GA4 Admin > Property details.")
    if r.status_code != 200:
        raise GAError(f"Google Analytics answered {r.status_code}: {r.text[:200]}")
    return r.json().get("reports", [])


def table(report: dict) -> list[dict]:
    """One dict per row, keyed by the dimension and metric names; metric values are numbers."""
    dims = [h["name"] for h in report.get("dimensionHeaders", [])]
    mets = [h["name"] for h in report.get("metricHeaders", [])]
    out = []
    for row in report.get("rows", []) or []:
        item = {d: v.get("value", "") for d, v in zip(dims, row.get("dimensionValues", []))}
        for m, v in zip(mets, row.get("metricValues", [])):
            try:
                item[m] = float(v.get("value", 0))
            except (TypeError, ValueError):
                item[m] = 0.0
        out.append(item)
    return out


def _range(days: int) -> list[dict]:
    return [{"startDate": f"{days}daysAgo", "endDate": "today"}]


def _req(dims: list[str], mets: list[str], days: int, limit: int = 10, order: str | None = None, desc: bool = True, ranges=None) -> dict:
    body = {"dateRanges": ranges or _range(days), "dimensions": [{"name": d} for d in dims], "metrics": [{"name": m} for m in mets], "limit": limit}
    if order:
        body["orderBys"] = [({"metric": {"metricName": order}, "desc": desc} if order in mets else {"dimension": {"dimensionName": order}, "desc": desc})]
    return body


def overview(days: int = 28, force: bool = False) -> dict:
    """The numbers behind the dashboard, or raises GAError."""
    key = (property_id(), days)
    hit = _cache.get(key)
    if hit and not force and hit["at"] > time.time() - CACHE_SECONDS:
        return hit["data"]
    two = [{"startDate": f"{days}daysAgo", "endDate": "today", "name": "this"},
           {"startDate": f"{2 * days}daysAgo", "endDate": f"{days + 1}daysAgo", "name": "previous"}]
    first = _batch([
        _req([], ["activeUsers", "newUsers", "sessions", "engagementRate", "averageSessionDuration", "screenPageViews", "keyEvents"], days, ranges=two),
        _req(["date"], ["activeUsers", "sessions", "keyEvents"], days, limit=400, order="date", desc=False),
        _req(["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "engagementRate", "keyEvents"], days, order="sessions"),
        _req(["pagePath"], ["screenPageViews", "activeUsers", "userEngagementDuration", "keyEvents"], days, limit=15, order="screenPageViews"),
        _req(["country"], ["activeUsers", "sessions", "engagementRate", "keyEvents"], days, order="activeUsers"),
    ])
    second = _batch([
        _req(["deviceCategory"], ["sessions", "engagementRate", "keyEvents"], days, order="sessions"),
        _req(["sessionSourceMedium"], ["sessions", "engagementRate", "keyEvents"], days, order="sessions"),
        _req(["eventName"], ["eventCount", "totalUsers"], days, limit=60, order="eventCount"),
        _req(["landingPage"], ["sessions", "engagementRate", "keyEvents"], days, limit=10, order="sessions"),
    ])
    totals = {"this": {}, "previous": {}}
    for row in table(first[0]):
        totals[row.get("dateRange", "this")] = row
    data = {"days": days, "totals": totals, "daily": table(first[1]), "channels": table(first[2]), "pages": table(first[3]),
            "countries": table(first[4]), "devices": table(second[0]), "sources": table(second[1]), "events": table(second[2]),
            "landing": table(second[3]), "fetched_at": time.time()}
    _cache[key] = {"at": time.time(), "data": data}
    return data


# ---------------------------------------------------------------------------------------------------- what to do about it
def pct_change(now: float, before: float) -> float | None:
    return None if not before else (now - before) / before * 100.0


def event_count(data: dict, name: str) -> float:
    return next((r["eventCount"] for r in data["events"] if r["eventName"] == name), 0.0)


def funnel(data: dict) -> list[dict]:
    """Visit to payment, from the events the site and the server already send."""
    steps = [("Visitors", data["totals"]["this"].get("activeUsers", 0.0)),
             ("Clicked a start button", event_count(data, "cta_click")),
             ("Created an account", event_count(data, "sign_up")),
             ("Placed an order", event_count(data, "order_submitted")),
             ("Paid", event_count(data, "purchase"))]
    top = steps[0][1] or 1.0
    return [{"label": n, "count": c, "share": 100.0 * c / top} for n, c in steps]


def insights(data: dict) -> list[dict]:
    """Plain-language findings with a next step, from simple rules over the numbers (no guessing beyond them)."""
    out: list[dict] = []
    t, p = data["totals"]["this"], data["totals"]["previous"]
    if not t.get("sessions"):
        return [{"level": "warn", "title": "No visits recorded in this period",
                 "detail": "Check that the cookie banner lets people accept analytics, and that the measurement id on the site is the property you are reading."}]
    change = pct_change(t.get("activeUsers", 0), p.get("activeUsers", 0))
    if change is not None and change <= -15:
        out.append({"level": "bad", "title": f"Visitors are down {abs(change):.0f}% on the previous period",
                    "detail": "Look at the channel table below to see which source dropped, and check search rankings for the pages that used to bring traffic."})
    elif change is not None and change >= 15:
        out.append({"level": "good", "title": f"Visitors are up {change:.0f}% on the previous period",
                    "detail": "Find which channel grew in the table below and put more into it."})
    if t.get("keyEvents", 0) == 0:
        out.append({"level": "warn", "title": "No key events (conversions) are being counted",
                    "detail": "In GA4 Admin > Events, mark sign_up, generate_lead, order_submitted and purchase as key events so reports show what the site earns."})
    if t.get("engagementRate", 1) < 0.5:
        out.append({"level": "warn", "title": f"Only {t['engagementRate'] * 100:.0f}% of visits engage (stay 10 seconds, view 2 pages or convert)",
                    "detail": "Check the top landing pages below: clearer headlines and a faster first screen usually move this the most."})
    for ch in data["channels"]:
        if ch.get("sessions", 0) >= 20 and ch.get("keyEvents", 0) == 0:
            out.append({"level": "warn", "title": f"{ch['sessionDefaultChannelGroup']} brings {ch['sessions']:.0f} visits and no sign-ups",
                        "detail": "Match the page they land on to what brought them (the ad, post or search), or rethink spending on this channel."})
            break
    for pg in data["pages"]:
        users = pg.get("activeUsers", 0)
        if pg.get("screenPageViews", 0) >= 30 and users and pg.get("userEngagementDuration", 0) / users < 12 and pg["pagePath"] not in ("/", "/login"):
            out.append({"level": "warn", "title": f"People leave {pg['pagePath']} within seconds",
                        "detail": f"{pg['screenPageViews']:.0f} views but about {pg['userEngagementDuration'] / users:.0f} seconds each. Review the opening, the title and the load speed."})
            break
    mobile = next((d for d in data["devices"] if d["deviceCategory"] == "mobile"), None)
    desktop = next((d for d in data["devices"] if d["deviceCategory"] == "desktop"), None)
    total_sessions = sum(d.get("sessions", 0) for d in data["devices"]) or 1
    if mobile and desktop and mobile["sessions"] / total_sessions >= 0.4 and desktop.get("engagementRate", 0) - mobile.get("engagementRate", 0) >= 0.15:
        out.append({"level": "warn", "title": "Mobile visitors engage far less than desktop visitors",
                    "detail": f"{mobile['sessions'] / total_sessions * 100:.0f}% of visits are on phones, with an engagement rate {(desktop['engagementRate'] - mobile['engagementRate']) * 100:.0f} points lower. Test the main pages on a phone."})
    clicks, signups = event_count(data, "cta_click"), event_count(data, "sign_up")
    if clicks >= 30 and signups / clicks < 0.1:
        out.append({"level": "warn", "title": f"Many click a start button ({clicks:.0f}) but few create an account ({signups:.0f})",
                    "detail": "Look at the sign-up page: fewer fields, a clear promise, and sign in with Google."})
    countries = data["countries"]
    users_total = sum(c.get("activeUsers", 0) for c in countries) or 1
    us = next((c for c in countries if c["country"] == "United States"), None)
    if users_total >= 50 and (not us or us["activeUsers"] / users_total < 0.3):
        share = 0 if not us else us["activeUsers"] / users_total * 100
        out.append({"level": "warn", "title": f"Only {share:.0f}% of visitors are in the United States",
                    "detail": "The US is the focus market. Check where search and social traffic comes from, and aim posts and ads at US searches."})
    if not out:
        out.append({"level": "good", "title": "Nothing alarming in this period", "detail": "Keep an eye on the funnel below and compare again next week."})
    return out
