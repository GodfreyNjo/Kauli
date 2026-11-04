"""The Podcast page (Resources > Podcast): play our shows and read their transcripts, and the staff tools that feed it.

Where the episodes come from (all of them show on /podcast with a player):
  - a podcast RSS feed, pulled on demand: any host (Spotify for Creators, Buzzsprout, Libsyn ...) and our own /podcast.xml. Each episode
    plays from its audio file.
  - a Spotify show or episode link, played with Spotify's own embedded player (Spotify's API is not needed for that).

Where the transcripts come from: Kauli itself. "Make transcript" on an episode orders a transcript through the normal order flow for the
publishing account (PODCAST_PUBLISHER_EMAIL, or the first address in KAULI_TEST_CLIENT_EMAILS). When that order is delivered Kauli
fires its ordinary signed webhook at /webhooks/podcast-transcripts on this very site, which checks the signature with that account's webhook
secret, downloads the finished text through the signed link in the message and attaches it to the episode. So publishing a transcript here
exercises exactly what a customer's own website would do with Kauli webhooks.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db

router = APIRouter()
A = None
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_EPISODES = 100
MAX_AUDIO_BYTES = 500 * 1024 * 1024
PAGE_SIZE = 6
HOOK_PATH = "/webhooks/podcast-transcripts"
SPOTIFY = re.compile(r"^https://open\.spotify\.com/(?:intl-[a-z]{2}/)?(show|episode)/([A-Za-z0-9]{10,30})(?:[/?#].*)?$")
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------ small helpers
def spotify_ref(url: str) -> tuple[str, str] | None:
    m = SPOTIFY.match((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def spotify_embed(kind: str, ident: str, height: int | None = None) -> str:
    return f"https://open.spotify.com/embed/{kind}/{ident}"


def safe_url(url: str) -> bool:
    """An https address that does not lead to this server's own network (the same check a customer's webhook address passes)."""
    try:
        return urlparse(url).scheme == "https" and bool(A._is_safe_webhook_url(url))
    except Exception:  # noqa: BLE001
        return False


def _clean(text: str | None, limit: int = 2000) -> str:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", plain).strip()[:limit]


def _inner(item, *tags: str) -> str:
    """All the text of the first of these child tags that exists, including any markup written directly inside it."""
    for tag in tags:
        el = item.find(tag)
        if el is not None:
            return "".join(el.itertext())
    return ""


def _duration(text: str | None) -> int | None:
    parts = [p for p in (text or "").strip().split(":") if p.strip().isdigit()]
    if not parts or len(parts) > 3:
        return None
    n = 0
    for p in parts:
        n = n * 60 + int(p)
    return n


def _publisher():
    """The client account that orders the transcripts (and whose webhook points back here)."""
    email = (os.environ.get("PODCAST_PUBLISHER_EMAIL") or "").strip().lower()
    if not email:
        from . import billing
        tester = sorted(billing.test_client_emails())
        email = tester[0] if tester else ""
    return db.get_user_by_email(email) if email else None


# ------------------------------------------------------------------------------------------------ pulling a feed
def fetch_feed(url: str) -> bytes:
    """The feed's bytes, following up to three redirects, each one checked, and never more than a few megabytes."""
    for _ in range(4):
        if not safe_url(url):
            raise ValueError("That address cannot be used for a feed. It must be an https link to a public website.")
        with httpx.stream("GET", url, timeout=20, follow_redirects=False, headers={"User-Agent": "KauliPodcastPage/1.0", "Accept": "application/rss+xml, application/xml, text/xml"}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            r.raise_for_status()
            data = b""
            for chunk in r.iter_bytes(65536):
                data += chunk
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError("That feed is larger than we can read.")
            return data
    raise ValueError("That feed redirected too many times.")


def parse_feed(data: bytes) -> tuple[str, list[dict]]:
    """(show title, episodes newest first) from an RSS podcast feed. Refuses XML that declares entities."""
    if re.search(rb"<!(DOCTYPE|ENTITY)", data[:4096] + data[-512:], re.I):
        raise ValueError("That file is not a podcast feed.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("That address did not return a podcast feed.") from None
    channel = root.find("channel")
    if channel is None:
        raise ValueError("That address did not return a podcast feed.")
    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio = (enclosure.get("url") if enclosure is not None else "") or ""
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or audio or link or item.findtext("title") or "").strip()
        if not guid:
            continue
        when = None
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            pass
        episodes.append({"guid": guid[:500], "title": _clean(item.findtext("title"), 300) or "Episode",
                         "description": _clean(_inner(item, "description", ITUNES + "summary")),
                         "audio_url": audio if audio.startswith("https://") else "", "page_url": link if link.startswith("https://") else "",
                         "published_at": when or time.time(), "duration_s": _duration(item.findtext(ITUNES + "duration"))})
    episodes.sort(key=lambda e: e["published_at"], reverse=True)
    return _clean(channel.findtext("title"), 200) or "Podcast", episodes[:MAX_EPISODES]


# ------------------------------------------------------------------------------------------------ shows and episodes
def list_shows():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM podcast_shows ORDER BY created_at").fetchall()
    conn.close()
    return rows


def get_episode(episode_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT e.*, s.title AS show_title FROM podcast_episodes e LEFT JOIN podcast_shows s ON s.id = e.show_id WHERE e.id = ?", (episode_id,)).fetchone()
    conn.close()
    return row


def list_episodes(visible_only: bool = True, limit: int = 500, offset: int = 0):
    conn = db.get_conn()
    rows = conn.execute("SELECT e.*, s.title AS show_title FROM podcast_episodes e LEFT JOIN podcast_shows s ON s.id = e.show_id "
                        + ("WHERE e.hidden = 0 " if visible_only else "") + "ORDER BY e.published_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    conn.close()
    return rows


def count_episodes() -> int:
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM podcast_episodes WHERE hidden = 0").fetchone()[0]
    conn.close()
    return n


def add_show(kind: str, url: str, title: str = "") -> tuple[bool, str]:
    url = (url or "").strip()
    if kind == "spotify":
        ref = spotify_ref(url)
        if not ref:
            return False, "That is not a Spotify show or episode link. Open it in Spotify, choose Share, then Copy link."
        conn = db.get_conn()
        sid = uuid.uuid4().hex[:10]
        if ref[0] == "show":
            conn.execute("INSERT INTO podcast_shows (id, title, kind, url, spotify_id, created_at) VALUES (?, ?, 'spotify', ?, ?, ?)", (sid, title.strip()[:200] or "Spotify show", url, ref[1], time.time()))
        else:
            show = conn.execute("SELECT id FROM podcast_shows WHERE kind = 'spotify' ORDER BY created_at LIMIT 1").fetchone()
            if not show:
                conn.execute("INSERT INTO podcast_shows (id, title, kind, url, spotify_id, created_at) VALUES (?, 'Spotify', 'spotify', '', '', ?)", (sid, time.time()))
                show_id = sid
            else:
                show_id = show["id"]
            conn.execute("INSERT OR IGNORE INTO podcast_episodes (id, show_id, guid, title, spotify_id, published_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (uuid.uuid4().hex[:10], show_id, "spotify:" + ref[1], title.strip()[:300] or "Episode", ref[1], time.time(), time.time()))
        conn.commit()
        conn.close()
        return True, "Added from Spotify."
    if kind == "feed":
        try:
            show_title, episodes = parse_feed(fetch_feed(url))
        except (ValueError, httpx.HTTPError) as exc:
            return False, str(exc) if isinstance(exc, ValueError) else "We could not reach that feed. Check the link and try again."
        conn = db.get_conn()
        if conn.execute("SELECT 1 FROM podcast_shows WHERE kind = 'feed' AND url = ?", (url,)).fetchone():
            conn.close()
            return False, "That feed is already added."
        sid = uuid.uuid4().hex[:10]
        conn.execute("INSERT INTO podcast_shows (id, title, kind, url, spotify_id, created_at) VALUES (?, ?, 'feed', ?, '', ?)", (sid, title.strip()[:200] or show_title, url, time.time()))
        conn.commit()
        conn.close()
        _store_episodes(sid, episodes)
        return True, f"Added {show_title} with {len(episodes)} episode(s)."
    return False, "Choose a feed or a Spotify link."


def _store_episodes(show_id: str, episodes: list[dict]) -> int:
    conn = db.get_conn()
    added = 0
    for ep in episodes:
        cur = conn.execute("INSERT OR IGNORE INTO podcast_episodes (id, show_id, guid, title, description, published_at, audio_url, page_url, duration_s, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex[:10], show_id, ep["guid"], ep["title"], ep["description"], ep["published_at"],
                                                                      ep["audio_url"], ep["page_url"], ep["duration_s"], time.time()))
        if cur.rowcount:
            added += 1
        else:                                                                    # already known: refresh the wording and the audio, never the transcript
            conn.execute("UPDATE podcast_episodes SET title = ?, description = ?, audio_url = ?, page_url = ?, duration_s = ? WHERE show_id = ? AND guid = ?",
                         (ep["title"], ep["description"], ep["audio_url"], ep["page_url"], ep["duration_s"], show_id, ep["guid"]))
    conn.commit()
    conn.close()
    return added


def pull_all() -> tuple[int, list[str]]:
    """Re-read every feed. Returns (new episodes, problems)."""
    new, problems = 0, []
    for show in list_shows():
        if show["kind"] != "feed":
            continue
        try:
            _title, episodes = parse_feed(fetch_feed(show["url"]))
            new += _store_episodes(show["id"], episodes)
        except (ValueError, httpx.HTTPError) as exc:
            problems.append(f"{show['title']}: {exc if isinstance(exc, ValueError) else 'could not be reached'}")
    return new, problems


# ------------------------------------------------------------------------------------------------ public pages
def _ctx(request: Request, **extra) -> dict:
    return {**A._marketing_context(home="/"), **extra}


def _public(ep) -> dict:
    d = dict(ep)
    d["embed"] = spotify_embed("episode", ep["spotify_id"]) if ep["spotify_id"] else ""
    d["has_audio"] = bool(ep["audio_url"]) and ep["audio_url"].startswith("https://")
    d["minutes"] = round((ep["duration_s"] or 0) / 60) or None
    d["date"] = datetime.fromtimestamp(ep["published_at"] or ep["created_at"], tz=timezone.utc).strftime("%d %b %Y")
    d["transcript_paragraphs"] = [p for p in re.split(r"\n\s*\n|\n", ep["transcript_text"] or "") if p.strip()]
    return d


@router.get("/podcast", response_class=HTMLResponse)
def podcast_page(request: Request, page: int = 1):
    total = count_episodes()
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(int(page or 1), 1), pages)
    shows = [s for s in list_shows() if s["kind"] == "spotify" and s["spotify_id"]]
    return A.templates.TemplateResponse(request, "podcast.html", _ctx(
        request, episodes=[_public(e) for e in list_episodes(True, PAGE_SIZE, (page - 1) * PAGE_SIZE)], page=page, pages=pages,
        spotify_shows=[{"title": s["title"], "embed": spotify_embed("show", s["spotify_id"])} for s in shows[:1]]))


@router.get("/podcast/{episode_id}", response_class=HTMLResponse)
def podcast_episode(request: Request, episode_id: str):
    ep = get_episode(episode_id)
    if not ep or ep["hidden"]:
        return HTMLResponse("Episode not found.", status_code=404)
    return A.templates.TemplateResponse(request, "podcast_episode.html", _ctx(request, ep=_public(ep)))


# ------------------------------------------------------------------------------------------------ the webhook that delivers a transcript
def _signature_ok(secret: str, headers, body: bytes) -> bool:
    stamp, sent = headers.get("x-kauli-timestamp", ""), headers.get("x-kauli-signature", "")
    try:
        if not secret or abs(time.time() - float(stamp)) > 600:
            return False
    except ValueError:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def _set_status(episode_id: str, status: str, error: str = "", text: str | None = None) -> None:
    conn = db.get_conn()
    if text is not None:
        conn.execute("UPDATE podcast_episodes SET transcript_status = ?, transcript_error = ?, transcript_text = ?, transcript_at = ? WHERE id = ?", (status, error, text, time.time(), episode_id))
    else:
        conn.execute("UPDATE podcast_episodes SET transcript_status = ?, transcript_error = ? WHERE id = ?", (status, error, episode_id))
    conn.commit()
    conn.close()


@router.post(HOOK_PATH)
async def transcript_webhook(request: Request):
    """Kauli's own 'order.ready' webhook arriving at this site. Only a message signed with the publishing account's webhook secret is believed."""
    body = await request.body()
    try:
        payload = json.loads(body)
        order_id = str((payload.get("order") or {}).get("id") or "")
    except (ValueError, AttributeError):
        return JSONResponse({"error": "bad body"}, status_code=400)
    order = db.get_order(order_id) if order_id else None
    hook = db.get_client_webhook(order["client_id"]) if order else None
    if not order or not hook or not _signature_ok(hook["secret"], request.headers, body):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    conn = db.get_conn()
    ep = conn.execute("SELECT * FROM podcast_episodes WHERE transcript_order_id = ?", (order_id,)).fetchone()
    conn.close()
    if not ep or payload.get("event") != "order.ready":
        return JSONResponse({"ok": True, "ignored": True})
    links = (payload.get("order") or {}).get("deliverables") or {}
    link = links.get("txt") or links.get("transcript") or ""
    own = urlparse(A._public_base_url(request))
    if not link or urlparse(link).netloc != own.netloc:                  # the signed link must be this site's own
        _set_status(ep["id"], "failed", "The message did not carry a transcript link from this site.")
        return JSONResponse({"ok": False, "error": "no transcript link"}, status_code=422)
    try:
        r = httpx.get(link, timeout=60, follow_redirects=False)
        r.raise_for_status()
        text = re.sub(r"\n{3,}", "\n\n", r.text.replace("\r\n", "\n")).strip()
        if not text:
            raise ValueError("the transcript was empty")
    except Exception as exc:  # noqa: BLE001 - the episode shows what went wrong; staff can send it again
        _set_status(ep["id"], "failed", f"The transcript could not be fetched: {str(exc)[:200]}")
        return JSONResponse({"ok": False, "error": "fetch failed"}, status_code=502)
    _set_status(ep["id"], "published", "", text)
    return JSONResponse({"ok": True, "episode": ep["id"], "characters": len(text)})


# ------------------------------------------------------------------------------------------------ staff
def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


def _back(kind: str, text: str):
    return RedirectResponse(f"/staff/podcast?{kind}={quote(text)}", status_code=303)


@router.get("/staff/podcast", response_class=HTMLResponse)
def staff_page(request: Request):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    publisher = _publisher()
    hook = db.get_client_webhook(publisher["id"]) if publisher else None
    base = A._public_base_url(request).rstrip("/")
    episodes = []
    for e in list_episodes(False, 200):
        d = dict(e)
        o = db.get_order(e["transcript_order_id"]) if e["transcript_order_id"] else None
        d["order_status"] = o["status"] if o else ""
        episodes.append(d)
    return A.templates.TemplateResponse(request, "staff_podcast.html", {
        "user": user, "theme": "dark", "shows": list_shows(), "episodes": episodes, "publisher": publisher, "hook_url": base + HOOK_PATH,
        "hook_ready": bool(hook and hook["url"] == base + HOOK_PATH), "languages": sorted(A.LANGUAGE_NAMES.items(), key=lambda kv: kv[1]),
        "notice": request.query_params.get("notice"), "error": request.query_params.get("error")})


@router.post("/staff/podcast/shows")
def staff_add_show(request: Request, kind: str = Form(""), url: str = Form(""), title: str = Form("")):
    if not _staff(request):
        return RedirectResponse("/login")
    ok, message = add_show(kind, url, title)
    return _back("notice" if ok else "error", message)


@router.post("/staff/podcast/pull")
def staff_pull(request: Request):
    if not _staff(request):
        return RedirectResponse("/login")
    new, problems = pull_all()
    if problems:
        return _back("error", "; ".join(problems))
    return _back("notice", f"Up to date. {new} new episode(s).")


@router.post("/staff/podcast/shows/{show_id}/remove")
def staff_remove_show(request: Request, show_id: str):
    if not _staff(request):
        return RedirectResponse("/login")
    conn = db.get_conn()
    conn.execute("DELETE FROM podcast_episodes WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM podcast_shows WHERE id = ?", (show_id,))
    conn.commit()
    conn.close()
    return _back("notice", "Removed from the Podcast page.")


@router.post("/staff/podcast/episodes/{episode_id}/hide")
def staff_hide(request: Request, episode_id: str, hidden: str = Form("1")):
    if not _staff(request):
        return RedirectResponse("/login")
    conn = db.get_conn()
    conn.execute("UPDATE podcast_episodes SET hidden = ? WHERE id = ?", (1 if hidden == "1" else 0, episode_id))
    conn.commit()
    conn.close()
    return _back("notice", "Hidden from the Podcast page." if hidden == "1" else "Shown on the Podcast page.")


@router.post("/staff/podcast/webhook/connect")
def staff_connect_webhook(request: Request):
    """Point the publishing account's Kauli webhook at this site, once."""
    if not _staff(request):
        return RedirectResponse("/login")
    publisher = _publisher()
    if not publisher:
        return _back("error", "No publishing account yet. Set PODCAST_PUBLISHER_EMAIL, or add an address to KAULI_TEST_CLIENT_EMAILS, and make sure that person has signed up.")
    db.set_client_webhook(publisher["id"], A._public_base_url(request).rstrip("/") + HOOK_PATH)
    return _back("notice", f"Connected: {publisher['email']} now sends finished transcripts to this site.")


def _download(url: str, dest: Path) -> int:
    total = 0
    for _ in range(4):
        if not safe_url(url):
            raise ValueError("The audio address cannot be used.")
        with httpx.stream("GET", url, timeout=120, follow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(1024 * 256):
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise ValueError("The audio file is larger than the upload limit.")
                    fh.write(chunk)
            return total
    raise ValueError("The audio address redirected too many times.")


@router.post("/staff/podcast/episodes/{episode_id}/transcribe")
def staff_transcribe(request: Request, episode_id: str, source_lang: str = Form("en")):
    """Order the transcript for an episode through the normal order flow. The transcript reaches the page by webhook when it is ready."""
    if not _staff(request):
        return RedirectResponse("/login")
    ep = get_episode(episode_id)
    publisher = _publisher()
    if not ep or not ep["audio_url"]:
        return _back("error", "This episode has no audio file to transcribe. Spotify-only episodes need a feed with the audio.")
    if not publisher:
        return _back("error", "No publishing account yet. Set PODCAST_PUBLISHER_EMAIL, or add an address to KAULI_TEST_CLIENT_EMAILS.")
    if source_lang not in A.LANGUAGE_NAMES:
        return _back("error", "Pick the language spoken in the episode.")
    hook = db.get_client_webhook(publisher["id"])
    base = A._public_base_url(request).rstrip("/")
    if not hook or hook["url"] != base + HOOK_PATH:
        return _back("error", "Connect the webhook first (the button above), so the transcript can come back to this page.")
    user = dict(publisher)
    user["client_scope_id"] = publisher["id"]
    request.state.api_user = user
    tmp_dir = Path(tempfile.mkdtemp(prefix="kauli-podcast-"))
    tmp = tmp_dir / "episode.mp3"
    try:
        _download(ep["audio_url"], tmp)
        from starlette.datastructures import Headers
        from fastapi import UploadFile
        with open(tmp, "rb") as fh:
            upload = UploadFile(file=fh, filename=re.sub(r"[^A-Za-z0-9._ -]+", "", ep["title"])[:80].strip() + ".mp3", headers=Headers({"content-type": "audio/mpeg"}))
            resp = A.create_order(
                request, audio=upload, upload_key="", youtube_url="", source_lang=source_lang, target_lang=source_lang,
                service_level="transcribe", addon_video_deliverables="", addon_human_voice_over="", addon_premium_voice="",
                premium_voice_id="", addon_audio_cleanup="", instr_speaker_ids="1", instr_verbatim_level="clean_read",
                instr_transcribe_lyrics="", instr_use_italics="", instr_existing_subs="ignore", instr_no_audio="tag",
                instr_wrong_language="tag", instr_instrumental_only="tag", instr_notes="Podcast episode for the Kauli Podcast page.",
                style_guide=None, idempotency_key=f"podcast-{ep['id']}", folder_name="Podcast", voice_clone_consent="", rush="", review_tier="ai")
    except Exception as exc:  # noqa: BLE001
        return _back("error", f"The order could not be placed: {str(exc)[:200]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    loc = resp.headers.get("location", "") if isinstance(resp, RedirectResponse) else ""
    found = re.match(r"^/client/orders/([0-9a-f]+)", loc)
    if not found:
        return _back("error", "The order could not be placed. Check the publishing account's plan and minutes.")
    conn = db.get_conn()
    conn.execute("UPDATE podcast_episodes SET transcript_order_id = ?, transcript_status = 'working', transcript_error = '' WHERE id = ?", (found.group(1), ep["id"]))
    conn.commit()
    conn.close()
    return _back("notice", "Transcript ordered. It appears on the episode by itself when Kauli delivers it.")
