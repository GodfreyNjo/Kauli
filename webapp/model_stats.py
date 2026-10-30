"""How the AI providers (including our own models) and the human reviewers are actually doing, for staff.

Everything here is read from work that really happened, nothing is estimated except our cost per minute (which staff set on the Providers page):

  usage     which provider handled each step of each order in the period (the one that actually ran, so a fallback counts for the
            provider that took over), the minutes it handled, how often it failed, and the model's own confidence in its output
  accuracy  for human-reviewed orders, the AI draft is compared word by word with what the reviewer delivered. "Accuracy" is the share of
            words the reviewer left alone, so a provider that needs fewer corrections scores higher. It is only as good as the review:
            orders nobody changed count as right. Providers with too little reviewed work show no figure rather than a shaky one.
  reviewers how many human-reviewed orders, how long they take, how much of the AI draft gets changed, and how customers rate them
"""
from __future__ import annotations

import re
import statistics
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from . import db, provider_health

router = APIRouter()
A = None

DAY = 86400
DONE = ("ready_for_delivery", "delivered")
FAILED = ("failed", "dead_letter")
MIN_WORDS_FOR_ACCURACY = 300          # fewer reviewed words than this and a percentage would say more than the data does
_WORD = re.compile(r"\w+", re.UNICODE)
_cache: dict[tuple, dict] = {}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------------------ word comparison
def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())[:400]


def word_errors(draft: str, final: str) -> tuple[int, int]:
    """(words changed, words in the final text): the word-level edit distance between what the AI wrote and what was delivered."""
    a, b = _words(draft), _words(final)
    if not b:
        return len(a), max(len(a), 1) if a else 0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1], len(b)


def _name(raw: str | None) -> str:
    """The provider as it ran: 'groq-whisper', 'own', 'lara'... a chain such as 'own>groq-whisper' counts for the last that ran."""
    raw = (raw or "").strip().lower()
    return raw.split(">")[-1].split("+")[-1] if raw else "stub"


# ------------------------------------------------------------------------------------------------------------ one order
def _record(order) -> dict:
    key = (order["id"], order["status"], order["status_changed_at"])
    hit = _cache.get(key)
    if hit is not None:
        return hit
    rec = {"asr": _name(order["asr"]), "mt": _name(order["mt"]), "tts": _name(order["tts"]), "minutes": float(order["duration_minutes"] or 0.0),
           "reviewed": order["review_tier"] == "human" and order["status"] in DONE, "asr_conf": None, "mt_conf": None, "segments": 0,
           "edited_src": 0, "edited_tgt": 0, "asr_err": 0, "asr_words": 0, "mt_err": 0, "mt_words": 0, "fallback": False}
    job = A._load_job(order) if A and order["outdir"] else None
    if job is not None:
        providers = getattr(job, "providers", None) or {}
        for step in ("asr", "mt", "tts"):
            if providers.get(step):
                actual = _name(providers[step])
                rec["fallback"] = rec["fallback"] or (actual != rec[step])
                rec[step] = actual
        speech = [s for s in job.segments if getattr(s, "segment_type", "speech") == "speech"]
        rec["segments"] = len(speech)
        confs = [s.source_confidence for s in speech if s.source_confidence]
        rec["asr_conf"] = statistics.fmean(confs) if confs else None
        mconfs = [s.translation_confidence for s in speech if s.translation_confidence]
        rec["mt_conf"] = statistics.fmean(mconfs) if mconfs else None
        for s in speech:
            src_edited = bool(s.source_edited_transcript) and s.source_edited_transcript != s.source_transcript
            drafted = s.literal if s.selected == "literal" else (s.spoken or s.literal)
            tgt_edited = bool(s.edited_text) and s.edited_text != drafted
            rec["edited_src"] += src_edited
            rec["edited_tgt"] += tgt_edited
            if rec["reviewed"]:
                e, n = word_errors(s.source_transcript, s.source_final_text)
                rec["asr_err"] += e
                rec["asr_words"] += n
                if rec["mt"] not in ("stub", "") and drafted:
                    e, n = word_errors(drafted, s.final_text)
                    rec["mt_err"] += e
                    rec["mt_words"] += n
    _cache[key] = rec
    if len(_cache) > 5000:
        _cache.clear()
    return rec


def _orders(days: int, now: float | None = None) -> list:
    now = now or time.time()
    conn = db.get_conn()
    try:
        return conn.execute("SELECT * FROM orders WHERE created_at >= ? ORDER BY created_at", (now - days * DAY,)).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------------------ the numbers
def _provider_table(rows: list[tuple], step: str, human_only_accuracy: bool = True) -> list[dict]:
    total_minutes = sum(r["minutes"] for _, r in rows) or 1.0
    groups: dict[str, list] = {}
    for order, rec in rows:
        if rec[step] in ("stub", ""):
            continue
        groups.setdefault(rec[step], []).append((order, rec))
    out = []
    for name, items in groups.items():
        minutes = sum(r["minutes"] for _, r in items)
        cost = provider_health.cost_per_min(name)
        err_key, words_key = ("asr_err", "asr_words") if step == "asr" else ("mt_err", "mt_words")
        words = sum(r.get(words_key, 0) for _, r in items)
        errs = sum(r.get(err_key, 0) for _, r in items)
        conf_key = "asr_conf" if step == "asr" else "mt_conf"
        confs = [r[conf_key] for _, r in items if r.get(conf_key) is not None]
        reviewed_orders = sum(1 for _, r in items if r["reviewed"] and r.get(words_key, 0))
        out.append({
            "name": name, "label": provider_health.NAMES.get(name, name), "orders": len(items), "minutes": round(minutes, 1),
            "share": round(100 * minutes / total_minutes, 1) if step != "tts" else None,
            "cost": round(cost * minutes, 2) if cost is not None else None, "cost_per_min": cost,
            "failed": sum(1 for o, _ in items if o["status"] in FAILED), "fallbacks": sum(1 for _, r in items if r["fallback"]),
            "confidence": round(100 * statistics.fmean(confs), 1) if confs else None,
            "reviewed_orders": reviewed_orders, "reviewed_words": words,
            "accuracy": round(100 * max(0.0, 1 - errs / words), 1) if words >= MIN_WORDS_FOR_ACCURACY else None,
        })
    return sorted(out, key=lambda r: -r["minutes"])


def _own_models(days: int, asr_rows: list[dict], mt_rows: list[dict]) -> dict:
    from kauli.providers import own_quality
    data = own_quality._data()
    asr = sorted(((lang, err) for lang, err in data.get("asr", {}).items() if err is not None), key=lambda x: x[1])
    mt = data.get("mt", {})
    own_names = {"own", "faster-whisper", "madlad"}
    return {
        "measured": data.get("measured", ""), "asr_threshold": own_quality.ASR_MAX_ERROR, "mt_threshold": own_quality.MT_MIN_CHRF,
        "asr_total": len(asr), "asr_approved": sum(1 for _, e in asr if e <= own_quality.ASR_MAX_ERROR),
        "asr_best": asr[:6], "asr_worst": asr[-6:][::-1] if len(asr) > 6 else [],
        "mt_from_en": sum(1 for v in mt.values() if (v or {}).get("from_en") is not None and v["from_en"] >= own_quality.MT_MIN_CHRF),
        "mt_to_en": sum(1 for v in mt.values() if (v or {}).get("to_en") is not None and v["to_en"] >= own_quality.MT_MIN_CHRF),
        "mt_total": len(mt), "model": provider_health.own_model_status(),
        "asr_share": round(sum(r["share"] or 0 for r in asr_rows if r["name"] in own_names), 1),
        "mt_share": round(sum(r["share"] or 0 for r in mt_rows if r["name"] in own_names), 1),
    }


def human_review(days: int = 30, now: float | None = None) -> dict:
    now = now or time.time()
    orders = [o for o in _orders(days, now) if o["review_tier"] == "human"]
    done = [o for o in orders if o["status"] in DONE]
    recs = [(o, _record(o)) for o in done]
    turn = sorted((o["status_changed_at"] - o["created_at"]) / 3600.0 for o in done if o["status_changed_at"] and o["created_at"])
    words = sum(r["asr_words"] + r["mt_words"] for _, r in recs)
    errs = sum(r["asr_err"] + r["mt_err"] for _, r in recs)
    segs = sum(r["segments"] for _, r in recs)
    changed = sum(max(r["edited_src"], 0) + r["edited_tgt"] for _, r in recs)
    pairs: dict[tuple, dict] = {}
    for o, r in recs:
        p = pairs.setdefault((o["source_lang"], o["target_lang"]), {"orders": 0, "minutes": 0.0, "words": 0, "errs": 0})
        p["orders"] += 1
        p["minutes"] += r["minutes"]
        p["words"] += r["asr_words"] + r["mt_words"]
        p["errs"] += r["asr_err"] + r["mt_err"]
    conn = db.get_conn()
    try:
        rate = conn.execute(
            "SELECT o.review_tier AS tier, COUNT(*) AS n, AVG(c.score) AS avg FROM csat_responses c JOIN orders o ON o.id = c.order_id "
            "WHERE c.created_at >= ? GROUP BY o.review_tier", (now - days * DAY,)).fetchall()
    finally:
        conn.close()
    csat = {r["tier"]: {"n": r["n"], "avg": round(r["avg"], 2)} for r in rate}
    return {
        "orders": len(orders), "delivered": len(done), "in_review": sum(1 for o in orders if o["status"] == "awaiting_review"),
        "returned": sum(1 for o in orders if o["status"] in ("returned_to_client", "editor_returned")), "rush": sum(1 for o in orders if o["is_rush"]),
        "minutes": round(sum(float(o["duration_minutes"] or 0) for o in orders), 1),
        "median_hours": round(statistics.median(turn), 1) if turn else None,
        "within_day": round(100 * sum(1 for t in turn if t <= 24) / len(turn), 1) if turn else None,
        "ai_accuracy": round(100 * max(0.0, 1 - errs / words), 1) if words >= MIN_WORDS_FOR_ACCURACY else None, "reviewed_words": words,
        "segments": segs, "segments_changed": changed, "change_rate": round(100 * changed / segs, 1) if segs else None,
        "pairs": sorted(({"source": s, "target": t, "orders": v["orders"], "minutes": round(v["minutes"], 1),
                          "accuracy": round(100 * max(0.0, 1 - v["errs"] / v["words"]), 1) if v["words"] >= MIN_WORDS_FOR_ACCURACY else None}
                         for (s, t), v in pairs.items()), key=lambda x: -x["minutes"]),
        "csat": csat,
    }


def snapshot(days: int = 30, now: float | None = None) -> dict:
    orders = _orders(days, now)
    rows = [(o, _record(o)) for o in orders]
    asr = _provider_table(rows, "asr")
    mt = _provider_table(rows, "mt")
    tts = _provider_table(rows, "tts")
    ai = [o for o in orders if o["review_tier"] == "ai"]
    reviewed_words = sum(r["asr_words"] + r["mt_words"] for _, r in rows)
    reviewed_errs = sum(r["asr_err"] + r["mt_err"] for _, r in rows)
    return {
        "days": days, "orders": len(orders), "minutes": round(sum(r["minutes"] for _, r in rows), 1), "instant_ai": len(ai),
        "failed": sum(1 for o in orders if o["status"] in FAILED), "fallbacks": sum(1 for _, r in rows if r["fallback"]),
        "ai_accuracy": round(100 * max(0.0, 1 - reviewed_errs / reviewed_words), 1) if reviewed_words >= MIN_WORDS_FOR_ACCURACY else None,
        "asr": asr, "mt": mt, "tts": tts, "own": _own_models(days, asr, mt), "human": human_review(days, now),
        "min_words": MIN_WORDS_FOR_ACCURACY,
    }


@router.get("/staff/models")
def staff_models(request: Request, days: int = 30):
    user = A.current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    days = days if days in (7, 30, 90, 365) else 30
    return A.templates.TemplateResponse(request, "staff_models.html", {"user": user, "s": snapshot(days), "days": days})
