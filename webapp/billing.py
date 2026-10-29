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

# ------------------------------------------------------- market pricing ----
# Two products, two markets. "ai" is the instant tier: Transkriptor/Claude/
# TTS output delivered the moment the pipeline finishes, with NO human in the
# loop (so it must always be labelled "AI-generated, not human-reviewed").
# "human" is the original Kauli product: AI draft, then a real reviewer signs
# it off before the client sees it. Rates are USD per audio-minute.
#
# Approved 2026-10-27 after benchmarking Transkriptor ($0.0035-0.033/min
# effective), Happy Scribe ($0.20/min PAYG, $2.00 human), Rev ($1.99 human),
# Rask ($0.67-1.10 dub) and ElevenLabs (~$0.18-0.91 dub). Estimated cost to
# us: ~$0.01-0.015 AI transcription, ~$0.05-0.08 AI dub (see
# ESTIMATED_AI_COST_PER_MINUTE) - a planning figure until the real
# Transkriptor API rate is confirmed.
#
# NOT in the approved card, my own interpolation - change freely: the human-
# reviewed TRANSLATION rows ("translate"/"human": 1.40 intl, 0.75 ea), picked
# so the human-review fee on top of the AI rate stays consistent with the
# approved transcript and dub rows. Without them the core product
# (human-verified Swahili<->English subtitles) would have no price at all.
REVIEW_TIERS = {
    "ai": {"name": "Instant AI", "human_reviewed": False,
           "blurb": "Ready in minutes. AI-generated - not reviewed by a person."},
    "human": {"name": "Human-reviewed", "human_reviewed": True,
              "blurb": "AI draft, then a real reviewer checks and signs it off before delivery."},
}
MARKETS = {
    "intl": "United States & international",
    "ea": "Kenya & East Africa",
    # Accounts created before market pricing existed. Keep paying what they
    # always paid for the human-reviewed product (SERVICE_LEVELS rates above);
    # they also get the new instant AI tier at East-Africa AI prices. Staff can
    # move an account to a real market (db.set_billing_market).
    "legacy": "Existing account pricing",
}
PRICE_CARD = {
    # Human-reviewed rows re-set 2026-10-28 to sit just under the mainstream human services (Rev $1.99 and Happy
    # Scribe $2.00 per minute for human transcription; Rev's human translated subtitles start at $6.49) while staying
    # well above the budget end (GoTranscript ~$1.00, TranscribeMe ~$0.79). Instant AI rows are unchanged. The lower
    # price market below is unchanged too. Orders already placed keep the rate they were priced at.
    "intl": {
        "transcribe": {"ai": 0.12, "human": 1.79},
        "translate": {"ai": 0.30, "human": 2.99},
        "dub": {"ai": 0.80, "human": 3.99},
    },
    "ea": {
        "transcribe": {"ai": 0.06, "human": 0.60},
        "translate": {"ai": 0.15, "human": 0.75},
        "dub": {"ai": 0.50, "human": 1.20},
    },
}
DEFAULT_MARKET = "intl"
# Display-only/signup default: where a visitor is, per Cloudflare's
# CF-IPCountry header. A VPN can fake it - the account market is fixed at
# signup and only staff can change it, so it's a nudge, not a lock.
EAST_AFRICA_COUNTRIES = {"KE", "UG", "TZ", "RW", "BI", "ET", "SS", "SO", "DJ", "ER"}
# The lower price card ("ea" is just its internal name) is for visitors in lower-income economies, roughly the World Bank's
# low and lower-middle income groups: Africa, South and South-East Asia and a few others. Edit this list freely.
LOWER_PRICE_COUNTRIES = EAST_AFRICA_COUNTRIES | {
    # rest of Africa
    "DZ", "AO", "BJ", "BW", "BF", "CV", "CM", "CF", "TD", "KM", "CG", "CD", "CI", "EG", "SZ", "GA", "GM", "GH", "GN", "GW", "LS", "LR",
    "MG", "MW", "ML", "MR", "MA", "MZ", "NA", "NE", "NG", "SN", "SL", "TG", "TN", "ZM", "ZW", "SD", "ST",
    # South and South-East Asia, Central Asia
    "IN", "PK", "BD", "LK", "NP", "BT", "AF", "PH", "ID", "VN", "KH", "LA", "MM", "TL", "UZ", "KG", "TJ",
    # a few others
    "BO", "HN", "NI", "HT", "SV", "PS", "YE", "SY", "PG",
}


def market_for_country(country_code: str | None) -> str:
    return "ea" if (country_code or "").strip().upper() in LOWER_PRICE_COUNTRIES else DEFAULT_MARKET


def rate_for(service_level: str, market: str | None, review_tier: str = "human") -> float:
    """The per-minute rate for one service level in one market/tier. Unknown
    or legacy markets fall back to the original SERVICE_LEVELS rates for the
    human tier, and to East-Africa AI prices for the AI tier."""
    review_tier = review_tier if review_tier in REVIEW_TIERS else "human"
    if market in PRICE_CARD:
        return PRICE_CARD[market][service_level][review_tier]
    if review_tier == "ai":
        return PRICE_CARD["ea"][service_level]["ai"]
    return SERVICE_LEVELS[service_level]["rate_per_min"]


def price_card_for(market: str | None) -> dict:
    """{service_level: {"ai": rate, "human": rate}} for templates/JS."""
    return {sl: {t: rate_for(sl, market, t) for t in REVIEW_TIERS} for sl in SERVICE_LEVELS}


# A new account's very first upload, when it is this short, always gets Transkriptor: it is the first impression of our quality.
FIRST_ORDER_QUALITY_MINUTES = 5.0
FREE_MINUTES_PER_MONTH = 30.0  # monthly free allowance - Instant AI transcription only. Served by the low-cost providers (see app._route_asr), so it costs cents

# The free allowance only ever buys transcription - not translation, not a
# dub. It's a "see the AI transcription quality for yourself" trial, not a
# way to get a free dub. Enforced in app.py's create_order(): free minutes
# are only even offered to order_cost_usd() when this is the service level;
# a translate/dub order is billed at the full rate from its first minute.
# The resulting order is also preview-only (see db.orders.is_free_preview /
# app.py's download gate) - transcript text shown on-screen, no download,
# until a real paid order covers it.
FREE_MINUTES_SERVICE_LEVEL = "transcribe"


def allowance_covers(plan: str, service_level: str, review_tier: str) -> bool:
    """Whether a plan's monthly included minutes pay for an order. Instant AI only (a person's time is never free).
    Every plan's allowance covers transcription; the paid plans' also covers Instant AI translation, so the allowance (thousands of minutes on a paid plan)
    minutes can be spent on transcripts, translations or any mix. Dubbing is always priced on its own."""
    if review_tier != "ai":
        return False
    if service_level == FREE_MINUTES_SERVICE_LEVEL:
        return True
    return service_level == "translate" and plan in ("pro", "premium", "enterprise")

# Until an account has chosen a plan it only gets to read this share of a
# finished transcript on screen; the rest is hidden (see app.py's
# _transcript_teaser). Choosing the Free plan - which costs nothing - shows it all.
TRANSCRIPT_TEASER_VISIBLE_FRACTION = 0.4

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
# Included Instant AI minutes a month. The first QUALITY minutes of each paid plan are transcribed by Transkriptor (our quality
# provider); the rest of the allowance goes to the low-cost providers (Groq, then AssemblyAI), which is what makes thousands of
# included minutes affordable. See app._route_asr and tests/test_plans_v2.py for the worst-case cost check.
PRO_MINUTES, PREMIUM_MINUTES, ENTERPRISE_MINUTES = 2500, 6500, 25000


def hours_suffix(minutes: float) -> str:
    """" (about 108 hours)" after a big minute count, where hours are what people picture; nothing for small ones."""
    return f" (about {round(minutes / 60):,} hours)" if minutes >= 6000 else ""
PRO_QUALITY_MINUTES, PREMIUM_QUALITY_MINUTES, ENTERPRISE_QUALITY_MINUTES = 150, 400, 1000

PLANS = {
    "free": {
        "name": "Free", "price_usd": 0, "price_usd_year": 0, "discount": 0.0, "included_ai_minutes": FREE_MINUTES_PER_MONTH,
        "mt": "local", "video_deliverables": False, "support": "-",
        "blurb": f"Pay as you go. Your first {FREE_MINUTES_PER_MONTH:.0f} minutes of Instant AI transcription are "
                 "free each month; anything beyond that is billed per minute. Downloads unlock once you have "
                 "chosen a plan.",
    },
    # A plan is a commitment, and the commitment is rewarded twice: Instant AI minutes included every month (worth
    # about what the plan costs), and a percentage off everything beyond them. Instant AI costs us roughly
    # $0.015 a minute (worst case about $0.033), so even the yearly price leaves a wide margin - see the test
    # tests/test_plans_v2.py that fails if a change here would let a plan lose money.
    "pro": {
        "name": "Pro", "price_usd": 19, "price_usd_year": 149, "discount": 0.0, "included_ai_minutes": PRO_MINUTES,
        "quality_minutes": PRO_QUALITY_MINUTES,
        "mt": "local", "video_deliverables": False, "support": "Email",
        "blurb": f"{PRO_MINUTES:,} Instant AI minutes every month for transcription and translation, email support.",
    },
    "premium": {
        "name": "Premium", "price_usd": 49, "price_usd_year": 379, "discount": 0.0, "included_ai_minutes": PREMIUM_MINUTES,
        "quality_minutes": PREMIUM_QUALITY_MINUTES,
        "mt": "claude", "video_deliverables": True, "support": "Priority",
        "blurb": f"{PREMIUM_MINUTES:,} Instant AI minutes{hours_suffix(PREMIUM_MINUTES)} every month for transcription and translation, burned captions & dubbed video "
                 "included, priority-quality translation and turnaround.",
    },
    # Not self-serve: arranged by hand for heavy users and organisations (custom rate, invoicing, a named contact).
    "enterprise": {
        "name": "Enterprise", "price_usd": 199, "price_usd_year": 1990, "discount": 0.0, "included_ai_minutes": ENTERPRISE_MINUTES,
        "quality_minutes": ENTERPRISE_QUALITY_MINUTES,
        "mt": "claude", "video_deliverables": True, "support": "Dedicated account manager",
        "blurb": "Negotiated volume rate, dedicated account manager, bank transfer billing.",
    },
}
PLAN_PERIOD_DAYS = 30
PLAN_YEAR_DAYS = 365


def plan_included_minutes(plan: str) -> float:
    """Instant AI transcription minutes a plan includes every month (the free plan's is the trial allowance)."""
    return float(PLANS.get(plan, PLANS["free"]).get("included_ai_minutes", FREE_MINUTES_PER_MONTH))


def yearly_monthly_equivalent(plan: str) -> float:
    return round(PLANS[plan]["price_usd_year"] / 12.0, 2)


def yearly_saving_pct(plan: str) -> int:
    monthly = PLANS[plan]["price_usd"] * 12
    return int(round((1 - PLANS[plan]["price_usd_year"] / monthly) * 100)) if monthly else 0

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
    # Premium AI voices from ElevenLabs on a full dub into English or Swahili (no model supports
    # Kikuyu), with or without human review. Priced from the unit-economics model just below, not
    # guessed: see premium_voice_unit_cost_usd() and premium_voice_min_rate(). The add-on is always
    # charged in full (plan discounts, free minutes and wallet credits never reduce an add-on).
    "premium_voice": {
        "name": "Premium AI voice (ElevenLabs)",
        "rate_per_min": 0.50,
        "included_in": (),
    },
    # Removes background noise and music from the recording BEFORE it is transcribed (ElevenLabs Voice
    # Isolator, $0.12 per audio-minute), which improves accuracy on field interviews and phone or WhatsApp
    # recordings. Priced from audio_cleanup_unit_cost_usd() below; switches itself off if that rises past the price.
    "audio_cleanup": {
        "name": "Audio clean-up (removes background noise first)",
        "rate_per_min": 0.30,
        "included_in": (),
    },
    # Studio's recognition: ElevenLabs Scribe v2 (excellent English, high-accuracy Swahili) with speaker labels
    # and word timings, instead of our standard recogniser. Scribe costs about $0.0037 a minute, so this is priced
    # for its value rather than its cost; the margin guard below still applies.
    # Human review by an outside language specialist for Chinese, German and Spanish (beyond Swahili, Kikuyu and
    # English, whose reviewers the human-reviewed rate already covers). A STARTING PROPOSAL: it is only offered
    # when base human rate + this add-on clears the cost floor below, so it cannot be sold under your contractor
    # cost. Set KAULI_EXTRA_REVIEW_COST_PER_MIN to the real contractor cost per audio-minute.
    "review_extra": {
        "name": "Review by a language specialist (Chinese, German, Spanish)",
        "rate_per_min": 1.20,
        "included_in": (),
    },
    "premium_asr": {
        "name": "Studio recognition + speaker labels (ElevenLabs Scribe)",
        "rate_per_min": 0.10,
        "included_in": (),
    },
}

# ---- Premium voice unit economics --------------------------------------------------------
# ElevenLabs bills per character: its API list price for the models we use (v3, v4, Multilingual
# v2) is $0.08 per 1,000 characters (elevenlabs.io/pricing/api, checked 2026-10-28; set
# ELEVENLABS_USD_PER_1K_CHARS if your plan differs). The numbers below are deliberately cautious.
PREMIUM_VOICE_USD_PER_1K_CHARS = 0.08
PREMIUM_VOICE_CHARS_PER_AUDIO_MINUTE = 1100   # speech runs about 900-1,000 characters a minute; Swahili runs longer
PREMIUM_VOICE_REWORK_FACTOR = 1.5             # retries, corrections and re-renders synthesise some lines again
PAYMENT_FEE_RATE = 0.039                      # Paystack's international card rate, taken from the whole payment
MIN_PREMIUM_MARGIN = 0.50                     # the add-on must keep at least this share after cost and fees


def premium_voice_unit_cost_usd() -> float:
    """Worst-case ElevenLabs cost of one audio-minute of premium voice."""
    try:
        per_1k = float(os.environ.get("ELEVENLABS_USD_PER_1K_CHARS") or PREMIUM_VOICE_USD_PER_1K_CHARS)
    except ValueError:
        per_1k = PREMIUM_VOICE_USD_PER_1K_CHARS
    return per_1k * PREMIUM_VOICE_CHARS_PER_AUDIO_MINUTE / 1000.0 * PREMIUM_VOICE_REWORK_FACTOR


def premium_voice_min_rate() -> float:
    """The lowest per-minute add-on price that still clears MIN_PREMIUM_MARGIN after the voice cost
    and the payment fee."""
    return premium_voice_unit_cost_usd() / (1.0 - PAYMENT_FEE_RATE - MIN_PREMIUM_MARGIN)


def premium_voice_margin_ok() -> bool:
    """False if the add-on price has fallen below the floor (e.g. ElevenLabs raised its prices):
    the app then stops offering premium voice instead of selling it at a loss."""
    return ADDONS["premium_voice"]["rate_per_min"] >= premium_voice_min_rate()


def premium_voice_chars_needed(minutes: float) -> int:
    return int(minutes * PREMIUM_VOICE_CHARS_PER_AUDIO_MINUTE * PREMIUM_VOICE_REWORK_FACTOR) + 1


# Audio clean-up: ElevenLabs Voice Isolator lists at $0.12 per audio-minute (the cleaned file is kept, so a
# retry does not pay again). Speaker labels use Scribe at $0.22 per hour, about $0.0037 a minute, so they are
# included free with the existing "Identify speakers" option; the figure is kept here so a test guards it.
AUDIO_CLEANUP_USD_PER_MIN = 0.12
AUDIO_CLEANUP_CREDITS_PER_MIN = 1500          # conservative plan-credit use per minute, for the spend ceiling
SPEAKER_LABELS_USD_PER_MIN = 0.22 / 60.0 * PREMIUM_VOICE_REWORK_FACTOR


def audio_cleanup_unit_cost_usd() -> float:
    try:
        return float(os.environ.get("ELEVENLABS_ISOLATOR_USD_PER_MIN") or AUDIO_CLEANUP_USD_PER_MIN)
    except ValueError:
        return AUDIO_CLEANUP_USD_PER_MIN


def audio_cleanup_min_rate() -> float:
    return audio_cleanup_unit_cost_usd() / (1.0 - PAYMENT_FEE_RATE - MIN_PREMIUM_MARGIN)


def audio_cleanup_margin_ok() -> bool:
    return ADDONS["audio_cleanup"]["rate_per_min"] >= audio_cleanup_min_rate()


# Clean-up on an Instant AI order carries a smaller markup than on a reviewed one: about 50% over what it costs us, which still
# leaves a quarter of the price after payment fees. Like every add-on it switches itself off if the cost ever rises past that.
AUDIO_CLEANUP_AI_RATE = 0.18
MIN_AUDIO_CLEANUP_AI_MARGIN = 0.25


def audio_cleanup_ai_min_rate() -> float:
    return audio_cleanup_unit_cost_usd() / (1.0 - PAYMENT_FEE_RATE - MIN_AUDIO_CLEANUP_AI_MARGIN)


def audio_cleanup_ai_margin_ok() -> bool:
    return AUDIO_CLEANUP_AI_RATE >= audio_cleanup_ai_min_rate()


def addon_rate(key: str, review_tier: str = "human") -> float:
    """What an add-on costs per audio-minute on this kind of order (clean-up is cheaper on Instant AI)."""
    if key == "audio_cleanup" and review_tier == "ai":
        return AUDIO_CLEANUP_AI_RATE
    return ADDONS[key]["rate_per_min"]


def audio_cleanup_credits_needed(minutes: float) -> int:
    return int(minutes * AUDIO_CLEANUP_CREDITS_PER_MIN) + 1


EXTRA_REVIEW_COST_USD_PER_MIN = 1.50     # assumed contractor cost per audio-minute until you set the real quote
MIN_EXTRA_REVIEW_MARGIN = 0.30


def extra_review_cost_usd() -> float:
    try:
        return float(os.environ.get("KAULI_EXTRA_REVIEW_COST_PER_MIN") or EXTRA_REVIEW_COST_USD_PER_MIN)
    except ValueError:
        return EXTRA_REVIEW_COST_USD_PER_MIN


def extra_review_min_total_rate() -> float:
    """The lowest total per-minute price (base human rate + the add-on) that still keeps MIN_EXTRA_REVIEW_MARGIN
    after the contractor's cost and the card fee."""
    return extra_review_cost_usd() / (1.0 - PAYMENT_FEE_RATE - MIN_EXTRA_REVIEW_MARGIN)


def review_extra_margin_ok(base_human_rate: float) -> bool:
    return base_human_rate + ADDONS["review_extra"]["rate_per_min"] >= extra_review_min_total_rate()


def premium_asr_margin_ok() -> bool:
    cost = SPEAKER_LABELS_USD_PER_MIN
    return ADDONS["premium_asr"]["rate_per_min"] >= cost / (1.0 - PAYMENT_FEE_RATE - MIN_PREMIUM_MARGIN)


def studio_extras_per_min(service_level: str) -> float:
    """What the Studio tier adds per audio-minute on top of the Human-reviewed rate: clean-up + Studio
    recognition, plus the premium voice on a dub. Each part is its own add-on line on the receipt."""
    total = ADDONS["audio_cleanup"]["rate_per_min"] + ADDONS["premium_asr"]["rate_per_min"]
    if service_level == "dub":
        total += ADDONS["premium_voice"]["rate_per_min"]
    return round(total, 2)


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
                    rush: bool = False, market: str | None = None, review_tier: str = "human") -> dict:
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
    rate = rate_for(service_level, market, review_tier)
    discount = PLANS[plan]["discount"]

    free_applied = min(minutes, max(0.0, free_minutes_available))
    billable_minutes = max(0.0, minutes - free_applied)
    gross = round(billable_minutes * rate, 2)
    discount_amount = round(gross * discount, 2)
    after_discount = round(gross - discount_amount, 2)

    credits_value_usd = credits_to_usd(max(0.0, wallet_credits_available))
    credits_applied_usd = round(min(after_discount, credits_value_usd), 2)
    credits_applied = round(usd_to_credits(credits_applied_usd), 2)

    addon_lines = [{"key": a, "name": ADDONS[a]["name"], "rate_per_min": addon_rate(a, review_tier),
                     "cost_usd": round(addon_rate(a, review_tier) * minutes, 2)} for a in addons]
    addon_cost = round(sum(line["cost_usd"] for line in addon_lines), 2)

    # See RUSH_SURCHARGE_PCT's comment - a % of the FULL, undiscounted
    # service value, applied even on minutes free/credits would otherwise
    # cover (real prioritization work, not a discount-eligible line item).
    full_service_value = round(minutes * rate, 2)
    rush_surcharge_usd = round(full_service_value * RUSH_SURCHARGE_PCT, 2) if rush else 0.0

    total = round(after_discount - credits_applied_usd + addon_cost + rush_surcharge_usd, 2)
    return {
        "minutes": round(minutes, 2), "rate_per_min": rate,
        "market": market or "legacy", "review_tier": review_tier,
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
           "price_usd": round(30 * SERVICE_LEVELS["dub"]["rate_per_min"] , 2)},
    "60": {"credits": round(60 * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR),
           "price_usd": round(60 * SERVICE_LEVELS["dub"]["rate_per_min"] , 2)},
    "150": {"credits": round(150 * SERVICE_LEVELS["dub"]["rate_per_min"] * CREDITS_PER_DOLLAR),
            "price_usd": round(150 * SERVICE_LEVELS["dub"]["rate_per_min"] , 2)},
}

# Same discount schedule as the fixed packages above, generalized to any
# client-chosen dollar amount - the tier boundaries and discounts match the
# fixed packages exactly (10%/15%/20%, at 30/60/150 minutes-of-dub-value
# equivalent spend), just not limited to those three exact amounts.
CREDIT_CUSTOM_MIN_MINUTES_EQUIV = 5
CREDIT_CUSTOM_MAX_MINUTES_EQUIV = 2000
_CREDIT_DISCOUNT_TIERS = [(0, 0.0)]          # no volume discount: the price is the same for everyone


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
    # Planning estimate for Transkriptor via the API (~$0.0035-0.033/min
    # depending on plan) - replace with the real rate once confirmed.
    "transkriptor": 0.015,
    # Lara Translate: ~$25 per 1M source characters; a minute of speech is
    # roughly 900-1,000 characters (much less in Chinese/Japanese/Korean).
    "lara": 0.025,
    "faster-whisper": 0.0, "stub": 0.0,
    "local": 0.0, "claude": 0.02,  # <- placeholder, replace with your real observed rate
    "piper": 0.0, "xtts": 0.0,
}


def test_client_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("KAULI_TEST_CLIENT_EMAILS", "").split(",") if e.strip()}


def _is_recurring(subscription) -> bool:
    try:
        return bool(subscription["paystack_subscription_code"])
    except (IndexError, KeyError):
        return False


def _grace_until(subscription) -> float:
    """While a renewal payment is failing the plan stays open until this time (see dunning.py)."""
    try:
        return subscription["grace_until"] or 0.0
    except (KeyError, IndexError):
        return 0.0


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
    grace = RECURRING_GRACE_DAYS * 86400 if _is_recurring(subscription) else 0
    if subscription["current_period_end"] and subscription["current_period_end"] + grace < time.time() and _grace_until(subscription) < time.time():
        return "free"  # lapsed - don't silently keep paid benefits past the period
    return subscription["plan"]


def _plan_in_force(subscription) -> str:
    """The plan a subscription row is actually delivering right now: its own plan while it is active and inside its
    period (plus the grace a renewing card gets), otherwise the free plan."""
    if subscription is None or subscription["status"] != "active":
        return "free"
    grace = RECURRING_GRACE_DAYS * 86400 if _is_recurring(subscription) else 0
    if subscription["current_period_end"] and subscription["current_period_end"] + grace < time.time() and _grace_until(subscription) < time.time():
        return "free"
    return subscription["plan"] if subscription["plan"] in PLANS else "free"


def _reward_minutes(subscription) -> float:
    """One-time referral minutes on a subscription row (0 where the row predates them)."""
    try:
        return subscription["reward_minutes"] or 0.0
    except (KeyError, IndexError):
        return 0.0


def free_minutes_remaining(subscription) -> float:
    """Instant AI transcription minutes left this month: the allowance of the plan in force (5 on the free plan,
    150 on Pro, 400 on Premium), plus any bonus_minutes an account manager has granted for onboarding (see
    db.grant_bonus_minutes), minus what has been used. Same counter and the same monthly reset for every plan, and
    a yearly plan gets a fresh allowance every month."""
    if subscription is None:
        return FREE_MINUTES_PER_MONTH
    used = subscription["minutes_used_this_period"] or 0.0
    bonus = (subscription["bonus_minutes"] or 0.0) + _reward_minutes(subscription)
    return max(0.0, plan_included_minutes(_plan_in_force(subscription)) + bonus - used)


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


def paystack_currency() -> str:
    """Whether USD charging is switched on: 'USD' once PAYSTACK_CURRENCY=USD
    is set (after enabling USD on the Paystack account), otherwise 'KES'. A
    Kenya-registered merchant account originally rejected USD charges
    ("Currency not supported by merchant"). Unset = KES, nothing changes."""
    return "USD" if os.environ.get("PAYSTACK_CURRENCY", "").strip().upper() == "USD" else "KES"


def paystack_currency_for_market(market: str | None, country: str | None = None) -> str:
    """The currency to actually charge THIS customer in. With USD switched on,
    US/international customers pay in dollars (what their card statement
    should show); Kenya & East Africa and legacy accounts stay on KES, since
    M-Pesa/mobile money only works in KES and those clients expect it."""
    if paystack_currency() == "USD":
        if country is None:
            return "USD" if market == "intl" else "KES"
        # Only a client in Kenya is charged in shillings; everyone else is charged in dollars.
        return "KES" if (country.strip().upper() == "KE" and market in ("ea", "legacy")) else "USD"
    return "KES"


def paystack_plan_code(plan: str, currency: str | None = None, yearly: bool = False) -> str | None:
    """Paystack plan code (PLN_...) for a subscription plan. Monthly: PAYSTACK_PLAN_PRO_USD / _KES (or the older
    unsuffixed PAYSTACK_PLAN_PRO). Yearly: PAYSTACK_PLAN_PRO_YEARLY_USD / _KES. Unset = that plan is still sold as
    a one-off purchase for its period, as before.

    A Paystack plan has one currency, so the plan made for the currency being charged wins, and a plan is never
    offered in a currency it was not made for: with only the other currency's plan set, the client is simply charged
    once. Write the codes with `python -m webapp.paystack_setup`."""
    p = plan.upper()
    if yearly:
        if currency:
            return os.environ.get(f"PAYSTACK_PLAN_{p}_YEARLY_{currency.upper()}", "").strip() or None
        return os.environ.get(f"PAYSTACK_PLAN_{p}_YEARLY", "").strip() or None
    if currency:
        specific = os.environ.get(f"PAYSTACK_PLAN_{p}_{currency.upper()}", "").strip()
        if specific:
            return specific
        if any(os.environ.get(f"PAYSTACK_PLAN_{p}_{c}", "").strip() for c in ("USD", "KES")):
            return None
    return (os.environ.get(f"PAYSTACK_PLAN_{p}", "").strip() or None)


def _plan_code_names(plan: str, yearly: bool) -> list[str]:
    p = plan.upper()
    if yearly:
        return [f"PAYSTACK_PLAN_{p}_YEARLY"] + [f"PAYSTACK_PLAN_{p}_YEARLY_{c}" for c in ("USD", "KES")]
    return [f"PAYSTACK_PLAN_{p}"] + [f"PAYSTACK_PLAN_{p}_{c}" for c in ("USD", "KES")]


def paystack_plan_codes(plan: str) -> set[str]:
    """Every Paystack plan code that means this plan, monthly or yearly - what a renewal webhook is matched against."""
    names = _plan_code_names(plan, False) + _plan_code_names(plan, True)
    return {v for v in (os.environ.get(n, "").strip() for n in names) if v}


def paystack_plan_is_yearly(code: str) -> bool:
    return any(os.environ.get(n, "").strip() == code for plan in PLANS for n in _plan_code_names(plan, True) if code)


# How long past current_period_end a RECURRING (Paystack-subscribed) account
# keeps its plan, so a renewal that lands a day late - or a 31-day month
# against our fixed 30-day period - never drops a paying client to Free.
RECURRING_GRACE_DAYS = 3


def paystack_initialize(email: str, amount_usd: float, reference: str, callback_url: str,
                        plan_code: str | None = None, currency: str | None = None) -> dict:
    """Returns {"authorization_url": ...} on success, {"error": ...} on
    failure - callers redirect the client to authorization_url, which is
    Paystack's own hosted checkout page. We never see or touch card data.

    `currency` is chosen per customer (see paystack_currency_for_market):
    USD charges the dollar amount as-is; KES converts with the same
    usd_to_kes() M-Pesa uses. Prices shown to clients are always USD.

    plan_code (a Paystack PLN_ code) turns the charge into a RECURRING
    subscription: Paystack then bills the card every month on its own. For
    a plan charge Paystack uses the plan's own amount/currency (it ignores
    ours), so the plan in the dashboard must be priced like PLANS."""
    key = _paystack_key()
    if not key:
        return {"error": "Paystack isn't configured (PAYSTACK_SECRET_KEY missing)."}
    currency = currency or paystack_currency()
    if currency == "USD":
        charge_amount = amount_usd
    else:
        charge_amount, _rate_source = usd_to_kes(amount_usd)
    body = {
        "email": email,
        "amount": int(round(charge_amount * 100)),  # Paystack wants the smallest unit
        "currency": currency,
        "reference": reference,
        "callback_url": callback_url,
    }
    if plan_code:
        body["plan"] = plan_code
    try:
        resp = httpx.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=10,
        )
        data = resp.json()
        if not data.get("status"):
            return {"error": data.get("message", "Paystack rejected the request.")}
        return {"authorization_url": data["data"]["authorization_url"]}
    except Exception as exc:
        return {"error": f"Couldn't reach Paystack: {exc}"}


def paystack_disable_subscription(subscription_code: str, email_token: str) -> dict:
    """Cancel a recurring Paystack subscription (stops future renewals; the
    client keeps what they've already paid for until the period ends)."""
    key = _paystack_key()
    if not key:
        return {"ok": False, "error": "Paystack isn't configured."}
    try:
        resp = httpx.post(
            "https://api.paystack.co/subscription/disable",
            headers={"Authorization": f"Bearer {key}"},
            json={"code": subscription_code, "token": email_token},
            timeout=10,
        )
        data = resp.json()
        if not data.get("status"):
            return {"ok": False, "error": data.get("message", "Paystack rejected the cancellation.")}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't reach Paystack: {exc}"}


def paystack_manage_link(subscription_code: str) -> str | None:
    """A secure Paystack page where the customer can change the card behind a subscription. None if Paystack will not give one."""
    key = _paystack_key()
    if not key or not subscription_code:
        return None
    try:
        resp = httpx.get(f"https://api.paystack.co/subscription/{subscription_code}/manage/link",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        data = resp.json()
        return (data.get("data") or {}).get("link") if data.get("status") else None
    except Exception:  # noqa: BLE001
        return None


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


def paystack_refund(transaction_reference: str) -> dict:
    """Real refund via Paystack's own /refund endpoint - a staff-only,
    manual, last-resort action (see app.py's staff_refund_trial_
    verification), not something a client ever triggers themselves.
    Amount omitted on purpose: Paystack defaults to refunding the FULL
    original transaction amount when it's left out, which is always
    correct here (the $1 trial-verification charge is never partially
    refunded). Refunds are queued for processing on Paystack's side, not
    instant - the real response reflects that ("pending"), which is
    surfaced to staff as-is rather than presented as already-done.
    Returns {"ok": True, "status": ..., "detail": dict} on success,
    {"ok": False, "error": ...} on failure."""
    key = _paystack_key()
    if not key:
        return {"ok": False, "error": "Paystack isn't configured (PAYSTACK_SECRET_KEY missing)."}
    try:
        resp = httpx.post(
            "https://api.paystack.co/refund",
            headers={"Authorization": f"Bearer {key}"},
            json={"transaction": transaction_reference},
            timeout=15,
        )
        data = resp.json()
        if not data.get("status"):
            return {"ok": False, "error": data.get("message", "Paystack rejected the refund request.")}
        refund_data = data.get("data", {})
        return {"ok": True, "status": refund_data.get("status", "pending"), "detail": refund_data}
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't reach Paystack: {exc}"}


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
