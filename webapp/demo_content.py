"""Content for the interactive "Hear Kauli" demo (/try).

The audio is rendered once with ElevenLabs by scripts/make_demo_audio.py and shipped as static files, so a visitor
playing the demo costs nothing and cannot spend our ElevenLabs quota. Every line below is the exact text that was
spoken or captioned, which keeps the captions honest.
"""
from __future__ import annotations

# What a visitor is probably making -> the Kauli service that fits it. Each scenario has a cast: a person on screen for every voice,
# and the same person for the same voice, so a man's voice is always a man on screen and a woman's a woman.
# Text with more than one speaker is written one turn to a line, "Speaker 1: ...", the way Kauli labels speakers in a transcript;
# the label is never spoken or burned into the picture as part of the sentence.
SCENARIOS = {
    "meeting": {
        "label": "Meetings and calls", "service": "transcribe", "service_label": "Transcription with speaker IDs",
        "cast": [
            {"name": "Speaker 1", "voice": "EXAVITQu4vr4xnSDxMaL", "gender": "f"},     # Sarah
            {"name": "Speaker 2", "voice": "iP95p4xoKVk53GoZ742B", "gender": "m"},     # Chris
        ],
        "speaker": "Speaker 1",
        "headline": "Every word captured, with speaker IDs and timestamps",
        "headline_en": "Every word captured, and an English translation beside it",
        "en": "Speaker 1: Thanks everyone for joining. Our launch moves to the fifteenth of March.\nSpeaker 2: Understood. Marketing needs the final assets by next Friday, and I will share the budget this afternoon.",
        "tr": {
            "es": "Speaker 1: Gracias a todos por unirse. Nuestro lanzamiento se mueve al quince de marzo.\nSpeaker 2: Entendido. Marketing necesita los materiales finales para el próximo viernes, y compartiré el presupuesto esta tarde.",
            "de": "Speaker 1: Danke an alle für die Teilnahme. Unser Launch wird auf den fünfzehnten März verschoben.\nSpeaker 2: Verstanden. Das Marketing braucht die finalen Materialien bis nächsten Freitag, und ich teile das Budget heute Nachmittag.",
            "zh": "Speaker 1: 感谢大家参加。我们的发布时间推迟到三月十五日。\nSpeaker 2: 明白了。市场部需要在下周五之前拿到最终素材，我今天下午会分享预算。",
            "sw": "Speaker 1: Asanteni nyote kwa kujiunga. Uzinduzi wetu umehamishwa hadi tarehe kumi na tano Machi.\nSpeaker 2: Nimeelewa. Idara ya masoko inahitaji nyenzo za mwisho kufikia Ijumaa ijayo, na nitashiriki bajeti alasiri hii.",
        },
    },
    "interview": {
        "label": "Interviews", "service": "transcribe", "service_label": "Transcription with speaker IDs",
        "cast": [
            {"name": "Speaker 1", "voice": "cjVigY5qzO86Huf0OWal", "gender": "m"},     # Eric
            {"name": "Speaker 2", "voice": "XrExE9yKIg1WjnnlVkGX", "gender": "f"},     # Matilda
        ],
        "speaker": "Speaker 1",
        "headline": "Who said what, with timestamps, ready to quote",
        "headline_en": "The interview in English, speaker by speaker",
        "en": "Speaker 1: What was the hardest part of building the clinic?\nSpeaker 2: Honestly, finding trained nurses. We started with three, and now we have twelve.",
        "tr": {
            "es": "Speaker 1: ¿Cuál fue la parte más difícil de construir la clínica?\nSpeaker 2: Sinceramente, encontrar enfermeras capacitadas. Empezamos con tres y ahora tenemos doce.",
            "de": "Speaker 1: Was war der schwierigste Teil beim Bau der Klinik?\nSpeaker 2: Ehrlich gesagt, ausgebildete Krankenschwestern zu finden. Wir haben mit drei angefangen, jetzt sind es zwölf.",
            "zh": "Speaker 1: 建这家诊所最难的部分是什么？\nSpeaker 2: 说实话，是找到受过培训的护士。我们一开始只有三位，现在有十二位。",
            "sw": "Speaker 1: Ni sehemu gani ngumu zaidi ya kujenga zahanati?\nSpeaker 2: Kusema kweli, kupata wauguzi waliofunzwa. Tulianza na watatu, sasa tuna kumi na wawili.",
        },
    },
    "podcast": {
        "label": "Podcasts", "service": "translate", "service_label": "Transcription + translation",
        "cast": [
            {"name": "Speaker 1", "voice": "bIHbv24MWmeRgasZH58o", "gender": "m"},     # Will
            {"name": "Speaker 2", "voice": "cgSgspJ2msm6clMCkdW9", "gender": "f"},     # Jessica
        ],
        "speaker": "Speaker 1",
        "headline": "A transcript and translation for every episode",
        "headline_en": "Your episode transcript, now in English",
        "en": "Speaker 1: Welcome back to the show. Today we are talking about growing a small business.\nSpeaker 2: Thanks for having me. My best advice is to listen to your customers first.",
        "tr": {
            "es": "Speaker 1: Bienvenidos de nuevo al programa. Hoy hablamos de cómo hacer crecer un pequeño negocio.\nSpeaker 2: Gracias por invitarme. Mi mejor consejo es escuchar primero a tus clientes.",
            "de": "Speaker 1: Willkommen zurück zur Sendung. Heute sprechen wir darüber, wie man ein kleines Unternehmen wachsen lässt.\nSpeaker 2: Danke für die Einladung. Mein bester Rat ist, zuerst auf die Kunden zu hören.",
            "zh": "Speaker 1: 欢迎回到节目。今天我们聊聊如何让小生意不断成长。\nSpeaker 2: 谢谢邀请。我最好的建议是先倾听你的客户。",
            "sw": "Speaker 1: Karibu tena kwenye kipindi. Leo tunazungumzia jinsi ya kukuza biashara ndogo.\nSpeaker 2: Asante kwa kunikaribisha. Ushauri wangu bora ni kuwasikiliza wateja wako kwanza.",
        },
    },
    "course": {
        "label": "Courses and training", "service": "translate", "service_label": "Transcription + translation",
        "cast": [{"name": "Instructor", "voice": "Xb7hH8MSUJpSbSDYk0k2", "gender": "f"}],   # Alice
        "speaker": "Instructor",
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
    "news": {
        "label": "News and broadcast", "service": "translate", "service_label": "Transcription + translation",
        "cast": [{"name": "Anchor", "voice": "onwK4e9ZLuTAKqWW03F9", "gender": "m"}],     # Daniel
        "speaker": "Anchor",
        "headline": "Subtitles ready for air, in every language",
        "headline_en": "English subtitles on a bulletin in another language",
        "en": "Good evening. City officials confirmed today that the new bridge will open to traffic on Monday morning. Delays are expected on the north side.",
        "tr": {
            "es": "Buenas noches. Las autoridades de la ciudad confirmaron hoy que el nuevo puente abrirá al tráfico el lunes por la mañana. Se esperan retrasos en el lado norte.",
            "de": "Guten Abend. Die Stadtverwaltung bestätigte heute, dass die neue Brücke am Montagmorgen für den Verkehr geöffnet wird. Auf der Nordseite sind Verzögerungen zu erwarten.",
            "zh": "晚上好。市政府今天确认，新大桥将于周一早晨通车。北侧预计会出现延误。",
            "sw": "Habari za jioni. Maafisa wa jiji walithibitisha leo kwamba daraja jipya litafunguliwa kwa magari Jumatatu asubuhi. Ucheleweshaji unatarajiwa upande wa kaskazini.",
        },
    },
    "field": {
        "label": "NGO and field work", "service": "translate", "service_label": "Transcription + translation",
        "cast": [{"name": "Field officer", "voice": "hpp4J3VqNfWAUOO0d1Us", "gender": "f"}],   # Bella
        "speaker": "Field officer",
        "headline": "Field recordings translated for your reports",
        "headline_en": "A field recording, now in English",
        "en": "We visited twelve villages this month. Most families now have clean water, but the school still needs desks and books.",
        "tr": {
            "es": "Este mes visitamos doce aldeas. La mayoría de las familias ya tiene agua limpia, pero la escuela todavía necesita pupitres y libros.",
            "de": "Diesen Monat haben wir zwölf Dörfer besucht. Die meisten Familien haben jetzt sauberes Wasser, aber die Schule braucht noch Tische und Bücher.",
            "zh": "这个月我们走访了十二个村庄。大多数家庭现在有了干净的水，但学校仍然需要课桌和书本。",
            "sw": "Mwezi huu tulitembelea vijiji kumi na viwili. Familia nyingi sasa zina maji safi, lakini shule bado inahitaji madawati na vitabu.",
        },
    },
    "video": {
        "label": "Marketing and creator videos", "service": "dub", "service_label": "Full dubbing",
        "cast": [{"name": "Narrator", "voice": "TX3LPaxmHKxFdv7VOQHJ", "gender": "m"}],   # Liam
        "speaker": "Narrator",
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
    (("podcast",), "podcast"),
    (("interview",), "interview"),
    (("news", "broadcast", "media"), "news"),
    (("ngo", "civil", "field", "nonprofit"), "field"),
    (("meeting", "webinar", "audio-to-text", "transcri"), "meeting"),
    (("course", "e-learning", "elearning", "training"), "course"),
    (("youtube", "video", "dubbing", "voice-over", "voiceover", "localization", "localisation", "marketing", "subtitle", "caption",
      "translation"), "video"),
)


def audio_name(scenario: str, lang: str | None) -> str:
    """The static file for a clip: the English source, or the dubbed version."""
    return f"src-{scenario}.mp3" if lang is None else f"dub-{scenario}-{lang}.mp3"


def audio_in_name(scenario: str, lang: str) -> str:
    """The clip spoken in the visitor's own language, for the "into English" direction. For the dubbing scenario that is the
    dubbed clip itself; the other two scenarios have their own recordings."""
    return audio_name(scenario, lang) if SCENARIOS[scenario]["service"] == "dub" else f"in-{scenario}-{lang}.mp3"


def turns(scenario: str, text: str) -> list[tuple[int, str]]:
    """(who, what) for each turn of a scenario's text, who being the index into its cast. Text without speaker labels is one turn
    by the first person."""
    cast = SCENARIOS[scenario]["cast"]
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        who, body = 0, line
        for i, person in enumerate(cast):
            if line.startswith(person["name"] + ": "):
                who, body = i, line[len(person["name"]) + 2:]
                break
        out.append((who, body))
    return out


def spoken_text(scenario: str, text: str) -> str:
    """The words alone, as they are spoken and as they are captioned (the speaker labels are for the transcript)."""
    return " ".join(body for _, body in turns(scenario, text))


def turns(scenario: str, text: str) -> list[tuple[int, str]]:
    """(who, what) for each turn of a scenario's text, who being the index into its cast. Text without speaker labels is one turn
    by the first person."""
    cast = SCENARIOS[scenario]["cast"]
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        who, body = 0, line
        for i, person in enumerate(cast):
            if line.startswith(person["name"] + ": "):
                who, body = i, line[len(person["name"]) + 2:]
                break
        out.append((who, body))
    return out


def spoken_text(scenario: str, text: str) -> str:
    """The words alone, as they are spoken and as they are captioned (the speaker labels are for the transcript)."""
    return " ".join(body for _, body in turns(scenario, text))


def all_clips() -> list[tuple[str, str, str | None, str]]:
    """(file name, scenario, language or None, text with its speaker labels) for every clip the demo plays."""
    clips = []
    for key, s in SCENARIOS.items():
        clips.append((audio_name(key, None), key, None, s["en"]))
        for lang, text in s["tr"].items():
            clips.append((audio_in_name(key, lang) if s["service"] != "dub" else audio_name(key, lang), key, lang, text))
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
    from urllib.parse import urlparse
    ref = (urlparse(referer or "").path or referer or "").lower()          # the page, not the site name (it has "media" in it)
    for words, scenario in REFERRER_SCENARIO:
        if any(w in ref for w in words):
            return scenario, "page"
    return "meeting", "default"


# Which demo videos have an original music bed under the voice (see scripts/make_demo_music.py): the marketing video, the
# course and the podcast, not the meetings, interviews or news.
MUSIC_BEDS = {"video": "bright", "course": "calm", "podcast": "calm"}


def video_name(scenario: str, lang: str) -> str:
    """The demo video of a scenario: "en" is the original, any other code is the result in that language
    (scripts/make_demo_videos.py makes them from the pre-rendered audio)."""
    return f"vid-{scenario}-{lang}.mp4"
