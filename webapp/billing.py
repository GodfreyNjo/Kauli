"""Pricing, currency conversion, and payment provider integrations.

Real money touches this file - keep everything else read from here, not
duplicated in app.py. Nothing here should ever process a real charge
without PAYSTACK_SECRET_KEY / M-Pesa credentials actually being configured;
every provider call checks for its own keys first and fails with a clear
message rather than pretending to work.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid

import httpx

# ---------------------------------------------------------------- pricing ----
# Pay-per-minute, priced by what's actually done to the audio - this is the
# real cost structure (external ASR/MT/TTS/voice-clone spend, a slice for
# editors, and margin for infra + company operations), not a flat
# subscription fee standing in for it. USD is the source of truth; M-Pesa
# amounts are converted at checkout time (see usd_to_kes below).
SERVICE_LEVELS = {
    "transcribe": {"name": "Transcription only", "rate_per_min": 0.40,
                   "asr": True, "mt": False, "tts": False},
    "translate": {"name": "Transcription + translation", "rate_per_min": 0.90,
                  "asr": True, "mt": True, "tts": False},
    "dub": {"name": "Full dub (transcription + translation + voice + dub)", "rate_per_min": 1.50,
            "asr": True, "mt": True, "tts": True},
}

FREE_MINUTES_PER_MONTH = 5.0  # trial allowance - transcription-only, preview-only, see below

# The free allowance only ever buys transcription - not translation, not a
# dub. It's a "see the AI transcription quality for yourself" trial, not a
# way to get a free dub. Enforced in app.py's create_order(): free minutes
# are only even offered to order_cost_usd() when this is the service level;
# a translate/dub order is billed at the full rate from its first minute.
# The resulting order is also preview-only (see db.orders.is_free_preview /
# app.py's download gate) - transcript text shown on-screen, no download,
# until a real paid order covers it.
FREE_MINUTES_SERVICE_LEVEL = "transcribe"

# A monthly subscription layered ON TOP of per-minute usage - not an
# alternative to it. It buys a discount on the per-minute rates above, plus
# feature access (translation quality, video deliverables, turnaround,
# support tier, billing method). A Pro subscriber still pays per order once
# their free minutes are used - just at 10% off. The discount numbers are
# my own addition on top of what you specified (a standard usage+tier
# hybrid, the same shape AWS/Twilio use) - say if you want them different.
PLANS = {
    "free": {
        "name": "Free", "price_usd": 0, "discount": 0.0,
        "mt": "local", "video_deliverables": False, "support": "-",
        "blurb": f"Pay as you go - {FREE_MINUTES_PER_MONTH:.0f} free minutes/month, then pay per minute "
                 "beyond that. Nothing downloads until an order's cost is covered, free or paid.",
    },
    "pro": {
        "name": "Pro", "price_usd": 19, "discount": 0.10,
        "mt": "local", "video_deliverables": False, "support": "Email",
        "blurb": "10% off usage rates, standard turnaround, email support.",
    },
    "premium": {
        "name": "Premium", "price_usd": 49, "discount": 0.20,
        "mt": "claude", "video_deliverables": True, "support": "Priority",
        "blurb": "20% off usage rates, Priority-quality translation, burned captions & "
                 "dubbed video included, priority turnaround.",
    },
    "enterprise": {
        "name": "Enterprise", "price_usd": 199, "discount": 0.30,
        "mt": "claude", "video_deliverables": True, "support": "Dedicated account manager",
        "blurb": "Negotiated volume rate, dedicated account manager, bank transfer billing.",
    },
}
PLAN_PERIOD_DAYS = 30

# Add-ons: a lower-tier plan can unlock a higher tier's feature for a single
# order, at a per-minute surcharge, instead of having to subscribe to the
# whole plan. Only ever offered for features that are actually functional
# right now - "priority translation" (Claude MT) is deliberately NOT here:
# app.py already silently degrades it to local MT whenever
# ANTHROPIC_API_KEY isn't set (true today), so charging extra for it would
# be billing for something that isn't actually delivered. video_deliverables
# has no such dependency - it's plain ffmpeg, always real.
ADDONS = {
    "video_deliverables": {
        "name": "Video deliverables (burned captions + dubbed video)",
        "rate_per_min": 0.30,
        "included_in": ("premium", "enterprise"),  # plans that already have this - no need to add it there
    },
    # Applied automatically (see app.py's MANUAL_TRANSCRIPTION_LANGUAGES),
    # never as a client-chosen upsell - included_in is empty on purpose, no
    # plan waives this, because it's a real paid-human-labor cost every
    # plan incurs the same way when there's no ASR model for the language.
    # $1.50/audio-minute: industry-standard professional human transcription
    # runs $1.00-$3.00/min ($1.00-$1.50 at standard, non-rush turnaround),
    # and agencies typically add a 30-50% premium for a language with a
    # scarce pool of qualified linguists - this sits in the middle of that
    # combined range. Revisit once you have a real observed cost from
    # whoever is actually doing the transcribing.
    "manual_transcription": {
        "name": "Manual transcription (no ASR model available for this language)",
        "rate_per_min": 1.50,
        "included_in": (),
    },
}


def order_cost_usd(minutes: float, service_level: str, plan: str, free_minutes_available: float,
                    addons: list[str] | None = None, wallet_minutes_available: float = 0.0) -> dict:
    """The actual per-order charge: apply whatever's left of the free
    monthly allowance first, then whatever's left of any prepaid wallet
    balance (bought in bulk, see WALLET_PACKAGES - already paid for, so it
    reduces this order's bill the same way free minutes do), discount
    whatever's still unpaid by the client's plan, price it at the chosen
    service level's rate, then add any paid add-ons on top (add-ons are
    priced against the FULL minute count, not reduced by free/wallet
    minutes or the plan discount - they're an upgrade on top of the base
    service, not part of what those cover). Returns a full breakdown, not
    just a number, so the checkout page can show its work rather than
    asking someone to trust a single figure."""
    addons = [a for a in (addons or []) if a in ADDONS and plan not in ADDONS[a]["included_in"]]
    rate = SERVICE_LEVELS[service_level]["rate_per_min"]
    discount = PLANS[plan]["discount"]
    free_applied = min(minutes, max(0.0, free_minutes_available))
    after_free = max(0.0, minutes - free_applied)
    wallet_applied = min(after_free, max(0.0, wallet_minutes_available))
    billable_minutes = max(0.0, after_free - wallet_applied)
    gross = round(billable_minutes * rate, 2)
    discount_amount = round(gross * discount, 2)
    addon_lines = [{"key": a, "name": ADDONS[a]["name"], "rate_per_min": ADDONS[a]["rate_per_min"],
                     "cost_usd": round(ADDONS[a]["rate_per_min"] * minutes, 2)} for a in addons]
    addon_cost = round(sum(line["cost_usd"] for line in addon_lines), 2)
    total = round(gross - discount_amount + addon_cost, 2)
    return {
        "minutes": round(minutes, 2), "rate_per_min": rate,
        "free_minutes_applied": round(free_applied, 2),
        "wallet_minutes_applied": round(wallet_applied, 2),
        "billable_minutes": round(billable_minutes, 2),
        "gross_usd": gross, "discount_pct": discount, "discount_usd": discount_amount,
        "addons": addon_lines, "addon_cost_usd": addon_cost,
        "total_usd": total,
    }


# Bulk minute packages, purchased once via the same Paystack/M-Pesa/bank
# checkout as everything else - priced at a modest discount off the "dub"
# service level's rate (the most expensive/common tier) as the bulk-buy
# incentive, never expire, and stack with (get applied after) the monthly
# free allowance on every order until used up.
WALLET_PACKAGES = {
    "30": {"minutes": 30, "price_usd": round(30 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.90, 2)},
    "60": {"minutes": 60, "price_usd": round(60 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.85, 2)},
    "150": {"minutes": 150, "price_usd": round(150 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.80, 2)},
}

# Same discount schedule as the fixed packages above, generalized to any
# client-chosen amount - the tier boundaries and discounts match the 30/60/
# 150 packages exactly (10%/15%/20%), just not limited to those three exact
# numbers. Below 30 minutes: no discount, same as buying at the normal rate.
WALLET_CUSTOM_MIN_MINUTES = 5
WALLET_CUSTOM_MAX_MINUTES = 2000
_WALLET_DISCOUNT_TIERS = [(150, 0.20), (60, 0.15), (30, 0.10), (0, 0.0)]


def wallet_discount_pct(minutes: float) -> float:
    for threshold, pct in _WALLET_DISCOUNT_TIERS:
        if minutes >= threshold:
            return pct
    return 0.0


def wallet_custom_price(minutes: float) -> dict:
    """Same math as the fixed WALLET_PACKAGES, just for a client-typed
    amount instead of one of three preset ones."""
    discount = wallet_discount_pct(minutes)
    base = minutes * SERVICE_LEVELS["dub"]["rate_per_min"]
    price = round(base * (1 - discount), 2)
    return {"minutes": minutes, "discount_pct": discount, "price_usd": price,
            "you_save_usd": round(base - price, 2)}

# --------------------------------------------------------- cost estimates ----
# For the internal margin dashboard (staff/ops) only - never shown to
# clients, never used in what they're charged. faster-whisper (ASR), Piper
# and XTTS (TTS), and the local MT fallback all run on this machine for
# real $0 marginal cost per job - there's no API bill for any of them.
# Claude MT is the one real per-job API cost, and this is a ROUGH,
# ROUND-NUMBER PLACEHOLDER (not pulled from Anthropic's actual pricing or
# your real token usage) - swap it for your own observed cost-per-minute
# once you have real Claude billing data to calibrate against. Treat
# anything the margin dashboard shows as directional, not exact accounting.
ESTIMATED_AI_COST_PER_MINUTE = {
    "faster-whisper": 0.0, "stub": 0.0,
    "local": 0.0, "claude": 0.02,  # <- placeholder, replace with your real observed rate
    "piper": 0.0, "xtts": 0.0,
}


def test_client_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("KAULI_TEST_CLIENT_EMAILS", "").split(",") if e.strip()}


def effective_plan(user, subscription) -> str:
    """What the user actually gets, not just what's on file - the
    KAULI_TEST_CLIENT_EMAILS allowlist (matching KAULI_STAFF_EMAILS'
    pattern) gives specific accounts full access regardless of billing
    status, for exactly the testing use case it's named for."""
    if user["email"].strip().lower() in test_client_emails():
        return "enterprise"
    if subscription is None:
        return "free"
    if subscription["status"] != "active":
        return "free"
    if subscription["current_period_end"] and subscription["current_period_end"] < time.time():
        return "free"  # lapsed - don't silently keep paid benefits past the period
    return subscription["plan"]


def free_minutes_remaining(subscription) -> float:
    """The trial allowance is FREE_MINUTES_PER_MONTH for everyone, on every
    plan, plus any bonus_minutes an account manager has granted for
    onboarding (see db.grant_bonus_minutes) - that's what makes a 10-minute
    sales demo possible without touching the base pricing at all."""
    if subscription is None:
        return FREE_MINUTES_PER_MONTH
    used = subscription["minutes_used_this_period"] or 0.0
    bonus = subscription["bonus_minutes"] or 0.0
    return max(0.0, FREE_MINUTES_PER_MONTH + bonus - used)


# ------------------------------------------------------------- currency ----
def usd_to_kes(amount_usd: float) -> tuple[float, str]:
    """Live rate with a graceful, configurable fallback - never blocks a
    checkout just because the free rate API is slow or down. Returns
    (amount_kes, source) so the UI can be honest about which one was used."""
    try:
        resp = httpx.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=4)
        resp.raise_for_status()
        rate = resp.json()["rates"]["KES"]
        return round(amount_usd * rate, 2), "live"
    except Exception:
        fallback_rate = float(os.environ.get("KAULI_USD_KES_FALLBACK_RATE", "129.0"))
        return round(amount_usd * fallback_rate, 2), "fallback"


# ------------------------------------------------------------- paystack ----
# Two separate key slots, not one - PAYSTACK_SECRET_KEY (test, sk_test_...)
# stays the default for all routine development/testing (everything this
# session has run so far), and PAYSTACK_LIVE_SECRET_KEY (sk_live_...) only
# gets used at all when PAYSTACK_LIVE_MODE is explicitly set. Going live is
# then a deliberate, reversible flip of one env var, not "whichever key
# happens to be pasted into the one slot this week" - and nothing here
# reaches for the live key by accident just because it's present in .env.
def _paystack_live_mode() -> bool:
    return os.environ.get("PAYSTACK_LIVE_MODE", "").strip().lower() in ("1", "true", "yes")


def _paystack_key() -> str | None:
    if _paystack_live_mode():
        return os.environ.get("PAYSTACK_LIVE_SECRET_KEY")
    return os.environ.get("PAYSTACK_SECRET_KEY")


def paystack_configured() -> bool:
    return bool(_paystack_key())


def paystack_initialize(email: str, amount_usd: float, reference: str, callback_url: str) -> dict:
    """Returns {"authorization_url": ...} on success, {"error": ...} on
    failure - callers redirect the client to authorization_url, which is
    Paystack's own hosted checkout page. We never see or touch card data.

    Charges in KES, not USD - discovered live against a real key that a
    Kenya-registered Paystack merchant account doesn't accept USD charges
    at all ("Currency not supported by merchant", not an auth error - the
    key itself was valid). Converts with the same usd_to_kes() M-Pesa
    already uses, so every payment path quotes in USD (the pricing model)
    but actually charges in KES (what the merchant account supports) the
    same way. All prices/pricing displayed to clients stay USD; only the
    actual currency of the charge changes."""
    key = _paystack_key()
    if not key:
        return {"error": "Paystack isn't configured (PAYSTACK_SECRET_KEY missing)."}
    amount_kes, _rate_source = usd_to_kes(amount_usd)
    try:
        resp = httpx.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "email": email,
                "amount": int(round(amount_kes * 100)),  # Paystack wants the smallest unit
                "currency": "KES",
                "reference": reference,
                "callback_url": callback_url,
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("status"):
            return {"error": data.get("message", "Paystack rejected the request.")}
        return {"authorization_url": data["data"]["authorization_url"]}
    except Exception as exc:
        return {"error": f"Couldn't reach Paystack: {exc}"}


def paystack_verify(reference: str) -> dict:
    key = _paystack_key()
    if not key:
        return {"error": "Paystack isn't configured."}
    try:
        resp = httpx.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("status"):
            return {"error": data.get("message", "verify failed")}
        return data["data"]  # includes status, reference, amount, etc.
    except Exception as exc:
        return {"error": str(exc)}


def paystack_verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Paystack signs every webhook with HMAC-SHA512 of the raw body using
    your secret key - if this doesn't match, the request didn't come from
    Paystack and must be ignored, full stop. This is the difference between
    a real webhook handler and one anyone on the internet can call to grant
    themselves a free subscription."""
    key = _paystack_key()
    if not key or not signature_header:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# --------------------------------------------------------------- m-pesa ----
MPESA_BASE = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}


def mpesa_configured() -> bool:
    return bool(os.environ.get("MPESA_CONSUMER_KEY")) and bool(os.environ.get("MPESA_SHORTCODE"))


def _mpesa_env() -> str:
    return os.environ.get("MPESA_ENV", "sandbox")


def _mpesa_access_token() -> str | None:
    key = os.environ.get("MPESA_CONSUMER_KEY")
    secret = os.environ.get("MPESA_CONSUMER_SECRET")
    if not key or not secret:
        return None
    base = MPESA_BASE[_mpesa_env()]
    resp = httpx.get(
        f"{base}/oauth/v1/generate?grant_type=client_credentials",
        auth=(key, secret), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def mpesa_stk_push(phone: str, amount_kes: float, reference: str, callback_url: str) -> dict:
    """Triggers the phone-PIN prompt. NOTE: callback_url must be a
    PUBLICLY reachable URL - Safaricom cannot call back to localhost, so
    this genuinely cannot be end-to-end tested without a public deploy or
    a tunnel (ngrok etc.) pointed at this machine. Returns
    {"checkout_request_id": ...} on success, {"error": ...} on failure."""
    if not mpesa_configured():
        return {"error": "M-Pesa isn't configured (MPESA_CONSUMER_KEY/MPESA_SHORTCODE missing)."}
    shortcode = os.environ["MPESA_SHORTCODE"]
    passkey = os.environ.get("MPESA_PASSKEY", "")
    try:
        token = _mpesa_access_token()
        if not token:
            return {"error": "M-Pesa credentials rejected - couldn't get an access token."}
        timestamp = time.strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        base = MPESA_BASE[_mpesa_env()]
        resp = httpx.post(
            f"{base}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(round(amount_kes)),
                "PartyA": phone,
                "PartyB": shortcode,
                "PhoneNumber": phone,
                "CallBackURL": callback_url,
                "AccountReference": reference,
                "TransactionDesc": "Kauli subscription",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ResponseCode") != "0":
            return {"error": data.get("errorMessage") or data.get("ResponseDescription", "STK push failed.")}
        return {"checkout_request_id": data["CheckoutRequestID"]}
    except Exception as exc:
        return {"error": f"Couldn't reach M-Pesa: {exc}"}


def new_reference() -> str:
    return uuid.uuid4().hex[:16]
