"""Helping every customer, new or long-standing, get more out of Kauli: the account monitoring, pointed at ALL clients.

Each customer account is read from what it actually did (orders, payments, visits, last seen) and put in one state:

  never_started  signed up a few days ago or more and has not uploaded anything
  drifting       used Kauli, has not paid, and has gone quiet for a couple of weeks or more
  underusing     active, but only ever uses one feature
  at_risk_paid   pays, and has slowed down (the win-back takes over after three weeks of silence)

Two things act on that:
  - a "next step" card inside the account, always on, so it costs nobody an email;
  - gentle emails, sent when staff press the button on /staff/engagement, or on their own once KAULI_ENGAGE_AUTOSEND is switched on.
Guardrails: one nudge at most every 14 days per person, three of each kind at most ever, never to anyone who unsubscribed, closed their account,
is on a team they do not own, has a failing payment, or is the win-back's to handle. No percentage offers: only help and a reason to come back.
"""
from __future__ import annotations

import html
import os
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import billing, db, mailer, onboarding, site_analytics

router = APIRouter()
A = None

DAY = 86400
MIN_GAP_DAYS = 14
MAX_PER_KIND = 3
MAX_SENDS_PER_SWEEP = 25
STATES = {
    "never_started": ("Signed up, never uploaded", "Their free minutes are unused."),
    "drifting": ("Used Kauli, then went quiet", "Not paying yet, quiet for 14+ days."),
    "underusing": ("Active, but one feature only", "Could get a lot more from translation, summaries or dubbing."),
    "at_risk_paid": ("Pays, and has slowed down", "A personal check-in before they leave."),
}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------------------- reading accounts
def customers(now: float | None = None) -> list[dict]:
    """Every account owner with the facts the states are made from."""
    now = now or time.time()
    today = int(now // DAY)
    conn = db.get_conn()
    try:
        owners = site_analytics._owners(conn)
        act = site_analytics.user_activity(conn, now - 180 * DAY)
        orders = {r["u"]: r for r in conn.execute(
            "SELECT client_id AS u, COUNT(*) AS n, COUNT(DISTINCT service_level) AS services, "
            "SUM(CASE WHEN service_level IN ('translate','dub') THEN 1 ELSE 0 END) AS tr, SUM(CASE WHEN ai_summary_text IS NOT NULL THEN 1 ELSE 0 END) AS summ, "
            "SUM(CASE WHEN service_level = 'dub' THEN 1 ELSE 0 END) AS dubs FROM orders GROUP BY client_id").fetchall()}
        subs = {r["user_id"]: r for r in conn.execute("SELECT * FROM subscriptions").fetchall()}
        failing = {r["user_id"] for r in conn.execute("SELECT user_id FROM payment_failures WHERE resolved_at IS NULL").fetchall()}
        paid_ever = {r["u"] for r in conn.execute("SELECT DISTINCT user_id AS u FROM payments WHERE status = 'completed' AND amount_usd > 0").fetchall()}
        nudges = {}
        for r in conn.execute("SELECT user_id, kind, COUNT(*) AS n, MAX(sent_at) AS last FROM engagement_messages GROUP BY user_id, kind").fetchall():
            nudges.setdefault(r["user_id"], {})[r["kind"]] = (r["n"], r["last"])
        winback = {r["user_id"] for r in conn.execute("SELECT user_id FROM winback_offers").fetchall()}
        onboarding_nudged = {r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM onboarding_messages WHERE kind = 'inactivity_nudge'").fetchall()} \
            if _has_table(conn, "onboarding_messages") else set()
    finally:
        conn.close()
    out = []
    for u in owners:
        uid = u["id"]
        days = act.get(uid, set())
        last = max(days) if days else None
        quiet = (today - last) if last is not None else None
        o = orders.get(uid)
        sub = subs.get(uid)
        paying = bool(sub and sub["plan"] in billing.PLANS and sub["plan"] != "free" and (sub["current_period_end"] or 0) > now - 3 * DAY)
        joined_days = int((now - (u["created_at"] or now)) // DAY)
        used = {"transcribe"} if o and o["n"] else set()
        if o and (o["tr"] or 0):
            used.add("translate")
        if o and (o["summ"] or 0):
            used.add("summary")
        if o and (o["dubs"] or 0):
            used.add("dub")
        state = None
        if uid in failing:
            state = None                                             # the failed-payment flow is already talking to them
        elif paying and quiet is not None and 10 <= quiet < 21:
            state = "at_risk_paid"
        elif not (o and o["n"]) and 3 <= joined_days <= 180 and uid not in paid_ever:
            state = "never_started"
        elif o and o["n"] and uid not in paid_ever and quiet is not None and 14 <= quiet <= 90 and uid not in winback:
            state = "drifting"
        elif o and o["n"] and (o["n"] or 0) >= 2 and len(used) == 1 and quiet is not None and quiet < 14:
            state = "underusing"
        out.append({"user": u, "id": uid, "state": state, "quiet": quiet, "orders": (o["n"] if o else 0), "paying": paying, "used": used,
                    "joined_days": joined_days, "nudges": nudges.get(uid, {}), "onboarding_nudged": uid in onboarding_nudged})
    return out


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def can_nudge(c: dict, now: float | None = None) -> bool:
    now = now or time.time()
    if not c["state"]:
        return False
    u = c["user"]
    if u["marketing_consent_at"] and not u["marketing_consent"]:
        return False                                                  # unsubscribed
    if c["state"] == "never_started" and c["onboarding_nudged"]:
        return False                                                  # the first-days reminder already went to them
    last_any = max([v[1] for v in c["nudges"].values()] + [0])
    if last_any and now - last_any < MIN_GAP_DAYS * DAY:
        return False
    return (c["nudges"].get(c["state"], (0, 0))[0]) < MAX_PER_KIND


def suggestion(c: dict) -> tuple[str, str, str]:
    """(what to try, the sentence, where it is) for someone who only uses one thing."""
    if "translate" not in c["used"]:
        return ("Translate", "Turn a transcript into another language: Instant AI translation covers 76+ languages, and it comes with your free minutes on Pro and Premium.", "/client#new-project")
    if "summary" not in c["used"]:
        return ("Summarise", "Open a finished order and press Summarise to get the key points, action items or a blog outline in a click.", "/client/home")
    return ("Dub", "Hear a video spoken in another language with an AI voice, with captions burned in.", "/client#new-project")


# ---------------------------------------------------------------------------------------------------------- the in-app card
def next_step(user_id: str, now: float | None = None) -> dict | None:
    """What this customer could do next, for the card on their home page. None when they are doing fine."""
    c = next((x for x in customers(now) if x["id"] == user_id), None)
    if not c or not c["state"]:
        return None
    free = billing.FREE_MINUTES_PER_MONTH
    if c["state"] == "never_started":
        return {"state": c["state"], "title": "Your first file is free", "text": f"You have {free:.0f} Instant AI minutes every month. Upload a recording and get a transcript back in minutes.",
                "cta": "Upload a file", "url": "/client#new-project"}
    if c["state"] == "drifting":
        return {"state": c["state"], "title": "Pick up where you left off", "text": "Your free minutes renew every month, so there is nothing to catch up on. Upload something new, or reopen a finished order.",
                "cta": "Start a project", "url": "/client#new-project"}
    if c["state"] == "underusing":
        what, text, url = suggestion(c)
        return {"state": c["state"], "title": f"Get more from your files: {what.lower()} them", "text": text, "cta": f"Try {what.lower()}", "url": url}
    return {"state": c["state"], "title": "How is Kauli working for you?", "text": "It has been quiet for a little while. If something is in the way, tell us and we will sort it out.",
            "cta": "Message us", "url": "/help"}


# ---------------------------------------------------------------------------------------------------------------- the emails
def _email(c: dict, base_url: str) -> tuple[str, list[str], str, str]:
    free = billing.FREE_MINUTES_PER_MONTH
    if c["state"] == "never_started":
        return ("Your free minutes are waiting", [f"You signed up for Kauli but have not uploaded anything yet. Your first {free:.0f} minutes of Instant AI transcription are free every month.",
                                                  "Upload a recording and you get a transcript back in minutes, ready to download as Word, text or subtitles."], "Upload your first file", f"{base_url}/client")
    if c["state"] == "drifting":
        return ("Pick up where you left off", ["It has been a little while since your last project on Kauli.",
                                               f"Your free minutes renew every month, so there is nothing to catch up on: upload a recording and carry on. If something got in the way last time, just reply and tell us."],
                "Start a project", f"{base_url}/client")
    if c["state"] == "underusing":
        what, text, url = suggestion(c)
        return (f"Get more from Kauli: {what.lower()}", ["You have been getting transcripts from Kauli, and there is more in it than that.", text], f"Try {what.lower()}", base_url + url)
    return ("How is Kauli working for you?", ["We noticed things have gone a bit quiet on your account, and we would like to know whether Kauli is doing what you need.",
                                             "If something is missing or in the way, reply to this email and tell us. A real person reads every reply."], "Open my account", f"{base_url}/client/home")


def send_nudge(c: dict, base_url: str) -> bool:
    """Record it first (so it is never sent twice if the email fails), then email it."""
    base_url = base_url.rstrip("/")
    u = c["user"]
    conn = db.get_conn()
    conn.execute("INSERT INTO engagement_messages (id, user_id, kind, sent_at) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, u["id"], c["state"], time.time()))
    conn.commit()
    conn.close()
    subject, lines, cta, link = _email(c, base_url)
    name = (u["display_name"] or u["email"].split("@")[0]).strip()
    unsub = f"{base_url}/unsubscribe/{u['id']}"
    text = f"Hi {name},\n\n" + "\n\n".join(lines) + f"\n\n{link}\n\nTalk soon,\nKauli Team\n\nNo more of these emails: {unsub}"
    inner = f'<p style="margin:0 0 14px;">Hi {html.escape(name)},</p>' + "".join(f'<p style="margin:0 0 14px;">{html.escape(x)}</p>' for x in lines) + '<p style="margin:0;">Talk soon,<br>Kauli Team</p>'
    try:
        if mailer.email_configured():
            mailer.send_email(u["email"], subject, mailer.wrap_email_html(inner, cta_text=cta, cta_url=link, footer_note=f'<a href="{unsub}">Stop these emails</a>', base_url=base_url), text)
    except Exception:  # noqa: BLE001
        pass
    return True


def due(now: float | None = None) -> list[dict]:
    return [c for c in customers(now) if can_nudge(c, now)]


def send_all(base_url: str, limit: int = MAX_SENDS_PER_SWEEP) -> int:
    n = 0
    for c in due()[:limit]:
        if send_nudge(c, base_url):
            n += 1
    return n


def autosend_enabled() -> bool:
    return os.environ.get("KAULI_ENGAGE_AUTOSEND", "").strip().lower() in ("1", "true", "yes", "on")


def sweep_once(base_url: str) -> dict:
    return {"sent": send_all(base_url) if autosend_enabled() else 0}


def summary(now: float | None = None) -> dict:
    cs = customers(now)
    counts = {k: sum(1 for c in cs if c["state"] == k) for k in STATES}
    return {"counts": counts, "total": len(cs), "healthy": sum(1 for c in cs if not c["state"]), "due": sum(1 for c in cs if can_nudge(c, now)), "rows": cs}


# ------------------------------------------------------------------------------------------------------------------- pages
def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


@router.get("/staff/engagement", response_class=HTMLResponse)
def staff_engagement(request: Request, notice: str | None = None):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    s = summary()
    rows = sorted((c for c in s["rows"] if c["state"]), key=lambda c: (not can_nudge(c), -(c["quiet"] or 0)))[:100]
    return A.templates.TemplateResponse(request, "staff_engagement.html", {
        "stuck": onboarding.first_orders_needing_help(), "csat": onboarding.csat_summary(), "ttfv": onboarding.ttfv_minutes(),
        "user": user, "s": s, "rows": rows, "states": STATES, "notice": notice, "autosend": autosend_enabled(), "mail_ready": mailer.email_configured(),
        "can": can_nudge, "gap": MIN_GAP_DAYS, "when": lambda ts: datetime.fromtimestamp(ts).strftime("%b %d") if ts else ""})


@router.post("/staff/engagement/send")
def staff_engagement_send(request: Request, user_id: str = Form("")):
    if not _staff(request):
        return RedirectResponse("/login", status_code=303)
    base = A._site_base(request)
    if user_id:
        c = next((x for x in customers() if x["id"] == user_id), None)
        sent = 1 if c and can_nudge(c) and send_nudge(c, base) else 0
    else:
        sent = send_all(base)
    return RedirectResponse(f"/staff/engagement?notice={sent}+nudge{'' if sent == 1 else 's'}+sent", status_code=303)
