"""Sign-up funnel audit: who signs up, how far they get, where they stop, and what staff can do to help.

Behind the "Analyse sign-ups" button on /staff/customers. It only READS the database (nothing is sent, changed or emailed), and every number
and every suggestion is worked out from the live data each time, so the page is never out of date.

    analyse()  ->  {
        "totals", "funnel", "recent", "stuck" (one diagnosis per client who is not yet a paying customer), "friction",
        "traffic", "suggestions" (prioritised, each with the evidence behind it), "caveats"
    }

Who is counted: real client accounts. Staff, team members, guest trial accounts and anything marked internal or test are left out, the same
rule the Clients list and Site analytics use, so the audit never mixes the team's own test orders into the story.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from urllib.parse import quote

from . import billing, db

READY = {"ready_for_delivery", "returned_to_client", "delivered", "completed"}
UNPAID = {"pending_payment"}
STAGES = ("signed_up", "opened_wizard", "uploaded", "ordered", "result_ready", "paid")
STAGE_LABEL = {
    "signed_up": "Signed up", "opened_wizard": "Opened the order form", "uploaded": "Uploaded a file", "ordered": "Placed an order",
    "result_ready": "Got a result", "paid": "Paid",
}
DAY = 86400


def _ts(v) -> float | None:
    """created_at columns hold either epoch seconds or an ISO string, depending on the table's age."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "")).timestamp()
        except ValueError:
            return None


def _days(ts: float | None, now: float) -> int | None:
    return None if ts is None else max(0, int((now - ts) // DAY))


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _real_clients(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, email, display_name, created_at, last_seen_at, account_status, onboarding_status
           FROM users
           WHERE role = 'client' AND COALESCE(is_internal, 0) = 0 AND COALESCE(is_guest, 0) = 0
             AND id NOT IN (SELECT member_user_id FROM team_members WHERE status = 'accepted' AND member_user_id IS NOT NULL)
           ORDER BY created_at DESC""").fetchall()
    return [dict(r) for r in rows]


def _client_facts(conn, uid: str, email: str, now: float) -> dict:
    """Everything one client has done, in one place."""
    wizard = {r[0] for r in conn.execute("SELECT DISTINCT step FROM wizard_step_events WHERE client_id = ?", (uid,))}
    wizard_last = conn.execute("SELECT MAX(created_at) FROM wizard_step_events WHERE client_id = ?", (uid,)).fetchone()[0]
    up_ok = conn.execute("SELECT COUNT(*) FROM upload_audit_log WHERE user_id = ? AND COALESCE(rejected, 0) = 0", (uid,)).fetchone()[0]
    up_last = conn.execute("SELECT MAX(created_at) FROM upload_audit_log WHERE user_id = ? AND COALESCE(rejected, 0) = 0", (uid,)).fetchone()[0]
    up_bad = [r[0] for r in conn.execute("SELECT reject_reason FROM upload_audit_log WHERE user_id = ? AND rejected = 1", (uid,))]
    orders = [dict(r) for r in conn.execute(
        "SELECT id, original_filename, status, service_level, duration_minutes, cost_usd, created_at, error FROM orders WHERE client_id = ? ORDER BY created_at", (uid,))]
    pays = [dict(r) for r in conn.execute(
        "SELECT status, amount_usd, provider, plan, order_id, created_at, meta FROM payments WHERE user_id = ? ORDER BY created_at", (uid,))]
    msgs = [dict(r) for r in conn.execute(
        "SELECT kind, status, sent_at, created_at, email_send_detail FROM onboarding_messages WHERE user_id = ? ORDER BY created_at", (uid,))]
    logins = conn.execute("SELECT COUNT(*) FROM security_events WHERE kind = 'login_success' AND user_id = ?", (uid,)).fetchone()[0]
    unconfirmed = conn.execute(
        "SELECT COUNT(*) FROM security_events WHERE kind = 'login_failed' AND lower(email) = lower(?) AND detail LIKE '%not confirmed%'", (email or "",)).fetchone()[0]
    signup = conn.execute("SELECT detail FROM security_events WHERE kind = 'signup' AND user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()
    return {"wizard": wizard, "wizard_last": _ts(wizard_last), "uploads_ok": up_ok, "upload_last": _ts(up_last), "uploads_bad": up_bad, "orders": orders, "payments": pays,
            "messages": msgs, "logins": logins, "unconfirmed": unconfirmed, "signup_method": (signup[0] if signup else None)}


def _stage_flags(f: dict) -> dict:
    paid = any(p["status"] == "completed" and (p.get("plan") != "trial_verification") and (p["amount_usd"] or 0) > 0.0 for p in f["payments"])
    verified = any(p["status"] == "completed" and p.get("plan") == "trial_verification" for p in f["payments"])
    orders = f["orders"]
    return {
        "signed_up": True,
        "opened_wizard": bool(f["wizard"]) or bool(orders),
        "uploaded": f["uploads_ok"] > 0 or bool(orders),
        "ordered": bool(orders),
        "result_ready": any(o["status"] in READY for o in orders),
        "paid": paid or verified,
    }


def _first_name(c: dict) -> str:
    """A real first name when the account has one ("Amina Otieno", "Amina"), otherwise "there": many display names are handles such as
    "iambrilliant2125", and "Hi Iambrilliant2125" would read badly."""
    name = (c.get("display_name") or "").strip()
    parts = name.split()
    if len(parts) >= 2 and parts[0].isalpha():
        return parts[0].title()
    if len(parts) == 1 and parts[0][:1].isupper() and parts[0].isalpha() and parts[0][1:].islower():
        return parts[0]
    return "there"


def _fmt_day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %B").lstrip("0") if ts else "recently"


SERVICE_WORDS = {"transcribe": "transcription", "translate": "transcription and translation", "dub": "dubbing"}


def _diagnose(c: dict, f: dict, flags: dict, now: float, base: str = "") -> dict | None:
    """Why this client has not become a paying customer, what a person at Kauli can do, and a ready-to-send email written from what they actually did
    (their file, their order, their dates). None when they already pay. The email is a DRAFT: staff read it, change it if they like, and press send."""
    if flags["paid"]:
        return None
    first = _first_name(c)
    age = _days(_ts(c["created_at"]), now)
    orders = f["orders"]
    unpaid = [o for o in orders if o["status"] in UNPAID]
    failed_pay = [p for p in f["payments"] if p["status"] == "failed"]
    pending_trial = [p for p in f["payments"] if p.get("plan") == "trial_verification" and p["status"] == "pending"]
    account = f"{base}/account" if base else "/account"
    free = f"{billing.FREE_MINUTES_PER_MONTH:.0f}"
    cta = "Open your Kauli account"
    if f["uploads_ok"] > 0 and not orders:
        code, sev = "lost_upload", "high"
        when = _fmt_day(f.get("upload_last"))
        why = "Uploaded a file, but no order was ever created. That looks like a technical stop on our side, not a lack of interest."
        do = ("Look in the server log around the time of the upload, then send the email: it apologises, and offers to place the order for them.")
        subject = "Your file on Kauli: we can help finish it"
        body = (f"Hi {first},\n\nYou uploaded a recording to Kauli on {when}, but we can't see an order created from it, so it never got processed. "
                "That's on our side, and we're sorry.\n\n"
                "The quickest fix: reply with the language spoken in the recording and what you need (a transcript, subtitles, a translation), "
                f"and we'll set it up for you and tell you when it's ready. Your {free} free minutes are still there. "
                f"Or you can upload it again here: {account}\n\nKauli Team")
    elif unpaid:
        code, sev = "waiting_to_pay", "high" if any((o["cost_usd"] or 0) >= 5 for o in unpaid) else "medium"
        total = round(sum(o["cost_usd"] or 0 for o in unpaid), 2)
        oldest = max((_days(_ts(o["created_at"]), now) or 0) for o in unpaid)
        latest = unpaid[-1]
        why = f"Placed {len(unpaid)} order(s) worth ${total:.2f} that are waiting for payment (the oldest {oldest} day(s) ago)." + (
            f" {len(failed_pay)} payment attempt(s) failed." if failed_pay else "")
        do = ("Send the email: it names their order, shows the price, and offers M-Pesa or bank transfer if card payment is the problem. Do not offer discounts.")
        link = f"{base}/account/orders/{latest['id']}" if base else f"/account/orders/{latest['id']}"
        what = (latest.get("original_filename") or "your recording")
        service = SERVICE_WORDS.get(latest.get("service_level") or "", "your order")
        mins = latest.get("duration_minutes") or 0
        one = (f"Your recording \"{what}\" is uploaded and waiting. Pay ${latest['cost_usd'] or 0:.2f} and we'll start it right away:"
               if len(unpaid) == 1 else
               f"Your {len(unpaid)} orders are uploaded and waiting, ${total:.2f} in all. Pay and we'll start them right away:")
        subject = "Your Kauli order is ready to start"
        body = (f"Hi {first},\n\n{one}\n{link}\n\n"
                "Card trouble? Reply and we'll help you pay by M-Pesa or bank transfer.\n\nKauli Team")
        cta = "Open your order"
        account = link
    elif failed_pay and not orders:
        code, sev = "payment_failed", "medium"
        why = f"{len(failed_pay)} payment attempt(s) failed and there is no order."
        do = "Send the email and offer another way to pay."
        subject = "Your payment on Kauli didn't go through"
        body = (f"Hi {first},\n\nWe saw that your payment on Kauli didn't go through. These things happen, and we can help you finish it another way, "
                f"such as M-Pesa or a bank transfer. Just reply to this email.\n\nYou can also try again here: {account}\n\nKauli Team")
    elif pending_trial and not orders:
        code, sev = "verification_abandoned", "medium"
        why = "Started the one-time $1 unlock and did not finish it."
        do = "Send the email. It explains what the $1 is, and offers help."
        subject = "Finishing setting up your Kauli account"
        body = (f"Hi {first},\n\nYou started setting up your Kauli account and the last step, the one-time $1, didn't finish. That $1 isn't a fee: "
                f"it goes into your own wallet and is used for your orders. If anything about it was unclear, reply and we'll help.\n\n"
                f"Pick up where you left off: {account}\n\nKauli Team")
    elif orders and flags["result_ready"]:
        code, sev = "free_result_no_payment", "low"
        first_order = next((o for o in orders if o["status"] in READY), orders[0])
        what = first_order.get("original_filename") or "your first file"
        why = "Got a free result and has not come back to pay or order again."
        do = "Send the email: it asks for feedback on the result and what they would need next. A short, personal note works better than another automatic one."
        subject = "How did your first Kauli result look?"
        body = (f"Hi {first},\n\nYour first Kauli result, for \"{what}\", has been sitting in your account since {_fmt_day(_ts(first_order['created_at']))}. "
                "We'd love to know how it looked: was it what you expected, and was anything wrong?\n\n"
                "Everything you see there is free to read. If you'd like to download or edit it, the price for that order is shown on the order page, "
                f"and if there's something you need that we don't offer yet, tell us.\n\nYour account: {account}\n\nKauli Team")
    elif f["unconfirmed"] and not f["logins"]:
        code, sev = "email_not_confirmed", "high"
        why = "Tried to sign in with an email address that was never confirmed."
        do = "Send the email, and resend the confirmation, or ask them to sign in with Google."
        subject = "Getting into your Kauli account"
        body = (f"Hi {first},\n\nWe saw you tried to sign in to Kauli, but your email address hadn't been confirmed yet. Check your inbox (and spam) for the "
                f"confirmation email, or choose \"Continue with Google\" on the sign-in page, which skips that step: {account}\n\n"
                "If it still doesn't work, reply here and we'll sort it out for you.\n\nKauli Team")
    elif flags["opened_wizard"]:
        code, sev = "started_no_upload", "medium"
        why = "Opened the order form but never uploaded a file."
        do = "Send the email. It asks what they wanted to transcribe, and offers to run a first file."
        subject = "Your first file on Kauli: shall we start it for you?"
        body = (f"Hi {first},\n\nYou started an order on Kauli but didn't get as far as uploading a file. Was something unclear or not working?\n\n"
                f"Your first {free} minutes are free. If you tell us what you'd like to transcribe or translate, and in which language, we'll help you run it. "
                f"Or try it yourself: {account}\n\nKauli Team")
    else:
        code, sev = "never_started", "medium" if (age or 0) <= 30 else "low"
        why = f"Signed up {age} day(s) ago and never opened the order form."
        do = "Send the email. It gives them one clear first step."
        subject = "Your free minutes on Kauli"
        body = (f"Hi {first},\n\nThanks for signing up to Kauli. The quickest way to see what it does is to upload one short recording: your first {free} minutes "
                f"are free, and you can read the result before deciding anything.\n\nStart here: {account}\n\n"
                "If you're not sure what to try, tell us what you do, and we'll suggest a first file.\n\nKauli Team")
    nudges = [m for m in f["messages"] if m["kind"] == "inactivity_nudge"]
    followups = [m for m in f["messages"] if m["kind"] == "staff_followup"]
    last_fu = max((m.get("sent_at") or m.get("created_at") or 0 for m in followups), default=None)
    return {"id": c["id"], "name": c["display_name"] or c["email"], "email": c["email"], "code": code, "severity": sev, "why": why, "do": do,
            "signup_days": age, "stage": max((i for i, s in enumerate(STAGES) if flags[s]), default=0), "orders": len(orders), "unpaid": len(unpaid),
            "nudges": len(nudges), "followups": len(followups), "last_followup": _fmt_day(last_fu) if last_fu else "", "signup_method": f["signup_method"],
            "subject": subject, "body": body, "cta": cta, "cta_url": account,
            "mailto": f"mailto:{c['email']}?subject={quote(subject)}&body={quote(body)}" if c["email"] else ""}


def _traffic(conn, now: float, signups_in_window: int) -> dict:
    first = conn.execute("SELECT MIN(day), MAX(day) FROM site_hits").fetchone()
    visitors = conn.execute("SELECT COUNT(DISTINCT visitor) FROM site_hits").fetchone()[0]
    if not visitors:
        return {"visitors": 0}
    login_v = conn.execute("SELECT COUNT(DISTINCT visitor) FROM site_hits WHERE path LIKE '/login%'").fetchone()[0]
    try_v = conn.execute("SELECT COUNT(DISTINCT visitor) FROM site_hits WHERE path LIKE '/try%'").fetchone()[0]
    channels = conn.execute("SELECT COALESCE(NULLIF(channel, ''), 'Unknown'), COUNT(DISTINCT visitor) FROM site_hits GROUP BY 1 ORDER BY 2 DESC LIMIT 6").fetchall()
    countries = conn.execute("SELECT COALESCE(country, 'Unknown'), COUNT(DISTINCT visitor) FROM site_hits GROUP BY 1 ORDER BY 2 DESC LIMIT 6").fetchall()
    mobile = conn.execute("SELECT COUNT(DISTINCT visitor) FROM site_hits WHERE device = 'Mobile'").fetchone()[0]
    direct = next((n for ch, n in channels if ch == "Direct"), 0)
    return {"visitors": visitors, "from": first[0], "to": first[1], "login_visitors": login_v, "try_visitors": try_v,
            "signups": signups_in_window, "visit_to_signup_pct": _pct(signups_in_window, visitors), "login_to_signup_pct": _pct(signups_in_window, login_v),
            "channels": [(a, b) for a, b in channels], "countries": [(a, b) for a, b in countries], "mobile_pct": _pct(mobile, visitors),
            "direct_pct": _pct(direct, visitors)}


def analyse(base_url: str = "") -> dict:
    """base_url is the public address of the site (no trailing slash), used for the links inside the draft emails."""
    base = base_url.rstrip("/")
    now = time.time()
    conn = db.get_conn()
    try:
        clients = _real_clients(conn)
        facts, flags_by, stuck = {}, {}, []
        for c in clients:
            f = _client_facts(conn, c["id"], c["email"], now)
            facts[c["id"]] = f
            flags_by[c["id"]] = _stage_flags(f)
            d = _diagnose(c, f, flags_by[c["id"]], now, base)
            if d:
                stuck.append(d)
        funnel = [{"key": s, "label": STAGE_LABEL[s], "count": sum(1 for fl in flags_by.values() if fl[s])} for s in STAGES]
        for i, row in enumerate(funnel):
            row["pct_of_signups"] = _pct(row["count"], len(clients))
            row["lost_from_previous"] = (funnel[i - 1]["count"] - row["count"]) if i else 0
            row["previous_label"] = funnel[i - 1]["label"] if i else ""
            row["previous_count"] = funnel[i - 1]["count"] if i else 0
        biggest = max(funnel[1:], key=lambda r: r["lost_from_previous"]) if len(funnel) > 1 and clients else None

        # the last 60 days, to see whether things are getting better or worse
        cutoff = now - 60 * DAY
        recent_ids = [c["id"] for c in clients if (_ts(c["created_at"]) or 0) >= cutoff]
        recent = {"signups": len(recent_ids), "never_ordered": sum(1 for u in recent_ids if not flags_by[u]["ordered"]),
                  "paid": sum(1 for u in recent_ids if flags_by[u]["paid"])}

        # everyone with an order waiting for payment, including clients who have paid before
        unpaid_clients = []
        for c in clients:
            un = [o for o in facts[c["id"]]["orders"] if o["status"] in UNPAID]
            if un:
                unpaid_clients.append({"id": c["id"], "name": c["display_name"] or c["email"], "orders": len(un),
                                       "value": round(sum(o["cost_usd"] or 0 for o in un), 2),
                                       "newest_days": min((_days(_ts(o["created_at"]), now) or 0) for o in un)})

        # friction signals
        unpaid_orders = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(o.cost_usd), 0), MIN(o.created_at) FROM orders o JOIN users u ON u.id = o.client_id
               WHERE o.status = 'pending_payment' AND u.role = 'client' AND COALESCE(u.is_internal, 0) = 0 AND COALESCE(u.is_guest, 0) = 0""").fetchone()
        pay_rows = conn.execute("SELECT provider, status, COUNT(*) FROM payments GROUP BY 1, 2").fetchall()
        rejects = Counter(r[0] for r in conn.execute("SELECT reject_reason FROM upload_audit_log WHERE rejected = 1"))
        unconfirmed_rows = conn.execute(
            "SELECT lower(email), MAX(created_at) FROM security_events WHERE kind = 'login_failed' AND detail LIKE '%not confirmed%' GROUP BY 1").fetchall()
        known = {(c["email"] or "").lower(): c for c in clients}
        unconfirmed = [{"email": e, "has_account": e in known, "last": _ts(t)} for e, t in unconfirmed_rows if e]
        nudges = conn.execute("SELECT COUNT(*) FROM onboarding_messages WHERE kind = 'inactivity_nudge'").fetchone()[0]
        nudge_users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM onboarding_messages WHERE kind = 'inactivity_nudge'")]
        nudge_then_ordered = sum(1 for u in nudge_users if u in flags_by and flags_by[u]["ordered"])
        nudge_stuck = conn.execute("SELECT COUNT(*) FROM onboarding_messages WHERE status NOT IN ('sent') AND email_send_detail IS NOT NULL").fetchone()[0]
        methods = Counter((f["signup_method"] or "unknown") for f in facts.values())
        leads_new = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'new'").fetchone()[0]
        leads_research = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'new' AND source = 'research'").fetchone()[0]
        trial_started = conn.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE plan = 'trial_verification'").fetchone()[0]
        trial_done = conn.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE plan = 'trial_verification' AND status = 'completed'").fetchone()[0]
        mpesa_failed = sum(n for p, s, n in pay_rows if p == "mpesa" and s == "failed")
        mpesa_all = sum(n for p, s, n in pay_rows if p == "mpesa")
        window_start = conn.execute("SELECT MIN(day) FROM site_hits").fetchone()[0]
        signups_window = sum(1 for c in clients if window_start and (_ts(c["created_at"]) or 0) >= (datetime.fromisoformat(window_start).timestamp() if window_start else 0))
        traffic = _traffic(conn, now, signups_window)
    finally:
        conn.close()

    friction = {
        "unpaid_orders": unpaid_orders[0], "unpaid_value": round(unpaid_orders[1], 2), "unpaid_oldest_days": _days(_ts(unpaid_orders[2]), now),
        "unpaid_clients": unpaid_clients,
        "uploads_rejected": dict(rejects), "unconfirmed": unconfirmed, "nudges_sent": nudges, "nudge_users": len(nudge_users), "nudge_then_ordered": nudge_then_ordered,
        "messages_not_delivered": nudge_stuck, "signup_methods": dict(methods), "leads_new": leads_new, "leads_research": leads_research,
        "trial_started": trial_started, "trial_done": trial_done, "mpesa_failed": mpesa_failed, "mpesa_all": mpesa_all,
    }
    stuck.sort(key=lambda d: ({"high": 0, "medium": 1, "low": 2}[d["severity"]], d["signup_days"] if d["signup_days"] is not None else 9999))
    result = {"generated_at": now, "totals": {"clients": len(clients), "stuck": len(stuck), "paying": len(clients) - len(stuck)}, "funnel": funnel,
              "biggest_drop": biggest, "recent": recent, "stuck": stuck, "friction": friction, "traffic": traffic}
    result["drafts"] = {d["id"]: {"name": d["name"], "email": d["email"], "code": d["code"], "subject": d["subject"], "body": d["body"], "cta": d["cta"],
                                  "last_followup": d["last_followup"]} for d in stuck}
    result["suggestions"] = _suggestions(result)
    result["caveats"] = _caveats(result)
    return result


def _suggestions(r: dict) -> list[dict]:
    """Prioritised things to do, each with the numbers that justify it."""
    out: list[dict] = []
    t, fr, funnel, stuck, rec = r["totals"], r["friction"], {f["key"]: f for f in r["funnel"]}, r["stuck"], r["recent"]
    by_code: dict[str, list[dict]] = {}
    for d in stuck:
        by_code.setdefault(d["code"], []).append(d)

    def add(priority, title, why, do, clients=None):
        out.append({"priority": priority, "title": title, "why": why, "do": do, "clients": clients or []})

    lost = by_code.get("lost_upload", [])
    if lost:
        add("high", f"Fix and follow up: {len(lost)} client(s) uploaded a file but no order exists",
            "These people did the hard part (signed up, opened the form, uploaded) and then something stopped the order from being created. "
            "That is the most expensive kind of drop, because it is not their choice.",
            "Check the server log and the upload records for these accounts, find why the final step failed, and fix it. Then write to each one personally "
            "and offer to place the order for them. Consider a visible 'we could not save your order, try again' message if none was shown.",
            [{"id": d["id"], "name": d["name"]} for d in lost])
    unconf = [u for u in fr["unconfirmed"]]
    if unconf:
        n_out = sum(1 for u in unconf if not u["has_account"])
        add("high", f"{len(unconf)} person(s) hit 'Email not confirmed' when signing in" + (f", {n_out} with no account at all" if n_out else ""),
            "Signing up with an email and password needs the address confirmed first. People who never confirm are lost without a trace, and are not "
            "in the clients list.",
            "Send them the confirmation again or invite them to use Google sign-in. In the product, show a 'resend confirmation email' button on that error, "
            "and put 'Continue with Google' first on the sign-up page.")
    uc = fr["unpaid_clients"]
    if fr["unpaid_orders"]:
        fresh = [u for u in uc if u["newest_days"] <= 30]
        add("high" if fresh else "medium", f"{fr['unpaid_orders']} order(s) worth ${fr['unpaid_value']:,.2f} are waiting for payment, from {len(uc)} client(s)",
            f"The oldest has waited {fr['unpaid_oldest_days']} day(s) and {len(fresh)} client(s) have placed one in the last 30 days. People placed the order and "
            "then stopped before paying. Customers who have already chosen what they want are the easiest to win back.",
            "Write to each client who is waiting, ask what stopped them (the price, the payment method, or not being sure about the result), and offer to help them "
            "pay by M-Pesa or bank transfer. Do not use discounts. Add a one-click 'Pay now' link to the order page and to the reminder email if it is not there.",
            [{"id": u["id"], "name": f"{u['name']} (${u['value']:,.2f})"} for u in uc])
    if t["clients"] >= 5 and (rec["signups"] and rec["never_ordered"] / rec["signups"] >= 0.5):
        add("high", f"{rec['never_ordered']} of the last {rec['signups']} sign-ups (60 days) never placed an order",
            f"Only {funnel['ordered']['count']} of {t['clients']} sign-ups ever placed an order ({funnel['ordered']['pct_of_signups']}%). The biggest drop is "
            f"'{r['biggest_drop']['label']}'." if r["biggest_drop"] else "Most sign-ups never place an order.",
            "Make the first minute easier: after sign-up, take the person straight to one big 'Upload a short file' step, show a sample result they can open "
            "without uploading anything, and say clearly that the first minutes are free. Reach out personally within 24 hours of every sign-up while the "
            "numbers are small: a short human email beats an automated one.")
    if fr["nudges_sent"] and not fr["nudge_then_ordered"]:
        add("medium", f"The automatic 'we noticed you have not started' emails are not working ({fr['nudges_sent']} sent, 0 led to an order)",
            f"{fr['nudge_users']} different people received one and none of them has placed an order since.",
            "Rewrite it as a short note from a real person with one concrete step ('send us one 5-minute file, we will run it'), and for East Africa try WhatsApp "
            "or a call instead of email. Measure again after 10 sign-ups.")
    if fr["messages_not_delivered"]:
        add("medium", f"{fr['messages_not_delivered']} onboarding email(s) were not delivered",
            "At least one automatic email is stuck because the email provider timed out.", "Check the email provider's status and re-send; make sure the job retries.")
    if fr["trial_started"] and fr["trial_started"] > fr["trial_done"]:
        add("medium", f"The $1 unlock step: {fr['trial_started']} started, {fr['trial_done']} finished",
            "Asking for a card payment before someone has seen value is a common place to lose people, especially next to 'no credit card required' on the site.",
            "Show what the $1 is for (it goes into their own wallet and is used for their orders), show the result first, and keep the wording consistent with the "
            "rest of the site. Review whether the unlock is needed before the first result.")
    if fr["mpesa_all"] and fr["mpesa_failed"] / fr["mpesa_all"] >= 0.5:
        add("medium", f"{fr['mpesa_failed']} of {fr['mpesa_all']} M-Pesa payments failed",
            "Mobile-money failures hit the customers we most want in Kenya.", "Test the M-Pesa flow end to end with a real phone, and show the reason when a payment fails.")
    tr = r["traffic"]
    if tr.get("visitors"):
        add("low" if tr["visit_to_signup_pct"] >= 1 else "medium", f"Website: {tr['visitors']:,} visitors, {tr['signups']} sign-up(s) ({tr['visit_to_signup_pct']}%)",
            f"{tr['login_visitors']} visitors reached the sign-in page and {tr['signups']} signed up ({tr['login_to_signup_pct']}%). "
            f"{tr['direct_pct']}% of visitors have no referrer, which usually means robots or untagged links, so the real rate is probably higher.",
            "Tag every link you share (utm_source) so real visitors can be told apart. Put a clear sign-up or try-it button in the first screen of each service page. "
            "Review which countries actually send sign-ups, not just visits.")
    if fr["leads_new"] >= 20:
        add("low", f"{fr['leads_new']} prospects in the CRM have not been contacted",
            f"{fr['leads_research']} came from research. They are people who may need Kauli and have heard nothing from us.",
            "Pick the 10 best-fitting leads each week and send a short personal message with an invitation to try one file.")
    stuck_never = by_code.get("never_started", []) + by_code.get("started_no_upload", [])
    if stuck_never:
        add("medium", f"{len(stuck_never)} client(s) signed up and never uploaded anything",
            "They were interested enough to sign up, and we have an email address for each of them.",
            "Send the personal welcome in the list below, one at a time. Ask what they wanted to transcribe, and offer to run the first file.",
            [{"id": d["id"], "name": d["name"]} for d in stuck_never])
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda s: rank[s["priority"]])
    return out


def _caveats(r: dict) -> list[str]:
    n = r["totals"]["clients"]
    caveats = [f"The sample is small ({n} real client accounts), so one person changes a percentage a lot. Read the percentages as a direction, not a verdict."]
    tr = r["traffic"]
    if tr.get("visitors"):
        caveats.append(f"Website visits are only recorded from {tr['from']}, so the traffic numbers cover about {max(1, (datetime.fromisoformat(tr['to']) - datetime.fromisoformat(tr['from'])).days + 1)} days.")
        if tr["direct_pct"] >= 80:
            caveats.append("Most visitors have no referrer and some countries look like data centers (for example Singapore), so part of the traffic is likely robots.")
    caveats.append("'Paid' means a confirmed payment from the client. A free result that was never paid for is counted as 'Got a result' only.")
    caveats.append("Team, test, guest and internal accounts are left out. Nothing here is sent to anyone: the 'Write to them' links only open your own email with a draft.")
    return caveats
