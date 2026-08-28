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

# A real $1 Paystack charge, required once before a client's FIRST free
# minute is ever spent (see app.py's create_order gate) - confirms a real
# card, not a throwaway signup farming the free allowance every month
# from a fresh account. Not a fee: the $1 becomes real wallet credit the
# moment it's paid (see app.py's _activate_payment "trial_verification"
# branch), automatically applied to whatever real order they submit
# next, the same wallet-credit mechanism a real top-up already uses.
# Paystack only, not M-Pesa - MPESA_ENV is still "sandbox" on the real
# server (confirmed live), so a real client's real phone number would
# just fail against it; Paystack is the one provider actually confirmed
# live (PAYSTACK_LIVE_MODE=true, a real sk_live_ key present).
TRIAL_VERIFICATION_FEE_USD = 1.00

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
    # A real professional human voice actor records the dub, instead of
    # (or as well as - the AI dub still gets made either way, see
    # webapp/app.py's create_order) synthetic TTS. Only offered on the
    # "dub" service level - nothing to voice on transcription/translation.
    #
    # $2.50/audio-minute is a STARTING PROPOSAL, not an observed market
    # rate - there is no real cost data yet (see PENDING note below), only
    # the same reasoning manual_transcription above used: professional
    # voice-over on international freelance platforms commonly runs
    # $2.50-$6.50/finished-minute, East African rates for Swahili/Kikuyu
    # talent are meaningfully lower than that range but not reliably
    # documented anywhere Kauli can cite - this sits at the low end of the
    # international range as a safe starting point. Revisit the moment you
    # have a real quoted rate from an actual actor - see
    # voice_actors.rate_per_min_usd, which is what ACTUAL payouts are
    # computed from per actor, not this flat number. This is only the
    # client-facing asking price before an actor is even cast.
    "human_voice_over": {
        "name": "Real human voice actor (instead of AI dub voice)",
        "rate_per_min": 2.50,
        "included_in": (),
    },
}

# Rush processing: a client-chosen surcharge on a single order in exchange
# for real, manual priority in the review queue - Godfrey picking it up and
# working it first, not a faster pipeline (there's still one operator and
# one background worker thread, see worker.py). Previously left
# undecided/declined (see tat.py's older docstring) since there was no real
# rush-pricing decision to build against; there is now. A flat percentage
# of the order's full (undiscounted) service value, not a per-minute
# ADDONS-style rate - the "sacrifice" scales with how much real work is
# being reprioritized, same reasoning as the plan discount already being a
# percentage rather than a flat amount. Applies even to a free-trial
# preview's minutes (see order_cost_usd) - jumping the queue is a real ask
# whether or not the underlying minutes were free.
RUSH_SURCHARGE_PCT = 0.50

# Enterprise SLA credit: if an order's real, client-facing promise
# (tat.py's deadline_at - already includes a 20% buffer specifically so a
# normal day delivers early, not exactly on time) passes with the order
# still not delivered, the client gets this fraction of what they paid
# for that order back as wallet credit, automatically - see
# webapp/app.py's _deadline_watch_once, which already detects a missed
# deadline for the staff alert and now also triggers this. Only
# "enterprise" tier - the plan whose whole pitch is dedicated turnaround
# and a "real account manager", so a broken promise there is the one that
# most needs a real, unprompted remedy rather than waiting for the client
# to notice and complain. 10% sits at the generous end of the 5-10% range
# real SLA credit policies commonly use - a broken promise to your
# highest-tier client is worth erring toward generous, not the minimum
# defensible number. Revisit once you have real missed-deadline data to
# weigh against actual margin impact.
ENTERPRISE_SLA_CREDIT_PCT = 0.10


# Credits: a fixed, tier-agnostic unit of prepaid value - 1 credit = $0.10,
# chosen because every current SERVICE_LEVELS/ADDONS rate divides into it
# as a whole number (transcribe 4/min, translate 9/min, dub 15/min, video
# deliverables 3/min, manual transcription 15/min), so "how far will my
# credits go" is always a clean number regardless of which tier they're
# spent on. Replaces the old "wallet minutes" - those were purchased at a
# price benchmarked off the dub rate but then spent minute-for-minute on
# ANY tier including much cheaper ones, letting a client extract far more
# real service value than they paid for (100 minutes bought at dub pricing
# spent entirely on transcribe-tier orders, which cost less than a third as
# much per minute). A credit's dollar value doesn't change with what it's
# spent on, so that mismatch can't happen.
CREDITS_PER_DOLLAR = 10


def usd_to_credits(usd: float) -> float:
    return usd * CREDITS_PER_DOLLAR


def credits_to_usd(credits: float) -> float:
    return credits / CREDITS_PER_DOLLAR


def order_cost_usd(minutes: float, service_level: str, plan: str, free_minutes_available: float,
                    addons: list[str] | None = None, wallet_credits_available: float = 0.0,
                    rush: bool = False) -> dict:
    """The actual per-order charge: apply whatever's left of the free
    monthly allowance first (in minutes - the trial is always denominated
    in minutes of transcription, see FREE_MINUTES_SERVICE_LEVEL), discount
    whatever's still unpaid by the client's plan, then apply whatever's
    left of any prepaid credit balance as real dollar value against that
    discounted base cost (never against add-ons or the rush surcharge -
    those are upgrades on top of the base service, not part of what a
    credit balance covers, matching the plan discount's own scope). Add-ons
    and the rush surcharge are priced against the FULL minute count/value,
    not reduced by free minutes or the plan discount. Returns a full
    breakdown, not just a number, so the checkout page can show its work
    rather than asking someone to trust a single figure."""
    addons = [a for a in (addons or []) if a in ADDONS and plan not in ADDONS[a]["included_in"]]
    rate = SERVICE_LEVELS[service_level]["rate_per_min"]
    discount = PLANS[plan]["discount"]

    free_applied = min(minutes, max(0.0, free_minutes_available))
    billable_minutes = max(0.0, minutes - free_applied)
    gross = round(billable_minutes * rate, 2)
    discount_amount = round(gross * discount, 2)
    after_discount = round(gross - discount_amount, 2)

    credits_value_usd = credits_to_usd(max(0.0, wallet_credits_available))
    credits_applied_usd = round(min(after_discount, credits_value_usd), 2)
    credits_applied = round(usd_to_credits(credits_applied_usd), 2)

    addon_lines = [{"key": a, "name": ADDONS[a]["name"], "rate_per_min": ADDONS[a]["rate_per_min"],
                     "cost_usd": round(ADDONS[a]["rate_per_min"] * minutes, 2)} for a in addons]
    addon_cost = round(sum(line["cost_usd"] for line in addon_lines), 2)

    # See RUSH_SURCHARGE_PCT's comment - a % of the FULL, undiscounted
    # service value, applied even on minutes free/credits would otherwise
    # cover (real prioritization work, not a discount-eligible line item).
    full_service_value = round(minutes * rate, 2)
    rush_surcharge_usd = round(full_service_value * RUSH_SURCHARGE_PCT, 2) if rush else 0.0

    total = round(after_discount - credits_applied_usd + addon_cost + rush_surcharge_usd, 2)
    return {
        "minutes": round(minutes, 2), "rate_per_min": rate,
        "free_minutes_applied": round(free_applied, 2),
        "billable_minutes": round(billable_minutes, 2),
        "gross_usd": gross, "discount_pct": discount, "discount_usd": discount_amount,
        "credits_applied": credits_applied, "credits_applied_usd": credits_applied_usd,
        "addons": addon_lines, "addon_cost_usd": addon_cost,
        "rush": rush, "rush_surcharge_usd": rush_surcharge_usd,
        "total_usd": total,
    }


# Bulk credit packages, purchased once via the same Paystack/M-Pesa/bank
# checkout as everything else - never expire, and stack with (get applied
# after) the monthly free minutes allowance on every order until used up.
# Same $ prices as the old fixed minute packages (30/60/150 min "at dub
# pricing, 10/15/20% off"); credits here are just that same real dollar
# value re-expressed in the new tier-agnostic unit (minutes * dub rate *
# CREDITS_PER_DOLLAR) - nobody who already bought one of these gets less
# than they paid for, they can just now spend it honestly at any tier.
CREDIT_PACKAGES = {
    "30": {"credits": round(30 * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR),
           "price_usd": round(30 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.90, 2)},
    "60": {"credits": round(60 * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR),
           "price_usd": round(60 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.85, 2)},
    "150": {"credits": round(150 * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR),
            "price_usd": round(150 * SERVICE_LEVELS["dub"]["rate_per_min"] * 0.80, 2)},
}

# Same discount schedule as the fixed packages above, generalized to any
# client-chosen dollar amount - the tier boundaries and discounts match the
# fixed packages exactly (10%/15%/20%, at 30/60/150 minutes-of-dub-value
# equivalent spend), just not limited to those three exact amounts.
CREDIT_CUSTOM_MIN_MINUTES_EQUIV = 5
CREDIT_CUSTOM_MAX_MINUTES_EQUIV = 2000
_CREDIT_DISCOUNT_TIERS = [(150, 0.20), (60, 0.15), (30, 0.10), (0, 0.0)]


def credit_discount_pct(minutes_equiv: float) -> float:
    for threshold, pct in _CREDIT_DISCOUNT_TIERS:
        if minutes_equiv >= threshold:
            return pct
    return 0.0


def custom_credit_price(minutes_equiv: float) -> dict:
    """Same math as the fixed CREDIT_PACKAGES, just for a client-typed
    spend amount instead of one of three preset ones - minutes_equiv is
    "how many dub-rate minutes of value is this", the same reference point
    the fixed packages use, purely to keep one familiar discount ladder;
    the actual balance credited is always in real credits."""
    discount = credit_discount_pct(minutes_equiv)
    base_usd = minutes_equiv * SERVICE_LEVELS["dub"]["rate_per_min"]
    price = round(base_usd * (1 - discount), 2)
    credits = round(minutes_equiv * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR)
    return {"credits": credits, "discount_pct": discount, "price_usd": price,
            "you_save_usd": round(base_usd - price, 2)}

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


def mpesa_live() -> bool:
    """Real, not just configured - sandbox credentials are enough to
    exercise the STK-push code path for testing, but a real client
    submitting against Safaricom's sandbox endpoint gets a request that
    silently goes nowhere real. This is the actual gate for whether a
    real client should ever be allowed to pick M-Pesa direct at checkout
    - see webapp/app.py's order_pay_checkout and order_pay_page, both of
    which used to check mpesa_configured() alone."""
    return mpesa_configured() and _mpesa_env() == "production"


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
