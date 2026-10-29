"""Winning back customers who paid and then went quiet: a unique coupon code for one free month of their subscription, offered once.

Who gets it: account owners who have PAID Kauli at some point (a plan, or a per-order payment) and have then been silent for a while,
including people whose subscription ran out and was not renewed. Someone still paying for an auto-renewing plan is not silent in the
sense that matters, so they are left alone. People who only ever used the free monthly minutes never get it: they have not shown they
will pay, and the free minutes are already there for them. Nobody is offered it twice, nobody who unsubscribed from marketing is emailed,
and the sweep that sends offers on its own is off until it is switched on in the server settings.

The code (RETURN-XXXX-XXXX) is unique, works for one account only, can be used once and expires 30 days after it is sent. Using it starts
30 days of the plan they had before (Pro if they never had one): no card, nothing to cancel, and it ends by itself. It is a gift of time,
never a percentage off a price and never cash.
"""
from __future__ import annotations

import html
import os
import secrets
import time
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import billing, db, mailer

router = APIRouter()
A = None

INACTIVE_DAYS = 21           # silent this long (no sign-in, order or payment) before we get in touch
GIVE_UP_DAYS = 365           # not heard from in a year: we leave them alone
OFFER_VALID_DAYS = 30        # how long the code can be used after it is sent
FREE_MONTH_DAYS = 30         # how long the free subscription lasts once the code is used
SWEEP_INTERVAL_S = 24 * 3600
MAX_SENDS_PER_SWEEP = 25
OPEN_STATUSES = ("returned_to_client", "pending_payment", "queued", "processing", "awaiting_review", "editor_returned")

SEGMENT_LABEL = {"stopped_paying": "Stopped paying", "went_quiet": "Paid, then went quiet"}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                    # no look-alike characters
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
    return f"RETURN-{part()}-{part()}"


def plan_for(sub) -> str:
    """The plan the free month gives: the one they had before, or Pro for people who only paid per order."""
    prior = sub["plan"] if sub and sub["plan"] in billing.PLANS else "free"
    return prior if prior in ("pro", "premium") else "pro"


# ----------------------------------------------------------------------------------------------------------- who qualifies
def _last_active(user, orders, payments) -> float:
    stamps = [user["created_at"] or 0, user["last_seen_at"] or 0]
    stamps += [o["created_at"] or 0 for o in orders] + [o["status_changed_at"] or 0 for o in orders]
    stamps += [p["completed_at"] or p["created_at"] or 0 for p in payments]
    return max(stamps)


def eligible(now: float | None = None) -> list[dict]:
    """Every account owner who should be offered the free month right now, with why. Never someone on free minutes only."""
    now = now or time.time()
    conn = db.get_conn()
    users = conn.execute(
        "SELECT * FROM users WHERE role = 'client' AND account_status != 'closed' "
        "AND id NOT IN (SELECT member_user_id FROM team_members WHERE status = 'accepted' AND member_user_id IS NOT NULL) "
        "AND id NOT IN (SELECT user_id FROM winback_offers)").fetchall()
    out = []
    for user in users:
        if user["marketing_consent_at"] and not user["marketing_consent"]:
            continue                                            # they unsubscribed: no promotional email
        payments = conn.execute("SELECT * FROM payments WHERE user_id = ? AND status = 'completed' AND amount_usd > 0", (user["id"],)).fetchall()
        sub = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user["id"],)).fetchone()
        had_plan = bool(sub and sub["plan"] and sub["plan"] != "free")
        if not payments and not had_plan:
            continue                                            # free minutes only: not part of this offer
        lapsed = had_plan and billing.effective_plan(user, sub) == "free"
        if had_plan and not lapsed and billing._is_recurring(sub):
            continue                                            # still paying every month: nothing to win back
        orders = list(db.list_orders_for_client(user["id"]))
        if any(o["status"] in OPEN_STATUSES for o in orders):
            continue                                            # something is still going on: not silent
        quiet_days = (now - _last_active(user, orders, payments)) / 86400
        if quiet_days < INACTIVE_DAYS or quiet_days > GIVE_UP_DAYS:
            continue
        out.append({"user": user, "segment": "stopped_paying" if lapsed else "went_quiet", "quiet_days": int(quiet_days),
                    "paid_total": round(sum(p["amount_usd"] for p in payments), 2)})
    conn.close()
    out.sort(key=lambda r: -r["quiet_days"])
    return out


# --------------------------------------------------------------------------------------------------------------- the offer
def _email(user, segment: str, plan: str, code: str, link: str) -> tuple[str, str, str]:
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    plan_name = billing.PLANS[plan]["name"]
    lead = ("We noticed your plan ended, and we would love to have you back." if segment == "stopped_paying"
            else "It has been a while since your last project, and we would love to see you back.")
    lines = [lead,
             f"So here is a month on us: {FREE_MONTH_DAYS} days of {plan_name}, free. No card needed and nothing to cancel; it simply ends after the month.",
             f"Your personal code is {code}. It works for your account only and can be used once, until {_date(time.time() + OFFER_VALID_DAYS * 86400)}. Open the link and press one button to start."]
    text = f"Hi {name},\n\n" + "\n\n".join(lines) + f"\n\n{link}\n\nTalk soon,\nKauli Team"
    inner = f'<p style="margin:0 0 14px;">Hi {html.escape(name)},</p>' + "".join(f'<p style="margin:0 0 14px;">{html.escape(x)}</p>' for x in lines) \
        + '<p style="margin:0;">Talk soon,<br>Kauli Team</p>'
    return "A free month of Kauli, on us", inner, text


def send_offer(user, segment: str, base_url: str) -> bool:
    """Record the offer and email it. The record comes first, so a person is never offered twice even if the email fails."""
    base_url = base_url.rstrip("/")
    code = new_code()
    plan = plan_for(db.get_subscription(user["id"]))
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO winback_offers (id, user_id, email, segment, plan, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (code, user["id"], user["email"], segment, plan, time.time()))
        conn.commit()
    except Exception:  # noqa: BLE001 - UNIQUE(user_id): already offered
        conn.close()
        return False
    conn.close()
    link = f"{base_url}/client/winback/{code}"
    try:
        if mailer.email_configured():
            subject, inner, text = _email(user, segment, plan, code, link)
            mailer.send_email(user["email"], subject, mailer.wrap_email_html(inner, cta_text="Get my free month", cta_url=link, base_url=base_url), text)
    except Exception:  # noqa: BLE001 - the offer still shows in their account
        pass
    return True


def open_offer(user_id: str):
    """The offer waiting for this customer to use, if there is one."""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM winback_offers WHERE user_id = ? AND claimed_at IS NULL AND sent_at >= ?",
                       (user_id, time.time() - OFFER_VALID_DAYS * 86400)).fetchone()
    conn.close()
    return row


def claim(user_id: str, code: str) -> str | None:
    """Start the free month. Returns an error message, or None when it worked. The code only ever works for the account it was made for."""
    code = (code or "").strip().upper()
    conn = db.get_conn()
    offer = conn.execute("SELECT * FROM winback_offers WHERE id = ? AND user_id = ?", (code, user_id)).fetchone()
    conn.close()
    if not offer:
        return "That code is not valid for this account."
    if offer["claimed_at"]:
        return "This code has already been used."
    if offer["sent_at"] < time.time() - OFFER_VALID_DAYS * 86400:
        return "This code has expired."
    now = time.time()
    user = db.get_user(user_id)
    sub = db.get_subscription(user_id)
    if sub and billing.effective_plan(user, sub) != "free" and sub["current_period_end"]:
        end = max(sub["current_period_end"], now) + FREE_MONTH_DAYS * 86400       # a plan still running just gets a month added
        conn = db.get_conn()
        conn.execute("UPDATE subscriptions SET current_period_end = ? WHERE user_id = ?", (end, user_id))
        conn.commit()
        conn.close()
    else:
        db.set_subscription_plan(user_id, offer["plan"], FREE_MONTH_DAYS)          # no card, no renewal: it lapses on its own
        end = now + FREE_MONTH_DAYS * 86400
    conn = db.get_conn()
    conn.execute("UPDATE winback_offers SET claimed_at = ?, expires_at = ? WHERE id = ?", (now, end, code))
    conn.commit()
    conn.close()
    return None


def send_all(base_url: str, limit: int = MAX_SENDS_PER_SWEEP) -> int:
    sent = 0
    for row in eligible()[:limit]:
        if send_offer(row["user"], row["segment"], base_url):
            sent += 1
    return sent


def autosend_enabled() -> bool:
    return os.environ.get("KAULI_WINBACK_AUTOSEND", "").strip().lower() in ("1", "true", "yes", "on")


def sweep_once(base_url: str) -> dict:
    return {"sent": send_all(base_url) if autosend_enabled() else 0}


def stats() -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT COUNT(*) AS sent, SUM(claimed_at IS NOT NULL) AS claimed FROM winback_offers").fetchone()
    recent = conn.execute("SELECT * FROM winback_offers ORDER BY sent_at DESC LIMIT 50").fetchall()
    conn.close()
    return {"sent": row["sent"] or 0, "claimed": row["claimed"] or 0, "recent": recent}


# ------------------------------------------------------------------------------------------------------------------- pages
def _owner(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "client" and user["client_scope_id"] == user["id"] else None


@router.get("/client/winback/{code}", response_class=HTMLResponse)
def winback_page(request: Request, code: str, error: str | None = None):
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next=/client/winback/{quote(code)}", status_code=303)
    conn = db.get_conn()
    offer = conn.execute("SELECT * FROM winback_offers WHERE id = ? AND user_id = ?", (code.strip().upper(), user["id"])).fetchone()
    conn.close()
    if not offer:
        return RedirectResponse("/client/billing?error=" + quote("That code is not valid for this account."), status_code=303)
    ended = offer["sent_at"] < time.time() - OFFER_VALID_DAYS * 86400 and not offer["claimed_at"]
    return A.templates.TemplateResponse(request, "winback.html", {
        "user": user, "role": "client", "offer": offer, "ended": ended, "error": error, "days": FREE_MONTH_DAYS, "plan_name": billing.PLANS[offer["plan"]]["name"],
        "until": _date(offer["expires_at"]) if offer["claimed_at"] else "", "open_until": _date(offer["sent_at"] + OFFER_VALID_DAYS * 86400)})


@router.post("/client/winback/{code}")
def winback_claim(request: Request, code: str):
    user = _owner(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    error = claim(user["id"], code)
    if error:
        return RedirectResponse(f"/client/winback/{quote(code)}?error={quote(error)}", status_code=303)
    return RedirectResponse(f"/client/winback/{quote(code.strip().upper())}", status_code=303)


@router.post("/client/redeem")
def redeem(request: Request, code: str = Form("")):
    """The "Have a code?" box on the billing page."""
    user = _owner(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    code = code.strip().upper()
    error = claim(user["id"], code)
    if error:
        return RedirectResponse("/client/billing?error=" + quote(error), status_code=303)
    return RedirectResponse(f"/client/winback/{quote(code)}", status_code=303)


def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


@router.get("/staff/winback", response_class=HTMLResponse)
def staff_winback(request: Request, notice: str | None = None):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return A.templates.TemplateResponse(request, "staff_winback.html", {
        "user": user, "rows": eligible(), "stats": stats(), "labels": SEGMENT_LABEL, "autosend": autosend_enabled(),
        "inactive_days": INACTIVE_DAYS, "days": FREE_MONTH_DAYS, "notice": notice, "when": _date, "mail_ready": mailer.email_configured()})


@router.post("/staff/winback/send")
def staff_winback_send(request: Request, user_id: str = Form("")):
    if not _staff(request):
        return RedirectResponse("/login", status_code=303)
    base = A._site_base(request)
    if user_id:
        row = next((r for r in eligible() if r["user"]["id"] == user_id), None)
        sent = 1 if row and send_offer(row["user"], row["segment"], base) else 0
    else:
        sent = send_all(base)
    return RedirectResponse(f"/staff/winback?notice={sent}+offer{'' if sent == 1 else 's'}+sent", status_code=303)
