"""Real outbound transactional email via Brevo's HTTP API - no SDK needed,
just httpx (already a dependency, same one billing.py uses for Paystack).
Every send is a genuine network call; nothing here pretends to succeed
without BREVO_API_KEY / BREVO_FROM_EMAIL actually being set and Brevo
actually accepting the request - see email_configured(), checked the same
way billing.paystack_configured() gates Paystack.

Set up: in Brevo, Settings -> SMTP & API -> API Keys -> generate a new API
key (never paste the key itself anywhere but .env - not in chat, not in
code, not in a commit). Then Settings -> Senders, Domains & Dedicated IPs
-> add and verify the address you'll send from (Brevo rejects sends from
an unverified sender). New Brevo accounts also need a verified phone
number before ANY send goes out, campaign or transactional - that's a
one-time Brevo account step, not something this code can do for you.
Then set in .env:
  BREVO_API_KEY=xkeysib-...
  BREVO_FROM_EMAIL=receipts@yourdomain.com
"""
from __future__ import annotations

import os

import httpx

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def email_configured() -> bool:
    return bool(os.environ.get("BREVO_API_KEY")) and bool(os.environ.get("BREVO_FROM_EMAIL"))


def send_email(to: str, subject: str, html_body: str, text_body: str | None = None) -> tuple[bool, str]:
    """Returns (ok, detail) - detail is Brevo's messageId on success, or a
    human-readable reason on failure. Never raises: a flaky or misconfigured
    provider should degrade to the manual "queued, staff forwards it"
    fallback, not crash whatever just triggered the send (a payment
    webhook, in every current caller)."""
    if not email_configured():
        return False, "email not configured (BREVO_API_KEY / BREVO_FROM_EMAIL not set)"
    payload = {
        "sender": {"name": "Kauli", "email": os.environ["BREVO_FROM_EMAIL"]},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if text_body:
        payload["textContent"] = text_body
    try:
        resp = httpx.post(
            BREVO_API_URL, json=payload, timeout=10.0,
            headers={"api-key": os.environ["BREVO_API_KEY"], "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if resp.status_code >= 300:
        return False, f"Brevo {resp.status_code}: {resp.text[:300]}"
    try:
        return True, resp.json().get("messageId", "sent")
    except Exception:
        return True, "sent"
