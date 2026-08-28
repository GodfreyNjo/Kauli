"""Shared staff-notification helper - used by both app.py (order
approvals, exception requests, new leads) and worker.py (background job
outcomes: ready for review, dead-lettered), which can't import from each
other (app.py imports worker.py at module load, so the reverse would be
circular). KAULI_STAFF_EMAILS is the same list db.init_db() already uses
to promote accounts to admin on startup - one real, already-configured
list, not a second one to keep in sync by hand.
"""
from __future__ import annotations

import os

from . import mailer


def notify_staff(subject: str, body: str, sender_name: str = "Kauli Operations") -> None:
    """Silently does nothing if the mailer isn't configured or no staff
    emails are set - the triggering event always still shows up in the
    relevant staff page regardless (queue, CRM, exceptions); this is a
    bonus heads-up, never the only way staff would find out.

    sender_name: same real sending address either way (see mailer.
    send_email) - just a different display name so a growth-side alert
    ("Kauli Marketing" - a new lead, a new signup) reads as visibly
    different in an inbox from an ops alert (a dead-lettered job, an
    exception request) at a glance, without a second inbox to check."""
    if not mailer.email_configured():
        return
    staff_emails = {e.strip() for e in os.environ.get("KAULI_STAFF_EMAILS", "").split(",") if e.strip()}
    html = "".join(f"<p>{part}</p>" for part in body.split("\n\n") if part.strip())
    for email in staff_emails:
        mailer.send_email(email, subject, html, body, sender_name=sender_name)
