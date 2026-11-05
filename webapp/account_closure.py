"""Closing an account, done respectfully: one self-serve page (no call or email needed), a one-question survey that is optional, an honest
alternative for whatever the reason is, a plain "what happens next", a close button that always works, and a confirmation email.
What people tell us goes to staff (a page, and a notification each time) so the reasons people leave get read and acted on.
"""
from __future__ import annotations

import html
import time
import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import billing, db, mailer

router = APIRouter()
A = None

REASONS = {
    "too_expensive": "It costs too much",
    "missing_feature": "A feature I need is missing",
    "switching": "I'm switching to another service",
    "finished": "My project is finished",
    "pause": "I need a break for now",
    "not_using": "I'm not using it enough",
    "other": "Something else",
}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def _client(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "client" else None


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""


def _facts(user) -> dict:
    scope = user["client_scope_id"]
    sub = db.get_subscription_current(scope)
    plan = billing.effective_plan(user, sub)
    orders = list(db.list_orders_for_client(scope))
    recurring = bool(sub and billing._is_recurring(sub) and sub["paystack_email_token"])
    # a plan whose renewal is failing still counts as a paid plan: the customer is being charged for it, so closing must stop that
    if recurring and plan == "free" and sub["plan"] in billing.PLANS:
        plan = sub["plan"]
    return {"plan": plan, "plan_name": billing.PLANS[plan]["name"], "paid": plan != "free", "recurring": recurring,
            "period_end": _date(sub["current_period_end"]) if sub and sub["current_period_end"] and plan != "free" else "",
            "orders": len(orders), "free_left": round(billing.free_minutes_remaining(sub)), "is_owner": user["client_scope_id"] == user["id"],
            "pro_price": billing.PLANS["pro"]["price_usd"]}


@router.get("/client/close-account", response_class=HTMLResponse)
def close_page(request: Request, error: str | None = None):
    user = _client(request)
    if not user:
        return RedirectResponse("/login?next=/client/close-account", status_code=303)
    return A.templates.TemplateResponse(request, "account_close.html", {"user": user, "role": "client", "reasons": REASONS, "facts": _facts(user), "error": error})


def _confirmation_email(user, facts: dict) -> tuple[str, str, str]:
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    lines = ["Your Kauli account is closed.",
             "You are signed out everywhere, and you cannot sign in with this account again unless we reopen it.",
             ("Your plan will not renew, and you will not be charged again." + (f" What you already paid for ran until {facts['period_end']}." if facts["paid"] and facts["period_end"] else ""))
             if facts["paid"] else "You were on Pay as you go, so there is nothing to cancel and nothing more to pay.",
             "Your orders and payment records are kept for accounting. If you want your files deleted as well, reply to this email and we will do it.",
             "Changed your mind? Reply to this email and we will reopen your account with everything as it was."]
    body = f"Hi {name},\n\n" + "\n\n".join(lines) + "\n\nTalk soon,\nKauli Team"
    inner = f'<p style="margin:0 0 14px;">Hi {html.escape(name)},</p>' + "".join(f'<p style="margin:0 0 14px;">{html.escape(x)}</p>' for x in lines) \
        + '<p style="margin:0;">Talk soon,<br>Kauli Team</p>'
    return "Your Kauli account is closed", inner, body


@router.post("/client/close-account")
def close_account(request: Request, confirm_text: str = Form(""), reason: str = Form(""), detail: str = Form(""), competitor: str = Form("")):
    user = _client(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    def back(message: str):
        return RedirectResponse(f"/client/close-account?error={quote(message)}", status_code=303)

    if confirm_text.strip().lower() != user["email"].strip().lower():
        return back("Type your email exactly as shown to confirm.")
    facts = _facts(user)
    if facts["is_owner"] and facts["recurring"]:
        # a closed account must never be charged again: stop the renewal first, and if that fails close nothing and say so
        sub = db.get_subscription(user["id"])
        result = billing.paystack_disable_subscription(sub["paystack_subscription_code"], sub["paystack_email_token"])
        if not result["ok"]:
            return back("We could not stop your subscription from renewing just now, so nothing was closed. Please try again in a minute, or email us and we will do it for you.")
        db.set_paystack_subscription(user["id"], None, None)
    reason = reason if reason in REASONS else ""
    note = " ".join(x for x in ((f"Switching to: {competitor.strip()[:80]}." if competitor.strip() and reason == "switching" else ""), detail.strip()[:1000]) if x)
    conn = db.get_conn()
    conn.execute("INSERT INTO account_exit_surveys (id, user_id, email, plan, reason, detail, orders_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (uuid.uuid4().hex[:12], user["id"], user["email"], facts["plan"], reason, note, facts["orders"], time.time()))
    conn.commit()
    conn.close()
    db.log_security_event("account_closed", user_id=user["id"], email=user["email"], ip_address=A.rate_limit.client_ip(request))
    db.close_account(user["id"])
    try:
        db.notify_all_staff("account_closed", f"Account closed: {user['email']} ({REASONS.get(reason, 'no reason given')})", "/staff/exit-feedback")
    except Exception:  # noqa: BLE001 - the survey row is the record
        pass
    try:
        if mailer.email_configured():
            subject, inner, body = _confirmation_email(user, facts)
            mailer.send_as("support", user["email"], subject, mailer.wrap_email_html(inner, base_url=A._site_base(request)), body)
    except Exception:  # noqa: BLE001 - closing must never fail because an email did
        pass
    request.session.clear()
    return RedirectResponse("/login?notice=account_closed", status_code=303)


@router.get("/staff/exit-feedback", response_class=HTMLResponse)
def exit_feedback(request: Request, days: int = 90):
    user = A.current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login", status_code=303)
    days = days if days in (30, 90, 365) else 90
    since = time.time() - days * 86400
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM account_exit_surveys WHERE created_at >= ? ORDER BY created_at DESC LIMIT 200", (since,)).fetchall()
    conn.close()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["reason"] or ""] = counts.get(r["reason"] or "", 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return A.templates.TemplateResponse(request, "staff_exit_feedback.html", {
        "user": user, "rows": rows, "counts": ordered, "total": len(rows), "reasons": REASONS, "days": days, "when": _date})
