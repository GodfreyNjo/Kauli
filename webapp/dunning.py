"""When a subscription payment fails (low funds, expired card, a bank hiccup) the customer has not chosen to leave, so we do not treat them
as if they had. Paystack retries the card by itself and tells us what happens, so this module sets NO timers of its own: it only reacts to
what Paystack reports.

  - a renewal fails: the plan stays open, a kind email goes out, and a polite banner points to a one-click card update
  - Paystack's next retry fails too: one more gentle email
  - a renewal finally goes through: the banner goes away
  - Paystack gives up and ends the subscription: the plan is paused, never deleted (account, files and free minutes stay), with a last
    "we are here when you are ready" email
  - at any point the customer can step down to Pay as you go instead

While a failure is open the plan is held open until Paystack settles it. BACKSTOP_DAYS is only a safety net so access cannot last forever if a
Paystack message never reaches us.
"""
from __future__ import annotations

import html
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import billing, db, mailer

router = APIRouter()
A = None

BACKSTOP_DAYS = 45
DAY = 86400


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)
    app_module.templates.env.globals["payment_issue"] = payment_issue


def _date(ts) -> str:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""


def _open(user_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM payment_failures WHERE user_id = ? AND resolved_at IS NULL", (user_id,)).fetchone()
    conn.close()
    return row


def payment_issue(user) -> dict | None:
    """What the banner and billing page show: only for the account owner while a failed payment is still unresolved."""
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return None
    return {"open": True} if _open(user["id"]) else None


def _set_grace(user_id: str, until: float) -> None:
    conn = db.get_conn()
    conn.execute("UPDATE subscriptions SET grace_until = ? WHERE user_id = ?", (until, user_id))
    conn.commit()
    conn.close()


def _send(user, subject: str, lines: list[str], base_url: str, cta_text: str | None = None, cta_url: str | None = None) -> None:
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    text = f"Hi {name},\n\n" + "\n\n".join(lines) + (f"\n\n{cta_url}" if cta_url else "") + "\n\nTalk soon,\nKauli Team"
    inner = f'<p style="margin:0 0 14px;">Hi {html.escape(name)},</p>' + "".join(f'<p style="margin:0 0 14px;">{html.escape(x)}</p>' for x in lines) \
        + '<p style="margin:0;">Talk soon,<br>Kauli Team</p>'
    try:
        if mailer.email_configured():
            mailer.send_as("billing", user["email"], subject, mailer.wrap_email_html(inner, cta_text=cta_text, cta_url=cta_url, base_url=base_url), text)
    except Exception:  # noqa: BLE001 - a failed email must never break the webhook
        pass


def record_failure(user, base_url: str, reason: str = "") -> None:
    """Paystack reports a failed subscription payment. First time: hold the plan open and send the heads-up. If Paystack's retry fails too,
    one more gentle email. Every later retry just counts."""
    now = time.time()
    base_url = base_url.rstrip("/")
    update_url = f"{base_url}/account/billing/update-card"
    row = _open(user["id"])
    conn = db.get_conn()
    if row:
        conn.execute("UPDATE payment_failures SET last_failed_at = ?, attempts = attempts + 1 WHERE user_id = ?", (now, user["id"]))
        send_again = row["attempts"] + 1 == 2
        conn.commit()
        conn.close()
        if send_again:
            _send(user, "We tried your payment again",
                  ["We tried your payment again, but it did not go through. To keep your plan without a break, you can update your details in a minute.",
                   "If money is tight right now, that is okay: you can move to Pay as you go in one click and keep everything."],
                  base_url, "Update payment method", update_url)
        return
    conn.execute("INSERT OR REPLACE INTO payment_failures (user_id, email, first_failed_at, last_failed_at, attempts, reason) VALUES (?, ?, ?, ?, 1, ?)",
                 (user["id"], user["email"], now, now, reason[:200]))
    conn.commit()
    conn.close()
    _set_grace(user["id"], now + BACKSTOP_DAYS * DAY)
    _send(user, "Your Kauli payment did not go through",
          ["Heads up: your automatic payment did not go through. This happens all the time: an expired card, low funds, a bank hiccup.",
           "Nothing changes for now. Your plan stays open while we try again, and your files and projects are exactly where you left them.",
           "When you have a minute, update your payment method and we will take care of the rest."],
          base_url, "Update payment method", update_url)


def resolve(user_id: str, outcome: str = "paid") -> None:
    row = _open(user_id)
    if not row:
        return
    conn = db.get_conn()
    conn.execute("UPDATE payment_failures SET resolved_at = ?, outcome = ? WHERE user_id = ?", (time.time(), outcome, user_id))
    conn.commit()
    conn.close()
    _set_grace(user_id, 0)


def finish(user, base_url: str) -> None:
    """Paystack has given up on the card: pause the plan, keep everything, say we are here when they are ready."""
    base_url = base_url.rstrip("/")
    db.set_paystack_subscription(user["id"], None, None)
    resolve(user["id"], "paused")
    _send(user, "Your Kauli plan is paused, and your files are safe",
          ["We could not take your last payment, so your plan is paused. Nothing has been deleted: your account, projects and files are all still here, and you can still use your 30 free Instant AI minutes every month.",
           "Whenever it suits you, pick your plan again with one click and carry on exactly where you were. We are here when you are ready."],
          base_url, "Restart my plan", f"{base_url}/account/billing#change-plan")


def expire_backstop(now: float | None = None) -> int:
    """Safety net only: a failure still open long after Paystack should have settled it (a message never arrived) stops holding the plan open."""
    now = now or time.time()
    conn = db.get_conn()
    rows = conn.execute("SELECT user_id FROM payment_failures WHERE resolved_at IS NULL AND first_failed_at < ?", (now - BACKSTOP_DAYS * DAY,)).fetchall()
    conn.close()
    for r in rows:
        resolve(r["user_id"], "lapsed")
    return len(rows)


# ------------------------------------------------------------------------------------------------------------------- pages
def _owner(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "client" and user["client_scope_id"] == user["id"] else None


@router.get("/account/billing/update-card")
def update_card(request: Request):
    """One click from the banner to a secure card form, with no hunting through menus."""
    user = _owner(request)
    if not user:
        return RedirectResponse("/login?next=/account/billing/update-card", status_code=303)
    sub = db.get_subscription(user["id"])
    if sub and sub["paystack_subscription_code"]:
        link = billing.paystack_manage_link(sub["paystack_subscription_code"])
        if link:
            return RedirectResponse(link, status_code=303)
    return RedirectResponse("/account/billing?error=" + quote("We could not open the card form just now. You can restart your plan from Plans, or try again in a minute."), status_code=303)


@router.post("/account/billing/switch-to-payg")
def switch_to_payg(request: Request):
    """The safety net: stop the failing renewal and keep the account on Pay as you go, so nobody has to leave."""
    user = _owner(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    sub = db.get_subscription(user["id"])
    if sub and billing._is_recurring(sub) and sub["paystack_email_token"]:
        result = billing.paystack_disable_subscription(sub["paystack_subscription_code"], sub["paystack_email_token"])
        if not result["ok"]:
            return RedirectResponse("/account/billing?error=" + quote(result["error"]), status_code=303)
    db.set_paystack_subscription(user["id"], None, None)
    resolve(user["id"], "downgraded")
    return RedirectResponse("/account/billing?notice=" + quote("You are on Pay as you go now. Nothing more will be charged, and all your files are still here."), status_code=303)


@router.get("/staff/payment-failures", response_class=HTMLResponse)
def staff_failures(request: Request):
    user = A.current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login", status_code=303)
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM payment_failures ORDER BY first_failed_at DESC LIMIT 100").fetchall()
    conn.close()
    return A.templates.TemplateResponse(request, "staff_payment_failures.html", {"user": user, "rows": rows, "when": _date})
