"""Where our own models are good enough to be used first, from measurement rather than hope.

scripts/eval_languages.py runs both models on real recordings and sentences in every language (FLEURS) and scripts/export_model_quality.py
saves the result next to this file (own_model_quality.json):
  asr  - word error rate of our own transcription model (character error rate for languages written without spaces); lower is better
  mt   - chrF of our own translation model, from English and into English; higher is better

A language is approved for Instant AI and the free minutes when speech is at or under ASR_MAX_ERROR, and a translation direction when its
score is at or over MT_MIN_CHRF. Anything not measured is not approved: those orders go to the outside providers as before.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ASR_MAX_ERROR = 0.25
MT_MIN_CHRF = 55.0
_FILE = Path(__file__).with_name("own_model_quality.json")


@lru_cache(maxsize=1)
def _data() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"asr": {}, "mt": {}}


def reload() -> None:
    _data.cache_clear()


def asr_languages() -> set[str]:
    """Languages our own transcription model is accurate enough in to go first."""
    return {lang for lang, err in _data().get("asr", {}).items() if err is not None and err <= ASR_MAX_ERROR}


def mt_ok(source_lang: str, target_lang: str) -> bool:
    """Our own translation model may go first for this direction. Pairs without English are not measured, so they are not approved. Swahili
    into English was checked by hand on real sentences and is good; English into Swahili and Kikuyu are not."""
    if target_lang in ("sw", "ki") or source_lang == "ki":
        return False
    mt = _data().get("mt", {})
    if target_lang == "en":
        if source_lang == "sw":
            return True
        return (mt.get(source_lang) or {}).get("to_en", 0) >= MT_MIN_CHRF if (mt.get(source_lang) or {}).get("to_en") is not None else False
    if source_lang == "en":
        score = (mt.get(target_lang) or {}).get("from_en")
        return score is not None and score >= MT_MIN_CHRF
    return False


def summary() -> dict:
    d = _data()
    return {"measured": d.get("measured", ""), "asr_ok": sorted(asr_languages()),
            "mt_from_en": sorted(l for l in d.get("mt", {}) if mt_ok("en", l)), "mt_to_en": sorted(l for l in d.get("mt", {}) if mt_ok(l, "en")) + ["sw"]}
