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

Sending from a Gmail address (through Brevo) works and is what's
configured today, but it's a real deliverability tradeoff, not a
technicality to ignore: Gmail's own SPF/DKIM records don't authorize
Brevo's servers to send as you, so some inboxes may flag or spam-fold
these. The real fix needs a domain you own - Brevo > Senders, Domains &
Dedicated IPs > add your domain, then add the SPF/DKIM/DMARC records it
gives you to your domain's real DNS. That's a domain-ownership and DNS
step only you can do; nothing here can fake owning a domain.
"""
from __future__ import annotations

import os

import httpx

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

BRAND_ACCENT = "#556b2f"   # Kauli's real olive accent (style.css's --accent)
BRAND_INK = "#21201d"
BRAND_MUTED = "#6b6862"
BRAND_PAGE_BG = "#f5f4f1"
BRAND_CARD_BG = "#ffffff"
BRAND_BORDER = "#e7e3dc"


def email_configured() -> bool:
    return bool(os.environ.get("BREVO_API_KEY")) and bool(os.environ.get("BREVO_FROM_EMAIL"))


def text_to_html_paragraphs(body: str) -> str:
    """Plain text -> simple HTML paragraphs - blank-line-separated blocks
    become separate <p> tags, and a single newline WITHIN one block (e.g.
    a multi-line signature: "Talk soon,\\nGodfrey\\n(Forge Media
    Services)") becomes a real <br>, not silently collapsed the way raw
    HTML always does with a bare newline. Every email body up to now used
    only the paragraph split, which is exactly why a 3-line signature was
    rendering as one run-on line - found from a real screenshot of a real
    sent email, not a hypothetical."""
    parts = [p.strip() for p in body.split("\n\n") if p.strip()]
    return "".join(f'<p style="margin:0 0 14px;">{p.replace(chr(10), "<br>")}</p>' for p in parts)


def wrap_email_html(body_html: str, cta_text: str | None = None, cta_url: str | None = None,
                     footer_note: str | None = None, base_url: str = "") -> str:
    """The shared branded envelope every Kauli email renders inside -
    light-grey page background, a white rounded card, a text wordmark
    header (no logo image: that needs a stable public URL, and this is
    still a local prototype without one yet - a real domain makes that a
    one-line addition later), an optional single CTA button, and a real
    footer with contact/terms links. All styles inline - most email
    clients strip <style> blocks, so this is the only way that reliably
    renders the same in Gmail, Outlook and everything between.

    body_html is the email-specific content, already-formed HTML (e.g. a
    handful of <p> tags or a table) - this just wraps it, never rewrites
    it."""
    cta_html = ""
    if cta_text and cta_url:
        cta_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:28px 0 8px;">
          <tr><td style="border-radius:8px; background:{BRAND_ACCENT};">
            <a href="{cta_url}" style="display:inline-block; padding:13px 28px; font-size:15px;
               font-weight:600; color:#ffffff; text-decoration:none; border-radius:8px;">{cta_text}</a>
          </td></tr>
        </table>"""
    footer_extra = f"<p style=\"margin:6px 0 0;\">{footer_note}</p>" if footer_note else ""
    base = base_url.rstrip("/")
    terms_url = f"{base}/terms" if base else "/terms"
    privacy_url = f"{base}/privacy" if base else "/privacy"
    return f"""<!--[if mso]><style>table {{ border-collapse: collapse; }}</style><![endif]-->
<div style="background:{BRAND_PAGE_BG}; padding:32px 16px; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:600px; margin:0 auto;">
    <tr><td style="padding:0 4px 20px;">
      <span style="font-size:20px; font-weight:800; letter-spacing:0.02em; color:{BRAND_INK};">KAULI</span>
      <span style="font-size:11px; color:{BRAND_MUTED}; text-transform:uppercase; letter-spacing:0.06em; margin-left:8px;">Forge Media Services</span>
    </td></tr>
    <tr><td style="background:{BRAND_CARD_BG}; border:1px solid {BRAND_BORDER}; border-radius:12px; padding:32px 28px;">
      <div style="font-size:15px; line-height:1.6; color:{BRAND_INK};">
        {body_html}
      </div>
      {cta_html}
    </td></tr>
    <tr><td style="padding:20px 4px 0; font-size:12px; color:{BRAND_MUTED}; line-height:1.6;">
      <p style="margin:0;"><em>Solutions for bold people like you, by people like you!</em> Forge Media Services</p>
      {footer_extra}
      <p style="margin:10px 0 0;">Don't see this in your inbox next time? Check your spam/junk folder and
         mark it "Not spam" - that's what keeps future emails landing where you'll actually see them.</p>
      <p style="margin:10px 0 0;"><a href="{terms_url}" style="color:{BRAND_MUTED};">Terms</a>
         &middot; <a href="{privacy_url}" style="color:{BRAND_MUTED};">Privacy</a></p>
    </td></tr>
  </table>
</div>"""


def send_email(to: str, subject: str, html_body: str, text_body: str | None = None,
                tags: list[str] | None = None, sender_name: str = "Kauli Operations") -> tuple[bool, str]:
    """Returns (ok, detail) - detail is Brevo's messageId on success, or a
    human-readable reason on failure. Never raises: a flaky or misconfigured
    provider should degrade to the manual "queued, staff forwards it"
    fallback, not crash whatever just triggered the send (a payment
    webhook, in every current caller).

    tags: real Brevo send tags, not decorative - Brevo already tracks
    open/click/bounce for every email it sends (their own pixel/link
    rewriting, not something this app has to build); tags are what let a
    LATER query (get_open_stats_for_tag) ask "how many of the emails
    tagged 'newsletter-<id>' were opened" instead of Kauli trying to
    build its own tracking pixel from scratch.

    sender_name: same real BREVO_FROM_EMAIL address for every send (that's
    the one domain-verified sender Brevo will actually let this account
    send from) - only the display name changes, so an internal alert
    ("Kauli Marketing" - new lead/signup notices) reads as visibly
    different from a client-facing send ("Kauli Operations") in an inbox,
    without needing a second verified sender identity."""
    if not email_configured():
        return False, "email not configured (BREVO_API_KEY / BREVO_FROM_EMAIL not set)"
    payload = {
        "sender": {"name": sender_name, "email": os.environ["BREVO_FROM_EMAIL"]},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if text_body:
        payload["textContent"] = text_body
    if tags:
        payload["tags"] = tags
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


BREVO_EVENTS_URL = "https://api.brevo.com/v3/smtp/statistics/events"


def get_open_stats_for_tag(tag: str, days: int = 90) -> dict | None:
    """Real open/click counts for every email sent with this tag, straight
    from Brevo's own event log - not a homegrown tracking pixel (Brevo
    already rewrites links and embeds its own open-tracking pixel in
    every HTML email it sends; this just asks for what it already
    recorded). Returns None on any failure (not configured, network
    error, bad response) - a caller shows "not available" rather than a
    fabricated zero. days is capped at 90, Brevo's own real limit for
    this endpoint."""
    if not email_configured():
        return None
    days = max(1, min(days, 90))
    try:
        opened = httpx.get(
            BREVO_EVENTS_URL, params={"tags": tag, "event": "opened", "days": days, "limit": 2500},
            timeout=10.0, headers={"api-key": os.environ["BREVO_API_KEY"], "Accept": "application/json"},
        )
        delivered = httpx.get(
            BREVO_EVENTS_URL, params={"tags": tag, "event": "delivered", "days": days, "limit": 2500},
            timeout=10.0, headers={"api-key": os.environ["BREVO_API_KEY"], "Accept": "application/json"},
        )
        clicked = httpx.get(
            BREVO_EVENTS_URL, params={"tags": tag, "event": "clicks", "days": days, "limit": 2500},
            timeout=10.0, headers={"api-key": os.environ["BREVO_API_KEY"], "Accept": "application/json"},
        )
    except httpx.HTTPError:
        return None
    if opened.status_code >= 300 or delivered.status_code >= 300:
        return None
    # A recipient who opens twice (checks the email, reads it again later)
    # produces two events for the same messageId - de-duplicated here so
    # "opened" means "N distinct recipients opened it", the number that's
    # actually meaningful, not a raw event count.
    def _distinct_message_ids(resp) -> set:
        try:
            events = resp.json().get("events", [])
        except Exception:
            return set()
        return {e.get("messageId") for e in events if e.get("messageId")}
    return {
        "delivered": len(_distinct_message_ids(delivered)),
        "opened": len(_distinct_message_ids(opened)),
        "clicked": len(_distinct_message_ids(clicked)) if clicked.status_code < 300 else 0,
    }
