"""Duration fitting. This is the part that makes a dub sound like a dub
rather than a voiceover that trails off the end of the shot."""
from __future__ import annotations

# Chars-per-second at a neutral speaking rate, per target language.
# THESE ARE PLACEHOLDERS. Run `kauli calibrate` against your actual TTS voice
# and replace them — a generic constant will cost you 10-15% fit rate.
DEFAULT_CPS = {
    # Calibrated 2026-08-19 against en_GB-alba-medium via `kauli calibrate`
    # (mean of 5 sample sentences: 16.94 cps). Re-run if you switch voices.
    "en": 16.94,
    "sw": 12.5,   # Swahili renders longer; expect to compress on en->sw
}

FIT_MIN = 0.90
FIT_MAX = 1.10
MAX_STRETCH_PCT = 8.0


def estimate_duration_ms(text: str, lang: str = "en", cps: float | None = None) -> int:
    rate = cps or DEFAULT_CPS.get(lang, 14.0)
    return int(round(len(text) / rate * 1000))


def fit_ratio(text: str, budget_ms: int, lang: str = "en", cps: float | None = None) -> float:
    if budget_ms <= 0:
        return 0.0
    return round(estimate_duration_ms(text, lang, cps) / budget_ms, 3)


def fit_status(ratio: float) -> str:
    if ratio == 0:
        return "unknown"
    if FIT_MIN <= ratio <= FIT_MAX:
        return "fits"
    if ratio > FIT_MAX:
        # Can a legal time-stretch rescue it?
        if ratio <= FIT_MAX * (1 + MAX_STRETCH_PCT / 100):
            return "needs_shortening"
        return "unfittable"
    return "needs_lengthening"


def choose_candidate(
    candidates: list[dict],
    budget_ms: int,
    lang: str = "en",
    cps: float | None = None,
    min_similarity: float = 0.85,
) -> tuple[dict, list[dict]]:
    """Pick the candidate that fits the budget while preserving meaning.

    candidates: [{"text": str, "similarity": float}, ...]
    Returns (chosen, scored_all).
    """
    scored = []
    for c in candidates:
        if not c.get("text"):
            continue
        est = estimate_duration_ms(c["text"], lang, cps)
        ratio = round(est / budget_ms, 3) if budget_ms else 0.0
        scored.append({
            **c,
            "est_duration_ms": est,
            "fit_ratio": ratio,
            "fit_status": fit_status(ratio),
        })

    if not scored:
        return {"text": "", "fit_ratio": 0.0, "fit_status": "unknown",
                "est_duration_ms": 0, "similarity": 0.0}, []

    # 1. Prefer candidates that fit AND preserve meaning, best meaning wins.
    fits = [c for c in scored
            if c["fit_status"] == "fits" and c.get("similarity", 1.0) >= min_similarity]
    if fits:
        return max(fits, key=lambda c: c.get("similarity", 1.0)), scored

    # 2. Otherwise take whatever is closest to 1.0 — never silently drop content.
    closest = min(scored, key=lambda c: abs(c["fit_ratio"] - 1.0))
    return closest, scored


def required_stretch_pct(est_ms: int, budget_ms: int) -> float:
    """Signed. Positive = must slow down. Clamped by the caller to MAX_STRETCH_PCT."""
    if not est_ms or not budget_ms:
        return 0.0
    return round((budget_ms - est_ms) / est_ms * 100, 2)
