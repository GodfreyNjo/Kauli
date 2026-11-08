"""Diagnostics: find out quickly what went wrong, and where.

Every unexpected failure gets a reference number (the request's trace id, which is also in the response header
`X-Trace-Id` and in every log line of that request). The failure is written to the `app_errors` table with where it
happened, so a developer can start from the number a customer quotes and land on the exact line, instead of searching
raw logs.

What lives here:
  record_error           store one failure (called by the global error handler)
  recent_groups          the failures of the last days, grouped so one bug shows as one row with a count
  find_error             one failure by its reference number, with its stack trace
  configuration_checks   which outside services are switched on, as yes/no (never the secrets themselves)
  init_sentry            optional: also send failures to Sentry when SENTRY_DSN is set and the sdk is installed

Nothing here may ever break a request: if recording an error fails, that is logged and swallowed.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import traceback

from . import db, logging_setup

log = logging_setup.get_logger("diagnostics")

MAX_KEPT_ERRORS = 2000
MAX_MESSAGE_CHARS = 300
MAX_STACK_CHARS = 4000
# tests are included so the diagnostics themselves can be tested
OUR_CODE_MARKERS = ("/webapp/", "/kauli/", "/tests/")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{28,}")

_TABLE = """
CREATE TABLE IF NOT EXISTS app_errors (
    id TEXT PRIMARY KEY,          -- the reference number shown to the customer (the request's trace id)
    created_at REAL NOT NULL,
    fingerprint TEXT NOT NULL,    -- groups repeats of the same bug
    error_type TEXT NOT NULL,
    message TEXT,
    where_raised TEXT,
    method TEXT,
    path TEXT,
    user_id TEXT,
    stack TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_errors_fingerprint ON app_errors (fingerprint, created_at);
"""


def _connection():
    """A database connection with the app_errors table guaranteed to exist (it is created on first use)."""
    conn = db.get_conn()
    conn.executescript(_TABLE)
    return conn


def scrub(text: str) -> str:
    """Remove what must not sit in an error log: email addresses and anything that looks like a key or token."""
    return _LONG_TOKEN.sub("[token]", _EMAIL.sub("[email]", text or ""))


def where_raised(error: BaseException) -> str:
    """The last place in our own code the error passed through, as 'webapp/app.py:123 in create_order'."""
    frames = traceback.extract_tb(error.__traceback__)
    ours = [f for f in frames if any(marker in f.filename.replace("\\", "/") for marker in OUR_CODE_MARKERS)]
    frame = (ours or frames or [None])[-1]
    if frame is None:
        return "unknown"
    short_name = frame.filename.replace("\\", "/").split("/app/")[-1]
    return f"{short_name}:{frame.lineno} in {frame.name}"


def fingerprint(error_type: str, where: str) -> str:
    """The same failure at the same place always has the same fingerprint, so repeats group into one row."""
    return hashlib.sha1(f"{error_type}|{where}".encode()).hexdigest()[:12]


def record_error(reference: str, method: str, path: str, user_id: str | None, error: BaseException) -> None:
    """Store one failure under its reference number. Never raises."""
    try:
        place = where_raised(error)
        stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        row = (reference, time.time(), fingerprint(type(error).__name__, place), type(error).__name__,
               scrub(str(error))[:MAX_MESSAGE_CHARS], place, method, path[:300], user_id,
               scrub(stack[-MAX_STACK_CHARS:]))
        _insert(row)
        _forget_old_errors()
    except Exception as problem:  # noqa: BLE001 - recording a failure must never cause another one
        log.warning("could not record an error", extra={"reference": reference, "problem": str(problem)[:200]})


def _insert(row: tuple) -> None:
    conn = _connection()
    conn.execute(
        "INSERT OR REPLACE INTO app_errors "
        "(id, created_at, fingerprint, error_type, message, where_raised, method, path, user_id, stack) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()


def _forget_old_errors() -> None:
    """Keep the table small: only the newest MAX_KEPT_ERRORS failures are kept."""
    conn = _connection()
    conn.execute("DELETE FROM app_errors WHERE id NOT IN "
                 "(SELECT id FROM app_errors ORDER BY created_at DESC LIMIT ?)", (MAX_KEPT_ERRORS,))
    conn.commit()
    conn.close()


def _newest(column: str) -> str:
    """SQL for 'the value of `column` on the newest row of the same fingerprint'."""
    return (f"(SELECT {column} FROM app_errors e2 WHERE e2.fingerprint = e.fingerprint "
            "ORDER BY created_at DESC LIMIT 1)")


def recent_groups(days: int = 7, limit: int = 50) -> list[dict]:
    """Failures of the last `days`, one row per bug: how often, first and last seen, and the newest reference."""
    conn = _connection()
    rows = conn.execute(
        "SELECT fingerprint, error_type, where_raised, COUNT(*) AS times, "
        "MIN(created_at) AS first_seen, MAX(created_at) AS last_seen, "
        f"{_newest('id')} AS latest_reference, {_newest('path')} AS latest_path, "
        f"{_newest('message')} AS latest_message "
        "FROM app_errors e WHERE created_at >= ? GROUP BY fingerprint ORDER BY last_seen DESC LIMIT ?",
        (time.time() - days * 86400, limit)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def find_error(reference: str) -> dict | None:
    """One failure by the reference number a customer or a log line gave."""
    conn = _connection()
    row = conn.execute("SELECT * FROM app_errors WHERE id = ?", (reference.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def configuration_checks() -> list[tuple[str, bool, str]]:
    """(what, switched on, what it affects) for each outside service. Yes or no only; secrets are never shown."""
    from . import billing, mailer, r2_uploads
    return [
        ("Email sending (Brevo)", mailer.email_configured(), "receipts, welcome and reminder emails"),
        ("Card payments (Paystack)", billing.paystack_configured(), "paying for orders by card or mobile money"),
        ("M-Pesa direct", billing.mpesa_live(), "paying by M-Pesa without Paystack (off while in test mode)"),
        ("Transcription (Transkriptor)", bool(os.environ.get("TRANSKRIPTOR_API_KEY")),
         "paid transcription; local models take over without it"),
        ("Translation and AI help (Anthropic)", bool(os.environ.get("ANTHROPIC_API_KEY")),
         "translation quality and AI assist"),
        ("Large uploads (R2 storage)", r2_uploads.r2_configured(), "uploads over 100 MB"),
        ("Error reporting (Sentry)", bool(os.environ.get("SENTRY_DSN")), "failures also sent to Sentry"),
    ]


def init_sentry() -> bool:
    """Also send failures to Sentry, when SENTRY_DSN is set and `sentry-sdk` is installed. Optional by design.

    Returns whether it is on.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, send_default_pii=False, traces_sample_rate=0.0)
        return True
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed (pip install sentry-sdk)")
        return False
