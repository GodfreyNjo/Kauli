"""When a meeting Kauli attended has become a transcript, write its notes (summary, decisions, action items) without anyone asking, and, if
the customer switched it on for their calendar, email those notes to everyone who was invited.

The customer already gets the usual "your order is ready" email and bell; connected tools (Slack, Google Docs, Notion and the rest) are
sent after the notes exist, so they include the summary.
"""
from __future__ import annotations

import html
import json
import logging
import time

from . import billing, connections, db, mailer, order_ai_assist, summary_templates

log = logging.getLogger("meeting_notes")
A = None
TEMPLATE = "general-meeting"
MAX_RECIPIENTS = 30


def register(app_module) -> None:
    global A
    A = app_module


def _meeting_of(order):
    m = db.get_meeting_by_order(order["id"])
    if not m:                                                            # the order can finish a moment before its meeting row is updated
        time.sleep(3)
        m = db.get_meeting_by_order(order["id"])
    return m


def write_notes(order) -> None:
    """Make the meeting summary once, in the General Meeting layout, and keep it on the order."""
    if order["ai_summary_text"]:
        return
    job = A._load_job(order)
    level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    result = order_ai_assist.generate_templated_summary(A._transcript_text_for_order(job, level), summary_templates.get(TEMPLATE))
    if result.get("ok"):
        db.save_order_summary(order["id"], TEMPLATE, result["summary"])
        db.set_order_ai_summary(order["id"], order_ai_assist.plain_summary(result["summary"]))


def email_attendees(order, meeting) -> int:
    """Send the notes to the people on the invite, only when the customer switched that on. Returns how many were emailed."""
    ev = db.calendar_event_for_meeting(meeting["id"])
    if not ev or not ev["attendees"]:
        return 0
    provider = (ev["event_id"] or "").split(":", 1)[0]
    row = connections.get_connection(meeting["client_id"], provider) if provider in connections.PROVIDERS else None
    if not row or not connections.settings_of(row).get("tell_attendees"):
        return 0
    notes = order["ai_summary_text"] or ""
    if not notes:
        return 0
    owner = db.get_user(meeting["client_id"])
    title = meeting["title"] or "your meeting"
    body = (f"Kauli Notetaker attended \"{title}\" at the request of {owner['email'] if owner else 'a participant'}. Here are the notes.\n\n"
            f"{notes}\n\nThe Kauli Team")
    try:
        people = json.loads(ev["attendees"])
    except ValueError:
        people = []
    sent = 0
    for email in people[:MAX_RECIPIENTS]:
        ok, _detail = mailer.send_email(email, f"Notes from {title}", mailer.wrap_email_html(mailer.text_to_html_paragraphs(html.escape(body, quote=False))), body,
                                        tags=["meeting-notes"], sender_name="Kauli Team")
        sent += 1 if ok else 0
    return sent


def prepare(order):
    """Called as an Instant AI order finishes. Does nothing for orders that did not come from a meeting. Returns the order as it is now."""
    try:
        meeting = _meeting_of(order)
        if not meeting:
            return order
        write_notes(order)
        order = db.get_order(order["id"]) or order
        email_attendees(order, meeting)
    except Exception:  # noqa: BLE001 - notes are a bonus; they must never hold up delivering the transcript
        log.warning("meeting notes failed for %s", order["id"], exc_info=True)
    return db.get_order(order["id"]) or order
