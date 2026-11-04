"""A kind word when someone has used half, and then most, of this month's free minutes.

Free minutes are there to try Kauli. The moment a customer has used half of them is the natural time to say what happens next, so they are never
surprised: how many are left, that they reset every month, what the pay-as-you-go price is, and that a monthly plan exists if they will keep
sending files. One in-app notice and one email at 50% and again at 80%, once per level per month. Nothing is offered as a discount.
"""
from __future__ import annotations

import time

from . import billing, db

LEVELS = (50, 80)


def _period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def status(user_id: str) -> dict | None:
    """Where a pay-as-you-go account stands on its free minutes this month; None for an account on a paid plan."""
    user = db.get_user(user_id)
    if user is None or user["role"] != "client":
        return None
    sub = db.get_subscription_current(user_id)
    plan = billing.effective_plan(user, sub)
    if plan != "free":
        return None
    total = billing.plan_included_minutes("free")
    if sub is not None:
        total += (sub["bonus_minutes"] or 0.0) + billing._reward_minutes(sub)
    used = (sub["minutes_used_this_period"] or 0.0) if sub is not None else 0.0
    if total <= 0:
        return None
    return {"used": round(used, 1), "total": round(total, 1), "left": round(max(0.0, total - used), 1), "pct": round(100.0 * used / total, 1)}


def banner(user) -> dict | None:
    """What the dashboard shows: the highest level reached, or None under 50%."""
    if not user or user["role"] != "client":
        return None
    st = status(user["client_scope_id"])
    if not st or st["pct"] < LEVELS[0]:
        return None
    return {**st, "level": max(lv for lv in LEVELS if st["pct"] >= lv)}


def _words(level: int, st: dict, name: str) -> tuple[str, str, str]:
    pro = billing.PLANS["pro"]
    price = billing.AI_PRICES["transcribe"] if hasattr(billing, "AI_PRICES") else 0.12
    if level >= 80:
        subject = f"You have {st['left']:g} free minutes left this month"
        lead = f"you have used {st['used']:g} of your {st['total']:g} free Instant AI minutes this month, so {st['left']:g} are left."
    else:
        subject = "You have used half of your free minutes this month"
        lead = f"you have used {st['used']:g} of your {st['total']:g} free Instant AI minutes this month, so {st['left']:g} are left."
    body = (f"Hi {name},\n\n{lead} They start again every month.\n\n"
            "Free minutes let you process a file and read the result. To download or edit a result, you pay for that order (pay as you go), and the exact price is always "
            f"shown before you order. After the free minutes, Instant AI transcription is ${price:.2f} per audio-minute.\n\n"
            f"If you expect to send more each month, the {pro['name']} plan (${pro['price_usd']} a month) includes {int(pro['included_ai_minutes']):,} Instant AI minutes for transcription "
            "and translation. You never have to switch: pay as you go stays available.\n\n"
            "If anything is unclear, just reply to this email and a person will answer.\n\nKauli Team")
    return subject, body, f"{pro['name']} plan"


def check(user_id: str, base_url: str, send) -> list[int]:
    """Call after an order uses free minutes. Sends each level the account has just reached, once per month. `send` is the app's
    _queue_and_send(user, kind, subject, body, base_url, cta_text, cta_url). Returns the levels sent."""
    st = status(user_id)
    if not st:
        return []
    user = db.get_user(user_id)
    sent = []
    for level in LEVELS:
        if st["pct"] >= level and db.mark_usage_prompt(user_id, _period(), level):
            subject, body, _ = _words(level, st, (user["display_name"] or "there").split(" ")[0])
            db.create_notification(user_id, f"usage_{level}", subject, "/client/billing#change-plan")
            try:
                send(user, f"usage_{level}", subject, body, base_url=base_url, cta_text="See your plan", cta_url=f"{base_url.rstrip('/')}/client/billing#change-plan")
            except Exception:  # noqa: BLE001 - the notice in the app is already there
                pass
            sent.append(level)
    return sent
