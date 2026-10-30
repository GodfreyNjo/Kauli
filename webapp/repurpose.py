"""Do more with a file the customer already sent: translate it, dub it or have a person check it, without uploading it again.

The suggestions come from what the order already is (a transcript can become a translation or a dub; a translation can become a dub or
another language; an Instant AI result can be upgraded to human review) and from what this customer usually orders (their most used
target languages come first). Each one is a single button that creates a normal order from the stored file (see create_order's
reuse_order_id), priced the usual way, so nothing about billing, free minutes or review changes.
"""
from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import billing, db

router = APIRouter()
A = None

COMMON_TARGETS = ("en", "sw", "es", "fr", "de", "pt", "ar", "hi", "zh", "it")


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def _languages_for(source_lang: str, level: str) -> list[tuple[str, str]]:
    """Target languages Instant AI can produce for this source: dubs only into English and Swahili, translation into any."""
    names = A.LANGUAGE_NAMES
    if level == "dub":
        codes = [c for c in ("en", "sw") if c in names]
    else:
        codes = [c for c in names if c != "ki"]
    return [(c, names[c]) for c in codes if c != source_lang]


def _ranked(codes: list[str], user_id: str) -> list[str]:
    """The customer's own favourite targets first, then their default, then the common ones, then the rest."""
    seen = Counter(o["target_lang"] for o in db.list_orders_for_client(user_id) if o["target_lang"])
    row = db.get_user(user_id)
    default = row["default_target_lang"] if row is not None and "default_target_lang" in row.keys() else None
    prefer = [c for c, _ in seen.most_common()] + ([default] if default else []) + list(COMMON_TARGETS)
    rank = {c: i for i, c in enumerate(dict.fromkeys(prefer))}
    return sorted(codes, key=lambda c: (rank.get(c, 999), c))


def _quote(minutes: float, level: str, tier: str, user, subscription, market) -> dict:
    plan = billing.effective_plan(user, subscription)
    free = 0.0
    if billing.allowance_covers(plan, level, tier):
        free = billing.free_minutes_remaining(subscription) if subscription else 0.0
    cost = billing.order_cost_usd(minutes, level, plan, free, market=market, review_tier=tier)
    return {"usd": cost["total_usd"], "included": cost["total_usd"] <= 0 and cost["free_minutes_applied"] > 0}


def suggestions(order, user) -> list[dict]:
    """What this order could become next, each with a price: [{key, title, blurb, level, tier, languages, usd, included, button}]"""
    source = order["source_lang"]
    level = order["service_level"] or "transcribe"
    minutes = float(order["duration_minutes"] or 0.0)
    scope = user["client_scope_id"]
    subscription = db.get_subscription_current(scope)
    market = db.get_billing_market(scope)
    ai_only_source = source in A.AI_ONLY_LANGUAGES
    out: list[dict] = []

    def add(key, title, blurb, new_level, tier, button):
        if tier == "human" and ai_only_source:
            return                                                  # there is no reviewer for the wider languages
        langs: list[tuple[str, str]] = []
        if new_level != "transcribe":
            if new_level == "dub" and ai_only_source:
                return
            if new_level == "translate" and not A._lara_configured():
                return
            names = {c: n for c, n in _languages_for(source, new_level) if not (key == "another" and c == order["target_lang"])}
            if not names:
                return
            langs = [(c, names[c]) for c in _ranked(list(names), scope)]
        q = _quote(minutes, new_level, tier, user, subscription, market)
        out.append({"key": key, "title": title, "blurb": blurb, "level": new_level, "tier": tier, "languages": langs,
                    "usd": q["usd"], "included": q["included"], "button": button})

    if level == "transcribe":
        add("translate", "Translate it", "Get the transcript in another language.", "translate", "ai", "Translate")
        add("dub", "Dub it", "A spoken version in English or Swahili, in a natural voice.", "dub", "ai", "Dub")
    elif level == "translate":
        add("another", "Translate into another language", "Same file, another language. No upload needed.", "translate", "ai", "Translate")
        add("dub", "Turn it into a dub", "Hear it in English or Swahili.", "dub", "ai", "Dub")
    else:
        add("another", "Translate into another language", "Add a transcript and translation in a language you have not ordered yet.", "translate", "ai", "Translate")
    if level == "transcribe" and order["review_tier"] == "ai" and not ai_only_source:
        add("review", "Have it checked by a person", "A dedicated editor checks the transcript against your audio.", "transcribe", "human", "Order review")
    return out


@router.get("/client/orders/{order_id}/more")
def more_from_this_file(request: Request, order_id: str):
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next=/client/orders/{order_id}/more", status_code=303)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    has_file = bool(order["source_youtube_id"]) or (bool(order["audio_path"]) and Path(order["audio_path"]).is_file())
    if order["status"] in ("pending_payment", "failed", "dead_letter") or not has_file:
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)
    return A.templates.TemplateResponse(request, "repurpose.html", {
        "user": user, "order": order, "cards": suggestions(order, user), "language_names": A.LANGUAGE_NAMES,
        "key_prefix": uuid.uuid4().hex[:12],
    })
