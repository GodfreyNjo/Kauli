"""Runs the real kauli pipeline in a background thread and syncs order status.

Deliberately reuses kauli.pipeline.run() unchanged rather than reimplementing
anything - this webapp is a UI on top of the existing engine, not a parallel
pipeline. One job at a time (this laptop, no GPU) is intentional; a real
task queue (Celery/RQ/Redis BullMQ/SQS) is explicitly a "not yet" per the
roadmap's tech stack section - that needs real infrastructure this local
prototype doesn't have. What's still worth having without it: bounded
retries for a transient-looking failure, and a dead-letter state instead of
either silently giving up after one try or retrying a genuinely broken file
forever and clogging the queue - the same protection a real message queue's
DLQ gives you, built on SQLite + threading instead of Redis/SQS.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kauli.pipeline import run as kauli_run  # noqa: E402
from kauli.models import Job  # noqa: E402
from . import db, logging_setup, mailer, notifications  # noqa: E402

log = logging_setup.get_logger("worker")

MAX_RETRIES = 2
RETRY_BACKOFF_S = (5, 20)  # delay before retry attempt 1, then attempt 2

# Only set when there's a real public URL to link to (see .env.example) -
# this module runs on a background thread with no HTTP request to build
# one from, unlike everywhere else in the app that derives it from
# request.base_url. Left unset (the honest default for "still a local
# prototype"), the client email below just doesn't include a link and
# says to log in instead - never a fabricated/broken URL.
PUBLIC_BASE_URL = os.environ.get("KAULI_PUBLIC_BASE_URL", "").rstrip("/")

# Same number app.py's onboarding messages already link to - duplicated
# here rather than imported (app.py imports this module, so the reverse
# would be circular), not a second real number to keep in sync by hand.
CONTACT_PHONE_WHATSAPP = "254712531841"


def notify_client_order_ready(order, base_url: str | None = None) -> None:
    """Real email the moment an order actually becomes ready - called both
    from here (fully automatic, no flags needed) and from app.py's
    staff_approve (a human cleared it out of review). base_url lets a
    caller with a real request build a working link; falls back to
    PUBLIC_BASE_URL, then to no link at all."""
    if not mailer.email_configured():
        return
    client = db.get_user(order["client_id"])
    if not client:
        return
    link_base = (base_url or PUBLIC_BASE_URL or "").rstrip("/")
    link = f"{link_base}/client/orders/{order['id']}" if link_base else None
    name = (client["display_name"] or client["email"].split("@")[0]).strip()
    body = (
        f"Hi {name},\n\n"
        f"Your order ({order['original_filename']}) is ready.\n\n"
        + (f"View and download it here: {link}\n\n" if link
           else "Log in to your Kauli dashboard to view and download it.\n\n")
        + f"Questions? Reply here or WhatsApp me: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
        + "Forge Media Services"
    )
    html = "".join(f"<p>{part}</p>" for part in body.split("\n\n") if part.strip())
    mailer.send_email(client["email"], "Your Kauli order is ready", html, body)


def notify_staff_needs_review(order) -> None:
    notifications.notify_staff(
        "Kauli: an order needs review",
        f"Order {order['id']} ({order['original_filename']}) finished processing and is waiting "
        f"in the review queue before it can go to the client.\n\nReview on /staff.",
    )


def notify_staff_dead_letter(order, error: str) -> None:
    notifications.notify_staff(
        "Kauli: an order failed and needs attention",
        f"Order {order['id']} ({order['original_filename']}) failed after {MAX_RETRIES} retries "
        f"and is now dead-lettered - it will NOT retry again on its own.\n\n"
        f"Error: {error}\n\nSee /staff/orders/{order['id']} to retry or investigate.",
    )


def _run_job(order_id: str) -> None:
    order = db.get_order(order_id)
    if not order:
        log.warning("job started for missing order", extra={"job_id": order_id})
        return
    db.update_order_status(order_id, "processing")
    log.info("job started", extra={"job_id": order_id, "user_id": order["client_id"]})
    try:
        kauli_run(
            audio_path=order["audio_path"],
            outdir=order["outdir"],
            asr=order["asr"],
            mt=order["mt"],
            tts=order["tts"],
            source_lang=order["source_lang"],
            target_lang=order["target_lang"],
            verbose=False,
        )
        manifest_path = Path(order["outdir"]) / "manifest.json"
        job = Job.load(str(manifest_path))
        # No flags at all -> nothing for staff to look at, goes straight to
        # ready for delivery. Otherwise it waits in the staff review queue -
        # content_safety_flagged (see upload_security.py) forces this even
        # when every segment individually looks fine, since that's a
        # different kind of signal than transcript/translation confidence
        # and needs a human's eyes on the actual video regardless.
        final_status = ("ready_for_delivery"
                         if job.flagged_count == 0 and not order["content_safety_flagged"]
                         else "awaiting_review")
        db.update_order_status(order_id, final_status)
        db.set_order_ai_cost(order_id, job.cost_usd)
        log.info("job finished", extra={"job_id": order_id, "user_id": order["client_id"],
                                         "final_status": final_status, "flagged_count": job.flagged_count,
                                         "ai_cost_usd": job.cost_usd})
        order = db.get_order(order_id)  # re-fetch: status/ai_cost above are now current
        if final_status == "ready_for_delivery":
            notify_client_order_ready(order)
        else:
            notify_staff_needs_review(order)
    except Exception as exc:  # noqa: BLE001 - surface it to the client/staff UI
        retry_count = db.increment_retry_count(order_id)
        if retry_count <= MAX_RETRIES:
            delay = RETRY_BACKOFF_S[min(retry_count - 1, len(RETRY_BACKOFF_S) - 1)]
            log.warning("job failed, retrying", extra={
                "job_id": order_id, "user_id": order["client_id"],
                "attempt": retry_count, "max_retries": MAX_RETRIES,
                "retry_in_s": delay, "error_message": str(exc),
            })
            # Same thread, same daemon job - no new infra to schedule a
            # delayed retry, just sleep-then-retry inline. Fine at this
            # scale (one job at a time); a real queue's scheduled-retry
            # feature is the thing to reach for once that's justified.
            time.sleep(delay)
            _run_job(order_id)
            return
        log.error("job exhausted retries, dead-lettering", extra={
            "job_id": order_id, "user_id": order["client_id"],
            "attempts": retry_count, "error_message": str(exc),
        }, exc_info=True)
        db.update_order_status(order_id, "dead_letter", error=str(exc))
        notify_staff_dead_letter(db.get_order(order_id), str(exc))


def submit_job(order_id: str) -> None:
    """Fire-and-forget: kicks off processing on a daemon thread."""
    t = threading.Thread(target=_run_job, args=(order_id,), daemon=True)
    t.start()
