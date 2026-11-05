"""The messages that follow a sale, each sent from the person a customer would expect to hear from:

  - "We have your order" (Kauli Orders): the moment work on an order starts. Transactional; on unless staff switch it off.
  - "How did it go?" (Kauli Customer Success): a few days after a delivery, asking for an honest review on Trustpilot. Everyone who got
    work delivered is asked on the same neutral terms (when their order was delivered), never picked by how happy they seem: Trustpilot's
    rules forbid inviting only the pleased customers, and the same email always carries a private way to tell us what went wrong.
  - "Time to come back?" (Kauli Customer Success): when a customer's own rhythm says they are due (the usual gap between their orders).
  - "You are one of our regulars" (Kauli Customer Success): recognition for customers with many orders. No money off: the standing rule is
    no discounts. The perks are attention (their replies are read first, they hear about new things first).
  - Win-back for customers who went quiet already exists in winback.py.

Everyone who unsubscribed from Kauli's emails is left out of the last three. Each message is recorded before it is sent, so a customer never
gets the same one twice even if sending fails. Everything except the order confirmation is OFF until staff switch it on at Staff > Lifecycle
emails, where they can see who would get it first and send a test to themselves.
"""
from __future__ import annotations

import html
import os
import re
import statistics
import time
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import quote, urlparse

from . import db, mailer, site_analytics

router = APIRouter()
A = None
DAY = 86400
REVIEW_AFTER_DAYS = 2                  # ask this long after delivery...
REVIEW_WINDOW_DAYS = 21                # ...and not at all once it is this old
REVIEW_REPEAT_DAYS = 90                # one review request per customer in this time
REBOOK_MIN_GAP_DAYS, REBOOK_MAX_GAP_DAYS, REBOOK_DEFAULT_GAP_DAYS = 14, 90, 30
REBOOK_REPEAT_DAYS = 60
REBOOK_STALE_DAYS = 180                # after this it is win-back territory
VIP_ORDERS, VIP_WINDOW_DAYS = 5, 180
MAX_SENDS_PER_SWEEP = 25
DEFAULT_TRUSTPILOT = "https://www.trustpilot.com/evaluate/kauli-forgemedia.com"
AUTOMATIONS = {
    "confirm": ("Order confirmation", "Emails the customer when work on their order starts.", True),
    "review": ("Review requests", "Asks customers for a Trustpilot review a few days after delivery.", False),
    "rebook": ("Come-back reminders", "Nudges a customer when their usual time between orders has passed.", False),
    "vip": ("Regulars", "Thanks customers with many orders and tells them what that means.", False),
}
AUTOMATED_NOTES = ("Recorded by Kauli", "Podcast episode")       # orders Kauli placed itself, not something a customer is waiting on


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------ settings
def enabled(key: str) -> bool:
    default = AUTOMATIONS[key][2]
    raw = db.get_flag(f"lifecycle_{key}", None)
    return default if raw is None else raw == "1"


def trustpilot_url() -> str:
    url = (db.get_flag("trustpilot_review_url", "") or os.environ.get("TRUSTPILOT_REVIEW_URL", "") or "").strip()
    return url if _trustpilot_ok(url) else DEFAULT_TRUSTPILOT


def _trustpilot_ok(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "").lower() in ("trustpilot.com", "www.trustpilot.com") and len(url) < 300


def _base() -> str:
    return (os.environ.get("KAULI_PUBLIC_BASE_URL", "") or "https://kauli-forgemedia.com").rstrip("/")


# ------------------------------------------------------------------------------------------------ the record of what was sent
def _record(user_id: str, kind: str, ref: str = "") -> bool:
    """True when this is the first time: the message is recorded before it is sent, so it can never go twice."""
    conn = db.get_conn()
    cur = conn.execute("INSERT OR IGNORE INTO lifecycle_messages (id, user_id, kind, ref, sent_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, user_id, kind, ref, time.time()))
    conn.commit()
    conn.close()
    return bool(cur.rowcount)


def last_sent(user_id: str, kind: str) -> float:
    conn = db.get_conn()
    row = conn.execute("SELECT MAX(sent_at) FROM lifecycle_messages WHERE user_id = ? AND kind = ?", (user_id, kind)).fetchone()
    conn.close()
    return row[0] or 0


def _unsubscribed(u) -> bool:
    return bool(u["marketing_consent_at"] and not u["marketing_consent"])


def _name(u) -> str:
    return (u["display_name"] or u["email"].split("@")[0]).strip()


def _email(purpose: str, u, subject: str, lines: list[str], cta: str, link: str, unsubscribe: bool, extra_html: str = "") -> bool:
    if not mailer.email_configured():
        return False
    base = _base()
    name = _name(u)
    text = f"Hi {name},\n\n" + "\n\n".join(lines) + f"\n\n{link}\n\nKauli Team"
    inner = (f'<p style="margin:0 0 14px;">Hi {html.escape(name)},</p>' + "".join(f'<p style="margin:0 0 14px;">{html.escape(x)}</p>' for x in lines)
             + extra_html + '<p style="margin:0;">Kauli Team</p>')
    footer = None
    if unsubscribe:
        unsub = f"{base}/unsubscribe/{u['id']}"
        text += f"\n\nNo more of these emails: {unsub}"
        footer = f'<a href="{unsub}">Stop these emails</a>'
    try:
        ok, _detail = mailer.send_as(purpose, u["email"], subject, mailer.wrap_email_html(inner, cta_text=cta, cta_url=link, footer_note=footer, base_url=base), text, tags=[f"lifecycle-{purpose}"])
        return bool(ok)
    except Exception:  # noqa: BLE001 - a message must never break whatever triggered it
        return False


# ------------------------------------------------------------------------------------------------ 1. we have your order
def _automated(order) -> bool:
    notes = (order["instr_notes"] or "") if "instr_notes" in order.keys() else ""
    return notes.startswith(AUTOMATED_NOTES)


def order_received(order) -> bool:
    """Called as work on a customer's order starts."""
    if not enabled("confirm") or _automated(order):
        return False
    client = db.get_user(order["client_id"])
    if not client or client["role"] != "client":
        return False
    if not _record(client["id"], "order_received", order["id"]):
        return False
    human = (order["review_tier"] or "ai") != "ai"
    when = ("A person reviews every human-reviewed order, so it takes a little longer. The expected delivery time is on your order page, and we email you again the moment it is ready."
            if human else "Instant AI orders usually finish within minutes, and we email you again the moment yours is ready.")
    return _email("orders", client, f"We have your order: {order['original_filename']}", [f"Thanks. We have started work on {order['original_filename']}.", when],
                  "Follow your order", f"{_base()}/client/orders/{order['id']}", unsubscribe=False)


# ------------------------------------------------------------------------------------------------ 2. how did it go
def _delivered_at(o) -> float:
    return o["status_changed_at"] or o["updated_at"] or o["created_at"] or 0


def _delivered_orders(conn, since: float | None = None):
    rows = conn.execute("SELECT * FROM orders WHERE status IN ('ready_for_delivery', 'delivered') ORDER BY created_at").fetchall()
    return [r for r in rows if not _automated(r) and (since is None or _delivered_at(r) >= since)]


def review_candidates(now: float | None = None) -> list[dict]:
    """Customers whose latest delivery is old enough to ask about and recent enough to remember: chosen by timing alone."""
    now = now or time.time()
    conn = db.get_conn()
    try:
        owners = {u["id"]: u for u in site_analytics._owners(conn)}
        latest: dict[str, object] = {}
        for o in _delivered_orders(conn):
            latest[o["client_id"]] = o                                  # orders are in date order, so the last one wins
        asked = {r["user_id"]: r["m"] for r in conn.execute("SELECT user_id, MAX(sent_at) AS m FROM lifecycle_messages WHERE kind = 'review_request' GROUP BY user_id").fetchall()}
    finally:
        conn.close()
    out = []
    for uid, o in latest.items():
        u = owners.get(uid)
        if not u or _unsubscribed(u):
            continue
        age = now - _delivered_at(o)
        if age < REVIEW_AFTER_DAYS * DAY or age > REVIEW_WINDOW_DAYS * DAY or now - asked.get(uid, 0) < REVIEW_REPEAT_DAYS * DAY:
            continue
        out.append({"user": u, "order": o, "asked": asked.get(uid, 0)})
    out.sort(key=lambda c: _delivered_at(c["order"]))
    return out


def all_review_choices(now: float | None = None) -> list[dict]:
    """For the staff to pick from by hand: every customer with a delivery, newest first, with when they were last asked."""
    now = now or time.time()
    conn = db.get_conn()
    try:
        owners = {u["id"]: u for u in site_analytics._owners(conn)}
        latest: dict[str, object] = {}
        for o in _delivered_orders(conn):
            latest[o["client_id"]] = o
        asked = {r["user_id"]: r["m"] for r in conn.execute("SELECT user_id, MAX(sent_at) AS m FROM lifecycle_messages WHERE kind = 'review_request' GROUP BY user_id").fetchall()}
    finally:
        conn.close()
    rows = [{"user": owners[uid], "order": o, "asked": asked.get(uid, 0)} for uid, o in latest.items() if uid in owners]
    rows.sort(key=lambda c: _delivered_at(c["order"]), reverse=True)
    return rows[:200]


def send_review_request(u, order, force: bool = False) -> bool:
    """One review request. Skips someone who unsubscribed, or who was asked within the repeat window (unless force)."""
    if _unsubscribed(u) or (not force and time.time() - last_sent(u["id"], "review_request") < REVIEW_REPEAT_DAYS * DAY):
        return False
    if not _record(u["id"], "review_request", order["id"]):
        return False
    base = _base()
    private = (f'<p style="margin:0 0 14px;">If something was not right, please tell us first, '
               f'<a href="{base}/help" style="color:{mailer.BRAND_ACCENT};">here</a>, and we will put it right. '
               f'You are always welcome to leave your review as well.</p>')
    return _email("success", u, "How did we do?", [f"We delivered {order['original_filename']} for you a few days ago, and we would love to know how it went.",
                                                  "If you have a minute, an honest review on Trustpilot helps other people decide whether Kauli is right for them, and tells us what to improve."],
                  "Review Kauli on Trustpilot", trustpilot_url(), unsubscribe=True, extra_html=private)


# ------------------------------------------------------------------------------------------------ 3. time to come back
def rebook_gap_days(order_times: list[float]) -> int:
    """The customer's own rhythm: the typical gap between their orders, kept between two weeks and three months (a month for one order)."""
    times = sorted(order_times)
    gaps = [(b - a) / DAY for a, b in zip(times, times[1:]) if b - a > 0.5 * DAY]
    gap = statistics.median(gaps) if gaps else REBOOK_DEFAULT_GAP_DAYS
    return int(min(REBOOK_MAX_GAP_DAYS, max(REBOOK_MIN_GAP_DAYS, round(gap))))


def rebook_candidates(now: float | None = None) -> list[dict]:
    now = now or time.time()
    conn = db.get_conn()
    try:
        owners = {u["id"]: u for u in site_analytics._owners(conn)}
        per: dict[str, list] = {}
        for o in _delivered_orders(conn):
            per.setdefault(o["client_id"], []).append(o)
        busy = {r["client_id"] for r in conn.execute("SELECT DISTINCT client_id FROM orders WHERE status IN ('processing', 'queued', 'pending_payment', 'awaiting_review')").fetchall()}
        sent = {r["user_id"]: r["m"] for r in conn.execute("SELECT user_id, MAX(sent_at) AS m FROM lifecycle_messages WHERE kind = 'rebook' GROUP BY user_id").fetchall()}
    finally:
        conn.close()
    out = []
    for uid, orders in per.items():
        u = owners.get(uid)
        if not u or _unsubscribed(u) or uid in busy:
            continue
        last = max(o["created_at"] for o in orders)
        gap = rebook_gap_days([o["created_at"] for o in orders])
        quiet = (now - last) / DAY
        if gap <= quiet <= REBOOK_STALE_DAYS and now - sent.get(uid, 0) >= REBOOK_REPEAT_DAYS * DAY and sent.get(uid, 0) < last:
            out.append({"user": u, "gap": gap, "quiet": int(quiet), "orders": len(orders)})
    out.sort(key=lambda c: -c["quiet"])
    return out


def send_rebook(c: dict) -> bool:
    u = c["user"]
    if not _record(u["id"], "rebook", str(int(time.time() // DAY))):
        return False
    weeks = max(1, round(c["quiet"] / 7))
    return _email("success", u, "Got another recording?", [f"It has been about {weeks} week{'s' if weeks != 1 else ''} since your last project on Kauli, which is around when you usually come back.",
                                                           "If you have a new recording, you can upload it and have the transcript back in minutes. If something got in the way last time, just reply and tell us."],
                  "Start a new order", f"{_base()}/client", unsubscribe=True)


# ------------------------------------------------------------------------------------------------ 4. regulars
def vip_candidates(now: float | None = None) -> list[dict]:
    now = now or time.time()
    conn = db.get_conn()
    try:
        owners = {u["id"]: u for u in site_analytics._owners(conn)}
        counts: dict[str, int] = {}
        for o in _delivered_orders(conn, now - VIP_WINDOW_DAYS * DAY):
            counts[o["client_id"]] = counts.get(o["client_id"], 0) + 1
        already = {r["user_id"] for r in conn.execute("SELECT user_id FROM lifecycle_messages WHERE kind = 'vip'").fetchall()}
    finally:
        conn.close()
    return sorted(({"user": owners[uid], "orders": n} for uid, n in counts.items() if n >= VIP_ORDERS and uid in owners and uid not in already and not _unsubscribed(owners[uid])),
                  key=lambda c: -c["orders"])


def vip_members() -> list:
    conn = db.get_conn()
    rows = conn.execute("SELECT u.id, u.email, u.display_name, m.sent_at FROM lifecycle_messages m JOIN users u ON u.id = m.user_id WHERE m.kind = 'vip' ORDER BY m.sent_at DESC").fetchall()
    conn.close()
    return rows


def send_vip(c: dict) -> bool:
    u = c["user"]
    if not _record(u["id"], "vip", ""):
        return False
    return _email("success", u, "You are one of our regulars", [f"You have had {c['orders']} projects delivered by Kauli in the last few months, and we noticed. Thank you.",
                                                               "From now on your messages to us are read first, and we will tell you about new things before we tell everyone else.",
                                                               "If there is something you wish Kauli did, reply to this email. It comes straight to a person on the team."],
                  "Open my account", f"{_base()}/client/home", unsubscribe=True)


# ------------------------------------------------------------------------------------------------ the sweep
def sweep_once() -> dict:
    sent = {"review": 0, "rebook": 0, "vip": 0}
    if enabled("review"):
        for c in review_candidates()[:MAX_SENDS_PER_SWEEP]:
            sent["review"] += 1 if send_review_request(c["user"], c["order"]) else 0
    if enabled("rebook"):
        for c in rebook_candidates()[:MAX_SENDS_PER_SWEEP]:
            sent["rebook"] += 1 if send_rebook(c) else 0
    if enabled("vip"):
        for c in vip_candidates()[:MAX_SENDS_PER_SWEEP]:
            sent["vip"] += 1 if send_vip(c) else 0
    return sent


# ------------------------------------------------------------------------------------------------ staff
def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


def _back(kind: str, text: str):
    return RedirectResponse(f"/staff/lifecycle?{kind}={quote(text)}", status_code=303)


@router.get("/staff/lifecycle", response_class=HTMLResponse)
def staff_page(request: Request):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    conn = db.get_conn()
    log = conn.execute("SELECT m.kind, m.sent_at, u.email FROM lifecycle_messages m LEFT JOIN users u ON u.id = m.user_id ORDER BY m.sent_at DESC LIMIT 40").fetchall()
    conn.close()
    return A.templates.TemplateResponse(request, "staff_lifecycle.html", {
        "user": user, "theme": "dark", "automations": [{"key": k, "title": v[0], "text": v[1], "on": enabled(k)} for k, v in AUTOMATIONS.items()],
        "trustpilot": trustpilot_url(), "trustpilot_default": trustpilot_url() == DEFAULT_TRUSTPILOT, "senders": mailer.SENDERS,
        "review_due": review_candidates(), "choices": all_review_choices(), "rebook_due": rebook_candidates(), "vip_due": vip_candidates(), "vips": vip_members(),
        "log": log, "limits": {"after": REVIEW_AFTER_DAYS, "window": REVIEW_WINDOW_DAYS, "repeat": REVIEW_REPEAT_DAYS, "vip_orders": VIP_ORDERS, "vip_days": VIP_WINDOW_DAYS},
        "email_ready": mailer.email_configured(), "notice": request.query_params.get("notice"), "error": request.query_params.get("error")})


@router.post("/staff/lifecycle/switch")
def staff_switch(request: Request, key: str = Form(""), on: str = Form("0")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    if key not in AUTOMATIONS:
        return _back("error", "Unknown automation.")
    db.set_flag(f"lifecycle_{key}", "1" if on == "1" else "0", by=user["id"])
    return _back("notice", f"{AUTOMATIONS[key][0]} are now {'on' if on == '1' else 'off'}.")


@router.post("/staff/lifecycle/trustpilot")
def staff_trustpilot(request: Request, url: str = Form("")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    url = url.strip()
    if url and not _trustpilot_ok(url):
        return _back("error", "That is not a Trustpilot address. It should start with https://www.trustpilot.com/")
    db.set_flag("trustpilot_review_url", url, by=user["id"])
    return _back("notice", "Review link saved." if url else "Back to the standard review link.")


@router.post("/staff/lifecycle/test")
def staff_test(request: Request, kind: str = Form("review")):
    """Send one sample to the staff member's own address, so they see exactly what a customer would."""
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    me = dict(user)
    me["marketing_consent_at"], me["marketing_consent"] = None, 1
    me["display_name"] = me.get("display_name") or "there"
    sample = {"filename": "Sample recording.mp3", "id": "sample"}
    if kind == "review":
        ok = _email("success", me, "How did we do?", [f"We delivered {sample['filename']} for you a few days ago, and we would love to know how it went.",
                                                     "If you have a minute, an honest review on Trustpilot helps other people decide whether Kauli is right for them, and tells us what to improve."],
                    "Review Kauli on Trustpilot", trustpilot_url(), unsubscribe=True,
                    extra_html=f'<p style="margin:0 0 14px;">If something was not right, please tell us first, <a href="{_base()}/help">here</a>, and we will put it right. You are always welcome to leave your review as well.</p>')
    elif kind == "rebook":
        ok = _email("success", me, "Got another recording?", ["It has been about 4 weeks since your last project on Kauli, which is around when you usually come back.",
                                                              "If you have a new recording, you can upload it and have the transcript back in minutes."], "Start a new order", f"{_base()}/client", unsubscribe=True)
    elif kind == "vip":
        ok = _email("success", me, "You are one of our regulars", ["You have had 5 projects delivered by Kauli in the last few months, and we noticed. Thank you.",
                                                                  "From now on your messages to us are read first, and we will tell you about new things before we tell everyone else."], "Open my account", f"{_base()}/client/home", unsubscribe=True)
    else:
        ok = _email("orders", me, f"We have your order: {sample['filename']}", [f"Thanks. We have started work on {sample['filename']}.", "Instant AI orders usually finish within minutes, and we email you again the moment yours is ready."],
                    "Follow your order", f"{_base()}/client", unsubscribe=False)
    return _back("notice" if ok else "error", f"A sample was sent to {user['email']}." if ok else "The sample could not be sent. Check that email is set up.")


@router.post("/staff/lifecycle/review-selected")
async def staff_review_selected(request: Request):
    """Send the review request to the customers staff ticked."""
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    ids = [v for v in form.getlist("customer") if isinstance(v, str)][:50]
    if not ids:
        return _back("error", "Tick at least one customer.")
    chosen = {c["user"]["id"]: c for c in all_review_choices()}
    sent = skipped = 0
    for uid in ids:
        c = chosen.get(uid)
        if c and send_review_request(c["user"], c["order"]):
            sent += 1
        else:
            skipped += 1
    return _back("notice", f"Sent {sent} review request(s)." + (f" {skipped} skipped: unsubscribed, asked in the last {REVIEW_REPEAT_DAYS} days, or not found." if skipped else ""))


@router.post("/staff/lifecycle/send-due")
def staff_send_due(request: Request, kind: str = Form("review")):
    """Send now what the automatic version would send on its next run, without turning the automation on."""
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    n = 0
    if kind == "review":
        n = sum(1 for c in review_candidates()[:MAX_SENDS_PER_SWEEP] if send_review_request(c["user"], c["order"]))
    elif kind == "rebook":
        n = sum(1 for c in rebook_candidates()[:MAX_SENDS_PER_SWEEP] if send_rebook(c))
    elif kind == "vip":
        n = sum(1 for c in vip_candidates()[:MAX_SENDS_PER_SWEEP] if send_vip(c))
    else:
        return _back("error", "Unknown message.")
    return _back("notice", f"Sent {n} message(s).")
