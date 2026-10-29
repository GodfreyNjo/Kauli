"""Listenable blog posts: a narrated audio version of each post, a podcast feed for Apple Podcasts, Spotify and the rest, and the staff page
that makes them.

How an episode is made (webapp/blog_audio.py, run in the background from Staff > Blog audio):
  1. a script written for the ear from the post's own words (Claude when a key is set, otherwise a plain conversion that never adds a fact),
  2. narrated with ElevenLabs in chunks, the chunks joined and levelled,
  3. saved in the data folder, served at /blog-audio/<slug>.mp3, and listed in /podcast.xml.
Two sizes: "summary" (the three key takeaways, about a minute) and "full" (the whole post). The ElevenLabs plan's remaining credits are
checked first, so a batch can never run past the subscription.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import db, order_ai_assist

router = APIRouter()
A = None
VOICE_ID = os.environ.get("KAULI_BLOG_VOICE", "cgSgspJ2msm6clMCkdW9")          # Jessica by default, set KAULI_BLOG_VOICE to change
MODEL_ID = os.environ.get("KAULI_BLOG_TTS_MODEL", "eleven_multilingual_v2")
CREDITS_PER_CHAR = 0.5 if "turbo" in MODEL_ID or "flash" in MODEL_ID else 1.0
CHUNK_CHARS = 4200
SITE_FALLBACK = "https://kauli-forgemedia.com"
SPOKEN_SITE = "kauli dash forge media dot com"
_lock = threading.Lock()                # one narration at a time
_queue: list[tuple[str, str]] = []
_worker_running = False


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def audio_dir() -> Path:
    d = db.DB_PATH.parent / "blog_audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------------- the script
class _Text(HTMLParser):
    """The post as speakable text: headings become their own sentences, lists become sentences, links keep their words, code is dropped."""
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "blockquote", "div", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = [""]
        self.skip = 0
        self.heading = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "pre", "code", "table", "figure", "svg", "iframe"):
            self.skip += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self.heading += 1
            self.parts.append("")
        elif tag in self.BLOCK:
            self.parts.append("")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "pre", "code", "table", "figure", "svg", "iframe"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2", "h3", "h4"):
            self.heading = max(0, self.heading - 1)
            self.parts[-1] = "\u0001" + self.parts[-1].strip()           # marks a heading line
            self.parts.append("")
        elif tag in self.BLOCK:
            self.parts.append("")

    def handle_data(self, data):
        if not self.skip:
            self.parts[-1] += data


def _clean_line(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def takeaways(body_html: str) -> list[str]:
    m = re.search(r'<div class="m-key-takeaways">(.*?)</div>', body_html or "", re.S)
    if not m:
        return []
    return [_clean_line(html.unescape(re.sub(r"<[^>]+>", "", li))) for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S) if li.strip()]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def plain_script(post, mode: str) -> str:
    """A script from the post's own words with nothing added: the intro and outro are the only lines that are ours."""
    title = post["title"].strip()
    intro = f"{_sentence(title)} This is a Kauli guide."
    outro = f"You can read the full guide and try Kauli free at {SPOKEN_SITE}."
    pts = takeaways(post["body_html"])
    if mode == "summary":
        names = ["One", "Two", "Three", "Four", "Five", "Six"]
        body = ["Here are the key takeaways."] + [f"Number {names[i].lower() if i < len(names) else i + 1}. {_sentence(p)}" for i, p in enumerate(pts)]
        if not pts and post["description"]:
            body = [_sentence(post["description"].strip())]
        return "\n\n".join([intro] + body + [outro])
    parser = _Text()
    parser.feed(post["body_html"] or "")
    lines = []
    for raw in parser.parts:
        heading = raw.startswith("\u0001")
        line = _clean_line(raw.lstrip("\u0001"))
        if not line or line.lower() == "key takeaways":
            continue
        lines.append(_sentence(line) if not heading else _sentence(line))
    return "\n\n".join([intro] + lines + [outro])


EAR_PROMPT = """You turn a written blog guide into a script for ONE narrator to read aloud as a podcast episode. Rules:
- Use only what the guide says. Never add a fact, number, name, claim or example that is not in it.
- Keep it natural to hear: short sentences, no bullet or list symbols, no "see below", "the table", "click here" or other references to what the reader sees.
- Read lists as flowing sentences ("First... Second... Third...").
- Keep the intro line "This is a Kauli guide." and end with the exact outro line given.
- Spell the web address as: kauli dash forge media dot com.
- Plain text only: no markdown, no stage directions, no headings marks."""


def ear_script(post, mode: str) -> tuple[str, str]:
    """(script, how it was made). Claude rewrites for the ear when it can; anything doubtful falls back to the plain conversion."""
    base = plain_script(post, mode)
    if not order_ai_assist.ai_assist_configured():
        return base, "plain"
    target = "about 150 words (a one-minute summary of the key takeaways)" if mode == "summary" else "about the same length as the guide"
    try:
        resp = order_ai_assist._client().messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=6000, system=EAR_PROMPT,
            messages=[{"role": "user", "content": f"Title: {post['title']}\nTarget length: {target}\n\nGuide, already converted to plain paragraphs:\n\n{base[:30000]}"}])
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = re.sub(r"[*_#`]+", "", text)
        ok = 40 <= len(text.split()) and (mode == "summary" or len(text) >= 0.5 * len(base)) and SPOKEN_SITE.split(" dot ")[0] in text
        return (text, "claude") if ok else (base, "plain")
    except Exception:  # noqa: BLE001 - never stop an episode over the rewrite
        return base, "plain"


def chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Paragraph-aligned pieces under the TTS limit (a paragraph longer than the limit is split at sentence ends)."""
    out, cur = [], ""
    for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        pieces = [para] if len(para) <= limit else re.findall(r"[^.!?]+[.!?]+\s*|[^.!?]+$", para)
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > limit:
                out.append(cur.strip())
                cur = ""
            cur += piece + ("\n\n" if len(piece) == len(para) else " ")
    if cur.strip():
        out.append(cur.strip())
    return out


# ---------------------------------------------------------------------------------------------------- the voice and the file
def remaining_credits() -> int | None:
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}, timeout=10)
        r.raise_for_status()
        d = r.json()
        return max(int(d["character_limit"]) - int(d["character_count"]), 0)
    except Exception:  # noqa: BLE001
        return None


def tts(text: str) -> bytes:
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}", params={"output_format": "mp3_44100_128"},
                   headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}, timeout=300,
                   json={"text": text, "model_id": MODEL_ID, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.1}})
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs refused ({r.status_code}): {r.text[:200]}")
    return r.content


def _run(cmd: list) -> None:
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def duration_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def join_and_level(parts: list[bytes], out: Path) -> None:
    """The narrated pieces end to end with a short breath between them, levelled for podcast listening (about -16 LUFS), mono 96 kbps."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        names = []
        for i, data in enumerate(parts):
            p = tmp / f"p{i}.mp3"
            p.write_bytes(data)
            names.append(p)
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        joined = tmp / "joined.mp3"
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
        _run(["ffmpeg", "-v", "error", "-y", "-i", joined, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur=0.6", "-ac", "1", "-ar", "44100",
              "-c:a", "libmp3lame", "-b:a", "96k", out])


# ---------------------------------------------------------------------------------------------------- the record of each episode
def _now() -> float:
    return time.time()


def get_row(post_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM blog_audio WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row


def _set(post_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    conn.execute(f"UPDATE blog_audio SET {cols} WHERE post_id = ?", (*fields.values(), post_id))
    conn.commit()
    conn.close()


def ready_for(post_id: str):
    row = get_row(post_id)
    if row and row["status"] == "ready" and row["audio_file"] and (audio_dir() / row["audio_file"]).exists():
        return row
    return None


def estimate(post, mode: str) -> dict:
    text = plain_script(post, mode)
    chars = len(text)
    return {"chars": chars, "credits": int(chars * CREDITS_PER_CHAR), "minutes": round(len(text.split()) / 150, 1)}


def enqueue(post_ids: list[str], mode: str) -> int:
    """Put posts in the narration queue (staff action). Returns how many were added."""
    global _worker_running
    added = 0
    conn = db.get_conn()
    for pid in post_ids:
        if not conn.execute("SELECT 1 FROM blog_posts WHERE id = ? AND status = 'published'", (pid,)).fetchone():
            continue
        existing = conn.execute("SELECT status FROM blog_audio WHERE post_id = ?", (pid,)).fetchone()
        if existing and existing["status"] in ("queued", "scripting", "voicing"):
            continue
        conn.execute("INSERT INTO blog_audio (post_id, mode, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?) "
                     "ON CONFLICT(post_id) DO UPDATE SET mode = excluded.mode, status = 'queued', error = NULL, updated_at = excluded.updated_at",
                     (pid, mode, _now(), _now()))
        _queue.append((pid, mode))
        added += 1
    conn.commit()
    conn.close()
    with _lock:
        start = not _worker_running and bool(_queue)
        if start:
            _worker_running = True
    if start:
        threading.Thread(target=_work, daemon=True).start()
    return added


def _work() -> None:
    global _worker_running
    try:
        while True:
            with _lock:
                if not _queue:
                    _worker_running = False
                    return
                pid, mode = _queue.pop(0)
            try:
                make_episode(pid, mode)
            except Exception as exc:  # noqa: BLE001
                _set(pid, status="failed", error=str(exc)[:300])
    finally:
        with _lock:
            _worker_running = False


def make_episode(post_id: str, mode: str) -> None:
    """Script, narration, file. Raises with a readable reason; the caller records it."""
    post = db.get_blog_post(post_id)
    if not post:
        raise RuntimeError("That post no longer exists.")
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise RuntimeError("ElevenLabs is not set up on this server.")
    _set(post_id, status="scripting")
    script, how = ear_script(post, mode)
    need = int(len(script) * CREDITS_PER_CHAR)
    if os.environ.get("ELEVENLABS_ALLOW_OVERAGE") != "1":
        left = remaining_credits()
        if left is None:
            raise RuntimeError("Could not check the ElevenLabs balance, so nothing was spent.")
        if left < need:
            raise RuntimeError(f"Needs about {need:,} ElevenLabs credits and only {left:,} are left this period. Nothing was spent.")
    _set(post_id, status="voicing", script_text=script, script_by=how, chars=len(script))
    parts = [tts(c) for c in chunks(script)]
    out = audio_dir() / f"{post['slug']}-{mode}.mp3"
    join_and_level(parts, out)
    digest = hashlib.sha256(((post["body_html"] or "") + mode + VOICE_ID).encode("utf-8")).hexdigest()[:16]
    _set(post_id, status="ready", audio_file=out.name, duration_s=duration_seconds(out), bytes=out.stat().st_size, content_hash=digest, error=None, voice_id=VOICE_ID)


def mark_stale_jobs_failed() -> None:
    """A restart in the middle of a narration leaves it 'voicing' for ever; say so instead."""
    try:
        conn = db.get_conn()
        conn.execute("UPDATE blog_audio SET status = 'failed', error = 'Interrupted by a restart. Make it again.' WHERE status IN ('queued', 'scripting', 'voicing')")
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------------- public routes
def _base(request: Request) -> str:
    try:
        return A._site_base(request)
    except Exception:  # noqa: BLE001
        return SITE_FALLBACK


@router.get("/blog-audio/{filename}")
def blog_audio_file(filename: str):
    if not re.fullmatch(r"[a-z0-9-]+\.mp3", filename):
        return HTMLResponse("Not found.", status_code=404)
    slug = re.sub(r"-(summary|full)\.mp3$", "", filename)
    post = db.get_blog_post_by_slug(slug, published_only=True)
    row = ready_for(post["id"]) if post else None
    if not row or row["audio_file"] != filename:
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(str(audio_dir() / filename), media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


@router.get("/podcast.xml")
def podcast_feed(request: Request):
    """The RSS feed podcast apps read. Submit https://kauli-forgemedia.com/podcast.xml to Apple Podcasts Connect and Spotify for Creators."""
    base = _base(request)
    cover = f"{base}/static/podcast-cover.jpg"
    contact = getattr(A, "CONTACT_EMAIL", "hello@kauli-forgemedia.com")
    items = []
    for post in db.list_blog_posts(published_only=True):
        row = ready_for(post["id"])
        if not row or not row["in_podcast"]:
            continue
        when = datetime.fromtimestamp(post["published_at"] or post["created_at"], tz=timezone.utc)
        url = f"{base}/blog-audio/{row['audio_file']}"
        desc = (post["description"] or post["title"]).strip()
        desc += (" Narrated with an AI voice. Read the full guide: " if row["mode"] == "full" else " A short audio summary, narrated with an AI voice. Read the full guide: ") + f"{base}/blog/{post['slug']}"
        items.append(
            "<item>"
            f"<title>{escape(post['title'])}</title>"
            f"<link>{base}/blog/{post['slug']}</link>"
            f"<description>{escape(desc)}</description>"
            f"<guid isPermaLink=\"false\">kauli-audio-{post['id']}</guid>"
            f"<pubDate>{format_datetime(when)}</pubDate>"
            f"<enclosure url=\"{escape(url)}\" length=\"{int(row['bytes'] or 0)}\" type=\"audio/mpeg\"/>"
            f"<itunes:duration>{_mmss(row['duration_s'] or 0)}</itunes:duration>"
            "<itunes:explicit>false</itunes:explicit><itunes:episodeType>full</itunes:episodeType>"
            "</item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
           "<title>Kauli Guides</title>"
           f"<link>{base}/blog</link>"
           f'<atom:link href="{base}/podcast.xml" rel="self" type="application/rss+xml"/>'
           "<language>en-us</language>"
           "<copyright>Kauli, a Forge Media Services product</copyright>"
           "<description>Short audio guides on transcription, translation, subtitles and dubbing, from Kauli. Narrated with an AI voice.</description>"
           "<itunes:author>Kauli</itunes:author>"
           "<itunes:summary>Short audio guides on transcription, translation, subtitles and dubbing, from Kauli. Narrated with an AI voice.</itunes:summary>"
           f"<itunes:owner><itunes:name>Kauli</itunes:name><itunes:email>{escape(contact)}</itunes:email></itunes:owner>"
           f'<itunes:image href="{cover}"/>'
           '<itunes:category text="Technology"/>'
           "<itunes:explicit>false</itunes:explicit><itunes:type>episodic</itunes:type>"
           + "".join(items) + "</channel></rss>")
    return Response(content=xml, media_type="application/rss+xml", headers={"Cache-Control": "public, max-age=900"})


# ---------------------------------------------------------------------------------------------------- staff
def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


@router.get("/staff/blog-audio", response_class=HTMLResponse)
def staff_page(request: Request, notice: str | None = None, error: str | None = None):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    rows = []
    for post in db.list_blog_posts(published_only=True):
        row = get_row(post["id"])
        rows.append({"post": post, "audio": row, "summary": estimate(post, "summary"), "full": estimate(post, "full"),
                     "ready": bool(row and row["status"] == "ready")})
    left = remaining_credits() if os.environ.get("ELEVENLABS_API_KEY") else None
    return A.templates.TemplateResponse(request, "staff_blog_audio.html", {
        "user": user, "theme": "dark", "rows": rows, "credits_left": left, "model": MODEL_ID, "per_char": CREDITS_PER_CHAR,
        "configured": bool(os.environ.get("ELEVENLABS_API_KEY")), "base": _base(request), "notice": notice, "error": error,
        "busy": _worker_running or bool(_queue), "mmss": _mmss})


@router.post("/staff/blog-audio/make")
def staff_make(request: Request, post_ids: list[str] = Form([]), mode: str = Form("summary")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    mode = mode if mode in ("summary", "full") else "summary"
    if not post_ids:
        return RedirectResponse("/staff/blog-audio?error=" + quote("Tick at least one post."), status_code=303)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return RedirectResponse("/staff/blog-audio?error=" + quote("ElevenLabs is not set up on this server."), status_code=303)
    n = enqueue(post_ids, mode)
    return RedirectResponse("/staff/blog-audio?notice=" + quote(f"Started {n} narration{'' if n == 1 else 's'}. This page shows progress; each takes a minute or two."), status_code=303)


@router.post("/staff/blog-audio/{post_id}/podcast")
def staff_toggle(request: Request, post_id: str, include: str = Form("1")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if get_row(post_id):
        _set(post_id, in_podcast=1 if include == "1" else 0)
    return RedirectResponse("/staff/blog-audio", status_code=303)


@router.post("/staff/blog-audio/{post_id}/delete")
def staff_delete(request: Request, post_id: str):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    row = get_row(post_id)
    if row:
        if row["audio_file"]:
            try:
                (audio_dir() / row["audio_file"]).unlink()
            except OSError:
                pass
        conn = db.get_conn()
        conn.execute("DELETE FROM blog_audio WHERE post_id = ?", (post_id,))
        conn.commit()
        conn.close()
    return RedirectResponse("/staff/blog-audio?notice=Removed.", status_code=303)
