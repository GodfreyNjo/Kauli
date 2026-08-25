"""Turnaround-time (deadline) calculation - a real, deterministic formula
from known variables (plan tier, service level, audio duration), not a
guess and not an AI estimate. Numbers below are a defensible starting
point (tier sets how fast you're promised results, service level and
duration set how much actual work - AI + human review - that takes), not
observed production data - there isn't any yet. Revisit these constants
once you have real completion times to compare against, same as
billing.ADDONS' manual_transcription rate comment.

Two deadlines are computed for every order, matching how Rev/3Play
actually run this:
  - internal_deadline_at: the real hard stop staff work against.
  - deadline_at (client-facing): internal + a buffer, so a normal day
    delivers EARLY rather than exactly on time - the buffer is the safety
    margin for a slow file, a sick reviewer, or a retry, not padding to
    hide behind.

Explicitly NOT built here (see chat): multi-worker auto-dispatch/load
balancing, or a "historical accuracy score" per reviewer - this app has
one operator and one background worker thread (see worker.py), not a pool
to route between.

Rush IS built (as of 2026-08-23, see billing.RUSH_SURCHARGE_PCT): a
client-chosen surcharge in exchange for real, manual queue priority - the
operator picking that order up and working it first - not a faster
pipeline, since there still isn't a worker pool to actually parallelize
across.
"""
from __future__ import annotations

import time

# Hours promised from "processing actually starts" (payment confirmed, or
# immediately for a $0 order) to client-facing delivery, before the
# service-level/duration add-on below - matches billing.PLANS' existing
# tiers, cheapest/slowest to most dedicated/fastest.
TIER_BASE_HOURS = {
    "free": 72.0,
    "pro": 72.0,
    "premium": 36.0,
    "enterprise": 18.0,
}

# How much of the base window a given service level actually needs -
# transcription-only is ASR + a light read-through; a full dub is ASR + MT
# + TTS + a real quality pass on the rendered audio.
SERVICE_LEVEL_TAT_MULTIPLIER = {
    "transcribe": 0.5,
    "translate": 0.75,
    "dub": 1.0,
}

# Real human-review time added on top of the base window, scaled by the
# file's actual length - a 2-hour file needs more reviewer minutes than a
# 10-minute one, whatever the tier. Minutes of review per audio-minute.
REVIEW_MINUTES_PER_AUDIO_MINUTE = {
    "transcribe": 0.15,
    "translate": 0.35,
    "dub": 0.60,
}

# The client-facing promise is set this much later than the real internal
# deadline - the "always deliver early" margin, not a lie about how long
# it takes.
CLIENT_BUFFER_PCT = 0.20

MIN_TAT_HOURS = 2.0  # a floor so a tiny file on the fastest tier doesn't promise "0 hours"

# The staff queue's "time in stage" flag (see staff_dashboard.html) - a
# flat, same-for-every-order threshold, not tier/service-aware like the
# real deadline above. That's deliberate: this isn't "will this miss its
# deadline" (the deadline pill already covers that per-order), it's "has
# this been sitting untouched in an active stage longer than ANY order
# reasonably should before someone looks at it" - a bottleneck alarm, not
# a second deadline system. A round starting guess, same as everything
# else in this file - revisit once you have real observed stage times.
STUCK_STAGE_THRESHOLD_SECONDS = 6 * 3600

# How much a paid rush surcharge (billing.RUSH_SURCHARGE_PCT) actually buys
# in turnaround - half the normal computed window, applied before the
# floor/buffer below so a rush order still gets a real, honest MIN_TAT_HOURS
# floor and client-facing buffer, not an unrealistic near-zero promise.
RUSH_TAT_MULTIPLIER = 0.5


def compute_deadlines(tier: str, service_level: str, duration_minutes: float,
                       start_at: float | None = None, rush: bool = False) -> dict:
    """start_at is when the clock actually starts - processing start
    (payment confirmed, or immediately for a free/fully-covered order),
    not order submission time; you can't promise a turnaround on money
    you don't have yet. Returns internal_deadline_at, deadline_at
    (client-facing), and the hour figures behind them, for transparency."""
    start_at = start_at if start_at is not None else time.time()
    base = TIER_BASE_HOURS.get(tier, TIER_BASE_HOURS["free"])
    multiplier = SERVICE_LEVEL_TAT_MULTIPLIER.get(service_level, 1.0)
    review_rate = REVIEW_MINUTES_PER_AUDIO_MINUTE.get(service_level, 0.6)
    duration_component_hours = (max(0.0, duration_minutes) * review_rate) / 60.0
    raw_hours = base * multiplier + duration_component_hours
    if rush:
        raw_hours *= RUSH_TAT_MULTIPLIER
    internal_tat_hours = max(MIN_TAT_HOURS, raw_hours)
    client_tat_hours = internal_tat_hours * (1 + CLIENT_BUFFER_PCT)
    return {
        "start_at": start_at,
        "internal_tat_hours": round(internal_tat_hours, 2),
        "client_tat_hours": round(client_tat_hours, 2),
        "internal_deadline_at": start_at + internal_tat_hours * 3600,
        "deadline_at": start_at + client_tat_hours * 3600,
        "rush": rush,
    }


def time_status(start_at: float | None, deadline_at: float | None, now: float | None = None) -> dict | None:
    """For the client-facing time bar - None if there's no deadline on
    file (an order created before this existed, or one still sitting in
    pending_payment with no clock started yet). 'level' drives the color:
    green (on track) / yellow (a quarter of the window left) / red (10%
    left or already overdue) - measured against the ORIGINAL window
    (deadline_at - start_at), not just raw time-until-deadline, so a
    72-hour order doesn't read as "red" the instant 8 hours have passed."""
    if not deadline_at or not start_at:
        return None
    now = now if now is not None else time.time()
    total_s = max(1.0, deadline_at - start_at)
    remaining_s = deadline_at - now
    pct_remaining = max(0.0, remaining_s / total_s)
    if remaining_s <= 0:
        level = "red"
    elif pct_remaining <= 0.10:
        level = "red"
    elif pct_remaining <= 0.25:
        level = "yellow"
    else:
        level = "green"
    return {
        "level": level, "overdue": remaining_s <= 0,
        "remaining_s": max(0.0, remaining_s), "pct_remaining": round(pct_remaining * 100, 1),
    }
