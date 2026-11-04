"""Tell a pay-as-you-go customer plainly when a monthly plan would have cost them less.

Only for work a monthly plan really covers: Instant AI transcription and translation come out of the plan's included minutes, while dubbing and
human review are always priced on their own, so they are left out of the comparison. This is a fact about their own recent orders, not a discount.
"""
from __future__ import annotations

import time

from . import billing, db


def pro_savings(user_id: str, days: int = 45) -> dict | None:
    user = db.get_user(user_id)
    if user is None or user["role"] != "client":
        return None
    sub = db.get_subscription_current(user_id)
    if billing.effective_plan(user, sub) != "free":
        return None
    since = time.time() - days * 86400
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT p.amount_usd AS usd, o.duration_minutes AS minutes, o.service_level AS level, o.review_tier AS tier, p.completed_at AS at "
            "FROM payments p JOIN orders o ON o.id = p.order_id WHERE p.user_id = ? AND p.status = 'completed' AND p.amount_usd > 0 AND p.completed_at >= ? "
            "ORDER BY p.completed_at DESC", (user_id, since)).fetchall()
    finally:
        conn.close()
    last_two = [r for r in rows if r["tier"] == "ai" and r["level"] in ("transcribe", "translate")][:2]
    if len(last_two) < 2:
        return None
    pro = billing.PLANS["pro"]
    paid = round(sum(r["usd"] for r in last_two), 2)
    minutes = round(sum(r["minutes"] or 0.0 for r in last_two), 1)
    if paid <= pro["price_usd"] or minutes > pro["included_ai_minutes"]:
        return None
    return {"paid": paid, "saved": round(paid - pro["price_usd"], 2), "minutes": minutes, "pro_price": pro["price_usd"],
            "pro_minutes": int(pro["included_ai_minutes"]), "orders": len(last_two)}
