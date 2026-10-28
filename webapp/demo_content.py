"""Content for the interactive "Hear Kauli" demo (/try).

The audio is rendered once with ElevenLabs by scripts/make_demo_audio.py and shipped as static files, so a visitor
playing the demo costs nothing and cannot spend our ElevenLabs quota. Every line below is the exact text that was
spoken or captioned, which keeps the captions honest.
"""
from __future__ import annotations

# What a visitor is probably making -> the Kauli service that fits it.
SCENARIOS = {
    "meeting": {
        "label": "Meetings and interviews", "service": "transcribe", "voice": "EXAVITQu4vr4xnSDxMaL",
        "service_label": "Transcription", "speaker": "Speaker 1",
        "headline": "Every word captured, with speakers and timestamps",
        "headline_en": "Every word captured, and an English translation beside it",
        "en": "Thanks everyone for joining. Our launch moves to the fifteenth of March, so marketing needs the final assets by next Friday. Priya will share the updated budget this afternoon.",
        "tr": {
            "es": "Gracias a todos por unirse. Nuestro lanzamiento se mueve al quince de marzo, así que marketing necesita los materiales finales para el próximo viernes. Priya compartirá el presupuesto actualizado esta tarde.",
            "de": "Danke an alle für die Teilnahme. Unser Launch wird auf den fünfzehnten März verschoben, daher braucht das Marketing die finalen Materialien bis nächsten Freitag. Priya teilt heute Nachmittag das aktualisierte Budget.",
            "zh": "感谢大家参加。我们的发布时间推迟到三月十五日，所以市场部需要在下周五之前拿到最终素材。普丽娅今天下午会分享更新后的预算。",
            "sw": "Asanteni nyote kwa kujiunga. Uzinduzi wetu umehamishwa hadi tarehe kumi na tano Machi, kwa hiyo idara ya masoko inahitaji nyenzo za mwisho kufikia Ijumaa ijayo. Priya atashiriki bajeti iliyosasishwa mchana huu.",
        },
    },
    "course": {
        "label": "Courses and training", "service": "translate", "voice": "Xb7hH8MSUJpSbSDYk0k2",
        "service_label": "Transcription + translation", "speaker": "Instructor",
        "headline": "Subtitles your learners can read in their own language",
        "headline_en": "English subtitles on a lesson in another language",
        "en": "Welcome to lesson one. Today you will learn how to read a balance sheet, step by step, even if you have never worked with numbers before.",
        "tr": {
            "es": "Bienvenidos a la primera lección. Hoy aprenderás a leer un balance general paso a paso, incluso si nunca has trabajado con números.",
            "de": "Willkommen zu Lektion eins. Heute lernst du Schritt für Schritt, wie man eine Bilanz liest, auch wenn du noch nie mit Zahlen gearbeitet hast.",
            "zh": "欢迎来到第一课。今天你将一步一步学习如何阅读资产负债表，即使你以前从未接触过数字。",
            "sw": "Karibu kwenye somo la kwanza. Leo utajifunza jinsi ya kusoma mizania hatua kwa hatua, hata kama hujawahi kufanya kazi na namba.",
        },
    },
    "video": {
        "label": "Marketing and creator videos", "service": "dub", "voice": "TX3LPaxmHKxFdv7VOQHJ",
        "service_label": "Full dubbing", "speaker": "Narrator",
        "headline": "The same video, spoken in a new language",
        "headline_en": "The same video, now spoken in English",
        "en": "Meet the new Aurora backpack. Waterproof, lightweight, and built to last for every journey. Order today and get free delivery.",
        "tr": {
            "es": "Conoce la nueva mochila Aurora. Impermeable, ligera y hecha para durar en cada viaje. Pídela hoy y recibe envío gratis.",
            "de": "Lernen Sie den neuen Aurora-Rucksack kennen. Wasserdicht, leicht und für jede Reise gemacht. Bestellen Sie heute und erhalten Sie kostenlosen Versand.",
            "zh": "全新极光背包闪亮登场。防水、轻便，为每一次旅程而生。今天下单，即可享受免费配送。",
            "sw": "Kutana na mkoba mpya wa Aurora. Haupitishi maji, ni mwepesi, na umetengenezwa kudumu katika kila safari. Agiza leo upate usafirishaji bila malipo.",
        },
    },
}

# Languages a visitor can translate into. `dot` is the badge used when there is no flag drawn for the language.
LANGUAGES = {
    "es": {"name": "Spanish", "native": "Español", "flag": "flag-es"},
    "de": {"name": "German", "native": "Deutsch", "flag": "flag-de"},
    "zh": {"name": "Chinese", "native": "中文", "flag": ""},
    "sw": {"name": "Swahili", "native": "Kiswahili", "flag": "flag-ke"},
}

# Visitor country (Cloudflare's CF-IPCountry) -> the language they are most likely to want.
COUNTRY_LANGUAGE = {
    **{c: "es" for c in ("ES", "MX", "AR", "CO", "CL", "PE", "VE", "EC", "GT", "CU", "BO", "DO", "HN", "PY", "SV", "NI",
                          "CR", "PA", "UY", "PR", "US")},
    **{c: "de" for c in ("DE", "AT", "CH", "LI", "LU")},
    **{c: "zh" for c in ("CN", "TW", "HK", "MO", "SG")},
    **{c: "sw" for c in ("KE", "TZ", "UG", "RW", "BI", "CD")},
}
# The first word of a browser's Accept-Language, when the country gives nothing.
BROWSER_LANGUAGE = {"es": "es", "de": "de", "zh": "zh", "sw": "sw"}

# Where the visitor came from -> what they are probably making.
REFERRER_SCENARIO = (
    (("podcast", "interview", "meeting", "webinar", "audio-to-text", "transcri"), "meeting"),
    (("course", "e-learning", "elearning", "training", "subtitle", "caption", "translation"), "course"),
    (("youtube", "video", "dubbing", "voice-over", "voiceover", "localization", "localisation", "marketing"), "video"),
)


def audio_name(scenario: str, lang: str | None) -> str:
    """The static file for a clip: the English source, or the dubbed version."""
    return f"src-{scenario}.mp3" if lang is None else f"dub-{scenario}-{lang}.mp3"


def audio_in_name(scenario: str, lang: str) -> str:
    """The clip spoken in the visitor's own language, for the "into English" direction. For the dubbing scenario that is the
    dubbed clip itself; the other two scenarios have their own recordings."""
    return audio_name(scenario, lang) if SCENARIOS[scenario]["service"] == "dub" else f"in-{scenario}-{lang}.mp3"


def all_clips() -> list[tuple[str, str, str | None, str]]:
    """(file name, voice id, language or None, text) for every clip the demo plays."""
    clips = []
    for key, s in SCENARIOS.items():
        clips.append((audio_name(key, None), s["voice"], None, s["en"]))
        for lang, text in s["tr"].items():
            clips.append((audio_in_name(key, lang) if s["service"] != "dub" else audio_name(key, lang), s["voice"], lang, text))
    return clips


def pick_language(country: str | None, accept_language: str | None, asked: str | None) -> tuple[str, str]:
    """(language, why) - an explicit choice wins, then the visitor's country, then their browser language."""
    if asked in LANGUAGES:
        return asked, "chosen"
    code = COUNTRY_LANGUAGE.get((country or "").strip().upper())
    if code:
        return code, "location"
    first = (accept_language or "").split(",")[0].strip().lower().split("-")[0]
    if first in BROWSER_LANGUAGE:
        return BROWSER_LANGUAGE[first], "browser"
    return "es", "default"


def pick_scenario(referer: str | None, asked: str | None) -> tuple[str, str]:
    """(scenario, why) - an explicit choice wins, then the page the visitor came from."""
    if asked in SCENARIOS:
        return asked, "chosen"
    ref = (referer or "").lower()
    for words, scenario in REFERRER_SCENARIO:
        if any(w in ref for w in words):
            return scenario, "page"
    return "meeting", "default"
