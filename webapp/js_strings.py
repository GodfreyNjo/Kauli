"""The words the page's scripts show, in the reader's language.

Most of a screen is built on the server and is translated as it is sent (webapp/page_translate.py). Some words are only made in the browser:
the steps while a file uploads, "5 min ago", the assistant's greeting, the order summary. Those scripts write their English with kT("...") (or
kauliT in base.html); this module finds every such string, translates the lot once per language (remembered for good in the same table as the
page translations) and the page hands the result to the scripts as window.KAULI_I18N. A string with a gap in it keeps the gap as {name}, which
the translator is told to leave alone, and the script fills it in.

A new kT("...") is picked up by itself: nothing to register here.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import page_translate

_STATIC = Path(__file__).parent / "static"
_FILES = ("client_wizard.js", "assistant.js", "hear_demo.js", "toast.js")
_STRING = r'"((?:[^"\\\n]|\\.)*)"'
_CALL = re.compile(r"\b(?:kT|kauliT)\(\s*" + _STRING)
_cache: list[str] | None = None


def _unescape(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\n", "\n").replace("\\u2026", "\u2026").replace("\\\\", "\\")


def english_strings() -> list[str]:
    """Every string the scripts ask to have translated, plus the names the order form puts in data attributes."""
    global _cache
    if _cache is not None:
        return _cache
    found: dict[str, None] = {}
    for name in _FILES:
        try:
            text = (_STATIC / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _CALL.finditer(text):
            found[_unescape(m.group(1))] = None
        if name == "assistant.js":
            block = re.search(r"var QUESTIONS = \{(.*?)\n  \};", text, re.S)
            for m in re.finditer(_STRING, block.group(1) if block else ""):
                found[_unescape(m.group(1))] = None
    try:
        base = (Path(__file__).parent / "templates" / "base.html").read_text(encoding="utf-8")
        for m in _CALL.finditer(base):
            found[_unescape(m.group(1))] = None
    except OSError:
        pass
    from . import billing
    for level in billing.SERVICE_LEVELS.values():
        found[level["name"]] = None
    for plain in ("Instant AI", "Human-reviewed", "Studio", "English", "Swahili", "Spanish", "German", "Chinese (Mandarin)"):
        found[plain] = None
    _cache = list(found)
    return _cache


def for_lang(lang: str, budget: float = 4.0) -> dict[str, str]:
    """{english: translated} for the scripts. Empty for English, and for any string the translator could not do in time (the script then shows
    the English, and the next page carries the rest)."""
    if not lang or lang == "en":
        return {}
    done, _complete = page_translate.translate_many(english_strings(), lang, budget)
    return {k: v for k, v in done.items() if v and v != k}
