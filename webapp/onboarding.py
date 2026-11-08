"""Getting a new customer's first order ready with as little friction as possible, while keeping the platform safe.

The idea: friction should land on the people who might abuse the service, not on everyone. Every new account gets a small, explainable risk
score from signals we already have; the score decides how generous the first order is:

    low risk     the first Instant AI order is processed and delivered free, up to 120 minutes (the customer reads the start of it; the
                 choosing a plan, which costs nothing for the Free plan, unlocks the rest)
    medium risk  the same, up to 45 minutes
    high risk    no free first order: they use the normal free minutes and pay as you go like anyone else (nothing is blocked)

Signals (each one a reason staff can read, never an automatic ban): a throw-away email address, a sign-up from a cloud server, several new
accounts from the same address in a day, and, in the other direction, signing in with Google (a verified identity).

Then the follow-through: how long a new customer takes to get their first result ("time to first result"), an alert to staff when a first
order is stuck, and a two-click satisfaction question after the first and third result (the 1 to 5 stars and a line).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import db

router = APIRouter()
A = None

DAY = 86400
CAPS = {"low": 120.0, "medium": 45.0, "high": 0.0}
CSAT_AFTER = (1, 3, 6)          # ask after the 1st, 3rd and 6th finished order
STUCK_HOURS = 2
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org",
    "throwawaymail.com", "yopmail.com", "sharklasers.com", "trashmail.com", "getnada.com", "maildrop.cc", "dispostable.com", "fakeinbox.com",
    "mohmal.com", "emailondeck.com", "moakt.com", "tempail.com", "mintemail.com", "spamgourmet.com", "burnermail.io", "tmpmail.org", "mailnesia.com",
}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ----------------------------------------------------------------------------------------------------------------- the score
def risk(user, now: float | None = None) -> dict:
    """{'score', 'level', 'reasons'} for a new account. Higher is riskier; every reason is something a person can check."""
    now = now or time.time()
    reasons: list[str] = []
    score = 0
    domain = (user["email"] or "").rsplit("@", 1)[-1].lower()
    if domain in DISPOSABLE:
        score += 60
        reasons.append("throw-away email address")
    try:
        datacenter = user["signup_ip_is_datacenter"]
    except (KeyError, IndexError):
        datacenter = None
    if datacenter:
        score += 30
        reasons.append("signed up from a cloud server")
    conn = db.get_conn()
    try:
        ip = None
        try:
            ip = user["signup_ip"]
        except (KeyError, IndexError):
            pass
        if ip:
            others = conn.execute("SELECT COUNT(*) AS n FROM users WHERE signup_ip = ? AND id != ? AND role = 'client' AND created_at >= ?",
                                  (ip, user["id"], (user["created_at"] or now) - DAY)).fetchone()["n"]
            if others >= 3:
                score += 50
                reasons.append(f"{others} other accounts from the same address in a day")
            elif others >= 1:
                score += 15
                reasons.append("another account from the same address")
        google = conn.execute("SELECT 1 FROM security_events WHERE user_id = ? AND kind = 'signup' AND detail LIKE 'google%' LIMIT 1", (user["id"],)).fetchone()
        if google:
            score -= 20
            reasons.append("signed in with Google (verified identity)")
    finally:
        conn.close()
    score = max(score, 0)
    level = "low" if score < 35 else "medium" if score < 60 else "high"
    return {"score": score, "level": level, "reasons": reasons}


def first_order_cap(user) -> float:
    """How many minutes of a first Instant AI order are processed free for this account."""
    return CAPS[risk(user)["level"]]


# --------------------------------------------------------------------------------------------------- time to first result
def ttfv_minutes(days: int = 90, now: float | None = None) -> dict:
    """How long new customers take to get a first finished result: the median and the share who got one within a day."""
    now = now or time.time()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT u.id, u.created_at AS joined, MIN(o.status_changed_at) AS done FROM users u JOIN orders o ON o.client_id = u.id "
            "WHERE u.role = 'client' AND u.created_at >= ? AND o.status IN ('ready_for_delivery', 'delivered') GROUP BY u.id", (now - days * DAY,)).fetchall()
        joined = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'client' AND created_at >= ?", (now - days * DAY,)).fetchone()["n"]
    finally:
        conn.close()
    mins = sorted(max((r["done"] - r["joined"]) / 60.0, 0) for r in rows if r["done"] and r["joined"])
    median = mins[len(mins) // 2] if mins else None
    within_day = sum(1 for m in mins if m <= 24 * 60)
    return {"median": median, "got_result": len(mins), "joined": joined, "within_day": within_day,
            "share_within_day": round(100.0 * within_day / joined, 1) if joined else 0.0}


def first_orders_needing_help(now: float | None = None) -> list[dict]:
    """New customers whose first order has not become a result: held for payment, failed, or waiting more than a couple of hours."""
    now = now or time.time()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT o.id AS order_id, o.client_id, o.status, o.created_at, u.email FROM orders o JOIN users u ON u.id = o.client_id "
            "WHERE u.role = 'client' AND o.status IN ('pending_payment', 'failed', 'dead_letter', 'awaiting_review', 'queued', 'processing', 'returned_to_client') "
            "AND o.created_at < ? AND NOT EXISTS (SELECT 1 FROM orders d WHERE d.client_id = o.client_id AND d.status IN ('ready_for_delivery', 'delivered')) "
            "ORDER BY o.created_at", (now - STUCK_HOURS * 3600,)).fetchall()
    finally:
        conn.close()
    seen, out = set(), []
    for r in rows:
        if r["client_id"] in seen:
            continue
        seen.add(r["client_id"])
        out.append({"order_id": r["order_id"], "user_id": r["client_id"], "email": r["email"], "status": r["status"], "hours": int((now - r["created_at"]) // 3600)})
    return out


def alert_stuck_first_orders(now: float | None = None) -> int:
    """Tell staff, once a day per customer, about a first order that is not moving, so someone helps before the customer gives up."""
    now = now or time.time()
    sent = 0
    for item in first_orders_needing_help(now):
        conn = db.get_conn()
        last = conn.execute("SELECT MAX(sent_at) AS t FROM engagement_messages WHERE user_id = ? AND kind = 'stuck_alert'", (item["user_id"],)).fetchone()["t"]
        if last and now - last < DAY:
            conn.close()
            continue
        conn.execute("INSERT INTO engagement_messages (id, user_id, kind, sent_at) VALUES (?, ?, 'stuck_alert', ?)", (uuid.uuid4().hex, item["user_id"], now))
        conn.commit()
        conn.close()
        db.notify_all_staff("first_order_stuck", f"A new customer's first order has been {item['status'].replace('_', ' ')} for {item['hours']}h: {item['email']}",
                            f"/staff/orders/{item['order_id']}")
        sent += 1
    return sent


# ------------------------------------------------------------------------------------------------------ the satisfaction question
def csat_due(user_id: str, order_id: str) -> bool:
    """Ask after the first, third and sixth finished order, once per order, and only if this order is one of those."""
    conn = db.get_conn()
    try:
        done = [r["id"] for r in conn.execute(
            "SELECT id FROM orders WHERE client_id = ? AND status IN ('ready_for_delivery', 'delivered') ORDER BY created_at", (user_id,)).fetchall()]
        if order_id not in done or (done.index(order_id) + 1) not in CSAT_AFTER:
            return False
        return conn.execute("SELECT 1 FROM csat_responses WHERE order_id = ?", (order_id,)).fetchone() is None
    finally:
        conn.close()


def csat_summary(days: int = 90, now: float | None = None) -> dict:
    now = now or time.time()
    conn = db.get_conn()
    try:
        r = conn.execute("SELECT COUNT(*) AS n, AVG(score) AS avg FROM csat_responses WHERE created_at >= ?", (now - days * DAY,)).fetchone()
        recent = conn.execute("SELECT score, comment FROM csat_responses WHERE comment != '' AND created_at >= ? ORDER BY created_at DESC LIMIT 5", (now - days * DAY,)).fetchall()
    finally:
        conn.close()
    return {"n": r["n"], "avg": round(r["avg"], 2) if r["avg"] else None, "recent": [dict(x) for x in recent]}


@router.post("/account/orders/{order_id}/rate")
def rate(request: Request, order_id: str, score: int = Form(0), comment: str = Form("")):
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login", status_code=303)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return RedirectResponse("/account/home", status_code=303)
    if 1 <= score <= 5 and csat_due(order["client_id"], order_id):
        conn = db.get_conn()
        conn.execute("INSERT INTO csat_responses (id, user_id, order_id, score, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex, user["id"], order_id, score, comment.strip()[:300], time.time()))
        conn.commit()
        conn.close()
    return RedirectResponse(f"/account/orders/{order_id}?notice=Thank+you%2C+that+helps+us+a+lot.", status_code=303)
