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

# Same contact details app.py's onboarding messages already use -
# duplicated here rather than imported (app.py imports this module, so
# the reverse would be circular), not second real values to keep in sync
# by hand.
CONTACT_PHONE_WHATSAPP = "254712531841"
CONTACT_EMAIL = "kahunyurogodfrey@gmail.com"
FOUNDER_NAME = "Godfrey Njoroge"


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
        + ("" if link else "Log in to your Kauli dashboard to view and download it.\n\n")
        + f"Questions? Reply here or WhatsApp me: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
        + f"Talk soon,\n{FOUNDER_NAME}\n(Forge Media Services)"
    )
    log_in_note = "" if link else '<p style="margin:0 0 14px;">Log in to your Kauli dashboard to view and download it.</p>'
    inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">Your order ({order["original_filename"]}) is ready.</p>'
        f'{log_in_note}'
        f'<p style="margin:0 0 14px;">Questions? '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">Reply</a> here or message me on '
        f'<a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a>.</p>'
        f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
    )
    html = mailer.wrap_email_html(inner, cta_text="View & download your files" if link else None,
                                   cta_url=link, base_url=link_base)
    mailer.send_email(client["email"], "Your Kauli order is ready", html, body)
    db.create_notification(client["id"], "order_ready", f"{order['original_filename']} is ready",
                            link=f"/client/orders/{order['id']}")


def notify_staff_needs_review(order) -> None:
    notifications.notify_staff(
        "Kauli: an order needs review",
        f"Order {order['id']} ({order['original_filename']}) finished processing and is waiting "
        f"in the review queue before it can go to the client.\n\nReview on /staff/jobs.",
    )
    db.notify_all_staff("needs_review", f"{order['original_filename']} is ready for review",
                         link=f"/staff/orders/{order['id']}")


def notify_staff_dead_letter(order, error: str) -> None:
    notifications.notify_staff(
        "Kauli: an order failed and needs attention",
        f"Order {order['id']} ({order['original_filename']}) failed after {MAX_RETRIES} retries "
        f"and is now dead-lettered - it will NOT retry again on its own.\n\n"
        f"Error: {error}\n\nSee /staff/orders/{order['id']} to retry or investigate.",
    )
    db.notify_all_staff("dead_letter", f"{order['original_filename']} failed and needs attention",
                         link=f"/staff/orders/{order['id']}")


def _run_job(order_id: str, is_retry: bool = False) -> None:
    order = db.get_order(order_id)
    if not order:
        log.warning("job started for missing order", extra={"job_id": order_id})
        return
    db.update_order_status(order_id, "processing")
    log.info("job started", extra={"job_id": order_id, "user_id": order["client_id"], "is_retry": is_retry})
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
            # Real cost bug this fixes: a retry used to always re-run ASR/MT
            # from scratch even when the FAILED attempt's own manifest
            # already had a real, correct transcript and translation sitting
            # on disk - re-billing a paid ASR provider (Transkriptor, per
            # minute) for the whole file again just because a LATER stage
            # (TTS/mix) is what actually failed. resume=True only on a
            # retry (never the original attempt) tells kauli.pipeline.run
            # to reuse whatever real work is already in that manifest,
            # per segment - see that function's own docstring.
            resume=is_retry,
        )
        manifest_path = Path(order["outdir"]) / "manifest.json"
        job = Job.load(str(manifest_path))
        # Every order waits for an explicit staff sign-off in Ereri
        # ("Approve & mark ready for delivery") before a client ever sees
        # it as ready - no auto-bypass, even when the AI drafted it with
        # zero flagged segments. This used to skip straight to
        # ready_for_delivery when flagged_count was 0, which meant some
        # orders reached the client without a real human ever looking at
        # them - directly contradicting "every order human-reviewed"
        # everywhere that's said (the FAQ, the blog, the marketing site).
        # flagged_count/content_safety_flagged still matter for how the
        # order is presented in the review queue, just not for whether
        # review happens at all.
        final_status = "awaiting_review"
        db.update_order_status(order_id, final_status)
        db.set_order_ai_cost(order_id, job.cost_usd)
        log.info("job finished", extra={"job_id": order_id, "user_id": order["client_id"],
                                         "final_status": final_status, "flagged_count": job.flagged_count,
                                         "ai_cost_usd": job.cost_usd})
        order = db.get_order(order_id)  # re-fetch: status/ai_cost above are now current
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
            _run_job(order_id, is_retry=True)
            return
        log.error("job exhausted retries, dead-lettering", extra={
            "job_id": order_id, "user_id": order["client_id"],
            "attempts": retry_count, "error_message": str(exc),
        }, exc_info=True)
        db.update_order_status(order_id, "dead_letter", error=str(exc))
        notify_staff_dead_letter(db.get_order(order_id), str(exc))


def submit_job(order_id: str, resume: bool = False) -> None:
    """Fire-and-forget: kicks off processing on a daemon thread.

    resume: same real-money reason as _run_job's automatic retry path -
    staff manually hitting Retry on a stuck/dead-lettered order (the
    /staff/orders/{id}/retry route) is exactly the same situation as an
    automatic retry (a previous attempt's real ASR/MT work may already be
    sitting in the manifest), just human-triggered instead of exception-
    triggered. Default False because a genuinely NEW order (client just
    uploaded, first submit_job call ever for this id) has no prior
    manifest to resume from - True is only correct for a real re-attempt."""
    t = threading.Thread(target=_run_job, args=(order_id,), kwargs={"is_retry": resume}, daemon=True)
    t.start()
