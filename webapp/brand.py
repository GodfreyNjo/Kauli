"""The Kauli name and logo as each reader sees them.

A reader whose language is written in another script sees the emblem with the name written in that script, so the logo is not the one
thing on the page they cannot read. The name stays "Kauli" in running text and wherever it is a legal name (invoices, terms); only the
logo lockup is localised. These renderings of the name are phonetic and should be checked by a native speaker before they are fixed in print.
"""
from __future__ import annotations

import re

WORDMARKS = {"zh": "考利", "ja": "カウリ", "ko": "카울리", "ar": "كاولي", "hi": "कौली"}
HTML_LANG = {"zh": "zh-Hans", "ja": "ja", "ko": "ko", "ar": "ar", "hi": "hi"}
# Noto (open licence) for the name and the page text in these scripts, loaded only for the readers who need it
FONT_FAMILY = {"zh": "Noto+Sans+SC", "ja": "Noto+Sans+JP", "ko": "Noto+Sans+KR", "ar": "Noto+Sans+Arabic", "hi": "Noto+Sans+Devanagari"}

_PICTURE = re.compile(r'<picture>\s*<source[^>]*kauli-logo-nav-(?P<v>light|sm)\.webp[^>]*>\s*<img\b(?P<a>[^>]*)>\s*</picture>', re.S | re.I)
_BARE = re.compile(r'<img\b(?P<a>[^>]*src="/static/kauli-logo-nav-(?P<v>light|sm)\.png"[^>]*)>', re.I)


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else ""


def _lockup(m: re.Match, lang: str) -> str:
    attrs, variant = m.group("a"), m.group("v")
    name = WORDMARKS[lang]
    height = _attr(attrs, "height") or "38"
    cls = _attr(attrs, "class")
    decorative = _attr(attrs, "alt") == "" and "alt=" in attrs
    label = "" if decorative else f' role="img" aria-label="Kauli ({name})"'
    tone = "on-dark" if variant == "light" else "on-light"
    return (f'<span class="logo-local {tone}"{label}><img src="/static/kauli-emblem.png" alt="" class="{cls}" height="{height}" '
            f'style="height:{height}px;width:auto" loading="lazy"><span class="logo-local-name" lang="{HTML_LANG[lang]}" aria-hidden="true" '
            f'style="font-size:{round(int(height) * 0.62) if height.isdigit() else 24}px">{name}</span></span>')


def localize_logos(page: str, lang: str) -> str:
    """Swaps the English logo for the emblem plus the name in the reader's script, and loads the font for it."""
    if lang not in WORDMARKS:
        return page
    page = _PICTURE.sub(lambda m: _lockup(m, lang), page)
    page = _BARE.sub(lambda m: _lockup(m, lang), page)
    if "fonts.googleapis.com/css2?family=Noto" not in page:
        link = (f'<link href="https://fonts.googleapis.com/css2?family={FONT_FAMILY[lang]}:wght@400;500;700&display=swap" rel="stylesheet" '
                f'media="print" onload="this.media=\'all\'">')
        page = page.replace("</head>", link + "</head>", 1)
    return page
