"""Per-language facts for the English 'X audio to text' pages and the
languages hub - the multilingual-SEO side of the site.

Honesty rule for everything built on this: the 74 non-core languages are
Instant AI TRANSCRIPTION only. Translation, dubbing and human review exist
only for Swahili, Kikuyu and English (see app.py's AI_ONLY_LANGUAGES), so no
page generated from this data may promise otherwise.

Autonyms and regions are plain reference facts (what the language is called by
its speakers, and where it is mainly spoken)."""
from __future__ import annotations

import re

# code -> (autonym, where it is mainly spoken). Codes match
# kauli.providers.asr.TRANSKRIPTOR_LANGUAGES.
LANGUAGE_FACTS = {
    "af": ("Afrikaans", "South Africa and Namibia"),
    "am": ("አማርኛ", "Ethiopia"),
    "ar": ("العربية", "the Middle East and North Africa"),
    "az": ("Azərbaycan dili", "Azerbaijan"),
    "bg": ("български", "Bulgaria"),
    "bn": ("বাংলা", "Bangladesh and India"),
    "bs": ("bosanski", "Bosnia and Herzegovina"),
    "ca": ("català", "Catalonia, Valencia and Andorra"),
    "cs": ("čeština", "Czechia"),
    "cy": ("Cymraeg", "Wales"),
    "da": ("dansk", "Denmark"),
    "de": ("Deutsch", "Germany, Austria and Switzerland"),
    "el": ("Ελληνικά", "Greece and Cyprus"),
    "es": ("Español", "Spain, Latin America and the United States"),
    "et": ("eesti", "Estonia"),
    "eu": ("euskara", "the Basque Country"),
    "fa": ("فارسی", "Iran and neighbouring countries"),
    "fi": ("suomi", "Finland"),
    "tl": ("Filipino", "the Philippines"),
    "fr": ("Français", "France, Canada, Belgium, Switzerland and Africa"),
    "ga": ("Gaeilge", "Ireland"),
    "gl": ("galego", "Galicia in Spain"),
    "gu": ("ગુજરાતી", "Gujarat in India"),
    "he": ("עברית", "Israel"),
    "hi": ("हिन्दी", "India"),
    "hr": ("hrvatski", "Croatia"),
    "hu": ("magyar", "Hungary"),
    "hy": ("հայերեն", "Armenia"),
    "id": ("Bahasa Indonesia", "Indonesia"),
    "is": ("íslenska", "Iceland"),
    "it": ("Italiano", "Italy and Switzerland"),
    "ja": ("日本語", "Japan"),
    "jv": ("Basa Jawa", "Java in Indonesia"),
    "ka": ("ქართული", "Georgia"),
    "kk": ("қазақ тілі", "Kazakhstan"),
    "km": ("ខ្មែរ", "Cambodia"),
    "kn": ("ಕನ್ನಡ", "Karnataka in India"),
    "ko": ("한국어", "South Korea"),
    "lo": ("ລາວ", "Laos"),
    "lt": ("lietuvių", "Lithuania"),
    "lv": ("latviešu", "Latvia"),
    "mk": ("македонски", "North Macedonia"),
    "ml": ("മലയാളം", "Kerala in India"),
    "mn": ("монгол", "Mongolia"),
    "mr": ("मराठी", "Maharashtra in India"),
    "ms": ("Bahasa Melayu", "Malaysia"),
    "mt": ("Malti", "Malta"),
    "my": ("မြန်မာ", "Myanmar"),
    "ne": ("नेपाली", "Nepal"),
    "nl": ("Nederlands", "the Netherlands and Belgium"),
    "no": ("norsk", "Norway"),
    "pa": ("ਪੰਜਾਬੀ", "Punjab in India"),
    "pl": ("polski", "Poland"),
    "ps": ("پښتو", "Afghanistan"),
    "pt": ("Português", "Brazil and Portugal"),
    "ro": ("română", "Romania"),
    "ru": ("русский", "Russia"),
    "si": ("සිංහල", "Sri Lanka"),
    "sk": ("slovenčina", "Slovakia"),
    "sl": ("slovenščina", "Slovenia"),
    "so": ("Soomaali", "Somalia"),
    "sq": ("shqip", "Albania"),
    "sr": ("српски", "Serbia"),
    "sv": ("svenska", "Sweden"),
    "ta": ("தமிழ்", "Tamil Nadu in India and Sri Lanka"),
    "te": ("తెలుగు", "Telangana and Andhra Pradesh in India"),
    "th": ("ไทย", "Thailand"),
    "tr": ("Türkçe", "Türkiye"),
    "uk": ("українська", "Ukraine"),
    "ur": ("اردو", "Pakistan and India"),
    "uz": ("oʻzbek", "Uzbekistan"),
    "vi": ("Tiếng Việt", "Vietnam"),
    "zh": ("中文（普通话）", "mainland China and Singapore"),
    "yue": ("粵語", "Hong Kong and Guangdong"),
}


def slugify(label: str) -> str:
    """'Chinese (Mandarin)' -> 'chinese-mandarin'."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def slug_for(code: str, label: str) -> str:
    return slugify(label)


def build_slug_index(languages: dict[str, str]) -> dict[str, str]:
    """slug -> language code for every AI-only language {code: label}."""
    return {slugify(label): code for code, label in languages.items()}


def display_name(label: str) -> str:
    """The natural way to say it in running text and search queries:
    'Chinese (Mandarin)' -> 'Mandarin Chinese'."""
    m = re.match(r"^(.+?) \((.+)\)$", label)
    return f"{m.group(2)} {m.group(1)}" if m else label
