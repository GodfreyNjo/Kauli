"""Server-side GA4 event tracking via Measurement Protocol.

Real gap this closes: _ga.html only ever tracks generic pageviews on the
7 public marketing pages. Every real conversion moment that actually
matters for understanding the business - an account that actually signed
up, a lead that actually submitted the contact form, an order that
actually got placed, money that actually cleared - happened with zero
named event anywhere. A client-side-only fix can't cover this properly
either: signup/login/order-submit/payment-confirmation are server
decisions (a client-side "on submit" handler only knows an attempt was
made, not whether it actually succeeded), and Google's own AI-powered
analysis inside GA4 (or anything built on top of the property) can only
ever be as good as the real events actually recorded.

Same consent boundary as the client-side loader (_ga.html): a Measurement
Protocol hit is only ever sent when the visitor's browser already carries
a real _ga cookie - which GA4's own gtag.js only ever sets after cookie
consent was accepted (base.html's cookie-notice/cookie-prefs script).
This never opts anyone new into tracking; it only extends tracking of
someone who already consented to cover the rest of their real journey,
including moments (a payment webhook, days after the client's own
browser session) that happen with no browser request in scope at all -
see client_id_from_request's own docstring for how that's handled.

Inert until GA_API_SECRET is set in .env - create one in the GA4 Admin
UI: Admin > Data Streams > (your web stream) > Measurement Protocol API
secrets. Same "silently inert until configured" pattern as
GOOGLE_ONE_TAP_CLIENT_ID/GOOGLE_SITE_VERIFICATION elsewhere in this app.
"""
from __future__ import annotations

import os
import threading

import httpx

# Overridable via GA_MEASUREMENT_ID so the property can be repointed via
# .env alone - this is just today's real, already-live ID (_ga.html's own
# literal), not invented for this module.
DEFAULT_MEASUREMENT_ID = "G-29L6CFPNEB"


def measurement_id() -> str:
    return os.environ.get("GA_MEASUREMENT_ID", DEFAULT_MEASUREMENT_ID)


def ga_configured() -> bool:
    return bool(os.environ.get("GA_API_SECRET"))


def client_id_from_request(request) -> str | None:
    """The real GA4 client id this visitor's browser already carries, if
    any - parsed from GA4's own _ga cookie (format GA1.1.<part1>.<part2>;
    the client_id GA's collect endpoint actually expects is those last two
    dot-separated numbers joined by a dot, not the whole cookie value).
    None if this visitor never had GA loaded/never consented - callers
    that have a live request (signup, login, the contact form, order
    submission) use this directly. A payment webhook has no such request
    at all (it arrives from Paystack's/Safaricom's own servers, carrying
    none of the customer's cookies) - see app.py's _checkout, which
    captures this same value up front, at the one point a real customer
    request IS in scope, and stores it on the payment's own meta for
    _activate_payment to read back later, whenever the webhook actually
    fires."""
    raw = request.cookies.get("_ga")
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) < 4:
        return None
    return f"{parts[2]}.{parts[3]}"


def send_event(client_id: str | None, event_name: str, params: dict | None = None) -> None:
    """Fire-and-forget, and non-blocking - the actual HTTP call runs in a
    background thread so a slow/unreachable Measurement Protocol endpoint
    never adds latency to the real request (signup, login, checkout) this
    is called from, and never raises back into it either. Silently does
    nothing when GA isn't configured or there's no real client_id to
    attribute this to (see client_id_from_request) - never fabricates one."""
    if not ga_configured() or not client_id:
        return
    secret = os.environ["GA_API_SECRET"]
    mid = measurement_id()

    def _send():
        try:
            httpx.post(
                "https://www.google-analytics.com/mp/collect",
                params={"measurement_id": mid, "api_secret": secret},
                json={"client_id": client_id, "events": [{"name": event_name, "params": params or {}}]},
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_send, daemon=True).start()
