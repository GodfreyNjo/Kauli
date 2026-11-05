"""Every page in the visitor's own language, not just the screens we translated by hand.

A visitor who reads Chinese should see Chinese everywhere: the public pages, sign-in, the order screens, billing, checkout, the account,
the messages after an action, and the emails. The hand-written catalog (webapp/ui_text.py) covers the main screens with careful wording;
this module covers everything else by translating the page's own words as the page is sent, so no screen can be left in English by accident.

How it works, in short:
  * the finished HTML is scanned for the words a person reads (text, button and field labels, image descriptions, page titles and search
    descriptions, structured data), leaving code, styles, transcripts (anything marked lang= or translate="no") and names alone;
  * each distinct sentence is translated once with Azure Translator and remembered for good in the database (table ui_translations), so a
    page that has been seen once in a language is instant afterwards and costs nothing more;
  * brand and product names (Kauli, Paystack, M-Pesa, YouTube, file formats...) are fenced off so they are never translated;
  * strings that are already in the target language (the hand-written catalog) are recognised and left as they are;
  * if the translator is slow or down, the visitor gets the page with whatever was translated and the rest in English, and the page is
    marked noindex so a half-translated page is never listed by a search engine.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db

# our language code -> Azure Translator code
AZURE_CODES = {"es": "es", "zh": "zh-Hans", "ar": "ar", "fr": "fr", "pt": "pt", "de": "de", "hi": "hi", "ja": "ja", "ko": "ko", "sw": "sw"}

# names that must come out exactly as they go in
BRAND_TERMS = ("Forge Media Services", "Microsoft Teams", "Airtel Money", "Kauli", "Paystack", "M-Pesa", "YouTube", "Google", "Spotify", "Slack", "Notion",
               "Zoom", "ElevenLabs", "WhatsApp", "Transkriptor", "Word", "PDF", "TXT", "SRT", "VTT", "CSV", "API", "MCP", "Ereri")
_BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(BRAND_TERMS, key=len, reverse=True)) + r")\b")

_SCRIPT_RANGES = {
    "zh": r"一-鿿㐀-䶿", "ja": r"぀-ヿ一-鿿", "ko": r"가-힯ᄀ-ᇿ", "ar": r"؀-ۿݐ-ݿ",
    "hi": r"ऀ-ॿ",
}
_SCRIPT_RE = {k: re.compile(f"[{v}]") for k, v in _SCRIPT_RANGES.items()}

_EN_STOP = frozenset("the a an and or of to in on for with your you we our is are be this that it as at by from not can will have has more all".split())
_OTHER_STOP = {
    "es": frozenset("el la los las de del y o en con para por que es un una su sus tu tus se al como mas pero".split()),
    "fr": frozenset("le la les des de du et ou en avec pour par que est un une son ses votre vos au aux comme plus mais".split()),
    "pt": frozenset("o a os as de do da dos das e ou em com para por que e um uma seu sua seus suas ao como mais mas".split()),
    "de": frozenset("der die das den dem des und oder in mit fur von zu ist ein eine sein ihre ihr nicht auch als wie mehr aber".split()),
    "sw": frozenset("na ya wa kwa ni la za katika kama hii huu yako yetu au pia lakini kwenye hadi".split()),
}


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


# ------------------------------------------------------------------------------------------------------- what is worth translating
def needs_translation(text: str, lang: str) -> bool:
    s = text.strip()
    if len(s) < 2 or len(s) > 9000:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False                                                    # numbers, symbols, or already in another script
    if lang in _SCRIPT_RE and _SCRIPT_RE[lang].search(s):
        return False                                                    # already in the target language
    if re.match(r"^(https?://|www\.|mailto:|tel:|[\w.+-]+@[\w-]+\.)", s) or re.match(r"^[#.]?[\w-]+(?:[_/.:#@][\w-]+)+$", s):
        return False                                                    # a link, an address, an identifier
    if re.fullmatch(r"[\W\d_]*(?:\$|US\$|USD|KES|KSh)?[\d.,\s]*(?:min|mins|MB|GB|%|s|h)?[\W]*", s):
        return False
    stop_other = _OTHER_STOP.get(lang)
    if stop_other:
        words = re.findall(r"[a-zA-ZÀ-ÿ']+", s.lower())
        en = sum(1 for w in words if w in _EN_STOP)
        other = sum(1 for w in words if w in stop_other)
        if other > en or (other and not en and len(words) > 2):
            return False                                                # already written in the target language
        if re.search(r"[À-ÿ]", s) and not en:
            return False                                                # accented words and no English: already translated
    return True


def _fence(text: str) -> str:
    """The text as HTML for the translator, with brand names marked do-not-translate."""
    escaped = _html.escape(text, quote=False)
    escaped = re.sub(r"\{[A-Za-z_]+\}", lambda m: f'<span class="notranslate">{m.group(0)}</span>', escaped)       # a gap a script fills in, like {n}
    return _BRAND_RE.sub(lambda m: f'<span class="notranslate">{m.group(1)}</span>', escaped)


def _unfence(text: str) -> str:
    return _html.unescape(re.sub(r"</?span[^>]*>", "", text or ""))


# --------------------------------------------------------------------------------------------------------------------- the cache
_mem: dict[tuple[str, str], str] = {}
_lock = threading.Lock()


def _cache_get(lang: str, texts: list[str]) -> dict[str, str]:
    hits: dict[str, str] = {}
    missing = []
    with _lock:
        for t in texts:
            v = _mem.get((lang, _h(t)))
            if v is not None:
                hits[t] = v
            else:
                missing.append(t)
    if missing:
        conn = db.get_conn()
        try:
            for i in range(0, len(missing), 400):
                part = missing[i:i + 400]
                hashes = {_h(t): t for t in part}
                marks = ",".join("?" for _ in hashes)
                rows = conn.execute(f"SELECT h, text FROM ui_translations WHERE lang = ? AND h IN ({marks})", (lang, *hashes)).fetchall()
                with _lock:
                    for r in rows:
                        t = hashes[r["h"]]
                        hits[t] = r["text"]
                        _mem[(lang, r["h"])] = r["text"]
        except Exception:  # noqa: BLE001 - a missing table on an old database just means nothing is cached yet
            pass
        finally:
            conn.close()
    return hits


def _cache_put(lang: str, pairs: dict[str, str]) -> None:
    if not pairs:
        return
    with _lock:
        for t, v in pairs.items():
            _mem[(lang, _h(t))] = v
    conn = db.get_conn()
    try:
        conn.executemany("INSERT OR REPLACE INTO ui_translations (h, lang, text, created_at) VALUES (?, ?, ?, ?)",
                         [(_h(t), lang, v, time.time()) for t, v in pairs.items()])
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------------------- Azure
def configured() -> bool:
    return bool(os.environ.get("AZURE_TRANSLATOR_KEY") and os.environ.get("AZURE_TRANSLATOR_REGION"))


def _azure_batch(texts: list[str], lang: str, timeout: float) -> list[str] | None:
    import httpx
    try:
        resp = httpx.post(
            "https://api.cognitive.microsofttranslator.com/translate",
            params={"api-version": "3.0", "from": "en", "to": AZURE_CODES[lang], "textType": "html"},
            headers={"Ocp-Apim-Subscription-Key": os.environ["AZURE_TRANSLATOR_KEY"], "Ocp-Apim-Subscription-Region": os.environ["AZURE_TRANSLATOR_REGION"],
                     "Content-Type": "application/json"},
            json=[{"text": _fence(t)} for t in texts], timeout=timeout)
        resp.raise_for_status()
        return [_unfence(item["translations"][0]["text"]) for item in resp.json()]
    except Exception:  # noqa: BLE001 - the page is served either way
        return None


def translate_many(strings: list[str], lang: str, budget: float = 8.0) -> tuple[dict[str, str], bool]:
    """{english: translated} for as many of the strings as could be done inside the time budget, and whether that was all of them."""
    if lang not in AZURE_CODES:
        return {}, True
    wanted = list(dict.fromkeys(s for s in strings if needs_translation(s, lang)))
    out = _cache_get(lang, wanted)
    todo = [s for s in wanted if s not in out]
    if not todo or not configured():
        return out, not todo
    deadline = time.time() + budget
    chunks, cur, size = [], [], 0
    for s in todo:
        if cur and (len(cur) >= 80 or size + len(s) > 18000):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s)
    if cur:
        chunks.append(cur)

    def run(chunk: list[str]):
        left = deadline - time.time()
        if left <= 0.5:
            return chunk, None
        return chunk, _azure_batch(chunk, lang, timeout=min(left, 12.0))

    done = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for chunk, result in pool.map(run, chunks):
            if result and len(result) == len(chunk):
                done.update({s: r for s, r in zip(chunk, result) if r and r.strip()})
    _cache_put(lang, done)
    out.update(done)
    return out, all(s in out for s in wanted)


# -------------------------------------------------------------------------------------------------------------------- the page
_PROTECT = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>|<!--.*?-->|<textarea\b[^>]*>.*?</textarea\s*>|<pre\b[^>]*>.*?</pre\s*>"
    r"|<code\b[^>]*>.*?</code\s*>|<svg\b[^>]*>.*?</svg\s*>|<noscript\b[^>]*>.*?</noscript\s*>", re.S | re.I)
_NOTRANS = re.compile(
    r"<(td|th|p|span|li|h[1-6]|a|strong|em|b|i|small|button|label|cite|blockquote)\b[^>]*?"
    r"(?:\btranslate=\"no\"|\bclass=\"[^\"]*\bnotranslate\b[^\"]*\"|\blang=\"[a-zA-Z-]+\")[^>]*>.*?</\1\s*>", re.S | re.I)
_TEXT = re.compile(r"(?<=>)([^<>]+)(?=<)")
_ATTR = re.compile(r'(\s(?:alt|title|placeholder|aria-label|aria-description|data-placeholder|data-label))="([^"]*)"', re.I)
_META = re.compile(r"<meta\b[^>]*>", re.I)
_META_KEYS = ("description", "og:title", "og:description", "og:image:alt", "twitter:title", "twitter:description", "twitter:image:alt")
_TOKEN = re.compile(r"\x00(\d+)\x00")
_LD_KEYS = {"name", "description", "text", "headline", "alternativeHeadline", "slogan", "caption", "abstract", "disambiguatingDescription"}


def _ld_strings(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _LD_KEYS and isinstance(v, str):
                out.append(v)
            elif k not in ("@context", "@type", "url", "@id", "image", "sameAs"):
                _ld_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _ld_strings(v, out)


def _ld_apply(node, tr: dict[str, str]):
    if isinstance(node, dict):
        return {k: (tr.get(v, v) if k in _LD_KEYS and isinstance(v, str) else (v if k in ("@context", "@type", "url", "@id", "image", "sameAs") else _ld_apply(v, tr)))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_ld_apply(v, tr) for v in node]
    return node


def localize_html(page: str, lang: str, budget: float = 8.0) -> tuple[str, bool]:
    """The page with its words in `lang`, and whether every one of them could be translated."""
    if lang not in AZURE_CODES:
        return page, True
    protected: list[str] = []

    def keep(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    # structured data is worth translating for search, so it is taken out of the protected scripts first
    ld_blocks: list[tuple[int, object]] = []

    def take_ld(m: re.Match) -> str:
        try:
            data = json.loads(m.group(2))
        except ValueError:
            return m.group(0)
        ld_blocks.append((len(ld_blocks), data))
        return f"<script{m.group(1)}>\x01LD{len(ld_blocks) - 1}\x01</script>"
    page = re.sub(r'<script((?:\s[^>]*)?\btype="application/ld\+json"[^>]*)>(.*?)</script>', take_ld, page, flags=re.S | re.I)

    body = _NOTRANS.sub(keep, page)
    body = _PROTECT.sub(keep, body)

    strings: list[str] = []
    for m in _TEXT.finditer(body):
        for part in _TOKEN.split(m.group(1))[0::2]:
            plain = _html.unescape(part).strip()
            if plain:
                strings.append(re.sub(r"\s+", " ", plain))
    for m in _ATTR.finditer(body):
        strings.append(_html.unescape(m.group(2)).strip())
    for m in _META.finditer(body):
        tag = m.group(0)
        key = re.search(r'(?:name|property)="([^"]+)"', tag)
        val = re.search(r'content="([^"]*)"', tag)
        if key and val and key.group(1) in _META_KEYS:
            strings.append(_html.unescape(val.group(1)).strip())
    ld_strings: list[str] = []
    for _, data in ld_blocks:
        _ld_strings(data, ld_strings)
    tr, complete = translate_many(strings + ld_strings, lang, budget)

    def swap_text(m: re.Match) -> str:
        pieces = _TOKEN.split(m.group(1))
        for i in range(0, len(pieces), 2):
            raw = pieces[i]
            plain = re.sub(r"\s+", " ", _html.unescape(raw).strip())
            if plain and plain in tr:
                lead = raw[: len(raw) - len(raw.lstrip())]
                trail = raw[len(raw.rstrip()):]
                pieces[i] = lead + _html.escape(tr[plain], quote=False) + trail
        out = ""
        for i, p in enumerate(pieces):
            out += p if i % 2 == 0 else f"\x00{p}\x00"
        return out
    body = _TEXT.sub(swap_text, body)
    body = _ATTR.sub(lambda m: f'{m.group(1)}="{_html.escape(tr.get(_html.unescape(m.group(2)).strip(), _html.unescape(m.group(2))), quote=True)}"', body)

    def swap_meta(m: re.Match) -> str:
        tag = m.group(0)
        key = re.search(r'(?:name|property)="([^"]+)"', tag)
        val = re.search(r'content="([^"]*)"', tag)
        if key and val and key.group(1) in _META_KEYS:
            new = tr.get(_html.unescape(val.group(1)).strip())
            if new:
                return tag.replace(val.group(0), f'content="{_html.escape(new, quote=True)}"')
        return tag
    body = _META.sub(swap_meta, body)

    # put the protected pieces back (they may nest inside each other's placeholders, so keep going until none are left)
    for _ in range(4):
        if "\x00" not in body:
            break
        body = _TOKEN.sub(lambda m: protected[int(m.group(1))], body)
    for idx, data in ld_blocks:
        body = body.replace(f"\x01LD{idx}\x01", json.dumps(_ld_apply(data, tr), ensure_ascii=False))
    return body, complete


def localize_text(text: str, lang: str, budget: float = 6.0) -> str:
    """One short message (an error, an email subject) in `lang`; the original if it cannot be translated."""
    out, _ = translate_many([text], lang, budget)
    return out.get(text, text)


def localize_json(data, lang: str, keys=("error", "message", "notice", "detail", "msg"), budget: float = 6.0):
    """The human-readable messages in a JSON answer, translated; everything else is left alone."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in keys and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    if not found:
        return data
    tr, _ = translate_many(found, lang, budget)

    def apply(node):
        if isinstance(node, dict):
            return {k: (tr.get(v, v) if k in keys and isinstance(v, str) else apply(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [apply(v) for v in node]
        return node
    return apply(data)


# ---------------------------------------------------------------------------------------------------- getting every page ready
WARM = {"running": False, "done": 0, "total": 0, "errors": 0, "started": 0.0, "last": ""}


def counts() -> dict[str, int]:
    """How many sentences are remembered per language."""
    conn = db.get_conn()
    try:
        return {r["lang"]: r["n"] for r in conn.execute("SELECT lang, COUNT(*) AS n FROM ui_translations GROUP BY lang").fetchall()}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()


def warm(asgi_app, langs: tuple[str, ...] | None = None) -> None:
    """Opens every translated public page once (through the app itself), so each is translated and remembered before a visitor or a search
    crawler asks for it. Runs in its own thread; progress is in WARM."""
    import asyncio

    import httpx

    if WARM["running"]:
        return
    WARM.update(running=True, done=0, total=0, errors=0, started=time.time(), last="")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://warm.local", timeout=90.0) as client:
            xml = (await client.get("/sitemap.xml")).text
            paths = []
            for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
                m = re.match(r"https?://[^/]+(/(?:es|zh|ar|fr|pt|de|hi|ja|ko|sw)/.+)$", loc)
                if m and (not langs or m.group(1).split("/")[1] in langs):
                    paths.append(m.group(1))
            WARM["total"] = len(paths)
            for path in paths:
                try:
                    r = await client.get(path, headers={"User-Agent": "Kauli-warmup"})
                    if r.status_code >= 400:
                        WARM["errors"] += 1
                except Exception:  # noqa: BLE001
                    WARM["errors"] += 1
                WARM["done"] += 1
                WARM["last"] = path

    try:
        asyncio.run(run())
    finally:
        WARM["running"] = False
