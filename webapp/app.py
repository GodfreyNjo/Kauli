"""Local demo platform for Kauli - client upload -> processing -> staff
review -> delivery, as a clickable UI on top of the real pipeline.

Auth is real (Supabase email/password) but everything else here still
isn't production: no payments, not deployed, single-tenant SQLite. It
exists so you can experience/demo the end-to-end flow before the roadmap's
later phases get built for real. Run with:
    uvicorn webapp.app:app --reload --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import asyncio
import functools
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import math
import threading
import time
import traceback
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import Body, FastAPI, Request, UploadFile, Form, File
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from . import billing, db, supabase_auth, worker, upload_security, logging_setup, rate_limit, medium_publish, devto_publish, blog_ai_assist, order_ai_assist, youtube_poll, mailer, notifications, tat, r2_uploads, ip_intel, ga_events, nav_icons, youtube_captions, lead_playbook, charts, transcript_export, mcp_server, notion_export  # noqa: E402
from kauli import caption_formats, timing  # noqa: E402
from kauli.models import Job, Segment, Word, apply_speaker_paragraphs, label_speakers, split_off_speaker_tag  # noqa: E402
from kauli.mixer import build_timeline, write_wav_mono, extract_reference_clip, extract_audio_window, time_stretch  # noqa: E402
from kauli.pipeline import split_gap_span, translate_segment, translate_gap_tag, apply_stretch_fit, resolve_voice_for_segment, _insert_non_speech_segments, spell_out_text, render_gap_audio, strip_bracket_tags_for_tts  # noqa: E402
from kauli.providers import get_asr, get_mt, get_tts  # noqa: E402
from kauli.providers.tts import PIPER_VOICES  # noqa: E402
from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES  # noqa: E402
from kauli.providers.asr_cloud import cheap_providers_configured  # noqa: E402
from kauli.providers import own_quality  # noqa: E402
from kauli import screen_text  # noqa: E402
from . import js_strings, staff_integrations, locales as site_locales, seo_languages, demo_content, human_review_content, meeting_bot, ui_text, locations, solution_pages, blog_guides, account_hub, blog_audio, cloud_import, calendar_feed, provider_health, connections, summary_templates, account_closure, winback, dunning, site_analytics, engagement, onboarding, repurpose, model_stats, transcript_reuse, page_translate, brand, seo_pages, usage_prompts, savings, weekly_kpi  # noqa: E402
from jinja2 import pass_context  # noqa: E402
from markupsafe import Markup, escape as _escape  # noqa: E402
from kauli.subtitles import to_srt, to_vtt, cue_dicts, _wrap_caption_text, _display_end_ms  # noqa: E402

WEBAPP_DIR = Path(__file__).parent
UPLOAD_DIR = WEBAPP_DIR / "data" / "uploads"
OUTPUT_DIR = WEBAPP_DIR / "data" / "output"
AVATAR_DIR = WEBAPP_DIR / "data" / "avatars"
RECEIPT_DIR = WEBAPP_DIR / "data" / "receipts"
BLOG_COVER_DIR = WEBAPP_DIR / "data" / "blog_covers"
ALLOWED_AVATAR_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
ALLOWED_IMAGE_TYPES = ALLOWED_AVATAR_TYPES  # same real formats, shared for any plain-image upload (blog covers too)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def is_video_file(path: str) -> bool:
    """Whether Ereri should render <video> (for visual context while
    editing) instead of an <audio> element - see editor.html."""
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def probe_duration_minutes(path: str) -> float:
    """Real duration via ffprobe (unlike kauli.pipeline.probe_duration_ms,
    which only reads WAV headers) - this is what usage billing charges
    against, so it needs to work on the mp3/mp4/m4a/whatever a client
    actually uploads, not just our own intermediate WAV files."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip()) / 60.0


# YouTube increasingly blocks yt-dlp's normal (web-client) requests with
# "Sign in to confirm you're not a bot" - a real, live, ongoing arms race
# (YouTube tightens it, yt-dlp/the community find the next first-party
# client that isn't yet blocked, repeat). First confirmed 2026-08-23
# (fixed then by requesting through the ANDROID client); recurred
# 2026-09-17 against a different real video, with "android"/"tv"/
# "web_safari"/"web" ALL failing too - the block was on this server's
# datacenter IP/request fingerprint itself, not any one player client.
#
# Fixed properly with a real proof-of-origin (PO) token instead of
# rotating through clients again - this is YouTube's own documented
# mechanism for "prove this request comes from a real player", and
# bgutil-ytdlp-pot-provider (see requirements.txt) is the current
# community-standard way to generate one WITHOUT needing an actual
# signed-in account's cookies on this server (unauthenticated
# visitor_data mode). It talks to a separate `bgutil-provider` sidecar
# container over the kauli_net Docker network (see this VM's `docker run`
# history) - base_url here is that container's name, resolved via
# Docker's own DNS on that user-defined network, not localhost (the two
# containers don't share a network namespace). Falls back to plain
# unauthenticated requests if the sidecar is ever down, same as yt-dlp
# always did before this existed - never hard-fails the whole order over
# a missing PO token. Real signed-in-account cookies remain the documented
# fallback if YouTube ever closes this off too, but that's a deliberate
# account-security tradeoff to make only if this stops being enough.
YT_DLP_EXTRACTOR_ARGS = {
    "extractor_args": {
        "youtube": {"player_client": ["android", "tv", "web_safari", "web"]},
        "youtubepot-bgutilhttp": {"base_url": ["http://bgutil-provider:4416"]},
    }
}


def _youtube_extra_opts() -> dict:
    """Everything above (client rotation, the PO-token sidecar) still left
    some real videos hitting "Sign in to confirm you're not a bot" -
    confirmed live (2026-09-17/18) that this is a datacenter-IP-level
    block on THIS SERVER's outbound IP itself, not a per-client or
    per-token gap - every client returned LOGIN_REQUIRED even with a real
    PO token attached. Two real, independent mitigations, both optional
    and inert until configured (same pattern as every other integration
    in this app), since neither is something code alone can turn on:

    - KAULI_YOUTUBE_COOKIES_FILE: a real cookies.txt from an actual
      signed-in YouTube account (Netscape format - "Get cookies.txt
      LOCALLY" or similar browser extension). yt-dlp requests AS that
      account instead of an anonymous visitor, which is a materially
      different, stronger signal to YouTube than any client/token trick.
      Treat this file as a real credential: 0600 permissions, never
      committed, rotated if the account ever gets flagged.
    - KAULI_YOUTUBE_PROXY: routes yt-dlp's requests through a residential
      proxy instead of this server's own datacenter IP - addresses the
      actual root cause search results and yt-dlp's own maintainers point
      to (YouTube trusts residential IPs more), rather than working around
      it client-side. A paid third-party service - this only points at
      one already configured by whoever sets the env var.

    sleep_interval/max_sleep_interval always apply (a real, free,
    zero-tradeoff mitigation - throttling this server's own request rate
    is good practice regardless of the two above) at a random 1-3s gap,
    which matters if a client ever queues several YouTube imports close
    together; a single request pays that cost once, not compounding."""
    opts: dict = {"sleep_interval": 1, "max_sleep_interval": 3}
    cookies_file = os.environ.get("KAULI_YOUTUBE_COOKIES_FILE", "").strip()
    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = cookies_file
    proxy = os.environ.get("KAULI_YOUTUBE_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy
    return opts


def explain_youtube_failure(exc: Exception) -> str:
    """What to tell a client when a YouTube link could not be fetched: the cause in plain words and what to do next, never the
    downloader's raw error (which is a wall of text about cookies that a client cannot act on)."""
    text = str(exc).lower()
    upload = "You can always download the video yourself and upload the file here instead, which is never blocked."
    if "confirm you" in text and "bot" in text or "sign in" in text and "cookies" in text:
        return ("YouTube is blocking automatic downloads from our servers right now, so we could not fetch this link. "
                "Please try again in a little while, or " + upload[0].lower() + upload[1:])
    if "private video" in text or "members-only" in text or "join this channel" in text:
        return "That video is private or for members only, so we cannot fetch it. Make it public or unlisted, or " + upload[0].lower() + upload[1:]
    if "not available" in text or "unavailable" in text or "removed" in text or "no longer available" in text:
        return "That video is unavailable (removed, region-locked or blocked). Check the link, or " + upload[0].lower() + upload[1:]
    if "age" in text and ("restrict" in text or "confirm your age" in text):
        return "That video is age-restricted, so we cannot fetch it automatically. " + upload
    if "live event" in text or "is live" in text or "premiere" in text:
        return "That video is a live stream or a premiere that has not finished. Try again once it has ended."
    if "unsupported url" in text or "is not a valid url" in text:
        return "That does not look like a YouTube video link. Paste the address of a single video."
    return "Couldn't fetch that YouTube link. Please check it and try again, or " + upload[0].lower() + upload[1:]


def _download_youtube(url: str, dest_dir: Path) -> tuple[Path, str, str | None]:
    """Downloads AUDIO ONLY - the pipeline (ASR/MT/TTS) only ever needs
    audio samples, and audio-only is a fraction of the size/time of the
    full video. Ereri shows the actual YouTube video for editing context
    via an embedded player instead (see editor.html + editor.js's
    MediaAdapter) - no video file touches this machine at all unless a
    burned-caption/dubbed-video deliverable is explicitly requested later
    (see render_video_deliverable, which fetches video only at that point).
    Returns (path, display_filename, youtube_video_id_or_None).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / "%(title).150s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        **YT_DLP_EXTRACTOR_ARGS,
        **_youtube_extra_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info.get("id")
        filename = Path(ydl.prepare_filename(info))
        # FFmpegExtractAudio changes the extension after post-processing.
        candidate = filename.with_suffix(".m4a")
        if candidate.exists():
            filename = candidate
        if not filename.exists():
            raise RuntimeError("download reported success but the output file is missing")
        return filename, filename.name, video_id


def fetch_youtube_video(video_id: str, dest_dir: Path) -> Path:
    """The on-demand counterpart to the audio-only fetch above: pulls the
    actual video, used only when a staff member requests a burned-caption
    or dubbed-video deliverable for a YouTube-sourced order (see
    render_video_deliverable) - not part of the normal pipeline path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{video_id}_video.mp4"
    if dest.exists():
        return dest
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(dest),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "noplaylist": True,
        **YT_DLP_EXTRACTOR_ARGS,
        **_youtube_extra_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    if not dest.exists():
        raise RuntimeError("video fetch reported success but the output file is missing")
    return dest


def render_burned_captions(video_path: Path, srt_path: Path, out_path: Path) -> None:
    """Hardsubs the SRT directly into the video frame via ffmpeg's
    subtitles filter - what a client asking for 'burned-in captions'
    actually means: the text is part of the pixels, no player support
    needed on the viewer's end."""
    # ffmpeg's subtitles filter needs colon-escaping in the path on top of
    # the usual shell quoting, since ':' also separates filter options.
    escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    # Lower-centre, clear of the bottom edge (where players and platforms crop or cover) and of the side margins:
    # Alignment=2 is bottom-centre, MarginV lifts it about a tenth of the picture off the edge. The captions
    # themselves are already two lines at most (kauli/subtitles.py).
    style = "Alignment=2,MarginV=30,MarginL=40,MarginR=40,FontSize=18,Outline=1.5,Shadow=0.5,BorderStyle=1"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
         "-vf", f"subtitles='{escaped}':force_style='{style}'",
         "-c:a", "copy", str(out_path)],
        check=True, timeout=1800,  # bounded, not unlimited - a crafted/corrupt file shouldn't hang this forever
    )


def render_dubbed_video(video_path: Path, dub_audio_path: Path, out_path: Path) -> None:
    """Replaces the video's original audio track with the dubbed one -
    what a client asking for a 'dubbed video' means: same picture, new
    voice track, ready to re-upload wherever the original video lives."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(video_path), "-i", str(dub_audio_path),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path)],
        check=True, timeout=1800,
    )


def _load_dotenv() -> None:
    """Tiny .env loader so ANTHROPIC_API_KEY etc. are actually picked up -
    nothing else in this project loads .env automatically."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
db.init_db()
db.flag_internal_by_rule()          # team, staff and test addresses are left out of the business numbers
blog_guides.seed_guides_at_startup()
db.replace_blog_text("first 5 minutes", f"first {billing.FREE_MINUTES_PER_MONTH:.0f} minutes")      # posts written when the allowance was 5
db.replace_blog_text("first 60 minutes", f"first {billing.FREE_MINUTES_PER_MONTH:.0f} minutes")
db.replace_blog_text("Pro includes 150 Instant AI minutes a month and Premium includes 400",
                     f"Pro includes {billing.PRO_MINUTES:,} Instant AI minutes a month and Premium includes {billing.PREMIUM_MINUTES:,}")
db.replace_blog_text("Premium includes 6,500, and they cover",
                     f"Premium includes {billing.PREMIUM_MINUTES:,}{billing.hours_suffix(billing.PREMIUM_MINUTES)}, and they cover")

IDLE_TIMEOUT_SECONDS = 30 * 60  # log out after 30 minutes of no requests

_PROCESS_STARTED_AT = time.time()  # for /status's uptime figure - real, not a fabricated SLA number

app = FastAPI(title="Kauli - demo platform")
# max_age here is a SLIDING idle timeout, not a fixed session lifetime:
# Starlette's SessionMiddleware re-signs the cookie (with a fresh timestamp)
# on every request where the session gets written to, and current_user()
# below writes to it on every authenticated request. So an active user's
# cookie keeps rolling forward and never expires; an idle one's signature
# ages past max_age and the next request just finds no valid session.
#
# The secret itself used to be a hardcoded, literally-named
# "local-demo-only-not-secret" string - harmless while this only ever ran
# on localhost, a real forgeable-session vulnerability the moment it's
# reachable from the internet (anyone who has this string can mint a
# valid session cookie for ANY account). Now read from KAULI_SESSION_SECRET;
# if it's missing, generate a random one for THIS process only rather than
# fall back to a known string - sessions just won't survive a restart
# until a real secret is set in .env, which is the right failure mode.
_session_secret = os.environ.get("KAULI_SESSION_SECRET")
if not _session_secret:
    _session_secret = secrets.token_hex(32)
    print("WARNING: KAULI_SESSION_SECRET not set in .env - using a random secret for this "
          "process only. Every session will be invalidated on the next restart. Set a real "
          "one before deploying anywhere reachable from the internet.", flush=True)
app.add_middleware(SessionMiddleware, secret_key=_session_secret,
                    max_age=IDLE_TIMEOUT_SECONDS,
                    # Secure flag off by default - correct for now (this
                    # only runs over plain HTTP locally; a Secure cookie is
                    # never sent over HTTP at all, which would silently
                    # break every login). Flip KAULI_HTTPS_ONLY_COOKIES=1
                    # once this is actually deployed behind real HTTPS.
                    # same_site defaults to "lax" (not set here), which is
                    # deliberate - it's real, working CSRF protection on
                    # its own for state-changing POSTs from another origin.
                    https_only=os.environ.get("KAULI_HTTPS_ONLY_COOKIES") == "1")

api_log = logging_setup.get_logger("api")


_MAX_ORDINARY_BODY_BYTES = 15 * 1024 * 1024  # 15MB - generous for any real JSON/form body that isn't a file upload


def _is_large_body_exempt(path: str) -> bool:
    """True for the real routes that legitimately carry a large multipart
    body - every one of these already has its OWN, correct-for-its-domain
    size enforcement further down the stack (upload_security.
    stream_save_with_limits' real, streamed 2GB cap for audio/video;
    presign-upload's R2 path checks head_object_size after the fact), so
    they're exempt from the blanket cap here rather than double-capped at
    a much smaller, wrong-for-video-files number. Matched by real prefix/
    suffix against request.url.path, NOT the route-template strings
    ("/client/orders/{order_id}/resume") those never equal - a request's
    actual path always has the real id substituted in."""
    if path in ("/client/orders", "/settings", "/api/v1/orders", "/try/start"):
        return True
    if path.startswith("/client/orders/") and path.endswith("/resume"):
        return True
    if path.startswith("/staff/orders/") and path.endswith("/human-voice-upload"):
        return True
    if path.startswith("/actor/orders/") and path.endswith("/upload"):
        return True
    if path.startswith("/staff/billing/confirm/"):
        return True
    if path.startswith("/staff/blog/") and path.endswith("/edit"):
        return True
    return False


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Rejects an oversized body via Content-Length BEFORE it's ever read
    into memory - every other POST/PUT route in this app (AI chat
    endpoints, webhooks, ordinary forms) has no size limit of its own at
    all; Starlette/uvicorn don't impose one by default either. Without
    this, one request with an enormous JSON or form body is a real,
    trivial memory-exhaustion DoS against any of them - the request size
    limit a real API needs, that nothing here provided until now.
    Webhooks are deliberately NOT exempt: Paystack/M-Pesa/Calendly payloads
    are always small, and the whole point of a size cap is stopping a huge
    body from being read into memory before anything (including a
    signature check) has validated it's real."""
    if not _is_large_body_exempt(request.url.path):
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_ORDINARY_BODY_BYTES:
            return JSONResponse({"error": "Request body too large."}, status_code=413)
    return await call_next(request)


# Pages a search engine has no business indexing: the signed-in app, the account utilities and machine endpoints.
_NOINDEX_PREFIXES = ("/client", "/staff", "/settings", "/actor", "/login", "/signup", "/forgot-password", "/reset-password",
                      "/logout", "/lang/", "/api/", "/webhooks/", "/billing/", "/receipts/", "/avatar/", "/notifications/",
                      "/download/", "/status", "/s/", "/r/")


def _seo_headers(request: Request, response) -> None:
    """noindex on private pages (robots.txt only asks crawlers not to visit; this tells them not to list a URL they
    reached anyway), and the language of each public page so crawlers and AI engines do not have to guess it."""
    path = request.url.path
    if path.startswith(_NOINDEX_PREFIXES):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    elif response.headers.get("content-type", "").startswith("text/html") and "content-language" not in response.headers:
        code = path.strip("/")
        response.headers["Content-Language"] = site_locales.LOCALES[code][1] if code in site_locales.LOCALES else "en"
        # Allow large image previews and full snippets in search results and AI answers for every public page.
        response.headers.setdefault("X-Robots-Tag", "max-image-preview:large, max-snippet:-1, max-video-preview:-1")


@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    """One UUID per request, correlating every log line it produces (and
    handed back in a response header) - the local, no-account-needed
    version of what the doc's OpenTelemetry/distributed-tracing section
    asks for. Wiring a real tracing backend later is a matter of
    forwarding this id, not restructuring anything - every request
    already carries one."""
    trace_id = uuid.uuid4().hex[:16]
    request.state.trace_id = trace_id
    started = time.time()

    # Global per-IP ceiling, generous enough that normal browsing never
    # gets near it - static assets excluded (one page load fetches many
    # of those legitimately). Tighter, endpoint-specific limits (login,
    # order submission) layer on top of this, not instead of it.
    if not request.url.path.startswith("/static/"):
        allowed, retry_after = rate_limit.check(f"ip:{rate_limit.client_ip(request)}", limit=300, window_s=60)
        if not allowed:
            return JSONResponse({"error": "Too many requests"}, status_code=429,
                                 headers={"Retry-After": str(retry_after), "X-Trace-Id": trace_id})

    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    _seo_headers(request, response)
    try:                                        # our own cookie-free page view counter (site_analytics.py); never in the way of a page
        _ua = request.headers.get("user-agent", "")
        if site_analytics.should_count(request.method, request.url.path, response.status_code, response.headers.get("content-type", ""), _ua,
                                       request.headers.get("dnt") == "1" or request.headers.get("sec-gpc") == "1"):
            _session = request.scope.get("session") or {}
            asyncio.get_running_loop().run_in_executor(
                None, site_analytics.record_hit, request.url.path, request.url.query, request.headers.get("referer", ""),
                request.headers.get("host", ""), rate_limit.client_ip(request), _ua, request.headers.get("cf-ipcountry"), _session.get("user_id"))
    except Exception:  # noqa: BLE001
        pass
    api_log.info("request", extra={
        "trace_id": trace_id, "method": request.method, "path": request.url.path,
        "status_code": response.status_code, "duration_ms": round((time.time() - started) * 1000, 1),
    })
    return response


# Real third-party origins this site actually loads, checked directly
# rather than guessed: Google Fonts (base.html), Cloudflare Web Analytics
# (base.html's beacon script + its RUM endpoint), Calendly's widget
# (marketing.html - script tag + the iframe/API calls it makes on its own),
# and the YouTube IFrame API (Ereri's editor - a youtube-sourced order
# embeds the real source video for editing context via editor.js's
# createYouTubeAdapter). Missing the YouTube origins here was a real bug:
# the player never loaded at all for any youtube-sourced order, silently
# blocked by this exact header - script-src blocked the iframe_api script
# itself, and frame-src (default-src's fallback, since frame-src wasn't
# set at all) blocked the actual embedded player iframe it creates.
# Paystack needs no entry here at all: checkout is a server-side redirect
# to Paystack's own hosted page (billing.py's authorization_url), never an
# embedded script or iframe on this site.
#
# https://*.r2.cloudflarestorage.com in connect-src is for the direct-to-R2
# upload flow (webapp/r2_uploads.py, client_wizard.js's bindUploadProgress) -
# a real, live-reproduced bug the first time this shipped: the presigned PUT
# itself was correctly signed and CORS was correctly configured on the
# bucket, but the browser never even got that far - THIS app's own CSP
# connect-src had no R2 entry at all, so it blocked the fetch before any
# CORS check ran. Confirmed via a real browser console error ("violates ...
# connect-src ... Refused to connect"), not just inferred - curl doesn't
# enforce CSP or CORS, so testing the presigned URL with curl alone missed
# this entirely.
#
# https://checkout.paystack.com in form-action - a second, real, live-
# reproduced instance of the exact same class of bug: order_pay.html's
# <form> posts to this app's own /client/orders/{id}/pay ('self' - fine),
# which then issues a real 303 redirect to Paystack's hosted checkout
# page. Chrome enforces form-action against the FINAL destination after
# a redirect, not just the form's own action URL - so every real payment
# attempt was silently blocked, in every browser (including Incognito,
# which ruled out an extension), even though the server-side redirect
# itself was fast and correct every single time. Confirmed via the exact
# console message: "Sending form data to 'https://checkout.paystack.com/
# ...' violates ... form-action 'self'. The request has been blocked."
# accounts.google.com/gsi/* entries below are Google Identity Services (the
# One Tap prompt, see _one_tap.html) - client, connect and frame per
# Google's own documented CSP requirements for gis/client. Without frame-src
# specifically, the prompt's own iframe silently never appears at all (same
# class of bug as the YouTube player note above - default-src has no
# fallback here since frame-src is explicitly set).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com https://assets.calendly.com "
    "https://www.youtube.com https://www.googletagmanager.com https://accounts.google.com "
    "https://www.dropbox.com https://apis.google.com https://js.live.net; "
    # style-src/font-src: api.fontshare.com serves the @font-face CSS itself
    # (style-src), cdn.fontshare.com serves the actual woff2/woff/ttf files
    # it points at (font-src) - same split as the existing Google Fonts
    # pair (fonts.googleapis.com serves CSS, fonts.gstatic.com serves files).
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://api.fontshare.com "
    "https://accounts.google.com https://www.gstatic.com; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.fontshare.com; "
    "img-src 'self' data: https://i.ytimg.com https://*.googleusercontent.com https://www.gstatic.com https://ssl.gstatic.com; "
    # google-analytics.com/analytics.google.com: real bug found and fixed
    # 2026-08-30 - GA's own script (script-src above) was allowed to load,
    # but the actual hit it sends on every pageview was still silently
    # blocked here the whole time, on every visitor who'd accepted
    # analytics cookies. Confirmed live via a real CSP violation in the
    # console before this fix, confirmed gone after it.
    "connect-src 'self' https://cloudflareinsights.com https://static.cloudflareinsights.com "
    "https://calendly.com https://*.calendly.com https://*.r2.cloudflarestorage.com "
    "https://www.google-analytics.com https://*.google-analytics.com https://*.analytics.google.com "
    "https://accounts.google.com https://www.googleapis.com https://content.googleapis.com https://api.dropboxapi.com https://login.live.com; "
    "frame-src https://calendly.com https://*.calendly.com https://www.youtube.com https://www.youtube-nocookie.com https://accounts.google.com "
    "https://docs.google.com https://content.googleapis.com https://www.dropbox.com; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self' https://checkout.paystack.com"
)
# 'unsafe-inline' on script-src is a real, known trade-off, not an
# oversight: this app's templates use inline <script> blocks throughout
# (dozens of them) rather than nonces, and rewriting every one to use a
# per-request nonce is a much bigger refactor than this pass covers. Worth
# doing later for defense-in-depth; the actual XSS exposure it protects
# against is already low here (autoescape is on everywhere, the only two
# |safe uses are staff-authored blog HTML and JSON explicitly escaped
# against script-tag breakout - see blog_post.html and the editor route).


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = _CSP
    # HSTS only means anything - and is only safe to send - once this is
    # actually served over real HTTPS. request.url.scheme is NOT that check
    # here: this app sits behind a Cloudflare Tunnel, which hands uvicorn a
    # plain-HTTP connection regardless of what the real visitor used (same
    # root cause _public_base_url's own docstring documents for the OAuth
    # redirect URL) - confirmed live: this header was silently never being
    # sent to a single real visitor despite the site genuinely being all-
    # HTTPS (curl https://kauli-forgemedia.com/ showed no
    # Strict-Transport-Security header at all before this fix).
    # x-forwarded-proto is Cloudflare's own standard header for exactly
    # this, set the same trustworthy way cf-connecting-ip already is (see
    # rate_limit.client_ip) - never trust it from a source that isn't
    # actually behind Cloudflare, but this app only serves real internet
    # traffic through the tunnel.
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEBAPP_DIR / "templates"))


def _static_version(filename: str) -> int:
    """The file's own real last-modified time, as a cache-busting ?v=
    query string on every <script src>/<link href> - a stale cached
    editor.js was a real, confirmed bug: this session edited that file
    many times, and a browser that kept an old cached copy while
    editor.html (a different file, fetched fresh) moved on could end up
    with mismatched JS and HTML - e.g. old JS trying to bind a click
    handler to a button a newer template had already removed, throwing
    partway through initialization and silently killing every handler
    meant to register AFTER it (tab switching, shortcuts, all of it).
    Falls back to a fixed number rather than crashing if the file's
    somehow missing - a bad cache-buster is still better than a 500."""
    try:
        return int((WEBAPP_DIR / "static" / filename).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["static_version"] = _static_version
templates.env.filters["timestamp_to_str"] = lambda ts: datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")
templates.env.filters["date_only"] = lambda ts: datetime.fromtimestamp(ts or 0).strftime("%b %d, %Y")
# Unambiguous long form for formal documents (receipts, invoices) - "Aug 22,
# 15:48" is fine for an activity feed, not for something someone might file
# for their own records.
templates.env.filters["timestamp_to_date"] = lambda ts: datetime.fromtimestamp(ts).strftime("%B %d, %Y")
templates.env.filters["short_date"] = lambda ts: datetime.fromtimestamp(ts).strftime("%b %d, %Y")


def _time_ago(ts: float | None) -> str:
    """"Never" for a real NULL (see users.last_seen_at's migration comment
    on why that's not backfilled) - never fabricates a "just now" for an
    account this app genuinely has no record of. Coarse on purpose (days,
    not seconds) - staff scanning a client list need "is this account
    still coming back", not a live countdown."""
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{max(1, int(s // 60))}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


templates.env.filters["time_ago"] = _time_ago


_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"\S+")


def _reading_time(body_html: str) -> str:
    """"N min read" - industry-standard estimate (~200 words/minute, the
    figure most style guides and CMSs settle on) off the real word count of
    the rendered post, tags stripped first so a URL inside an href or a
    class name never inflates it. Always at least 1 - a real reader never
    sees "0 min read" on a short post."""
    if not body_html:
        return "1 min read"
    text = _TAG_RE.sub(" ", body_html)
    words = len(_WORD_RE.findall(text))
    minutes = max(1, round(words / 200))
    return f"{minutes} min read"


templates.env.filters["reading_time"] = _reading_time


# Real content-navigation links, not "backlinks" - a backlink is a link
# FROM another site TO ours, which nothing on our own page can create.
# What this actually does: hyperlink the first mention of a key term in a
# post to (a) a more specific page of ours on the same topic, spreading
# real internal link equity and giving a reader somewhere useful to go
# next, or (b) a genuinely authoritative external source for a general
# term we don't have our own page about (Wikipedia, MDN, an official spec)
# - standard, honest editorial practice that builds topical trust, not an
# attempt to manufacture backlinks. Ordered longest-phrase-first so e.g.
# "voice cloning consent" (if ever used verbatim) would take priority over
# the shorter "voice cloning" - matters because only the FIRST match wins
# per post, see _autolink_key_terms.
_GLOSSARY_LINKS: list[tuple[str, str, bool]] = [
    # phrase (matched case-insensitively) -> (url, is_external)
    ("clean read", "/blog/clean-read-vs-verbatim-choosing-the-right-transcript-style", False),
    ("full verbatim", "/blog/clean-read-vs-verbatim-choosing-the-right-transcript-style", False),
    ("voice cloning", "/blog/voice-cloning-consent-gate", False),
    ("code-switching", "/blog/sheng-vs-standard-swahili-machine-translation", False),
    ("Sheng", "/blog/sheng-vs-standard-swahili-machine-translation", False),
    ("video dubbing", "/solutions/video-dubbing", False),
    ("video transcription", "/solutions/video-transcription", False),
    ("video translation", "/solutions/video-translation", False),
    ("video localization", "/solutions/video-localization", False),
    ("audio to text", "/solutions/audio-to-text", False),
    ("podcast transcription", "/solutions/podcast-transcription", False),
    ("interview transcription", "/solutions/interview-transcription", False),
    ("YouTube to text", "/solutions/youtube-video-to-text", False),
    ("closed captions", "/solutions/captioning", False),
    ("voice over", "/solutions/voice-over", False),
    ("e-learning", "/solutions/e-learning", False),
    ("formatting standards", "/formatting-standards", False),
    ("automatic speech recognition", "https://en.wikipedia.org/wiki/Speech_recognition", True),
    ("text-to-speech", "https://en.wikipedia.org/wiki/Speech_synthesis", True),
    ("WebVTT", "https://developer.mozilla.org/en-US/docs/Web/API/WebVTT_API", True),
    ("SubRip", "https://en.wikipedia.org/wiki/SubRip", True),
    ("Swahili language", "https://en.wikipedia.org/wiki/Swahili_language", True),
    ("Kikuyu language", "https://en.wikipedia.org/wiki/Kikuyu_language", True),
    ("Gĩkũyũ", "https://en.wikipedia.org/wiki/Kikuyu_language", True),
    ("deepfake", "https://en.wikipedia.org/wiki/Deepfake", True),
    ("European Accessibility Act", "https://ec.europa.eu/social/main.jsp?catId=1202", True),
]
_GLOSSARY_LINKS.sort(key=lambda t: -len(t[0]))

_HTML_TOKEN_RE = re.compile(r"(<[^>]+>)")
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z0-9]+)")
_SKIP_LINKING_INSIDE = {"a", "h1", "h2", "h3", "h4", "h5", "h6", "script", "style"}


def _autolink_key_terms(body_html: str, own_slug: str = "") -> str:
    """Hyperlinks the FIRST mention of each glossary term in this post - one
    link per term, never more, so a term mentioned five times doesn't turn
    into five identical links (that reads as spam, not navigation). Never
    links inside an existing <a>, inside a heading, or a term whose own
    target IS this post (no self-link to the page you're already on)."""
    if not body_html:
        return body_html
    used_terms: set[str] = set()
    tokens = _HTML_TOKEN_RE.split(body_html)
    skip_depth = 0  # >0 while inside an <a>/heading/script/style span

    for i, tok in enumerate(tokens):
        if tok.startswith("<"):
            m = _TAG_NAME_RE.match(tok)
            if m and m.group(1).lower() in _SKIP_LINKING_INSIDE:
                skip_depth += -1 if tok.startswith("</") else 1
                skip_depth = max(0, skip_depth)
            continue
        if skip_depth > 0 or not tok.strip():
            continue
        # At most ONE substitution per token (break on first hit): once a
        # token contains real markup, testing further phrases against it
        # again risks a later phrase matching text that's now inside the
        # href we just inserted, not the visible prose - a plain text
        # token starting the loop clean is the invariant this whole
        # function depends on, so it can never be violated mid-loop.
        for phrase, url, external in _GLOSSARY_LINKS:
            if phrase in used_terms:
                continue
            if own_slug and url.endswith(own_slug):
                continue
            pattern = re.compile(r"(?<![\w-])(" + re.escape(phrase) + r")(?![\w-])", re.IGNORECASE)
            new_tok, n = pattern.subn(
                (f'<a href="{url}" target="_blank" rel="noopener">\\1</a>' if external
                 else f'<a href="{url}">\\1</a>'),
                tok, count=1,
            )
            if n:
                tokens[i] = new_tok
                used_terms.add(phrase)
                break
    return "".join(tokens)


templates.env.filters["autolink_terms"] = _autolink_key_terms
# Single source of truth for the signup-form hint text - defined once
# alongside the actual policy in supabase_auth.py, not retyped per route.
templates.env.globals["password_policy_hint"] = supabase_auth.PASSWORD_POLICY_HINT
# A callable, not a value baked in at startup - the top banner (base.html)
# needs this to be honest about whether Paystack is actually live RIGHT
# NOW, not whatever it was when the process started.
templates.env.globals["paystack_live_mode"] = billing._paystack_live_mode
templates.env.globals["password_min_length"] = supabase_auth.PASSWORD_MIN_LENGTH
templates.env.globals["free_minutes"] = billing.FREE_MINUTES_PER_MONTH
# The client/staff deadline time-bar (order_detail.html, staff_dashboard.html)
# always wants "right now" freshly evaluated at render time, not a value
# computed once at request-handling time and threaded through every route
# that shows an order - same reasoning as paystack_live_mode above.
templates.env.globals["tat_status"] = tat.time_status
# Client-facing status label - the raw underscore-replace fallback
# ("editor_returned" -> "editor returned", "returned_to_client" ->
# "returned to client") reads like an internal system log, not something
# written for the person paying for this. Real gaps this closes:
# "editor_returned" named a Kauli-internal role the client has no reason
# to know about, and told them nothing about whether THEY need to do
# anything; "returned_to_client" and "pending_payment" both mean "we're
# waiting on you" but read as passive/administrative instead of
# actionable. Every other status still needs a human label too, just one
# that's less of a rewrite - this fully replaces the old single-status
# override.
_CLIENT_STATUS_LABELS = {
    "pending_payment": "Awaiting your payment",
    "queued": "Queued",
    "processing": "In progress",
    "awaiting_review": "In staff review",
    "editor_returned": "Being revised",
    "ready_for_delivery": "Your order is ready",
    "delivered": "Delivered",
    "returned_to_client": "Needs your input",
    "failed": "Something went wrong - we're on it",
    "dead_letter": "Something went wrong - we're on it",
}
templates.env.filters["client_status_label"] = (
    lambda status: _CLIENT_STATUS_LABELS.get(status, status.replace("_", " "))
)


def _duration_short(seconds) -> str:
    """"2h 15m" / "45m" / "3d 2h" - the staff queue's "time in stage"
    column (see staff_dashboard.html). Real elapsed time since
    status_changed_at, not an estimate."""
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


templates.env.filters["duration_short"] = _duration_short


def _short_day(day: str) -> str:
    """"2026-10-05" -> "Oct 5" for a chart tick; anything unexpected is shown as it came."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.strftime('%b')} {d.day}"
    except (TypeError, ValueError):
        return str(day)


def _daily_trend_svg(daily_trend: list[dict], width: int = 760, height: int = 240) -> str:
    """Hand-rolled inline SVG chart, real data only (see db.daily_job_trend) - no charting library. Two smooth series (created vs
    delivered per day), a soft area under "delivered", horizontal gridlines with the counts, and a few date ticks. The colours are
    tokens (the accent for delivered, a muted grey for created) so both themes read correctly."""
    if not daily_trend:
        return ""
    n = len(daily_trend)
    pad_l, pad_r, pad_t, pad_b = 30, 10, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    peak = max(1, max(max(d["created"], d["delivered"]) for d in daily_trend))
    step = 10 if peak > 40 else 5 if peak > 20 else 2 if peak > 8 else 1
    top = ((peak + step - 1) // step) * step
    x_step = plot_w / max(1, n - 1)

    def xy(i: int, key: str) -> tuple[float, float]:
        return pad_l + i * x_step, pad_t + plot_h - (daily_trend[i][key] / top) * plot_h

    def curve(key: str) -> str:
        pts = [xy(i, key) for i in range(n)]
        if n == 1:
            return f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        for i in range(1, n):
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
            mx = (x0 + x1) / 2
            d += f" C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
        return d

    base_y = pad_t + plot_h
    grid = ""
    for v in range(0, top + 1, step):
        y = pad_t + plot_h - (v / top) * plot_h
        grid += (f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="currentColor" stroke-opacity="{0.22 if v == 0 else 0.1}" '
                 f'stroke-dasharray="{"" if v == 0 else "3 4"}"/>'
                 f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" font-size="10" fill="currentColor" opacity="0.6" text-anchor="end">{v}</text>')
    ticks = ""
    tick_idx = sorted({round(i * (n - 1) / 5) for i in range(6)}) if n > 1 else [0]
    for i in tick_idx:
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        ticks += (f'<text x="{pad_l + i * x_step:.1f}" y="{height - 6}" font-size="10" fill="currentColor" opacity="0.6" '
                  f'text-anchor="{anchor}">{_short_day(daily_trend[i]["day"])}</text>')
    area = f'{curve("delivered")} L{pad_l + (n - 1) * x_step:.1f},{base_y} L{pad_l},{base_y} Z'
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Jobs created and delivered, last {n} days" '
        f'xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">'
        f'<defs><linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity="0.28"/>'
        f'<stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>'
        f'{grid}'
        f'<path d="{area}" fill="url(#trend-fill)"/>'
        f'<path d="{curve("created")}" fill="none" stroke="currentColor" stroke-opacity="0.38" stroke-width="2" stroke-linecap="round"/>'
        f'<path d="{curve("delivered")}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round"/>'
        f'{ticks}'
        f'</svg>'
    )


def _pct_change(current: float, previous: float) -> float | None:
    """Real percentage change, current vs previous. None (rendered as "-",
    never a fabricated 0% or a divide-by-zero) when there's no previous-
    period data to compare against - see staff_overview."""
    if not previous:
        return None
    return round(((current - previous) / previous) * 100)


def _plan_chosen(client_id: str) -> bool:
    """True once the account (the team owner - plans are account-level) has
    chosen a plan or made a real payment. Downloads of any kind need this."""
    row = db.get_user(client_id)
    return bool(row and (row["role"] != "client" or row["plan_chosen_at"]))


class ApiOrderError(Exception):
    """Raised by _client_dashboard_error when create_order() is being driven by
    POST /api/v1/orders - the API has no form to re-render, so the same
    validation message goes back as JSON instead."""


def current_user(request: Request):
    # Set ONLY by api_create_order, after it has checked a real API key, and
    # only on that one request's state - a browser session can never reach it.
    api_user = getattr(request.state, "api_user", None)
    if api_user is not None:
        return api_user
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get_user(user_id)
    if user is None:
        return None
    if user["account_status"] == "closed":
        # Blocks the account without a special error message that would
        # confirm to someone else that this email exists and was closed -
        # looks the same as any other invalid session.
        request.session.clear()
        return None
    request.session["last_seen"] = time.time()  # touch -> resets the idle clock
    # Separate from the session-idle touch above: a real, durable
    # last_seen_at on the user row itself, so staff can see "is this
    # account still active" days or weeks later, not just within one
    # session. Throttled to roughly once per 5 minutes per session (not
    # every single request) - a SQLite write on every page load would be
    # real, needless load for a signal that doesn't need second-level
    # precision. See db.touch_last_seen and the staff friction view.
    _last_touch = request.session.get("last_seen_at_touch", 0)
    if time.time() - _last_touch > 300:
        db.touch_last_seen(user["id"])
        request.session["last_seen_at_touch"] = time.time()
    # Team accounts: an accepted team member sees/acts on the OWNER's
    # orders and billing, never their own (they have no orders of their
    # own at all, most likely) - client_scope_id is the id every
    # order/billing ownership check and list query should use instead of
    # user["id"]. Converted to a plain dict here (sqlite3.Row can't take
    # a new key) - every existing user["field"] access, in every route
    # and template, keeps working identically either way.
    user = dict(user)
    user["client_scope_id"] = (
        (db.get_team_owner_for_member(user["id"]) if user["role"] == "client" else None) or user["id"]
    )
    return user


def _resolve_role_and_admin(email: str) -> tuple[str, bool]:
    """Three ways to end up staff: (1) your email is in the KAULI_STAFF_EMAILS
    env var - the founder/admin set, the only one that also grants
    is_admin; (2) an admin invited your email via the staff admin panel
    before you signed up (db.staff_invites); (3) an admin promoted your
    existing client account directly (db.promote_to_staff - doesn't go
    through here, it flips the role on an existing row). Only used at
    account-CREATION time (get_or_create_user's was_new branch) - an
    existing account's role is whatever's in the users table, not
    recomputed from this on every login.

    A fourth way lands as 'voice_actor' instead: staff already added this
    email to the voice_actors roster (see staff_voice_actors_create) with
    no account linked to it yet - db.is_invited_voice_actor is the exact
    same "not consumed yet" check is_invited_staff does, just against
    that table instead of a separate invites one, since the roster row
    itself already IS the invite (see that function's own comment)."""
    is_env_staff = supabase_auth.role_for_email(email) == "staff"
    if is_env_staff:
        return "staff", True
    if db.is_invited_staff(email):
        return "staff", False
    if db.is_invited_voice_actor(email):
        return "voice_actor", False
    return "client", False


def _top_ranges(order):
    """When the captions of this order move to the top of the picture: while the video has its own text or a graphic at the bottom (found
    once per video, see kauli/screen_text.py), or all the time when the customer said the video already has burned-in captions."""
    try:
        if order["instr_existing_subs"] in ("match_existing", "flag_discrepancy"):
            return [(0, 10 ** 9)]
        return screen_text.cached(order["outdir"]) or None
    except Exception:  # noqa: BLE001 - captions simply stay at the bottom
        return None


def _load_job(order) -> Job | None:
    manifest_path = Path(order["outdir"]) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        job = Job.load(str(manifest_path))
        job.top_ranges = _top_ranges(order)            # not part of the saved manifest: it is read from screen_text.json each time
        return job
    except Exception:
        return None


def _core_deliverables_ready(order) -> bool:
    """The files this service level actually promises exist on disk yet -
    not the optional video add-on (that's staff-generated on demand and
    isn't part of what every order gets), just the core output for
    whatever was ordered."""
    level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    outdir = Path(order["outdir"])
    required = []
    if level["asr"]:
        required.append(outdir / f"transcript_{order['source_lang']}.srt")
    if level["mt"]:
        required += [outdir / f"subs_{order['target_lang']}.srt", outdir / f"subs_{order['target_lang']}.vtt"]
    if level["tts"]:
        required.append(outdir / f"dub_{order['target_lang']}.wav")
    return bool(required) and all(f.exists() for f in required)


DIFFICULTY_SURCHARGE_THRESHOLD = 0.35  # 35%+ of segments genuinely hard to transcribe
DIFFICULTY_SURCHARGE_DEFAULT_PCT = 0.25  # suggested +25% - staff can propose a different amount


def audio_difficulty_rate(job: Job) -> float:
    """What fraction of segments the source audio itself was genuinely
    hard to transcribe - background noise, overlapping speakers, heavy
    accents, low volume. Deliberately narrower than job.flagged_count:
    that also counts low_mt_confidence / duration_overflow / unfittable,
    which are translation-fit issues on OUR side, not anything about the
    client's file - charging a difficulty surcharge for our own MT/TTS
    limitations wouldn't be fair, so only low_asr_confidence counts here."""
    if not job.segments:
        return 0.0
    hard = sum(1 for s in job.segments if "low_asr_confidence" in (s.review_reasons or []))
    return hard / len(job.segments)


def workflow_steps_for_order(order) -> list[dict]:
    """The Ereri workflow stepper's step list - built from what this
    specific job's service level actually includes (billing.SERVICE_LEVELS),
    not a fixed pipeline every job has to pretend to go through. A
    transcription-only order never sees a translation or voice-clone step;
    a full dub sees all of them. "deliverables" is computed live from real
    files on disk, never a checkbox someone can tick without the work
    actually being done; "complete" mirrors the order's real status rather
    than keeping its own separate state."""
    level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    done = db.get_workflow_steps_raw(order)
    # Real bug this fixes: these two labels were hardcoded "Swahili
    # source"/"English translation" regardless of the order's actual
    # source_lang/target_lang - correct for sw->en (the common case) but
    # backwards for any other real, already-supported direction (e.g.
    # en->sw), where staff would see "Swahili source" on a step that's
    # actually the ENGLISH source transcript.
    source_name = LANGUAGE_NAMES.get(order["source_lang"], order["source_lang"])
    target_name = LANGUAGE_NAMES.get(order["target_lang"], order["target_lang"])
    steps = []
    if level["asr"]:
        steps.append({"key": "source", "label": f"{source_name} source", "manual": True,
                      "done": bool(done.get("source"))})
    if level["mt"]:
        steps.append({"key": "target", "label": f"{target_name} translation", "manual": True,
                      "done": bool(done.get("target"))})
    if level["tts"]:
        steps.append({"key": "voice", "label": "Clone voice & apply to dub", "manual": True,
                      "done": bool(done.get("voice"))})
    steps.append({"key": "deliverables", "label": "Check deliverables", "manual": False,
                  "done": _core_deliverables_ready(order)})
    steps.append({"key": "complete", "label": "Mark complete", "manual": False,
                  "done": order["status"] in ("ready_for_delivery", "delivered")})
    for i, step in enumerate(steps, start=1):
        step["number"] = i
    return steps


GAP_THRESHOLD_MS = 1000  # a full second or more of non-speech gets a sound-tag cell


def _repair_leaked_translations(job: Job) -> bool:
    """A translation that kept a model's own commentary (an embedded {"literal": ...} object, "**Note:** ..." paragraphs) is cleaned. Only the machine's
    text is touched, never a line a person wrote. True when anything changed."""
    from kauli.providers.mt import clean_model_leak
    changed = False
    for seg in job.segments:
        for field in ("literal", "spoken"):
            value = getattr(seg, field, "") or ""
            cleaned = clean_model_leak(value)
            if cleaned != value:
                setattr(seg, field, cleaned)
                changed = True
        for alt in (seg.alternates or []):
            value = alt.get("text") or ""
            cleaned = clean_model_leak(value)
            if cleaned != value:
                alt["text"] = cleaned
                changed = True
    return changed


def _split_long_gaps(job: Job) -> bool:
    """Cut every non-speech segment longer than two seconds into one-second cells (see kauli.pipeline.split_gap_span) so each second can carry its
    own sound tag. A tag already typed stays on the first second. A silence that already has rendered dub audio is left alone: its audio belongs to
    the whole stretch. True when anything changed."""
    changed = False
    out: list = []
    for seg in job.segments:
        if getattr(seg, "segment_type", "speech") != "gap" or seg.end_ms - seg.start_ms < 2 * GAP_THRESHOLD_MS or getattr(seg, "audio_path", None):
            out.append(seg)
            continue
        spans = split_gap_span(seg.start_ms, seg.end_ms)
        for k, (a, b) in enumerate(spans):
            if k == 0:
                seg.start_ms, seg.end_ms = a, b
                out.append(seg)
            else:
                out.append(Segment(segment_id=f"gap-{uuid.uuid4().hex[:10]}", index=0, start_ms=a, end_ms=b, segment_type="gap", source_confidence=1.0,
                                   source_language=seg.source_language, target_language=seg.target_language))
        changed = True
    if changed:
        for i, seg in enumerate(out):
            seg.index = i
        job.segments = out
    return changed


def _find_gaps(seg) -> list[tuple[int, int]]:
    """Real silence/non-speech intervals, from the SOURCE ASR words' actual
    timestamps - the only place genuine word-level timing exists."""
    gaps: list[tuple[int, int]] = []
    cursor = seg.start_ms
    for w in seg.words:
        if w.start_ms - cursor >= GAP_THRESHOLD_MS:
            gaps.append((cursor, w.start_ms))
        cursor = max(cursor, w.end_ms)
    if seg.end_ms - cursor >= GAP_THRESHOLD_MS:
        gaps.append((cursor, seg.end_ms))
    return gaps


def _tokenize_with_speaker_tag(text: str) -> list[dict]:
    """Splits text into word tokens for the cell-builders below - a
    leading speaker tag ("RIGATHI GACHAGUA:") stays ONE token instead of
    being split on its internal space like an ordinary two-word phrase
    would be, everything after it splits normally. See
    kauli.models.split_off_speaker_tag for the shared detection rule.
    Without this, a speaker tag typed or macro-inserted as one unit still
    split back into separate word-cells the next time cells were rebuilt
    from the saved text (every reload, every save) - the client-side
    macro trick that merged it into one cell was never durable past that.

    Also real paragraph breaks: editor.js's insertParagraphBreak
    (Ctrl+Enter) writes a real "\\n\\n" into the saved text (see its own
    cellsText()), but this function used to just .split() on ALL
    whitespace, silently discarding that marker - a paragraph break looked
    fine in the browser until the next reload or autosave-triggered cell
    rebuild, at which point it simply vanished, no error, just gone.
    Each paragraph after the first gets its leading token flagged
    para_start=True, which buildCells (editor.js) turns back into a real
    line break the same way a live Ctrl+Enter does.

    A bracket tag like "[MUZIKI YACHEZA]" gets the same one-token
    treatment as a speaker tag, for the same reason: plain .split() broke
    a multi-word tag across separate cells ("[MUZIKI" / "YACHEZA]"), so
    editing one half left the brackets themselves mismatched, and nothing
    about the cell UI signalled that a "word" here was actually one
    indivisible caption tag."""
    tokens = []
    paragraphs = [p for p in re.split(r"\n\n+", text) if p.strip()]
    for p_idx, para in enumerate(paragraphs):
        tag, remainder = split_off_speaker_tag(para.strip())
        para_tokens = ([{"text": tag, "speaker_tag": True, "bracket_tag": False}] if tag else [])
        for w in re.findall(r"\[[^\]]*\]|\S+", remainder):
            para_tokens.append({"text": w, "speaker_tag": False, "bracket_tag": w.startswith("[")})
        for t_idx, tok in enumerate(para_tokens):
            tok["para_start"] = p_idx > 0 and t_idx == 0
        tokens += para_tokens
    return tokens


def _build_source_cells(seg) -> list[dict]:
    """Step 1 of the editor: the Swahili ASR transcript. Real per-word
    timing and confidence, straight from faster-whisper - this is the most
    accurate data in the whole manifest, so it's what's shown until a human
    actually corrects the segment.

    Once corrected, a saved edit is a free-form string, not a 1:1 relabeling
    of the original ASR words (a word may have been added, removed, or
    reworded) - so the ORIGINAL words no longer describe what's actually
    there. Rebuilding cells from seg.words regardless of the edit was a real
    bug: the correction was genuinely saved (source_final_text used it
    correctly downstream), but the editor's own word cells silently ignored
    it and kept showing the un-corrected ASR output on every reload, making
    a saved correction look lost. Once edited, cells are rebuilt from the
    saved text instead, with APPROXIMATE per-word timing (same proportional-
    by-character-count approach as _build_target_cells, for the same
    reason: there's no real per-word alignment for hand-edited text).
    """
    if seg.source_edited_transcript:
        tokens = _tokenize_with_speaker_tag(seg.source_edited_transcript)
        if tokens and getattr(seg, "para_start_source", False):
            tokens[0]["para_start"] = True
        total_chars = sum(len(tok["text"]) for tok in tokens) or 1
        duration = max(1, seg.end_ms - seg.start_ms)
        cells = []
        t = seg.start_ms
        for tok in tokens:
            dur = int(duration * (len(tok["text"]) / total_chars))
            cells.append({"type": "word", "text": tok["text"], "start_ms": t, "end_ms": t + dur,
                          "confidence": 1.0, "approx": True, "speaker_tag": tok["speaker_tag"],
                          "para_start": tok["para_start"], "bracket_tag": tok["bracket_tag"]})
            t += dur
        if cells:
            cells[-1]["end_ms"] = seg.end_ms  # absorb rounding drift at the tail
    else:
        cells = [
            {"type": "word", "text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms,
             "confidence": w.confidence}
            for w in seg.words
        ]
        if cells and getattr(seg, "para_start_source", False):
            cells[0]["para_start"] = True
    # Real gap cells either way - a source correction session is exactly
    # where a sound tag like [Muziki Wacheza] gets noticed first.
    cells += [{"type": "gap", "start_ms": g0, "end_ms": g1} for g0, g1 in _find_gaps(seg)]
    cells.sort(key=lambda c: c["start_ms"])
    return cells


def _build_target_cells(seg) -> list[dict]:
    """Step 2 of the editor: the EDITABLE English final_text, split into
    words with APPROXIMATE per-word timing (there is no real word-level
    alignment for translated text - this proportions each word across the
    segment's real duration by character count, the same char-rate idea
    kauli/timing.py already uses for duration fitting), interleaved with
    GAP cells at the same real silence intervals as the source. Gap cells
    start empty - an editor can type a sound tag like [MUSIC] into one.
    """
    tokens = _tokenize_with_speaker_tag(seg.final_text)
    if tokens and getattr(seg, "para_start_target", False):
        tokens[0]["para_start"] = True
    total_chars = sum(len(tok["text"]) for tok in tokens) or 1
    duration = max(1, seg.end_ms - seg.start_ms)
    cells = []
    t = seg.start_ms
    for tok in tokens:
        dur = int(duration * (len(tok["text"]) / total_chars))
        cells.append({"type": "word", "text": tok["text"], "start_ms": t, "end_ms": t + dur,
                      "speaker_tag": tok["speaker_tag"], "para_start": tok["para_start"],
                      "bracket_tag": tok["bracket_tag"]})
        t += dur
    if cells:
        cells[-1]["end_ms"] = seg.end_ms  # absorb rounding drift at the tail

    cells += [{"type": "gap", "start_ms": g0, "end_ms": g1} for g0, g1 in _find_gaps(seg)]
    cells.sort(key=lambda c: c["start_ms"])
    return cells


def _render_segment_audio(order, job: Job, seg, tts_provider, voice_id) -> None:
    """Synthesizes ONE segment's audio and applies the real time-stretch-
    fit logic (the same kauli.pipeline.run's own TTS loop uses) - mutates
    seg in place, doesn't touch the timeline or write any deliverable
    file (callers do that once, not per segment). Skips a gap segment or
    one with no real text - speaking either aloud makes no sense, and
    calling synthesize("") is what crashed a real voice-clone run on this
    exact codebase the first time an order had gap segments: coqui-tts's
    synthesizer only assigns its internal `sens` list `if text:`, so an
    empty string with a real speaker_wav present (voice cloning) skips
    both that assignment AND the library's own "nothing to do" guard,
    and crashes with an UnboundLocalError deep inside its code instead.
    Same guard kauli.pipeline.run's own TTS loop already had - this just
    brings both editor-side render paths in line with it.

    voice_id is the order's own default/single voice - a segment whose
    speaker has its OWN assigned voice (job.speaker_voices) uses that
    instead, same resolve_voice_for_segment kauli.pipeline.run's TTS loop
    uses, so a multi-speaker order stays consistent regardless of which
    render path touched a given segment."""
    if getattr(seg, "segment_type", "speech") == "gap":
        # Real music/applause/laughter/SFX bed from the source, not
        # silence - see kauli.pipeline.render_gap_audio's own docstring.
        # Covers the editor-triggered re-render paths (staff_set_dub_voice,
        # editor_retranslate_all's full rebuild) the same way the main
        # pipeline run already does.
        render_gap_audio(seg, order["audio_path"], Path(order["outdir"]) / "segments", tts_provider.sample_rate)
        return
    # A leading speaker tag ("RIGATHI GACHAGUA:") is caption metadata, not
    # a line to speak aloud - see kauli.pipeline.run's identical stripping
    # in the main pipeline's own TTS loop. subs_*.srt/vtt still caption the
    # full text, tag included - only what's actually synthesized changes.
    _tag, text = split_off_speaker_tag(seg.final_text.strip())
    text = text.strip()
    # Same reasoning, for an inline [Applause]/[MUZIKI YACHEZA] sound tag
    # typed into an otherwise-spoken segment's text - the voice reads the
    # real words, never the bracket tag itself.
    text = strip_bracket_tags_for_tts(text)
    if not text:
        return
    if seg.spell_out:
        text = spell_out_text(text)
    raw = Path(order["outdir"]) / "segments" / f"{seg.segment_id}.wav"
    seg_voice_id = resolve_voice_for_segment(job, seg, voice_id, tts_provider.name)
    rendered = tts_provider.synthesize(text, str(raw), voice_id=seg_voice_id)

    seg.time_stretch_pct = 0.0
    raw, rendered = apply_stretch_fit(seg, raw, rendered, Path(order["outdir"]) / "segments")

    # A human editor's own deliberate pace override (see the voice-direction
    # route), layered ON TOP of whatever the automatic fit-to-slot above
    # already did - same time_stretch (pitch-preserving ffmpeg atempo) tool,
    # just a second pass. Deliberately does NOT try to re-fit back into
    # budget_ms afterward: an editor asking for "20% slower" is explicitly
    # choosing to run past the original slot, and mixer.build_timeline's own
    # non-overlap guarantee (hard-clip at the next segment's start) is what
    # keeps that from ever audibly colliding with what comes after.
    if seg.manual_pace_pct:
        factor = 1.0 / (1.0 + seg.manual_pace_pct / 100.0)
        paced = Path(order["outdir"]) / "segments" / f"{seg.segment_id}_paced.wav"
        if time_stretch(str(raw), str(paced), factor):
            raw = paced
            rendered = int(round(rendered / factor))
            if "manual_pace_override" not in seg.review_reasons:
                seg.review_reasons.append("manual_pace_override")
            seg.review_flag = True

    seg.audio_path = str(raw)
    seg.rendered_duration_ms = rendered
    seg.voice_id = seg_voice_id or tts_provider.name


def _resynthesize_full_dub(order, job: Job, tts_name: str, voice_id: str | None,
                            only_speaker_id: str | None = None) -> None:
    """Re-render every segment's audio with a specific provider/voice and
    rebuild the mixed dub track - what a voice change needs that a single
    corrected segment (see _apply_segment_edit) doesn't: every segment has
    to move to the new voice together, or the dub would switch voices
    mid-file. Text itself is untouched (whatever's already in
    seg.final_text, edits included) - only who's saying it changes.

    only_speaker_id restricts the re-render to just that speaker's
    segments (see editor_assign_speaker_voice) - assigning ONE character a
    voice shouldn't re-synthesize every other speaker's already-fine
    audio too. voice_id still only applies to the matching segments;
    resolve_voice_for_segment (inside _render_segment_audio) is what
    actually picks it up per-speaker either way."""
    tts_provider = get_tts(tts_name)
    for seg in job.segments:
        if only_speaker_id and seg.speaker_id != only_speaker_id:
            continue
        _render_segment_audio(order, job, seg, tts_provider, voice_id)

    sr = tts_provider.sample_rate
    track = build_timeline(
        [(s.start_ms, s.audio_path) for s in job.segments],
        job.source_duration_ms or (job.segments[-1].end_ms if job.segments else 0),
        sample_rate=sr,
    )
    write_wav_mono(str(Path(order["outdir"]) / f"dub_{order['target_lang']}.wav"), track, sr)
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))


def _run_xtts_clone_job(order_id: str) -> None:
    """Background-thread body for a real speaker clone - see the warning in
    kauli.providers.tts.XTTSCloneTTS about consent, and the module
    docstring in webapp/worker.py for why this is a plain daemon thread
    rather than a real task queue (one job at a time, no GPU, deliberately
    not built out further yet)."""
    order = db.get_order(order_id)
    if not order:
        return
    job = _load_job(order)
    if job is None:
        db.set_dub_voice_job_status(order_id, "failed:job data missing")
        return
    try:
        ref_path = Path(order["outdir"]) / "reference_speaker.wav"
        # Speech segments only - same reasoning as kauli/pipeline.py's own
        # reference-clip selection: a long music/silence gap segment could
        # easily be the single longest segment in the file, which would
        # extract a "voice" reference containing no voice at all. This
        # entry point re-clones an ALREADY-processed order (the pipeline's
        # own selection only runs once, at first processing), so it needs
        # the same filter independently.
        speech_segments = [s for s in job.segments if getattr(s, "segment_type", "speech") != "gap"]
        if not speech_segments:
            raise RuntimeError("No speech segments to clone a reference from.")
        ref_seg = max(speech_segments, key=lambda s: s.end_ms - s.start_ms)
        ref_end = min(ref_seg.end_ms, ref_seg.start_ms + 20_000)  # XTTS wants 6-20s
        if not extract_reference_clip(order["audio_path"], ref_seg.start_ms, ref_end, str(ref_path)):
            raise RuntimeError("Couldn't extract a reference clip (ffmpeg missing?).")
        _resynthesize_full_dub(order, job, "xtts", str(ref_path))
        db.set_dub_voice(order_id, "xtts", job_status=None)
    except Exception as exc:  # noqa: BLE001 - surface it to the editor UI
        traceback.print_exc()
        db.set_dub_voice_job_status(order_id, f"failed:{exc}")


def _current_tts_route(order) -> tuple[str, str | None]:
    """Whatever voice the dub is CURRENTLY using (see the dub-voice picker)
    - a per-segment or bulk re-render should always match it, never
    silently fall back to a provider's default and give one segment (or
    one retranslated batch) a different voice than everything around it."""
    dub_voice = order["dub_voice"]
    if dub_voice == "xtts":
        ref_path = Path(order["outdir"]) / "reference_speaker.wav"
        if ref_path.exists():
            return "xtts", str(ref_path)
    elif dub_voice and dub_voice.startswith("piper:") and dub_voice[6:] in PIPER_VOICES:
        return "piper", str(PROJECT_ROOT / PIPER_VOICES[dub_voice[6:]]["path"])
    return order["tts"], None


def _resynthesize_one_segment(order, job: Job, seg) -> None:
    """Re-render exactly one segment's audio from its CURRENT seg.final_text
    (whatever that is right now - a fresh translation, a hand-edit, or
    both), in the dub's current voice, then rebuild the mixed timeline so
    the delivered dub_<lang>.wav reflects it. Never called for a gap
    segment - see the callers' own guards (a sound tag an editor types,
    e.g. "[music playing]", belongs in the captions, not spoken aloud).
    Applies the SAME real time-stretch-fit logic kauli.pipeline.run() uses
    (not a simplified copy) so a segment fixed here ends up fitted to its
    slot exactly like one that was right the first time, instead of
    silently skipping the fit step a fresh pipeline run always applies.

    A no-op whenever the dub's current voice is "human" (see
    _activate_human_recording): the delivered dub_<lang> file is then a
    real actor's full take, not a per-segment mix - re-rendering just this
    one segment with the AI TTS provider and folding it back into the
    timeline would silently splice an AI voice into the middle of a human
    recording. The text correction itself (seg.final_text) still saves
    either way; only the audio re-render is skipped."""
    if order["dub_voice"] == "human":
        return
    tts_name, voice_id = _current_tts_route(order)
    tts_provider = get_tts(tts_name)
    _render_segment_audio(order, job, seg, tts_provider, voice_id)

    sr = tts_provider.sample_rate
    track = build_timeline(
        [(s.start_ms, s.audio_path) for s in job.segments],
        job.source_duration_ms or (job.segments[-1].end_ms if job.segments else 0),
        sample_rate=sr,
    )
    write_wav_mono(str(Path(order["outdir"]) / f"dub_{order['target_lang']}.wav"), track, sr)


def _apply_segment_edit(order, job: Job, seg, text: str, resynthesize: bool) -> None:
    """Shared by the plain-form review screen and the JS editor's save calls -
    one place that knows how to correct a segment, optionally re-render its
    audio, and rebuild the deliverables that depend on it."""
    seg.edited_text = text.strip() or None
    seg.approved = True
    seg.translation_stale = False  # a human just hand-edited this English text themselves -
    # whether or not they clicked Re-translate, they've now dealt with it directly

    # Never true speech synthesis for a gap segment, even if resynthesize
    # is requested - a sound tag an editor types ("[music playing]") is
    # meant to appear in the delivered captions, not get spoken aloud by
    # the TTS voice. It still shows up in subs_*.srt/vtt below exactly
    # like any other segment's text - only the actual audio render is
    # skipped here.
    if resynthesize and order["tts"] != "stub" and getattr(seg, "segment_type", "speech") != "gap":
        _resynthesize_one_segment(order, job, seg)

    manifest_path = Path(order["outdir"]) / "manifest.json"
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(
        to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(
        to_vtt(job), encoding="utf-8")
    job.save(str(manifest_path))


# Source languages the client can pick when submitting a job. Adding a
# language here only ever follows someone actually verifying the ASR/MT
# combination works on it - never added on spec (see the Kikuyu feasibility
# research this list came out of).
SOURCE_LANGUAGES = {
    "sw": "Swahili",
    "en": "English",
    "ki": "Kikuyu (Gikuyu)",
}

# The ~70 further languages Instant AI can TRANSCRIBE (nothing else: no
# translation, voice or human review - see TRANSKRIPTOR_LANGUAGES). Kept
# separate from SOURCE_LANGUAGES on purpose: that dict is the languages Kauli
# fully supports end to end (and is what the marketing copy lists as such).
AI_ONLY_LANGUAGES = {code: label for code, (label, _t) in TRANSKRIPTOR_LANGUAGES.items()
                      if code not in SOURCE_LANGUAGES}
# Every code a stored order can carry, for display names.
LANGUAGE_NAMES = {**AI_ONLY_LANGUAGES, **SOURCE_LANGUAGES}
templates.env.globals["language_total"] = len(LANGUAGE_NAMES)
templates.env.globals["language_names"] = LANGUAGE_NAMES
# Bank transfer is kept only for accounts that already existed when it was switched off for new clients
# (override the date with BANK_TRANSFER_LEGACY_BEFORE, a Unix time). M-Pesa is offered only to people in Kenya
# (Cloudflare's country header) and to the older Kenyan accounts; everyone else pays by card.
BANK_TRANSFER_LEGACY_BEFORE = float(os.environ.get("BANK_TRANSFER_LEGACY_BEFORE") or 1793201400)   # 2026-10-28 15:30 UTC


def _bank_transfer_ok(user) -> bool:
    try:
        created = user["created_at"]
    except (KeyError, IndexError, TypeError):
        created = None
    return created is None or created < BANK_TRANSFER_LEGACY_BEFORE


def _mobile_money_ok(request: Request, user) -> bool:
    if (request.headers.get("cf-ipcountry") or "").strip().upper() == "KE":
        return True
    try:
        return bool(user) and db.get_billing_market(user["client_scope_id"]) == "legacy"
    except (KeyError, IndexError, TypeError):
        return False


def _first_name(user) -> str:
    """What to call a client in a greeting. A real name ("Edwin G K") gives its first word. An account that only has
    the start of its email address ("esirachewww6", "jane.doe42") gives the first letters-only chunk, without the
    digits. Nothing usable gives an empty string, so the greeting simply drops the name."""
    import re as _re
    try:
        name = (user["display_name"] or "").strip()
        email_prefix = (user["email"] or "").split("@")[0].strip()
    except (KeyError, IndexError, TypeError):
        return ""
    if not name:
        name = email_prefix
    looks_like_handle = name.lower() == email_prefix.lower() and " " not in name
    word = name.split()[0] if name.split() else ""
    if looks_like_handle:
        word = next((c for c in _re.split(r"[^A-Za-z]+", name) if len(c) >= 3), "")
    word = _re.sub(r"[^A-Za-z\u00C0-\u024F'\-]", "", word)
    return (word[:1].upper() + word[1:].lower()) if len(word) >= 2 else ""


templates.env.globals["first_name"] = _first_name
templates.env.globals["hours_suffix"] = billing.hours_suffix
templates.env.globals["billing_saving"] = lambda plan: int(round((1 - plan["price_usd_year"] / (plan["price_usd"] * 12)) * 100)) if plan["price_usd"] else 0
templates.env.globals["meetings_enabled"] = meeting_bot.configured
templates.env.globals["bank_ok"] = _bank_transfer_ok
templates.env.globals["mpesa_ok"] = _mobile_money_ok
templates.env.globals["pending_count"] = lambda u: db.pending_orders_count(u["client_scope_id"]) if (u and u["role"] == "client") else 0
templates.env.globals["plan_chosen"] = lambda u: bool(u) and (u["role"] != "client" or _plan_chosen(u["client_scope_id"]))


# ---- the language a person's screens are shown in (words live in webapp/ui_text.py)
LANG_COOKIE = "kauli_lang"


def _accept_language(request) -> str | None:
    """The language the browser asks for first among the ones Kauli speaks (zh-CN, zh-TW and so on all read as zh), else None."""
    try:
        header = request.headers.get("accept-language", "") if request is not None else ""
    except Exception:  # noqa: BLE001
        return None
    ranked = []
    for part in header.split(","):
        tag, _, q = part.strip().partition(";q=")
        if not tag:
            continue
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((weight, tag.split("-")[0].lower()))
    for _w, code in sorted(ranked, key=lambda x: -x[0]):
        if ui_text.valid(code):
            return code
    return None


def _ui_lang(request, user=None) -> str:
    """The language of this screen: the /xx/ address it was asked for, else their saved language, else the cookie (set by the translated
    site or the switcher), else what their browser asks for, else English."""
    try:
        if user is not None and user["role"] != "client":
            return "en"                      # staff and voice-talent screens are English
        code = user["ui_lang"] if user is not None else None
    except (KeyError, IndexError, TypeError):
        code = None
    forced = getattr(getattr(request, "state", None), "lang_override", None) if request is not None else None
    if ui_text.valid(forced):
        return forced
    if ui_text.valid(code):
        return code
    code = request.cookies.get(LANG_COOKIE) if request is not None else None
    if ui_text.valid(code):
        return code
    return _accept_language(request) or "en"


# ---- every page in the visitor's language (the engine is webapp/page_translate.py)
_LOCALIZE_SKIP = ("/static/", "/api/", "/webhooks/", "/staff", "/actor", "/lang/", "/avatar/", "/blog-cover/", "/media", "/download", "/s/", "/r/",
                  "/healthz", "/status", "/sitemap", "/robots", "/llms", "/feed", "/podcast", "/mcp", "/.well-known", "/favicon", "/sw.js", "/logout")
_PREFIXED_PATH = re.compile(r"^/([a-z]{2})(/.+)$")
_FILE_LIKE = re.compile(r"\.(png|jpe?g|webp|gif|svg|ico|css|js|json|xml|txt|pdf|mp3|mp4|webm|woff2?|zip|docx|srt|vtt)$", re.I)
_OG_LOCALE = {"es": "es_ES", "zh": "zh_CN", "ar": "ar_AR", "fr": "fr_FR", "pt": "pt_BR", "de": "de_DE", "hi": "hi_IN", "ja": "ja_JP", "ko": "ko_KR", "sw": "sw_KE", "en": "en_US"}


def _is_public_page(path: str) -> bool:
    """A page a search engine may list (so it has a /xx/ address in each language)."""
    if path == "/":
        return True
    return not (path.startswith(_NOINDEX_PREFIXES) or path.startswith(_LOCALIZE_SKIP) or _FILE_LIKE.search(path) or path.strip("/") in site_locales.LOCALES)


def _lang_url(base: str, code: str, path: str) -> str:
    if code == "en":
        return f"{base}{path}"
    return f"{base}/{code}" if path == "/" else f"{base}/{code}{path}"


def _finish_localized_html(request: Request, page: str, lang: str, prefixed: bool, path: str, complete: bool) -> str:
    """The language tags, the canonical address and the alternates for the other languages; links that stay in the language."""
    page = re.sub(r'<html\b[^>]*>', lambda m: re.sub(r'\s(?:lang|dir)="[^"]*"', "", m.group(0))[:-1] + f' lang="{ui_text.HTML_LANG.get(lang, lang)}"'
                  + (' dir="rtl"' if ui_text.direction(lang) == "rtl" else "") + ">", page, count=1)
    page = brand.localize_logos(page, lang)
    if not (_is_public_page(path) and complete):
        return page
    base = _site_base(request)
    if prefixed:
        canonical = f'<link rel="canonical" href="{_lang_url(base, lang, path)}">'
        if re.search(r'<link\b[^>]*rel="canonical"', page):
            page = re.sub(r'<link\b[^>]*rel="canonical"[^>]*>', canonical, page, count=1)
        else:
            page = page.replace("</head>", canonical + "</head>", 1)
        page = re.sub(r'(<meta\b[^>]*property="og:url"[^>]*content=")[^"]*"', lambda m: m.group(1) + _lang_url(base, lang, path) + '"', page, count=1)
        if 'property="og:locale"' not in page:
            page = page.replace("</head>", f'<meta property="og:locale" content="{_OG_LOCALE.get(lang, "en_US")}"></head>', 1)

        def prefix_link(m: re.Match) -> str:
            href = m.group(2)
            target = href.split("#")[0].split("?")[0]
            if href.startswith("//") or not _is_public_page(target):
                return m.group(0)
            new = f"/{lang}" if target == "/" else f"/{lang}{href}"
            return f'{m.group(1)}"{new}"'
        page = re.sub(r'(<a\b[^>]*?\shref=)"(/[^"]*)"', prefix_link, page)
    if "hreflang=" not in page:
        alts = "".join(f'<link rel="alternate" hreflang="{"en" if c == "en" else ui_text.HTML_LANG.get(c, c)}" href="{_lang_url(base, c, path)}">' for c in ui_text.LANGS)
        alts += f'<link rel="alternate" hreflang="x-default" href="{_lang_url(base, "en", path)}">'
        page = page.replace("</head>", alts + "</head>", 1)
    return page


@app.middleware("http")
async def localize_pages(request: Request, call_next):
    """Shows each page in the language of the person reading it. A /xx/ address (/zh/solutions/podcasters) is the indexable version of a
    public page; anywhere else the language comes from the account, the cookie or the browser. See webapp/page_translate.py."""
    path = request.url.path
    prefixed = None
    m = _PREFIXED_PATH.match(path)
    if m and m.group(1) in ui_text.LANGS and m.group(1) != "en" and request.method in ("GET", "HEAD") and _is_public_page(m.group(2)):
        prefixed = m.group(1)
        path = m.group(2)
        request.scope["path"] = path
        request.scope["raw_path"] = path.encode()
        request.state.lang_override = prefixed
    elif request.method == "GET" and path.strip("/") in site_locales.LOCALES:
        request.state.lang_override = path.strip("/")                     # /zh is the hand-written Chinese home page
    elif (request.method == "GET" and path != "/lang" and _is_public_page(path) and LANG_COOKIE not in request.cookies and "session" not in request.cookies
          and not site_analytics.is_bot(request.headers.get("user-agent", ""))):
        wanted = _accept_language(request)
        if wanted and wanted != "en" and page_translate.configured():
            # first visit from a browser set to another language: send them to that language's address (and remember it)
            target = f"/{wanted}" if path == "/" else f"/{wanted}{path}"
            if request.url.query:
                target += "?" + request.url.query
            resp = RedirectResponse(target, status_code=302)
            resp.headers["Vary"] = "Accept-Language, Cookie"
            return _set_lang_cookie(resp, wanted)
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    skip = path.startswith(_LOCALIZE_SKIP) and not path.startswith("/receipts")
    if skip or request.method == "HEAD" or response.status_code >= 500 or not (ctype.startswith("text/html") or ctype.startswith("application/json")):
        return response
    try:
        user = current_user(request)
    except Exception:  # noqa: BLE001
        user = None
    lang = _ui_lang(request, user)
    if lang == "en" or not (page_translate.configured() or prefixed):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    raw = [(k, v) for k, v in response.raw_headers if k.lower() not in (b"content-length", b"content-type")]
    if ctype.startswith("application/json"):
        try:
            data = await run_in_threadpool(page_translate.localize_json, json.loads(body.decode("utf-8")), lang)
            out = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception:  # noqa: BLE001
            out = body
        new = Response(content=out, status_code=response.status_code, media_type="application/json")
    else:
        page, complete = await run_in_threadpool(page_translate.localize_html, body.decode("utf-8", "replace"), lang)
        page = _finish_localized_html(request, page, lang, bool(prefixed), path, complete)
        new = Response(content=page.encode("utf-8"), status_code=response.status_code, media_type="text/html")
        new.headers["Content-Language"] = ui_text.HTML_LANG.get(lang, lang)
        new.headers["Vary"] = "Accept-Language, Cookie"
        if not complete:
            new.headers["X-Robots-Tag"] = "noindex"                      # never list a half-translated page
            new.headers["Cache-Control"] = "no-store"
    ct = [(k, v) for k, v in new.raw_headers if k.lower() in (b"content-length", b"content-type")]
    mine = [(k, v) for k, v in new.raw_headers if k.lower() not in (b"content-length", b"content-type")]
    taken = {k.lower() for k, _ in mine}
    new.raw_headers = [(k, v) for k, v in raw if k.lower() not in taken] + mine + ct
    if prefixed:
        _set_lang_cookie(new, prefixed)
    return new


def _set_lang_cookie(response, code: str):
    response.set_cookie(LANG_COOKIE, code, max_age=365 * 86400, path="/", samesite="lax",
                        secure=os.environ.get("KAULI_PUBLIC_BASE_URL", "").startswith("https"))
    return response


@pass_context
def _t(ctx, key, **kw):
    """The text for `key` in the viewer's language. The catalog is ours, so its text is trusted; anything passed in
    (a name, a price) is escaped, and a link passed in as Markup stays a link."""
    lang = _ui_lang(ctx.get("request"), ctx.get("user"))
    text = ui_text.CATALOG.get(lang, {}).get(key) or ui_text.EN.get(key) or key
    if not kw:
        return Markup(_escape(text))
    try:
        return Markup(_escape(text)).format(**kw)
    except (KeyError, IndexError, ValueError):
        return Markup(_escape(ui_text.EN.get(key, key))).format(**kw)


@pass_context
def _ui_lang_global(ctx):
    return _ui_lang(ctx.get("request"), ctx.get("user"))


@pass_context
def _js_i18n(ctx):
    """The scripts' own words for this reader (see webapp/js_strings.py); {} for English."""
    lang = _ui_lang(ctx.get("request"), ctx.get("user"))
    if lang == "en" or not page_translate.configured():
        return {}
    try:
        return js_strings.for_lang(lang)
    except Exception:  # noqa: BLE001 - the scripts simply show English if this fails
        return {}


@pass_context
def _page_lang(ctx):
    code = _ui_lang(ctx.get("request"), ctx.get("user"))
    return ui_text.HTML_LANG.get(code, code)


@pass_context
def _page_dir(ctx):
    return ui_text.direction(_ui_lang(ctx.get("request"), ctx.get("user")))


@pass_context
def _ts_ui(ctx, ts):
    """A short date and time in the viewer's language."""
    return ui_text.format_ts(_ui_lang(ctx.get("request"), ctx.get("user")), datetime.fromtimestamp(ts))


templates.env.globals.update(t=_t, ui_lang=_ui_lang_global, page_lang=_page_lang, page_dir=_page_dir,
                              ui_languages=[(c, ui_text.NATIVE[c]) for c in ui_text.LANGS], ui_native=ui_text.NATIVE)
templates.env.filters["ts_ui"] = _ts_ui
def _hero_demo() -> dict:
    """The real demo video on the home page (static/demo/vid-video-<lang>.mp4, made by scripts/make_demo_videos.py) and the first sentence of its
    script in each language, taken from webapp/demo_content.py so the words shown are the words the video says."""
    scenario = demo_content.SCENARIOS["video"]

    def first(text: str) -> str:
        return re.split(r"(?<=[.\u3002])\s*", text.strip())[0]
    return {"en": first(scenario["en"]), "rows": [{"code": c, "name": demo_content.LANGUAGES[c]["name"], "line": first(scenario["tr"][c])} for c in ("es", "de", "zh", "sw")]}


templates.env.globals["hero_demo"] = _hero_demo
templates.env.globals["usage_banner"] = usage_prompts.banner
templates.env.globals["pro_savings"] = lambda u: savings.pro_savings(u["client_scope_id"]) if u and u["role"] == "client" else None
templates.env.filters["blog_ctas"] = lambda body, post: Markup(seo_pages.add_blog_ctas(str(body), post["title"], post["slug"]))
templates.env.globals["site_verification"] = lambda: {"google": os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip(), "bing": os.environ.get("BING_SITE_VERIFICATION", "").strip()}
def _human_minutes_left(u):
    """Included human-reviewed minutes this client has left this month (0 on a plan without any)."""
    if not u or u["role"] != "client":
        return 0
    sub = db.get_subscription_current(u["client_scope_id"])
    return round(billing.human_minutes_remaining(sub, billing.effective_plan(u, sub)), 1)


templates.env.globals["human_minutes_left"] = _human_minutes_left
templates.env.globals["allowance_translate"] = lambda u: bool(u) and u["role"] == "client" and billing.allowance_covers(
    billing.effective_plan(u, db.get_subscription_current(u["client_scope_id"])), "translate", "ai")


@app.get("/lang/{code}", include_in_schema=False)
def set_language(request: Request, code: str, next: str = "/"):
    """The language switcher: remember the choice on the account (so every device follows) and in a cookie."""
    if not ui_text.valid(code):
        return Response(status_code=404)
    user = current_user(request)
    if user and not getattr(request.state, "api_user", None):
        db.set_ui_lang(user["id"], code)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return _set_lang_cookie(resp, code)


def _lara_configured() -> bool:
    """Lara Translate is Kauli's translation engine for everything that is not
    a premium plan's Claude upgrade. Instant AI translation across the wider
    language list is only offered when its keys are set - never advertised in
    the order form otherwise."""
    return bool(os.environ.get("LARA_ACCESS_KEY_ID") and os.environ.get("LARA_ACCESS_KEY_SECRET"))


OWN_MT_MAX_MINUTES = float(os.environ.get("KAULI_OWN_MT_MAX_MINUTES") or 15)       # our own model translates a few seconds a sentence: short files only
OWN_ASR_MAX_MINUTES = float(os.environ.get("KAULI_OWN_ASR_MAX_MINUTES") or 20)


def _pick_mt(source_lang: str, target_lang: str, plan_mt: str, review_tier: str, minutes: float = 0.0) -> tuple[str | None, str | None]:
    """(mt provider name, None) or (None, user-facing error). Lara first
    (the chosen engine); a priority plan's Claude upgrade, the Kikuyu
    Claude-first rule, and the original local/Azure fallbacks are kept for
    anything Lara can't take - so nothing changes for an account until Lara's
    keys are actually set."""
    pinned = provider_health.default_for("mt", source_lang, target_lang)           # staff's default on the Providers page, when it can take this pair
    if pinned:
        return pinned, None
    lara_ok = _lara_configured() and not provider_health.blocked("lara")
    if source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # Kikuyu: Claude if connected (as before), otherwise Lara (which
        # supports ki-KE) - always followed by a human review.
        if provider_health.usable("claude"):
            return "claude", None
        if lara_ok:
            return "lara", None
        return None, ("Kikuyu translation needs our AI translation provider connected first, "
                      "which isn't done yet - contact us and we'll handle this one manually "
                      "in the meantime.")
    if (plan_mt == "claude" and review_tier == "ai" and provider_health.usable("claude") and not provider_health.is_paused("claude")):
        return "claude", None  # Premium and Enterprise: high-quality translation, given automatically (Lara takes over if Claude is down or off)
    if (review_tier == "ai" and minutes <= OWN_MT_MAX_MINUTES and own_quality.mt_ok(source_lang, target_lang)
            and provider_health.madlad_ok(source_lang, target_lang) and not provider_health.is_paused("madlad")):
        return "madlad", None  # Instant AI: our own translation model goes first where it was measured good; the paid engines are behind it
    if review_tier == "ai" and lara_ok:
        return "lara", None
    if plan_mt == "claude" and provider_health.is_enabled("claude"):
        return "claude", None  # a Premium/Enterprise plan's priority-translation upgrade (unless staff turned Claude off)
    if lara_ok:
        return "lara", None
    if _lara_configured():                 # Lara is set aside after a failure (or turned off): another connected engine first
        if provider_health.usable("azure-translate"):
            return "azure-translate", None
        if provider_health.madlad_ok(source_lang, target_lang):
            return "madlad", None
        return "lara", None
    # Lara not connected: the original behaviour, unchanged.
    mt = plan_mt
    if mt == "claude" and not provider_health.is_enabled("claude"):
        mt = "local"                       # staff turned Claude off: the same fallbacks as any other plan
    if mt == "local" and not (source_lang == "sw" and target_lang == "en"):
        if provider_health.usable("azure-translate"):
            return "azure-translate", None
        if provider_health.madlad_ok(source_lang, target_lang):
            return "madlad", None
        return None, ("Translating that language pair needs our translation provider connected first, "
                      "which isn't done yet - contact us and we'll handle this one manually in the meantime.")
    return mt, None

# No ASR model available to us - local Whisper or any vendor we checked -
# recognizes these languages at all. Orders in one of these get the
# "manual" provider instead of faster-whisper: real ffmpeg-detected speech
# segments with no transcript text, so a human transcriber types the source
# straight into the editor instead of the model guessing wrong. See
# kauli/providers/asr.py:ManualASR - every segment starts at 0.0 confidence,
# which already routes it into the normal staff review queue for free.
MANUAL_TRANSCRIPTION_LANGUAGES = {"ki"}


# ----------------------------------------------------------- marketing ----
CONTACT_EMAIL = "hello@kauli-forgemedia.com"
# base.html's account menu (every authenticated page) needs a real contact
# link without every single route threading it through by hand - same
# reasoning as the other templates.env.globals assignments near the top
# of this file (tat_status, free_minutes, etc).
templates.env.globals["contact_email"] = CONTACT_EMAIL
# Unset until a real Google Cloud OAuth Client ID (with kauli-forgemedia.com
# added as an authorized JavaScript origin) is put in .env - see
# _one_tap.html, which no-ops entirely when this is empty, same "silently
# inert until configured" pattern as calendly_url above.
templates.env.globals["google_one_tap_client_id"] = os.environ.get("GOOGLE_ONE_TAP_CLIENT_ID")
# Same "unset -> inert" pattern for the two search-engine ownership-
# verification meta tags (base.html) - Google Search Console and Bing
# Webmaster Tools each hand you a token to paste in as one of these once
# you add the site as a property there; nothing to fabricate here, this
# just gives the real token somewhere to go without a code change.
templates.env.globals["google_site_verification"] = os.environ.get("GOOGLE_SITE_VERIFICATION")
templates.env.globals["bing_site_verification"] = os.environ.get("BING_SITE_VERIFICATION")
# Single source of truth for the GA4 property id, shared with the server-
# side Measurement Protocol calls in ga_events.py - was a literal
# hardcoded separately in _ga.html before, which could drift from
# whatever ga_events.py sends events to.
templates.env.globals["ga_measurement_id"] = ga_events.measurement_id()
# Real WCAG 2.2.1 (Timing Adjustable) gap until now - a real 30-minute
# idle logout (see IDLE_TIMEOUT_SECONDS/SessionMiddleware above) existed
# with no warning before it happened at all; base.html's own idle-warning
# script needs the real number, not a guessed one, to warn far enough
# ahead of the actual cutoff.
templates.env.globals["idle_timeout_seconds"] = IDLE_TIMEOUT_SECONDS
templates.env.globals["js_i18n"] = _js_i18n
templates.env.globals["nav_icon"] = nav_icons.nav_icon
templates.env.globals["icon_svg"] = nav_icons.icon_svg
templates.env.globals["trend_arrow"] = nav_icons.trend_arrow
templates.env.globals["youtube_oauth_configured"] = youtube_captions.youtube_oauth_configured()
templates.env.globals["horizontal_bar_chart"] = charts.horizontal_bar_chart
templates.env.globals["funnel_chart_svg"] = charts.funnel_chart_svg
templates.env.globals["status_bar_chart_svg"] = charts.status_bar_chart_svg

# Real answers only - every figure here is read from billing.py, not typed
# in twice, so a rate change can never leave the FAQ quietly wrong. No
# fabricated turnaround SLA (nothing in the app enforces one), no
# fabricated client counts.
_EXTRA_REVIEW_CANDIDATES = ("zh", "de", "es")


def _review_extra_languages() -> set[str]:
    """Chinese, German and Spanish, human-reviewed by outside contractors. ON by default; set
    KAULI_REVIEW_EXTRA_LANGUAGES to a shorter list (e.g. "zh") or to "none" to narrow or switch it off. The real
    cost per audio-minute is KAULI_EXTRA_REVIEW_COST_PER_MIN."""
    raw = os.environ.get("KAULI_REVIEW_EXTRA_LANGUAGES", "zh,de,es")
    return {c.strip() for c in raw.split(",") if c.strip() in _EXTRA_REVIEW_CANDIDATES and c.strip() in LANGUAGE_NAMES}


def _human_review_language_list() -> str:
    """"Swahili, Kikuyu and English", plus Chinese, German and Spanish once those are switched on."""
    short = {"zh": "Chinese (Mandarin)", "de": "German", "es": "Spanish"}
    names = ["Swahili", "Kikuyu", "English"] + [short[c] for c in _EXTRA_REVIEW_CANDIDATES if c in _review_extra_languages()]
    return ", ".join(names[:-1]) + " and " + names[-1]


_HR_NAMES = _human_review_language_list()   # "Swahili, Kikuyu, English, Chinese, German and Spanish"


def _dub_language_list() -> str:
    """Where dubbing works today: the Studio languages (English and Swahili, plus Chinese, German and Spanish once on)."""
    names = {"en": "English", "sw": "Swahili", "zh": "Chinese (Mandarin)", "de": "German", "es": "Spanish"}
    got = [names[c] for c in ["en", "sw"] + [c for c in _EXTRA_REVIEW_CANDIDATES if c in _review_extra_languages()]]
    return ", ".join(got[:-1]) + " and " + got[-1]


_DUB_NAMES = _dub_language_list()


MARKETING_FAQ = [
    {"q": "How much does it cost?",
     "a": f"Starting at $0: your first {billing.FREE_MINUTES_PER_MONTH:.0f} minutes of Instant AI transcription are free every month, to process and read. "
          "To download or edit a result you pay for that order. After the free minutes you pay per audio-minute, and the exact price, for Instant AI or human review, is shown before you order.",
     "link": ("#pricing", "See all prices")},
    {"q": "What is the difference between Instant AI and Human-reviewed?",
     "a": "Instant AI is generated by machine and ready in minutes, and it is labelled as not reviewed by a "
          "person. Human-reviewed adds a dedicated editor who checks the result against the audio before delivery "
          f"({_HR_NAMES}).",
     "link": ("/blog/ai-vs-human-transcription-which-do-you-need", "How to choose")},
    {"q": "How long does an order take?",
     "a": "Instant AI orders are usually ready within minutes of upload, and longer files take longer. "
          "Human-reviewed orders depend on length and the review queue, and you see an estimated delivery "
          "time when you order."},
    {"q": "Which languages do you support?",
     "a": f"Instant AI transcribes {len(AI_ONLY_LANGUAGES) + len(SOURCE_LANGUAGES)} languages and translates "
          "between them (except Kikuyu). Dubbing and human review are available in selected languages, and more are coming soon.",
     "link": ("/human-review", "See the human review languages")},
    {"q": "What is the difference between verbatim and clean read?",
     "a": "Clean read tidies filler words and false starts for easy reading. Verbatim keeps everything exactly "
          "as spoken, which legal and research work often needs. On Human-reviewed orders you choose per order.",
     "link": ("/formatting-standards", "How we format your content")},
    {"q": "Is my uploaded file safe?",
     "a": "Uploads are checked against their real file type, scanned for malware and validated as genuine "
          "audio or video before processing. Only your account and our staff can see your files.",
     "link": ("/privacy", "Read the privacy policy")},
    {"q": "Can you clone a voice, and do you make deepfakes?",
     "a": "Voice cloning needs your confirmation that you hold the rights and the speaker's consent, and it "
          "only ever translates what the speaker actually said. There is no way to have new words spoken in a "
          "cloned voice, and we refuse orders meant to impersonate someone.",
     "link": ("/terms#deepfakes", "Read our terms")},
    {"q": "Can I use a human voice actor or dub several speakers?",
     "a": "Yes to both. A real voice actor can be added to a human-reviewed full dub for "
          f"${billing.ADDONS['human_voice_over']['rate_per_min']:.2f} per audio-minute, and each speaker in a "
          "video keeps a consistent voice throughout the dub."},
    {"q": "Do I need to install anything?",
     "a": "No. Kauli runs in your browser: upload a file or paste a YouTube link, then download your files "
          "when they are ready."},
]


# Dedicated per-audience pages (see solution_page.html / the /solutions/{slug}
# route below) - real SEO/AEO practice from the doc you shared: a specific
# audience's page can rank for specific intent a one-page homepage can't,
# and gives that visitor language addressed to their actual situation. Every
# claim here is grounded in a feature that's actually built (human review,
# consent-gated cloning, the real TAT/deadline system, YouTube auto-import,
# burned captions) - nothing here promises anything the product doesn't do.
SOLUTION_PAGES = {
    "audio-to-text": {
        "title": "Audio to Text Converter - Transcribe Audio in 76+ Languages",
        "meta_description": "Convert audio and video to text in minutes in 76+ languages, starting at $0 with free minutes every month. "
                             "Download as Word, text or SRT. Pay as you go, with optional human review for up to 99% accuracy.",
        "kicker": "Audio to text",
        "h1": "Convert audio to text in minutes - in 76+ languages",
        "intro": "Upload a recording or paste a YouTube link and get a timestamped transcript back in "
                 "minutes, ready to download as Word, plain text or SRT. Pay by the minute - no subscription required.",
        "why_heading": "Built for getting text out of audio, fast",
        "points": [
            {"title": "76+ languages, one upload",
             "body": "Spanish, French, German, Arabic, Hindi, Mandarin, Swahili and dozens more. Pick the "
                      "language the speaker uses and we transcribe it - no per-language pricing."},
            {"title": "Word, text and SRT downloads",
             "body": "Download your transcript as a Word document (.docx), plain text (.txt) or an SRT file "
                      "with timestamps. Speaker labels are included where the recording has more than one voice."},
            {"title": "Timestamped lines",
             "body": "Every line carries its start time, so you can jump straight to a moment in a long "
                      "recording. Timing is per line, not per word."},
            {"title": "Honest about what AI can and can't do",
             "body": "Instant AI is machine transcription, not reviewed by a person, and says so on every "
                      "order. Accuracy depends on audio quality, accents and background noise - check names "
                      f"and numbers before you publish. For {_HR_NAMES} you can add human review."},
            {"title": "Simple per-minute pricing",
             "body": "Starting at $0: your first minutes every month are free, and the price is shown before you order."},
        ],
        "cta_heading": "Transcribe your first file free",
        "cta_body": "Upload a recording or paste a link - your first minutes are free.",
        "related": ["youtube-video-to-text", "podcast-transcription", "video-transcription"],
        "faq": [
            {"q": "What file types can I upload?",
             "a": "Audio or video in any common format - for example MP3, WAV, M4A, MP4 and MOV. If a file "
                  "won't process, the order form tells you why."},
            {"q": "How long does it take?",
             "a": "Instant AI orders are normally ready within minutes of upload. Very long files take "
                  "longer. Human-reviewed orders take longer because a person checks them before delivery."},
            {"q": "Is the transcript reviewed by a person?",
             "a": "Not on Instant AI - it is machine-generated and labelled that way. Human review is "
                  f"available for {_HR_NAMES} orders."},
            {"q": "Can I get captions or subtitles too?",
             "a": "Yes - SRT files are included, and Instant AI can translate your transcript into another "
                  "language (machine translation, not reviewed by a person). Dubbing is available for "
                  f"Swahili, Kikuyu and English, and human review for {_HR_NAMES}."},
        ],
    },
    "youtube-video-to-text": {
        "title": "YouTube Video to Text - Transcribe Any YouTube Link",
        "meta_description": "Paste a YouTube link and get the transcript back as Word, text or SRT - starting at $0, in 76+ languages. Pay as you go.",
        "kicker": "YouTube video to text",
        "h1": "Turn a YouTube video into text - paste the link",
        "intro": "Paste a YouTube link, choose the spoken language and get a timestamped transcript back "
                 "in minutes. Download it as Word, plain text or SRT, and add translated subtitles with "
                 "Instant AI.",
        "why_heading": "From YouTube link to transcript",
        "points": [
            {"title": "No downloading or re-uploading",
             "body": "Paste the link - we fetch the audio ourselves. Only submit videos you own or have "
                      "the rights to use."},
            {"title": "76+ spoken languages",
             "body": "Choose the language spoken in the video. Instant AI transcribes it and can translate it; "
                      f"you can add human review for {_HR_NAMES}, and dubbing for {_DUB_NAMES}."},
            {"title": "Subtitle files ready for YouTube",
             "body": "Download SRT or VTT files and upload them to your video in YouTube Studio, one per "
                      "language, so viewers can pick their own."},
            {"title": "If a link can't be fetched, upload the file",
             "body": "YouTube sometimes blocks automated downloads for particular videos. If that happens "
                      "to yours, upload the video file instead and the rest works exactly the same."},
            {"title": "Pay per minute, no subscription required",
             "body": "Starting at $0: your first few minutes are free, and the price is shown before you order. No subscription required."},
        ],
        "cta_heading": "Paste a YouTube link and try it",
        "cta_body": "Your first minutes are free.",
        "related": ["audio-to-text", "captioning", "youtubers"],
        "faq": [
            {"q": "Can I transcribe someone else's YouTube video?",
             "a": "Only submit videos you own or have the rights to use. Transcribing a video you don't "
                  "have rights to may breach the creator's rights or YouTube's terms."},
            {"q": "Why did my YouTube link fail?",
             "a": "YouTube occasionally blocks automated access to specific videos. Upload the video file "
                  "instead - the result is identical."},
            {"q": "How do I get the subtitles onto my video?",
             "a": "Download the SRT or VTT file from your finished order, then open the video in YouTube "
                  "Studio, go to Subtitles, add the language and upload the file."},
        ],
    },
    "podcast-transcription": {
        "title": "Podcast Transcription - Episode Transcripts in Minutes",
        "meta_description": "Transcribe podcast episodes to text in minutes, with timestamps and speaker labels, starting at $0. "
                             "Download as Word, text or SRT. Pay as you go.",
        "kicker": "Podcast transcription",
        "h1": "Podcast transcripts in minutes, for pennies a minute",
        "intro": "Upload an episode and get a timestamped transcript back in minutes - for show notes, "
                 "blog posts, accessibility and search. Download as Word, plain text or SRT.",
        "why_heading": "Built for show notes, not just archives",
        "points": [
            {"title": "Speaker labels",
             "body": "Hosts and guests are labelled where the recording has multiple voices, so the "
                      "transcript reads like a conversation."},
            {"title": "Timestamped lines",
             "body": "Jump to any moment from the transcript - handy for chapters, clips and quotes."},
            {"title": "Word, text and SRT",
             "body": "Paste into your CMS from the .txt, edit in the .docx, or use the SRT for captions "
                      "on video versions of the episode."},
            {"title": "Priced for a weekly show",
             "body": "Your first minutes are free, and the price of a full episode is shown before you order. Pay as you go, no subscription required."},
            {"title": "Shows in many languages",
             "body": f"Instant AI transcribes 76+ languages, and podcasts in {_HR_NAMES} can add human review."},
        ],
        "cta_heading": "Transcribe your next episode",
        "cta_body": "Your first minutes are free.",
        "related": ["audio-to-text", "youtube-video-to-text", "video-transcription"],
        "faq": [
            {"q": "How accurate is it?",
             "a": "It depends on audio quality, accents and background noise. Instant AI is machine "
                  "transcription and isn't reviewed by a person - skim it for names, numbers and "
                  "jargon before publishing. We don't publish a headline accuracy figure we can't prove."},
            {"q": "Do you label different speakers?",
             "a": "Yes, where the recording has more than one voice, speakers are labelled in the transcript."},
        ],
    },
    "interview-transcription": {
        "title": "Interview Transcription - Researchers, Journalists and Teams",
        "meta_description": "Transcribe interviews and recordings with speaker labels and timestamps in "
                             "minutes, in 76+ languages. Word, text and SRT downloads. Human review "
                             "available for up to 99% accuracy.",
        "kicker": "Interview transcription",
        "h1": "Interview transcripts with speaker labels and timestamps",
        "intro": "For researchers, journalists and teams who need to get from a recording to quotable, "
                 "searchable text quickly - in the language the interview was actually held in.",
        "why_heading": "Built for interviews, not just meetings",
        "points": [
            {"title": "Speaker labels and timestamps",
             "body": "Each line shows who spoke and when, so you can find and verify a quote against the "
                      "recording."},
            {"title": "Interviews in 76+ languages",
             "body": "Transcribe the interview in the language it was held in, and translate it with Instant AI. "
                      f"For {_HR_NAMES}, add human review."},
            {"title": "Clean read or verbatim",
             "body": "On human-reviewed orders, choose a cleaned-up read or a "
                      "verbatim record that keeps every false start."},
            {"title": "Word and text exports",
             "body": "Download a .docx to annotate or a .txt to feed your analysis tools."},
            {"title": "Human review where it counts",
             "body": "Instant AI isn't reviewed by a person. For a published quote or a research record in "
                      f"{_HR_NAMES}, add human review so a dedicated editor checks it against the audio."},
        ],
        "cta_heading": "Try it on a real interview",
        "cta_body": "Your first minutes are free.",
        "related": ["audio-to-text", "video-transcription", "captioning"],
        "faq": [
            {"q": "Is my recording kept private?",
             "a": "Files are processed to produce your transcript and are only visible to your account "
                  "and our staff. See our privacy policy for how files and our transcription provider are handled."},
            {"q": "Can you transcribe interviews in languages other than English?",
             "a": "Yes - Instant AI transcribes 76+ languages, and human review is available in "
                  f"{_HR_NAMES}. Kikuyu goes through a real human transcriber, since no speech-recognition model supports it yet."},
        ],
    },
}

SOLUTION_PAGES.update(solution_pages.build(billing, _HR_NAMES, _DUB_NAMES))
solution_pages.refresh(SOLUTION_PAGES, _HR_NAMES)
SOLUTION_PAGES = {k: SOLUTION_PAGES[k] for k in (
    "ngos", "youtubers", "media-broadcast", "e-learning", "voice-over", "captioning", "video-translation", "audio-to-text",
    "youtube-video-to-text", "podcast-transcription", "interview-transcription", "video-transcription", "video-dubbing",
    "video-localization", "call-centers", "accessibility-compliance")}


@app.get("/solutions/{slug}", response_class=HTMLResponse)
def solution_page(request: Request, slug: str):
    page = SOLUTION_PAGES.get(slug)
    if not page:
        return HTMLResponse("Page not found.", status_code=404)
    user = current_user(request)
    if user:
        return RedirectResponse(
            "/staff" if user["role"] == "staff" else "/actor" if user["role"] == "voice_actor" else "/client/home")
    # Real internal linking between the real, related pages - "connect the
    # services" rather than let each page dead-end. Silently skips any slug
    # that isn't (shouldn't happen with the hardcoded lists above, but a
    # typo here should never 500 a real page over a broken "related" link).
    related_pages = [
        {"slug": s, "kicker": SOLUTION_PAGES[s]["kicker"], "h1": SOLUTION_PAGES[s]["h1"]}
        for s in page.get("related", []) if s in SOLUTION_PAGES
    ]
    guides = []
    for g in page.get("guides", []):
        post = db.get_blog_post_by_slug(g, published_only=True)
        if post:
            guides.append({"slug": g, "title": post["title"], "description": post["description"]})
    choose = []
    for option in page.get("choose", []):
        slug_for = solution_pages.guide_slug_for(option, slug)
        post = db.get_blog_post_by_slug(slug_for, published_only=True) if slug_for else None
        choose.append({**option, "guide": {"slug": slug_for, "title": post["title"]} if post else None})
    return templates.TemplateResponse(request, "solution_page.html",
        {**_marketing_context(home="/", market=_display_market(request)), "page": page, "related_pages": related_pages,
         "guides": guides, "choose": choose})


def _display_market(request: Request) -> str:
    """Which price card a PUBLIC page shows: an explicit ?market=intl|ea
    wins (the on-page switcher), otherwise the visitor's country from
    Cloudflare's CF-IPCountry header. Display only - an account's real
    market is fixed at signup (see the signup block in the login handler)."""
    explicit = request.query_params.get("market")
    if explicit in ("intl", "ea"):
        return explicit
    return billing.market_for_country(request.headers.get("cf-ipcountry"))


_MARQUEE_LANGUAGES = [
    ("en", "English", "flag-en"), ("sw", "Kiswahili", "flag-ke"), ("ki", "Kikuyu", "flag-ke"),
    ("fr", "Français", "flag-fr"), ("es", "Español", "flag-es"), ("ar", "العربية", "flag-sa"),
    ("pt", "Português", "flag-pt"), ("de", "Deutsch", "flag-de"), ("it", "Italiano", ""), ("zh", "中文", ""),
    ("hi", "हिन्दी", ""), ("ja", "日本語", ""), ("ko", "한국어", ""), ("tr", "Türkçe", ""), ("ru", "Русский", ""),
    ("nl", "Nederlands", ""), ("pl", "Polski", ""), ("id", "Bahasa Indonesia", ""), ("vi", "Tiếng Việt", ""),
    ("uk", "Українська", ""),
]


def _language_marquee() -> list[dict]:
    """The languages shown flowing across the home page: only codes we really support, each linked to the
    page that exists for it."""
    slug_by_code = {code: slug for slug, code in AI_LANGUAGE_SLUGS.items()}
    items = []
    for i, (code, name, flag) in enumerate(_MARQUEE_LANGUAGES):
        if code not in LANGUAGE_NAMES:
            continue
        href = ("/solutions/audio-to-text" if code == "en" else "/solutions/video-transcription" if code in ("sw", "ki")
                else f"/transcribe/{slug_by_code[code]}" if code in slug_by_code else "/languages")
        items.append({"code": code, "name": name, "flag": flag, "tone": i % 5, "href": href})
    return items


def _marketing_context(sent: bool = False, lead_error: str | None = None, home: str = "",
                        market: str = billing.DEFAULT_MARKET) -> dict:
    """Shared by both marketing.html render points below - keeps them from
    quietly drifting apart the way two copies of the same context dict
    always eventually do. home="" on the homepage itself (bare #anchor
    links), "/" on a /solutions/* page reusing _nav.html (which has no
    #how/#demo/etc. sections of its own to scroll to)."""
    return {
        "user": None, "home": home,
        "display_market": market, "price_card": billing.price_card_for(market),
        "review_tiers": billing.REVIEW_TIERS,
        "plans": billing.PLANS, "service_levels": billing.SERVICE_LEVELS,
        "addons": billing.ADDONS,
        "free_minutes": billing.FREE_MINUTES_PER_MONTH,
        "email": CONTACT_EMAIL,
        "language_marquee": _language_marquee(),
        "human_review_languages": _human_review_language_list(), "human_review_count": len(_human_review_live()),
        "calendly_url": os.environ.get("KAULI_CALENDLY_URL"),
        "faq": MARKETING_FAQ,
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "sent": sent,
        "lead_error": lead_error,
    }


# ------------------------------------------------- multilingual SEO ----
# Three layers, all honest about what the product does: (1) one English
# "X audio to text" page per transcription language (/transcribe/<slug>) for
# people searching in English; (2) translated landing pages (/es, /zh, /ar...)
# for people searching in their own language; (3) a /languages hub, hreflang
# alternates, sitemap entries and llms.txt so search engines and AI answer
# engines can find all of it. The 74 non-core languages are TRANSCRIPTION
# ONLY - nothing here may claim translation/dubbing/review for them.
_CORE_LANGUAGE_ROWS = [
    # (code, english label, autonym, regions, available, url)
    ("sw", "Swahili", "Kiswahili", "East Africa",
     "Instant AI + human review, translation and dubbing", "/solutions/video-transcription"),
    ("en", "English", "English", "Worldwide",
     "Instant AI + human review, translation and dubbing", "/solutions/audio-to-text"),
    ("ki", "Kikuyu (Gikuyu)", "Gĩkũyũ", "Kenya",
     "Human transcription + translation and dubbing", "/solutions/video-transcription"),
]
AI_LANGUAGE_SLUGS = seo_languages.build_slug_index(AI_ONLY_LANGUAGES)  # slug -> code
_LOCALE_OPTIONS = [(code, meta[0]) for code, meta in site_locales.LOCALES.items()]


def _site_base(request: Request) -> str:
    """The public address of this site, for canonical links, sitemaps and structured data. Behind the proxy the
    request arrives as plain http, so a real host is always given as https; only localhost and the test client keep
    what the request says."""
    configured = os.environ.get("KAULI_PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    host = request.url.netloc
    local = host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]", "testserver") or host.endswith(".localhost")
    return f"{request.url.scheme if local else 'https'}://{host}"


templates.env.globals["site_origin"] = pass_context(lambda ctx: _site_base(ctx["request"]))


def _hreflang_alternates(base: str) -> list[tuple[str, str]]:
    return [("en", f"{base}/")] + [(meta[1], f"{base}/{code}") for code, meta in site_locales.LOCALES.items()]


def _price_strings(market: str) -> dict:
    card = billing.price_card_for(market)
    f = lambda x: f"{x:.2f}"  # noqa: E731
    return {
        "t": f(card["transcribe"]["ai"]), "tr": f(card["translate"]["ai"]), "d": f(card["dub"]["ai"]),
        "ht": f(card["transcribe"]["human"]), "htr": f(card["translate"]["human"]), "hd": f(card["dub"]["human"]),
    }


def _language_total() -> int:
    return len(AI_ONLY_LANGUAGES) + len(SOURCE_LANGUAGES)


def _logged_in_redirect(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse(
            "/staff" if user["role"] == "staff" else "/actor" if user["role"] == "voice_actor" else "/client/home")
    return None


def _render_locale_page(request: Request, code: str):
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    native, hreflang, direction = site_locales.LOCALES[code]
    p = _price_strings(_display_market(request))
    fmt = dict(p, n=_language_total(), free=int(billing.FREE_MINUTES_PER_MONTH))
    s = {k: v.format(**fmt) for k, v in site_locales.STRINGS[code].items()}
    base = _site_base(request)
    lang_pills = [(r[2], r[1]) for r in _CORE_LANGUAGE_ROWS] + sorted(
        ((seo_languages.LANGUAGE_FACTS[c][0], label) for c, label in AI_ONLY_LANGUAGES.items()), key=lambda x: x[1])
    lang_links = {r[1]: r[5] for r in _CORE_LANGUAGE_ROWS}
    lang_links.update({label: f"/transcribe/{seo_languages.slugify(label)}" for label in AI_ONLY_LANGUAGES.values()})
    resp = templates.TemplateResponse(request, "landing_localized.html", {
        "user": None, "s": s, "p": p, "locale": code, "hreflang": hreflang, "text_dir": direction,
        "locale_options": _LOCALE_OPTIONS, "base_url": base,
        "hreflang_alts": _hreflang_alternates(base), "hreflang_default": f"{base}/",
        "faq": [(s["q1"], s["a1"]), (s["q2"], s["a2"]), (s["q3"], s["a3"]), (s["q4"], s["a4"])],
        "lang_pills": lang_pills, "lang_links": lang_links,
    })
    # Someone reading the translated site keeps that language through sign-up and into their account.
    return _set_lang_cookie(resp, code) if ui_text.valid(code) else resp


def _make_locale_route(code: str):
    def locale_page(request: Request):
        return _render_locale_page(request, code)
    locale_page.__name__ = f"locale_page_{code}"
    return locale_page


for _locale_code in site_locales.LOCALES:
    app.add_api_route(f"/{_locale_code}", _make_locale_route(_locale_code), methods=["GET"],
                       response_class=HTMLResponse, include_in_schema=False)


def _language_page_dict(code: str) -> dict:
    """The solution_page.html content for one 'X audio to text' page.
    Everything stated here is true of the product today: Instant AI
    transcription only, the language-specific facts are plain reference data."""
    label = seo_languages.display_name(AI_ONLY_LANGUAGES[code])
    autonym, regions = seo_languages.LANGUAGE_FACTS[code]
    price = billing.PRICE_CARD["intl"]["transcribe"]["ai"]
    show_autonym = autonym.lower() != label.lower()
    name = f"{label} ({autonym})" if show_autonym else label
    return {
        "title": f"{label} Audio to Text - AI {label} Transcription",
        "meta_description": f"Transcribe {name} audio and video to text in minutes, from ${price:.2f} per minute, "
                             "and translate it with Instant AI. Download as Word, text or SRT. Pay as you go, first minutes free.",
        "kicker": f"{label} transcription",
        "h1": f"{label} audio to text - transcribe {label} recordings in minutes",
        "intro": f"Upload a {label} recording or paste a YouTube link, choose {label} as the spoken language "
                 f"and get a timestamped transcript back in minutes. {label} is mainly spoken in {regions}.",
        "why_heading": f"{label} transcription with Kauli",
        "points": [
            {"title": f"Transcribe {name}",
             "body": f"Pick {label} as the spoken language when you order. Instant AI transcribes it and "
                      "returns a transcript with a start time on every line, and speaker labels where "
                      "the recording has more than one voice."},
            {"title": "Word, text and SRT downloads",
             "body": "Download your transcript as a Word document (.docx), plain text (.txt) or an SRT file."},
            {"title": "Simple per-minute pricing",
             "body": "Starting at $0, no subscription required. Your first minutes are free, and the price is shown before you order."},
            {"title": "Honest about the limits",
             "body": f"Instant AI is machine transcription and is not reviewed by a person; accuracy depends on "
                      f"audio quality, accent and background noise, so check names and numbers before you "
                      f"publish. Instant AI translation is machine translation, also not reviewed by a person. "
                      f"Dubbing is currently for {_DUB_NAMES}, and human review for {_HR_NAMES}; more languages are coming soon."},
        ],
        "cta_heading": f"Transcribe your {label} audio",
        "cta_body": "Your first minutes are free.",
        "related": ["audio-to-text", "youtube-video-to-text", "podcast-transcription"],
        "faq": [
            {"q": f"Can you transcribe {label} audio?",
             "a": f"Yes. Choose {label} as the spoken language and Kauli's Instant AI transcribes it, returning "
                  "Word, text and SRT files."},
            {"q": f"Can you translate {label} into English?",
             "a": f"Yes. Choose 'Transcription + translation', pick {label} as the spoken language and English "
                  "(or another language) as the target. It is Instant AI machine translation - not reviewed "
                  f"by a person. Dubbing is currently for {_DUB_NAMES}, and human review for {_HR_NAMES}; more languages are coming soon."},
            {"q": f"How accurate is {label} transcription?",
             "a": "It depends on audio quality, accents and background noise. Instant AI is machine "
                  "transcription and is not reviewed by a person, so we don't publish a headline accuracy "
                  "figure. Skim the transcript for names, numbers and specialist terms before you publish."},
            {"q": f"How much does {label} transcription cost?",
             "a": "Starting at $0: your first minutes are free, then it is priced per audio-minute and shown before you order, with no subscription required."},
        ],
    }


@app.get("/transcribe/{slug}", response_class=HTMLResponse)
def language_page(request: Request, slug: str):
    code = AI_LANGUAGE_SLUGS.get(slug)
    if not code:
        return HTMLResponse("Page not found.", status_code=404)
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    related_pages = [
        {"slug": s, "kicker": SOLUTION_PAGES[s]["kicker"], "h1": SOLUTION_PAGES[s]["h1"]}
        for s in ("audio-to-text", "youtube-video-to-text", "podcast-transcription") if s in SOLUTION_PAGES
    ]
    page = _language_page_dict(code)
    if code in seo_pages.REVIEWED:
        page = {**page, **seo_pages.language_page_extras(code, billing.PRICE_CARD["intl"]["transcribe"]["ai"],
                                                          billing.PRICE_CARD["intl"]["transcribe"]["human"], code in _human_review_live())}
    return templates.TemplateResponse(request, "solution_page.html", {
        **_marketing_context(home="/", market=_display_market(request)),
        "page": page, "related_pages": related_pages,
    })


@app.get("/transcribe/{slug}/{task}", response_class=HTMLResponse)
def language_task_page(request: Request, slug: str, task: str):
    """A language plus a job people search for ("Spanish interview transcription"), for the languages Kauli reviews with a person."""
    code = seo_pages.known(slug)
    if not code or task not in seo_pages.TASKS:
        return HTMLResponse("Page not found.", status_code=404)
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    card = billing.PRICE_CARD["intl"]["transcribe"]
    page = seo_pages.combo_page(code, task, card["ai"], card["human"], code in _human_review_live(), seo_pages.REVIEWED[code][1])
    related_pages = [{"slug": s, "kicker": SOLUTION_PAGES[s]["kicker"], "h1": SOLUTION_PAGES[s]["h1"]} for s in page["related"] if s in SOLUTION_PAGES]
    return templates.TemplateResponse(request, "solution_page.html", {
        **_marketing_context(home="/", market=_display_market(request)),
        "page": page, "related_pages": related_pages,
    })


@app.get("/solutions", response_class=HTMLResponse)
def solutions_index(request: Request):
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    rows = [{"slug": s, "kicker": p["kicker"], "h1": p["h1"], "intro": p.get("intro", "")} for s, p in SOLUTION_PAGES.items()]
    return templates.TemplateResponse(request, "solutions_index.html", {
        **_marketing_context(home="/", market=_display_market(request)), "rows": rows, "base_url": _site_base(request)})


@app.get("/languages", response_class=HTMLResponse)
def languages_hub(request: Request):
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    rows = [{"label": r[1], "autonym": r[2], "regions": r[3], "available": r[4], "url": r[5]}
            for r in _CORE_LANGUAGE_ROWS]
    for code, label in sorted(AI_ONLY_LANGUAGES.items(), key=lambda kv: kv[1]):
        autonym, regions = seo_languages.LANGUAGE_FACTS[code]
        rows.append({"label": label, "autonym": autonym, "regions": regions,
                     "available": "Instant AI transcription + translation", "url": f"/transcribe/{seo_languages.slugify(label)}"})
    return templates.TemplateResponse(request, "languages_hub.html", {
        **_marketing_context(home="/", market=_display_market(request)),
        "rows": rows, "total": len(rows), "base_url": _site_base(request),
        "price": f"{billing.PRICE_CARD['intl']['transcribe']['ai']:.2f}", "locale_options": _LOCALE_OPTIONS,
    })


# ------------------------------------------------------- onboarding CS ----
# The honest version of the "Founder's Welcome" / "CSM handoff" pattern:
# These come from the Kauli team (Forge Media Services), signed as a team and
# never under one person's name. Every message is queued (see db.py's
# onboarding_messages) AND, now that Brevo is configured, auto-sent - see
# _queue_and_send below. A staff view of the queue (and anything that
# needed a manual fallback) lives on /staff/leads.
TEAM_NAME = "Kauli Team"                  # how every customer email is signed


def _queue_and_send(user, kind: str, subject: str, body: str, base_url: str = "",
                     cta_text: str | None = None, cta_url: str | None = None,
                     html_inner: str | None = None) -> None:
    """Writes the message to onboarding_messages (the honest, always-there
    record) and, if a real mailer is configured, immediately attempts to
    actually send it - same "create the real record first, then try to
    deliver it, fall back gracefully" pattern as _activate_payment's
    receipt email. body is plain text (the team's own plain wording) -
    always the text_body fallback, and the HTML source too unless
    html_inner is given (a caller-built version with real <a> links on
    "reply"/"WhatsApp" and a properly line-broken signature - see
    mailer.text_to_html_paragraphs's docstring for why the mechanical
    version couldn't do that on its own)."""
    message_id = db.queue_onboarding_message(user["id"], kind, subject, body)
    if mailer.email_configured():
        inner = html_inner if html_inner is not None else mailer.text_to_html_paragraphs(body)
        html = mailer.wrap_email_html(inner, cta_text=cta_text, cta_url=cta_url, base_url=base_url)
        ok, detail = mailer.send_email(user["email"], subject, html, body)
        db.set_onboarding_message_email_result(message_id, ok, detail)




# Same real personal inbox worker.py's own CONTACT_EMAIL constant uses
# for this exact purpose (that module can't import this one - circular -
# so it keeps its own copy) - internal ops alerts go to the owner address directly,
# never the public hello@ support alias these functions are announcing
# activity ABOUT, not activity sent TO.
INTERNAL_NOTIFY_EMAIL = "kahunyurogodfrey@gmail.com"


def _capture_signup_ip_intel(user_id: str, request: Request) -> None:
    """Real IP captured synchronously (cheap, one DB write) - the actual
    datacenter check runs in a background thread since the FIRST call in
    this process does a real network fetch (see ip_intel.py) that must
    never add latency to an actual signup/login response. Fire-and-
    forget: a failed or slow check just leaves signup_ip_is_datacenter
    NULL, which staff_clients.html already renders as "not checked",
    never as a false negative."""
    ip = rate_limit.client_ip(request)
    db.set_signup_ip(user_id, ip)

    def _check():
        result = ip_intel.is_datacenter_ip(ip)
        if result is not None:
            db.set_signup_ip_datacenter_flag(user_id, result)

    threading.Thread(target=_check, daemon=True, name=f"ip-intel-{user_id}").start()


def _notify_internal_new_signup(user, base_url: str = "") -> None:
    """Real-time ops alert the instant a new CLIENT account is actually
    created (called from the same was_new branch _queue_welcome_message
    already fires from, in both /login and /signup) - staff/voice-actor
    account creation isn't a marketing event, so this is only ever called
    for role == 'client'. Fire-and-forget like every other mailer call in
    this file: a flaky send here must never block the signup itself."""
    if not mailer.email_configured():
        return
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    link = f"{base_url.rstrip('/')}/staff/clients/{user['id']}" if base_url else f"/staff/clients/{user['id']}"
    subject = f"New Kauli signup: {name}"
    body = f"New client account created.\n\nName: {name}\nEmail: {user['email']}\n\n{link}"
    html = (
        f'<p style="margin:0 0 14px;">New client account created.</p>'
        f'<p style="margin:0 0 14px;">Name: {name}<br>Email: {user["email"]}</p>'
    )
    html = mailer.wrap_email_html(html, cta_text="View account", cta_url=link, base_url=base_url)
    mailer.send_email(INTERNAL_NOTIFY_EMAIL, subject, html, body, sender_name="Kauli Marketing")


def _notify_internal_new_lead(lead_id: str, name: str, email: str, org_type: str | None,
                               volume_estimate: str | None, base_url: str = "") -> None:
    """Same real-time ops alert, for the website contact form
    (request_callback) instead of an account signup - a lead is often the
    FIRST real signal a prospect exists, well before (or instead of) them
    ever creating an account."""
    if not mailer.email_configured():
        return
    link = f"{base_url.rstrip('/')}/staff/leads/{lead_id}" if base_url else f"/staff/leads/{lead_id}"
    subject = f"New Kauli lead: {name}"
    detail_lines = f"Name: {name}\nEmail: {email}\n"
    if org_type:
        detail_lines += f"Organization type: {org_type}\n"
    if volume_estimate:
        detail_lines += f"Estimated volume: {volume_estimate}\n"
    body = f"New website lead.\n\n{detail_lines}\n{link}"
    html_detail = f'<p style="margin:0 0 14px;">Name: {name}<br>Email: {email}'
    if org_type:
        html_detail += f'<br>Organization type: {org_type}'
    if volume_estimate:
        html_detail += f'<br>Estimated volume: {volume_estimate}'
    html_detail += '</p>'
    html = mailer.wrap_email_html(
        f'<p style="margin:0 0 14px;">New website lead.</p>{html_detail}',
        cta_text="View lead", cta_url=link, base_url=base_url)
    mailer.send_email(INTERNAL_NOTIFY_EMAIL, subject, html, body, sender_name="Kauli Marketing")


def _queue_welcome_message(user, base_url: str = "") -> None:
    if db.has_onboarding_message(user["id"], "welcome"):
        return  # already queued once for this account - don't duplicate on a second login
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    subject = f"Welcome to Kauli, {name} - your {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are ready"
    body = (
        f"Hi {name},\n\n"
        "Welcome aboard - we're glad you're here.\n\n"
        f"Your account has {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes loaded already - "
        "here's the fastest way to see a real result:\n\n"
        "1. Open your dashboard.\n"
        "2. Upload a short video or audio clip, or paste a YouTube link.\n"
        "3. Pick your language pair (e.g. Swahili -> English).\n"
        "4. We'll notify you the moment your result is ready to download. Instant AI results arrive in "
        "minutes and are AI-generated; Human-reviewed results are checked by a dedicated editor first.\n\n"
        "If anything's unclear, or you'd rather just talk it through, just reply to this email.\n\n"
        f"Talk soon,\n{TEAM_NAME}"
    )
    cta_url = f"{base_url.rstrip('/')}/client" if base_url else "/client"
    html_inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">Welcome aboard - we\'re glad you\'re here.</p>'
        f'<p style="margin:0 0 6px;">Your account has <strong>{billing.FREE_MINUTES_PER_MONTH:.0f} free '
        f'minutes</strong> loaded already - here\'s the fastest way to see a real result:</p>'
        f'<ol style="margin:0 0 14px; padding-left:20px;">'
        f'<li style="margin-bottom:4px;">Open your dashboard.</li>'
        f'<li style="margin-bottom:4px;">Upload a short video or audio clip, or paste a YouTube link.</li>'
        f'<li style="margin-bottom:4px;">Pick your language pair (e.g. Swahili &rarr; English).</li>'
        f'<li>We\'ll notify you the moment your result is ready to download. Instant AI results arrive in minutes and are AI-generated; Human-reviewed results are checked by a dedicated editor first.</li>'
        f'</ol>'
        f'<p style="margin:0 0 14px;">If anything\'s unclear, or you\'d rather just talk it through, '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email.</p>'
        f'<p style="margin:0;">Talk soon,<br>{TEAM_NAME}</p>'
    )
    _queue_and_send(user, "welcome", subject, body, base_url=base_url,
                     cta_text="Upload your first file", cta_url=cta_url, html_inner=html_inner)


def _queue_first_payment_message(user, base_url: str = "") -> None:
    if db.has_onboarding_message(user["id"], "first_payment"):
        return
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    subject = "Thanks for trusting Kauli with your first order"
    body = (
        f"Hi {name},\n\n"
        "Thanks for the vote of confidence - that's your first paid order with Kauli, and we don't "
        "take that lightly at this stage.\n\n"
        "We'll be keeping an eye on this one personally. If the turnaround or the quality isn't "
        "what you expected, just reply to this email - and we'll make it right.\n\n"
        "One honest ask: how's it going so far?"
    )
    feedback_base = f"{base_url.rstrip('/')}" if base_url else ""
    rating_row = (
        f'<p style="margin:18px 0 0;">'
        f'<a href="{feedback_base}/feedback/first_payment/{user["id"]}/great" style="color:{mailer.BRAND_ACCENT}; font-weight:600; text-decoration:none;">Great</a>'
        f' &nbsp;&middot;&nbsp; '
        f'<a href="{feedback_base}/feedback/first_payment/{user["id"]}/good" style="color:{mailer.BRAND_MUTED}; font-weight:600; text-decoration:none;">Good</a>'
        f' &nbsp;&middot;&nbsp; '
        f'<a href="{feedback_base}/feedback/first_payment/{user["id"]}/needs_work" style="color:{mailer.BRAND_MUTED}; font-weight:600; text-decoration:none;">Needs work</a>'
        f'</p>'
    )
    body = body + f"\n\nTalk soon,\n{TEAM_NAME}"
    message_id = db.queue_onboarding_message(user["id"], "first_payment", subject, body)
    if mailer.email_configured():
        html_body = (
            f'Thanks for the vote of confidence - that\'s your first paid order with Kauli, and we '
            f'don\'t take that lightly at this stage.\n\n'
            f'We\'ll be keeping an eye on this one personally. If the turnaround or the quality isn\'t '
            f'what you expected, tell us directly.'
        )
        inner = (
            f'<p style="margin:0 0 14px;">Hi {name},</p>'
            + mailer.text_to_html_paragraphs(html_body)
            + f'<p style="margin:0 0 14px;">Just '
              f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email '
              f'- and we\'ll make it right.</p>'
            f'<p style="margin:0 0 14px;">One honest ask: how\'s it going so far?</p>'
        )
        inner += rating_row
        inner += f'<p style="margin:20px 0 0;">Talk soon,<br>{TEAM_NAME}</p>'
        html = mailer.wrap_email_html(inner, base_url=base_url)
        ok, detail = mailer.send_email(user["email"], subject, html, body)
        db.set_onboarding_message_email_result(message_id, ok, detail)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Logged-in visitors go straight to their workspace, same as before.
    # Logged-out visitors now land on the public marketing page instead of
    # being bounced straight to /login - this is the "get clients from"
    # front door, not the app itself.
    user = current_user(request)
    if user:
        return RedirectResponse(
            "/staff" if user["role"] == "staff" else "/actor" if user["role"] == "voice_actor" else "/client/home")
    return templates.TemplateResponse(request, "marketing.html",
        {**_marketing_context(sent=request.query_params.get("sent") == "1", market=_display_market(request)),
         "demo": _try_context(request), "hreflang_alts": _hreflang_alternates(_site_base(request)),
         "hreflang_default": f"{_site_base(request)}/"})


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", _marketing_context(home="/"))


@app.get("/developers", response_class=HTMLResponse)
def developers_page(request: Request):
    ctx = _marketing_context(home="/")
    ctx["base"] = _site_base(request)
    return templates.TemplateResponse(request, "developers.html", ctx)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _marketing_context(home="/"))


@app.get("/formatting-standards", response_class=HTMLResponse)
def formatting_standards_page(request: Request):
    """Client-safe explanation of how a delivered transcript/caption/dub is
    formatted by default - real output-facing conventions only, not
    Ereri's internal editing mechanics (that's /staff/guide). Public, no
    login redirect - same treatment as /privacy and /terms - so a client
    can review this before deciding whether our defaults are right for
    them or whether to pick different options / upload their own style
    guide at order time."""
    return templates.TemplateResponse(request, "formatting_standards.html", _marketing_context(home="/"))


# ------------------------------------------------------------ lead qual ----
# Triage signal only, not a gate - a free-email lead still becomes a real
# lead (see request_callback below). Real early NGOs/individual decision
# makers in Kenya often use personal email before they have a corporate
# domain; rejecting those outright would cost real leads this business
# doesn't yet have the volume to afford losing.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "aol.com", "protonmail.com", "yahoo.co.uk", "msn.com",
}


def _is_personal_email_domain(email: str) -> bool:
    domain = email.strip().lower().rsplit("@", 1)[-1] if "@" in email else ""
    return domain in _PERSONAL_EMAIL_DOMAINS


# --------------------------------------------------------------- blog ----
def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


@app.get("/blog/rss.xml")
def blog_rss(request: Request):
    """The blog as an RSS 2.0 feed (newest 30 published posts): readers, aggregators and search engines use it to
    find new posts quickly."""
    from email.utils import formatdate
    from xml.sax.saxutils import escape
    base = _site_base(request)
    items = []
    for p in list(db.list_blog_posts(published_only=True))[:30]:
        when = p["published_at"] or p["updated_at"] or p["created_at"]
        link = f"{base}/blog/{p['slug']}"
        items.append(f"<item><title>{escape(p['title'])}</title><link>{link}</link><guid isPermaLink=\"true\">{link}</guid>"
                     f"<pubDate>{formatdate(when, usegmt=True)}</pubDate><description>{escape(p['description'] or '')}</description></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>Kauli blog</title>'
           f"<link>{base}/blog</link><description>Guides on transcription, translation, subtitles and dubbing.</description>"
           "<language>en</language>" + "".join(items) + "</channel></rss>")
    return Response(content=xml, media_type="application/rss+xml")


@app.get("/blog", response_class=HTMLResponse)
def blog_index(request: Request):
    return templates.TemplateResponse(request, "blog_index.html", {
        **_marketing_context(home="/"),
        "posts": db.list_blog_posts(published_only=True),
        "blog_categories": db.BLOG_CATEGORIES,
    })


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(request: Request, slug: str):
    post = db.get_blog_post_by_slug(slug, published_only=True)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    db.increment_blog_post_views(post["id"])
    # Real per-view analytics (see blog_views' own schema comment) - country
    # comes free from Cloudflare's own header, no geolocation API needed.
    # visitor_id is generated once per anonymous session (not a durable
    # cross-session fingerprint) purely to approximate "how many distinct
    # people read this", same session Starlette already keeps for everyone.
    if "blog_visitor_id" not in request.session:
        request.session["blog_visitor_id"] = uuid.uuid4().hex
    db.record_blog_view(post["id"], request.headers.get("cf-ipcountry"), request.session["blog_visitor_id"])
    # Real last-touch attribution for _complete_auth_session below - the
    # post someone was reading right before they signed up, not guessed
    # from a UTM param nobody actually attaches to an internal blog link.
    request.session["last_blog_post_id"] = post["id"]
    author = db.get_user(post["author_id"]) if post["author_id"] else None
    return templates.TemplateResponse(request, "blog_post.html", {
        **_marketing_context(home="/"),
        "post": post,
        "audio": blog_audio.ready_for(post["id"]),
        "author": author,
        "author_name": author["display_name"] if author else "Kauli",
        "published_at_iso": datetime.fromtimestamp(post["published_at"]).isoformat() if post["published_at"] else None,
        "updated_at_iso": datetime.fromtimestamp(post["updated_at"]).isoformat() if post["updated_at"] else None,
    })


@app.get("/sitemap.xml")
def sitemap(request: Request):
    """<lastmod> only where a real modification timestamp exists (blog
    posts have one - updated_at) - the static marketing/solution pages
    don't have real per-page change tracking, so they're listed without
    one rather than a fabricated date that would just be a guess.
    <priority> is this site's own honest ranking of its pages against each
    other (homepage highest, the actual commercial-intent solution pages
    next, reference/legal pages lowest) - not a claim about anything
    outside that, which is all <priority> has ever meant."""
    base = f"{_site_base(request)}"
    # (url, priority) - /formatting-standards was a real gap: a real, live,
    # indexable page that was simply never listed here at all.
    static_urls = [
        (f"{base}/", "1.0"),
        (f"{base}/blog", "0.6"),
        (f"{base}/formatting-standards", "0.4"),
        (f"{base}/terms", "0.2"),
        (f"{base}/privacy", "0.2"),
        (f"{base}/developers", "0.5"),
        (f"{base}/demo", "0.6"),
        (f"{base}/try", "0.7"),
        (f"{base}/human-review", "0.8"),
    ]
    static_urls += [(f"{base}/human-review/{slug}", "0.7") for slug in human_review_content.ORDER]
    static_urls += [(f"{base}/locations/{slug}", "0.6") for slug in locations.OFFICES]
    static_urls += [(f"{base}/solutions/{slug}", "0.8") for slug in SOLUTION_PAGES]
    static_urls += [(f"{base}/languages", "0.8"), (f"{base}/solutions", "0.8")]
    static_urls += [(f"{base}/transcribe/{slug}", "0.6") for slug in AI_LANGUAGE_SLUGS]
    static_urls += [(f"{base}/transcribe/{seo_pages.REVIEWED[c][0]}/{t}", "0.7") for c in seo_pages.REVIEWED for t in seo_pages.TASKS]
    # The homepage and the translated landing pages are alternates of each
    # other - every one lists the whole set, itself included.
    alt_urls = {f"{base}/"} | {f"{base}/{code}" for code in site_locales.LOCALES}
    static_urls += [(f"{base}/{code}", "0.7") for code in site_locales.LOCALES]
    alt_links = "".join(
        f'<xhtml:link rel="alternate" hreflang="{hl}" href="{href}"/>' for hl, href in _hreflang_alternates(base))
    alt_links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{base}/"/>'
    # Every public page also has an address in each language (/zh/solutions/..., see localize_pages); each one lists the whole set.
    not_translated = ("/terms", "/privacy", "/formatting-standards", "/developers", "/demo")

    def alternates(path: str) -> str:
        links = "".join(f'<xhtml:link rel="alternate" hreflang="{"en" if c == "en" else ui_text.HTML_LANG.get(c, c)}" href="{_lang_url(base, c, path)}"/>'
                        for c in ui_text.LANGS)
        return links + f'<xhtml:link rel="alternate" hreflang="x-default" href="{_lang_url(base, "en", path)}"/>'

    def localizable(path: str) -> bool:
        return page_translate_enabled and _is_public_page(path) and path != "/" and not path.startswith(not_translated)

    page_translate_enabled = page_translate.configured()
    entries = []
    for u, p in static_urls:
        path = u[len(base):] or "/"
        entries.append(f"  <url><loc>{u}</loc>{alt_links if u in alt_urls else (alternates(path) if localizable(path) else '')}<priority>{p}</priority></url>")
        if localizable(path):
            entries += [f"  <url><loc>{_lang_url(base, c, path)}</loc>{alternates(path)}<priority>{max(0.3, float(p) - 0.1):.1f}</priority></url>"
                        for c in ui_text.LANGS if c != "en"]
    for p in db.list_blog_posts(published_only=True):
        loc = f"{base}/blog/{p['slug']}"
        lastmod = datetime.fromtimestamp(p["updated_at"]).strftime("%Y-%m-%d") if p["updated_at"] else None
        stamp = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        path = f"/blog/{p['slug']}"
        entries.append(f"  <url><loc>{loc}</loc>{stamp}{alternates(path) if localizable(path) else ''}<priority>0.5</priority></url>")
        if localizable(path):
            entries += [f"  <url><loc>{_lang_url(base, c, path)}</loc>{stamp}{alternates(path)}<priority>0.4</priority></url>" for c in ui_text.LANGS if c != "en"]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
           + "\n".join(entries) + "\n</urlset>")
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt(request: Request):
    """Everything public is open to every crawler; the signed-in app, the account utilities and the machine endpoints
    are not (and also answer with a noindex header, see add_trace_id). The AI search and answer crawlers are named
    so nobody has to guess that they are welcome: real user-agent strings only."""
    base = _site_base(request)
    ai_bots = ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User", "Claude-SearchBot", "PerplexityBot",
               "Perplexity-User", "Google-Extended", "Applebot-Extended", "CCBot", "Bytespider", "Amazonbot", "Meta-ExternalAgent")
    private = ("/client", "/staff", "/settings", "/actor", "/lang/", "/api/", "/receipts/", "/avatar/", "/status",
              "/webhooks/", "/billing/", "/notifications/", "/download/", "/s/", "/r/")
    body = "User-agent: *\nAllow: /\n" + "".join(f"Disallow: {p}\n" for p in private)
    body += "".join(f"\nUser-agent: {bot}\nAllow: /\n" for bot in ai_bots)
    body += f"\nSitemap: {base}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")


@app.get("/google5dd3b740dee7aa1f.html")
def google_search_console_verification():
    """Google Search Console's file verification for this site: the file must answer with exactly this line. Served as plain
    text so the page-translation layer never touches it."""
    return Response(content="google-site-verification: google5dd3b740dee7aa1f.html", media_type="text/plain")


def _llms_text(request: Request, full: bool) -> str:
    """A plain-text map of the business for a language model to read: what Kauli does, what it costs, where, and in which
    languages, with a link for every claim. Built from the same tables the site itself uses, so it cannot drift."""
    base = _site_base(request)
    card = billing.PRICE_CARD["intl"]
    total = len(AI_ONLY_LANGUAGES) + len(SOURCE_LANGUAGES)
    hr = _hr_languages()
    hr_live = [l["name"] for l in hr if l["live"]]
    hr_soon = [l["name"] for l in hr if not l["live"]]
    solution_lines = "\n".join(f"- [{p['title']}]({base}/solutions/{slug})" for slug, p in SOLUTION_PAGES.items())
    posts = db.list_blog_posts(published_only=True)
    post_lines = "\n".join(f"- [{p['title']}]({base}/blog/{p['slug']})" for p in posts[: (60 if full else 12)])
    summary = (
        f"> Kauli turns audio and video into transcripts, subtitles, translations and dubbed audio in {total} languages, "
        "with human review in selected languages. It is a product of Forge Media Services, with offices in Nairobi, Kenya "
        "and Dallas, Texas, and it serves clients worldwide. Two ways to order: Instant AI (ready in minutes, machine-generated, "
        "not reviewed by a person) and Human-reviewed (a dedicated editor checks the work against the source audio before delivery). "
        "Human review covers Swahili, Kikuyu, English and more.")
    key_pages = f"""## Key pages
- [Homepage]({base}/) - overview, pricing, FAQ
- [Hear a demo, no sign-up]({base}/try) - pick a scenario and language and play the result
- [Human review]({base}/human-review) - the languages a person checks, and how
- [All languages]({base}/languages)
- [Developers: REST API]({base}/developers)
- [Book a demo]({base}/demo)
- [Kauli Nairobi]({base}/locations/nairobi) / [Kauli Dallas]({base}/locations/dallas)
{solution_lines}
- [Blog]({base}/blog)
- [How we format your content]({base}/formatting-standards)
- [Terms]({base}/terms) / [Privacy]({base}/privacy)"""
    contact = f"## Contact\n{CONTACT_EMAIL}\nNairobi: {locations.OFFICES['nairobi']['address_line']}\nDallas: {locations.OFFICES['dallas']['address_line']}"
    if not full:
        return f"# Kauli\n\n{summary}\n\nMore detail for AI readers: {base}/llms-full.txt\n\n{key_pages}\n\n## Blog posts\n{post_lines}\n\n{contact}\n"
    rates = "\n".join(
        f"- {sl['name']}: Instant AI ${card[key]['ai']:.2f}, human-reviewed ${card[key]['human']:.2f} per audio-minute"
        for key, sl in billing.SERVICE_LEVELS.items())
    plan_lines = []
    for key in ("free", "pro", "premium"):
        p = billing.PLANS[key]
        if key == "free":
            plan_lines.append(f"- Free: no card needed, {p['included_ai_minutes']:.0f} Instant AI minutes free every month, then pay as you go.")
        else:
            plan_lines.append(
                f"- {p['name']}: ${p['price_usd']} a month, {p['included_ai_minutes']:,.0f} Instant AI minutes{billing.hours_suffix(p['included_ai_minutes'])} (transcription and translation) included "
                f"every month{', burned captions and dubbed video included' if p['video_deliverables'] else ''}.")
    plan_lines.append("- Enterprise: negotiated volume rate, invoicing and a named account manager. Contact us.")
    meetings = ("Kauli can join Zoom, Google Meet and Microsoft Teams meetings, record them and return a transcript (Pro and Premium plans)."
                if meeting_bot.configured() else "A meeting notetaker for Zoom, Google Meet and Microsoft Teams is coming soon.")
    hr_lines = "\n".join(f"- [{l['name']}]({base}/human-review/{l['slug']}) - {'available now' if l['live'] else 'coming soon'}" for l in hr)
    offices = "\n".join(
        f"- [Kauli {o['city']}]({base}/locations/{slug}): {o['address_line']}" for slug, o in locations.OFFICES.items())
    locales_line = ", ".join(f"{base}/{c} ({m[0]})" for c, m in site_locales.LOCALES.items())
    return f"""# Kauli

{summary}

## What Kauli does
- Transcription: audio and video to text with speakers and timestamps, downloaded as TXT, DOCX, SRT or VTT.
- Translation: transcripts and subtitles between {total} languages (machine translation on Instant AI; reviewed by a person on human-reviewed orders).
- Subtitles and captions for YouTube and any video; connect a YouTube channel to publish captions.
- Dubbing: the same video spoken in a new language.
- Human review: an editor checks the work against the audio in selected languages.
- {meetings}
- A REST API for creating orders from software: {base}/developers

## Pricing (US dollars, per audio-minute)
{rates}
Rates can differ by region; the exact price of an order is always shown before it is confirmed.
Human review costs more than Instant AI because a person does the work, and takes longer.

## Plans
{chr(10).join(plan_lines)}
Plans are billed monthly. Pay as you go needs no plan.

## Languages
Instant AI transcription: {total} languages. The full list is at {base}/languages, and each language has a page at {base}/transcribe/<language>.
Human review is available now in {', '.join(hr_live)}; coming soon: {', '.join(hr_soon) if hr_soon else 'more languages'}.
{hr_lines}
The site and the account are also available in {len(site_locales.LOCALES) + 1} languages: {locales_line}.

## Offices and where we serve
Kauli is an online service and works with clients in every country. Offices:
{offices}

{key_pages}

## Blog posts
{post_lines}

{contact}
"""


@app.get("/llms.txt")
def llms_txt(request: Request):
    return Response(content=_llms_text(request, full=False), media_type="text/plain; charset=utf-8")


@app.get("/llms-full.txt")
def llms_full_txt(request: Request):
    return Response(content=_llms_text(request, full=True), media_type="text/plain; charset=utf-8")


@app.get("/locations/{slug}", response_class=HTMLResponse)
def location_page(request: Request, slug: str):
    office = locations.OFFICES.get(slug)
    if not office:
        return HTMLResponse("Page not found.", status_code=404)
    redirect = _logged_in_redirect(request)
    if redirect:
        return redirect
    base = _site_base(request)
    url = f"{base}/locations/{slug}"
    other_slug = next(k for k in locations.OFFICES if k != slug)
    address = {"@type": "PostalAddress", "streetAddress": office["street"], "addressLocality": office["city"],
               "addressCountry": office["country_code"]}
    if office["region"]:
        address["addressRegion"] = office["region"]
    if office["postal"]:
        address["postalCode"] = office["postal"]
    business = {
        "@context": "https://schema.org", "@type": "LocalBusiness", "@id": f"{url}#office", "name": f"Kauli {office['city']}",
        "url": url, "image": f"{base}/static/kauli-social.png", "email": CONTACT_EMAIL, "description": office["description"],
        "address": address, "areaServed": "Worldwide",
        "parentOrganization": {"@type": "Organization", "name": "Forge Media Services", "url": f"{base}/"},
        "brand": {"@type": "Brand", "name": "Kauli"},
        "knowsLanguage": ["en", "sw", "ki", "es", "zh", "ar", "fr", "pt", "de", "hi", "ja", "ko"],
        "makesOffer": [{"@type": "Offer", "itemOffered": {"@type": "Service", "name": name}} for name, _text, _href in office["serves"]],
    }
    return templates.TemplateResponse(request, "location.html", {
        **_marketing_context(home="/", market=_display_market(request)),
        "office": office, "slug": slug, "other": {"slug": other_slug, "city": locations.OFFICES[other_slug]["city"]},
        "jsonld": [business, _faq_jsonld(office["faq"]),
                   _breadcrumb_jsonld(base, [("Kauli", "/"), (f"Kauli {office['city']}", f"/locations/{slug}")])],
    })


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    """Staff-only - real operational detail (queue depth, which payment
    providers are live, dead-letter counts) isn't something to publish to
    the open internet just because a status page looks professional.

    Otherwise a real status page, not a static 'All Systems Operational'
    badge - every row below is checked live, right now, the moment this
    loads. Core platform (site + database) is the only thing that drives
    the top banner; a payment integration being unconfigured is reported
    honestly but isn't treated as an outage - M-Pesa being sandbox-only
    right now is a real, deliberate state, not a failure."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db_ok, db_detail = True, "Reachable"
    try:
        conn = db.get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as exc:
        db_ok, db_detail = False, f"Query failed: {exc}"

    mpesa_configured = bool(os.environ.get("MPESA_CONSUMER_KEY")) and bool(os.environ.get("MPESA_CONSUMER_SECRET"))
    claude_configured = bool(os.environ.get("ANTHROPIC_API_KEY"))
    core_checks = [
        {"name": "Website & client app", "ok": True, "detail": "Reachable - you're looking at it right now"},
        {"name": "Database", "ok": db_ok, "detail": db_detail},
    ]
    integration_checks = [
        {"name": "Card / M-Pesa / Airtel Money via Paystack", "ok": billing.paystack_configured(),
         "detail": ("Live - real payments" if billing._paystack_live_mode() else "Configured (test mode only)")
                    if billing.paystack_configured() else "Not configured"},
        {"name": "M-Pesa direct (STK push)", "ok": mpesa_configured,
         "detail": ("Configured (" + ("live" if os.environ.get("MPESA_ENV") == "production" else "sandbox - test only") + ")")
                    if mpesa_configured else "Not configured"},
        {"name": "Priority AI translation (Claude)", "ok": claude_configured,
         "detail": "Connected" if claude_configured else "Not connected - orders fall back to the local translation model"},
    ]
    counts = db.orders_by_status()
    return templates.TemplateResponse(request, "status.html", {
        **_marketing_context(),
        "core_checks": core_checks,
        "integration_checks": integration_checks,
        "all_core_ok": all(c["ok"] for c in core_checks),
        "queue_depth": counts.get("queued", 0) + counts.get("processing", 0),
        "dead_letter_count": counts.get("dead_letter", 0),
        "uptime_seconds": time.time() - _PROCESS_STARTED_AT,
        "checked_at": datetime.now(),
    })


def _elevenlabs_configured() -> bool:
    """Premium voice is offered only while there is a key AND the add-on price still clears the
    margin floor at the current ElevenLabs cost - it is never sold at a loss."""
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and billing.premium_voice_margin_ok() and provider_health.is_enabled("elevenlabs")


def _studio_languages() -> set[str]:
    """Where Studio (human review + ElevenLabs) and the premium voice work: the human-reviewed languages
    ElevenLabs also speaks. Kikuyu is out - no ElevenLabs model supports it."""
    return {"en", "sw"} | _review_extra_languages()


def _extra_review_ok(market: str | None, service_level: str) -> bool:
    return billing.review_extra_margin_ok(billing.rate_for(service_level, market, "human"))


templates.env.globals["extra_review_languages"] = lambda: sorted(_review_extra_languages())
templates.env.globals["premium_voice_languages"] = lambda: sorted(_studio_languages())
templates.env.globals["language_names"] = LANGUAGE_NAMES


def _studio_available() -> bool:
    """The Studio tier is offered only while every ElevenLabs part of it is on, and priced above its margin floor."""
    return _elevenlabs_configured() and _audio_cleanup_available() and billing.premium_asr_margin_ok()


def _audio_cleanup_available() -> bool:
    """Offered only with a key and while the add-on price still clears the margin floor at today's cost. (The Instant AI price has
    its own, smaller floor: see billing.audio_cleanup_ai_margin_ok.)"""
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and (billing.audio_cleanup_margin_ok() or billing.audio_cleanup_ai_margin_ok())


_ELEVEN_BALANCE_CACHE: dict = {"at": 0.0, "remaining": None}


def _elevenlabs_remaining_chars() -> int | None:
    """Characters left on the ElevenLabs plan this period (None if it cannot be read), cached a minute."""
    if time.time() - _ELEVEN_BALANCE_CACHE["at"] < 60:
        return _ELEVEN_BALANCE_CACHE["remaining"]
    remaining = None
    try:
        resp = httpx.get("https://api.elevenlabs.io/v1/user/subscription",
                         headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}, timeout=10.0,
                         follow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
        remaining = max(int(data["character_limit"]) - int(data["character_count"]), 0)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        remaining = None
    _ELEVEN_BALANCE_CACHE.update({"at": time.time(), "remaining": remaining})
    return remaining


def _audio_cleanup_capacity_ok(minutes: float) -> bool:
    """Same spend ceiling as the premium voice: only taken while the plan has the credits left."""
    if os.environ.get("ELEVENLABS_ALLOW_OVERAGE") == "1":
        return True
    remaining = _elevenlabs_remaining_chars()
    return remaining is not None and remaining >= billing.audio_cleanup_credits_needed(minutes)


def _premium_capacity_ok(minutes: float) -> bool:
    """Spend ceiling: an order is only taken if the ElevenLabs plan has the characters left for it, so the
    voice bill can never run past the subscription into pay-per-use overage. Set ELEVENLABS_ALLOW_OVERAGE=1
    to accept orders beyond the plan (and enable overage on the ElevenLabs side)."""
    if os.environ.get("ELEVENLABS_ALLOW_OVERAGE") == "1":
        return True
    remaining = _elevenlabs_remaining_chars()
    return remaining is not None and remaining >= billing.premium_voice_chars_needed(minutes)


_PREMIUM_VOICES_CACHE: dict = {"at": 0.0, "voices": []}


def _premium_voices(force: bool = False) -> list[dict]:
    """ElevenLabs' PREMADE voices, cached for an hour. Only premade ones: the account's own cloned
    or custom voices belong to someone and are never offered to customers."""
    if not _elevenlabs_configured():
        return []
    if not force and time.time() - _PREMIUM_VOICES_CACHE["at"] < 3600 and _PREMIUM_VOICES_CACHE["voices"]:
        return _PREMIUM_VOICES_CACHE["voices"]
    try:
        resp = httpx.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                         timeout=15.0, follow_redirects=False)
        resp.raise_for_status()
        raw = resp.json().get("voices", [])
    except (httpx.HTTPError, ValueError):
        return _PREMIUM_VOICES_CACHE["voices"]            # a stale list beats none
    voices = []
    for v in raw:
        vid = str(v.get("voice_id") or "")
        if v.get("category") != "premade" or not re.fullmatch(r"[A-Za-z0-9]{10,40}", vid):
            continue
        labels = v.get("labels") or {}
        voices.append({"id": vid, "name": str(v.get("name") or "Voice")[:60],
                       "description": " - ".join(str(labels[k]) for k in ("gender", "accent", "age") if labels.get(k))[:80]})
    voices.sort(key=lambda x: x["name"].lower())
    _PREMIUM_VOICES_CACHE.update({"at": time.time(), "voices": voices})
    return voices


@app.get("/client/premium-voices")
def client_premium_voices(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    voices = _premium_voices()
    return JSONResponse({"available": _elevenlabs_configured() and bool(voices), "voices": voices})


DEMO_INTERESTS = {
    "transcription": "Transcription",
    "translation": "Translation and subtitles",
    "dubbing": "Dubbing and voice over",
    "localization": "Full localization",
    "api": "API and integrations",
    "enterprise": "Enterprise or a custom volume plan",
    "other": "Something else",
}


def _calendly_embed_url(name: str, email: str) -> str | None:
    """The scheduling page with the visitor's name and email already filled in.
    None until KAULI_CALENDLY_URL is set (a round-robin team event in Calendly)."""
    base = os.environ.get("KAULI_CALENDLY_URL")
    if not base:
        return None
    from urllib.parse import urlencode
    return base + ("&" if "?" in base else "?") + urlencode(
        {"name": name, "email": email, "hide_gdpr_banner": 1, "primary_color": "2262ec", "text_color": "030712"})


def _demo_response(request: Request, error: str | None = None, form: dict | None = None, status_code: int = 200):
    lead = request.session.get("demo_lead")
    ctx = _marketing_context(home="/")
    ctx.update({
        "interests": DEMO_INTERESTS, "demo_error": error, "form": form or {}, "demo_lead": lead,
        "calendly_embed_url": _calendly_embed_url(lead["name"], lead["email"]) if lead else None,
    })
    return templates.TemplateResponse(request, "demo.html", ctx, status_code=status_code)


@app.get("/demo", response_class=HTMLResponse)
def demo_page(request: Request):
    return _demo_response(request)


@functools.lru_cache(maxsize=1)
def _demo_timing() -> dict:
    """Who speaks when in every demo clip (scripts/make_demo_audio.py writes it): clip file name -> [[speaker, start_s, end_s], ...]."""
    try:
        return json.loads((Path(__file__).parent / "static" / "demo" / "timing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _try_context(request: Request) -> dict:
    """What the /try demo needs: the clips, the captions and the visitor's likely language and service.
    The audio is pre-rendered (scripts/make_demo_audio.py), so nothing here costs ElevenLabs characters."""
    country = request.headers.get("cf-ipcountry")
    accept = request.headers.get("accept-language")
    suggested, language_why = demo_content.pick_language(country, accept, None)
    language, _ = demo_content.pick_language(country, accept, request.query_params.get("lang"))
    scenario_suggested, scenario_why = demo_content.pick_scenario(request.headers.get("referer"), None)
    scenario, _ = demo_content.pick_scenario(request.headers.get("referer"), request.query_params.get("use"))
    dub_live = _studio_languages()
    static_dir = Path(__file__).parent / "static" / "demo"
    scenarios = {}
    for key, s in demo_content.SCENARIOS.items():
        dub, inlang = {}, {}
        for lang in s["tr"]:
            if s["service"] == "dub" and (static_dir / demo_content.audio_name(key, lang)).exists():
                dub[lang] = f"/static/demo/{demo_content.audio_name(key, lang)}"
            if (static_dir / demo_content.audio_in_name(key, lang)).exists():
                inlang[lang] = f"/static/demo/{demo_content.audio_in_name(key, lang)}"
        videos = {lang: f"/static/demo/{demo_content.video_name(key, lang)}" for lang in ["en", *s["tr"]]
                  if (static_dir / demo_content.video_name(key, lang)).exists()}
        scenarios[key] = {
            "video": videos, "music": key in demo_content.MUSIC_BEDS,
            "multi": len(s["cast"]) > 1, "cast": [c["name"] for c in s["cast"]],
            "label": s["label"], "service": s["service"], "service_label": s["service_label"], "speaker": s["speaker"],
            "headline": s["headline"], "en": s["en"], "tr": s["tr"], "src": f"/static/demo/{demo_content.audio_name(key, None)}",
            "dub": dub, "inlang": inlang, "headline_en": s["headline_en"],
        }
    market = _display_market(request)
    card = billing.price_card_for(market)
    return {
        "scenario": scenario, "language": language, "suggested": suggested, "language_why": language_why,
        "dir": "to" if request.query_params.get("dir") == "to" else "from",
        "scenario_suggested": scenario_suggested, "scenario_why": scenario_why, "scenarios": scenarios,
        "timing": _demo_timing(), "labels": demo_content.speaker_labels(),
        "languages": {code: {**l, "dub_live": code in dub_live} for code, l in demo_content.LANGUAGES.items()},
        "rates": {k: {"ai": f"{card[k]['ai']:.2f}", "human": f"{card[k]['human']:.2f}"} for k in billing.SERVICE_LEVELS},
    }


_COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia", "KE": "Kenya", "NG": "Nigeria", "IN": "India",
    "TZ": "Tanzania", "UG": "Uganda", "RW": "Rwanda", "CD": "Democratic Republic of the Congo", "CN": "China", "TW": "Taiwan",
    "HK": "Hong Kong", "SG": "Singapore", "DE": "Germany", "AT": "Austria", "CH": "Switzerland", "ES": "Spain", "MX": "Mexico",
    "AR": "Argentina", "CO": "Colombia", "FR": "France", "BE": "Belgium", "SN": "Senegal", "CI": "C\u00f4te d'Ivoire",
}

_HR_FAQ = [
    ("What is human review?", "A dedicated editor checks the transcript, translation or subtitles against your audio before the order is marked ready. "
     "Instant AI orders are machine-generated and say so; human-reviewed orders have a person's sign-off."),
    ("Which languages have human review?", "Each language on this page shows whether it is available now or coming soon. Instant AI transcription and "
     "translation work in many more languages today."),
    ("How much does human review cost?", "It is priced per audio-minute and the exact price is shown before you confirm any order."),
    ("How long does it take?", "Longer than Instant AI, because a person checks the work. You see an estimated delivery time when you order."),
    ("Can I see the result before I download it?", "Yes. A finished order plays in the browser with live captions and a transcript that follows along, "
     "and you can ask for a revision on anything you want changed."),
]


def _human_review_live() -> set[str]:
    """Language codes human review can really be ordered in right now (the same rule the order form uses)."""
    return {"en", "sw", "ke"} | _review_extra_languages()


def _hr_blogs(keywords: list[str], limit: int = 3) -> list[dict]:
    posts = db.list_blog_posts(published_only=True)

    def score(p):
        text = f"{p['title']} {p['slug']} {p['description'] or ''}".lower()
        return sum(1 for k in keywords if k in text)

    ranked = sorted(posts, key=lambda p: (-score(p), -(p["published_at"] or 0)))
    return [{"slug": p["slug"], "title": p["title"], "description": p["description"]} for p in ranked[:limit]]


def _hr_languages() -> list[dict]:
    live = _human_review_live()
    out = []
    for slug in human_review_content.ORDER:
        lang = human_review_content.LANGUAGES[slug]
        out.append({**lang, "slug": slug, "live": lang["code"] in live})
    return out


def _hr_context(request: Request) -> dict:
    ctx = _marketing_context(home="/", market=_display_market(request))
    card = billing.price_card_for(_display_market(request))
    ctx.update({"langs": _hr_languages(), "benefits": human_review_content.BENEFITS, "tips": human_review_content.TIPS,
                "human_rate": f"{card['transcribe']['human']:.2f}", "faq": _HR_FAQ})
    return ctx


def _faq_jsonld(pairs) -> dict:
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs]}


def _breadcrumb_jsonld(base: str, trail: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name, "item": f"{base}{path}"} for i, (name, path) in enumerate(trail)]}


@app.get("/human-review", response_class=HTMLResponse)
def human_review_hub(request: Request):
    ctx = _hr_context(request)
    base = _site_base(request)
    ctx["blogs"] = _hr_blogs(human_review_content.GENERAL_KEYWORDS)
    ctx["jsonld"] = [
        {"@context": "https://schema.org", "@type": "WebPage", "name": "Human review in selected languages", "url": f"{base}/human-review",
         "description": "Real editors check transcripts, subtitles and dubs against your audio.", "isPartOf": {"@type": "WebSite", "name": "Kauli", "url": f"{base}/"}},
        {"@context": "https://schema.org", "@type": "ItemList", "name": "Languages with human review",
         "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": f"{l['name']} human review", "url": f"{base}/human-review/{l['slug']}"} for i, l in enumerate(ctx["langs"])]},
        _faq_jsonld(_HR_FAQ),
        _breadcrumb_jsonld(base, [("Kauli", "/"), ("Human review", "/human-review")]),
    ]
    return templates.TemplateResponse(request, "human_review_hub.html", ctx)


@app.get("/human-review/{slug}", response_class=HTMLResponse)
def human_review_language(request: Request, slug: str):
    lang = human_review_content.LANGUAGES.get(slug)
    if not lang:
        return HTMLResponse("Not found.", status_code=404)
    ctx = _hr_context(request)
    base = _site_base(request)
    live = lang["code"] in _human_review_live()
    ctx.update({"lang": {**lang, "slug": slug}, "live": live, "dub_live": live,
                "blogs": _hr_blogs(lang["keywords"])})
    countries = [{"@type": "Country", "name": _COUNTRY_NAMES.get(c, c)} for c in lang["countries"]]
    ctx["jsonld"] = [
        {"@context": "https://schema.org", "@type": "Service", "name": f"{lang['name']} human review: transcription, translation, subtitles and dubbing",
         "serviceType": "Transcription, translation, subtitling and dubbing", "description": lang["description"], "url": f"{base}/human-review/{slug}",
         "provider": {"@type": "Organization", "name": "Kauli", "url": f"{base}/"}, "areaServed": countries,
         "availableLanguage": {"@type": "Language", "name": lang["name"]}},
        _faq_jsonld(lang["faq"]),
        _breadcrumb_jsonld(base, [("Kauli", "/"), ("Human review", "/human-review"), (lang["name"], f"/human-review/{slug}")]),
    ]
    return templates.TemplateResponse(request, "human_review_language.html", ctx)


@app.get("/try", response_class=HTMLResponse)
def try_page(request: Request):
    ctx = _marketing_context(home="/", market=_display_market(request))
    ctx["demo"] = _try_context(request)
    return templates.TemplateResponse(request, "try.html", ctx)


@app.get("/demo/new")
def demo_new(request: Request):
    request.session.pop("demo_lead", None)
    return RedirectResponse("/demo", status_code=303)


@app.post("/demo")
def demo_submit(request: Request, first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""),
                company: str = Form(""), job_title: str = Form(""), interest: str = Form(""),
                languages: str = Form(""), volume_estimate: str = Form(""), org_type: str = Form(""),
                message: str = Form(""), website: str = Form("")):
    if website.strip():                      # honeypot: pretend it worked
        return RedirectResponse("/demo", status_code=303)
    allowed, retry_after = rate_limit.check(f"demo:{rate_limit.client_ip(request)}", limit=5, window_s=600)
    form = {"first_name": first_name, "last_name": last_name, "email": email, "company": company,
            "job_title": job_title, "interest": interest, "languages": languages,
            "volume_estimate": volume_estimate, "org_type": org_type, "message": message}
    if not allowed:
        return _demo_response(request, f"Too many requests - try again in {retry_after // 60 + 1} minute(s).",
                              form, 429)
    first, last, email = first_name.strip()[:80], last_name.strip()[:80], email.strip().lower()[:200]
    if not first or not last or "@" not in email or "." not in email.split("@")[-1]:
        return _demo_response(request, "Please enter your first and last name and a valid work email.", form, 400)
    if not company.strip() or not volume_estimate.strip() or not org_type.strip() or interest not in DEMO_INTERESTS:
        return _demo_response(request, "Please tell us your company, what you are interested in, your monthly "
                                       "volume and what best describes your organization.", form, 400)
    name = f"{first} {last}"
    notes = [f"Job title: {job_title.strip()[:120] or '-'}", f"Interested in: {DEMO_INTERESTS[interest]}",
             f"Languages: {languages.strip()[:200] or '-'}"]
    if message.strip():
        notes += ["", message.strip()[:2000]]
    lead_id = db.create_lead(name, email, None, company.strip()[:160], "\n".join(notes), None, source="demo",
                             volume_estimate=volume_estimate.strip(), org_type=org_type.strip(),
                             personal_email_flag=_is_personal_email_domain(email))
    _notify_internal_new_lead(lead_id, name, email, org_type.strip(), volume_estimate.strip(),
                              base_url=str(request.base_url))
    ga_events.send_event(ga_events.client_id_from_request(request), "generate_lead",
                         {"org_type": org_type.strip(), "volume_estimate": volume_estimate.strip(), "kind": "demo"})
    request.session["demo_lead"] = {"first": first, "name": name, "email": email}
    return RedirectResponse("/demo", status_code=303)


@app.post("/contact/request-callback")
def request_callback(request: Request, name: str = Form(""), email: str = Form(""),
                      phone: str = Form(""), company: str = Form(""),
                      message: str = Form(""), preferred_time: str = Form(""),
                      website: str = Form(""), volume_estimate: str = Form(""),
                      org_type: str = Form("")):
    # Honeypot: a field real visitors never see or fill (hidden off-screen in
    # marketing.html, not display:none - some bots skip those), so anything
    # non-empty here is a bot. Pretend success rather than 400 - telling a
    # bot "wrong field" just teaches it to leave that one blank next time.
    if website.strip():
        return RedirectResponse("/demo", status_code=303)
    # Public, unauthenticated, no CAPTCHA - the one endpoint on this site a
    # spam bot can hit for free. Keyed on IP, tight window; the global
    # per-IP limiter above (300/min) is nowhere near tight enough for a lead
    # form specifically.
    allowed, retry_after = rate_limit.check(f"callback:{rate_limit.client_ip(request)}", limit=5, window_s=600)
    if not allowed:
        return templates.TemplateResponse(request, "marketing.html",
            {**_marketing_context(lead_error=f"Too many requests - try again in {retry_after // 60 + 1} minute(s)."), "demo": _try_context(request)},
            status_code=429)
    # Form(""), not Form(...) - a blank-valued field is dropped entirely by
    # Starlette's form parser (indistinguishable from a missing one), so
    # Form(...) here would 422 with a raw JSON error instead of the
    # friendly message below whenever someone submits with an empty Name.
    name = name.strip()
    email = email.strip().lower()
    if not name or "@" not in email:
        return templates.TemplateResponse(request, "marketing.html",
            {**_marketing_context(lead_error="Please enter your name and a valid email address."), "demo": _try_context(request)},
            status_code=400)
    if not volume_estimate.strip() or not org_type.strip():
        return templates.TemplateResponse(request, "marketing.html",
            {**_marketing_context(lead_error="Please select your estimated volume and organization type."), "demo": _try_context(request)},
            status_code=400)
    lead_id = db.create_lead(name, email, phone.strip() or None, company.strip() or None,
                              message.strip() or None, preferred_time.strip() or None,
                              volume_estimate=volume_estimate.strip() or None, org_type=org_type.strip() or None,
                              personal_email_flag=_is_personal_email_domain(email))
    _notify_internal_new_lead(lead_id, name, email, org_type.strip() or None, volume_estimate.strip() or None,
                               base_url=str(request.base_url))
    # GA4's own recommended lead-gen event name.
    ga_events.send_event(ga_events.client_id_from_request(request), "generate_lead",
                          {"org_type": org_type.strip() or None, "volume_estimate": volume_estimate.strip() or None})
    # Redirect (not a direct render) so refreshing the confirmation page
    # never re-submits the form - standard POST/redirect/GET.
    return RedirectResponse("/demo", status_code=303)


def _safe_next(path: str | None) -> str:
    """Only ever a relative in-app path - "/client/orders/xyz", never a
    protocol-relative or absolute URL (which could redirect somewhere
    outside Kauli after a real login). Falls back to "/" (the normal
    role-based landing page) for anything else, including empty/missing."""
    if path and path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


def _public_base_url(request: Request) -> str:
    """Prefer the real configured public URL (KAULI_PUBLIC_BASE_URL, the
    same one worker.py already uses for its own no-request-object case)
    over request.base_url, for any URL a third party validates against an
    exact allowlist. Confirmed live: behind Cloudflare's tunnel, this app
    sees the connecting peer as the Docker bridge gateway (172.17.0.1 in
    every real request log), which uvicorn's ProxyHeadersMiddleware
    doesn't trust by default - so request.base_url's scheme comes back as
    the internal "http" hop, not the real external "https", and Supabase's
    real redirect-URL allowlist match is exact, not scheme-agnostic. Falls
    back to request.base_url for local dev, where that env var is
    correctly left unset."""
    return os.environ.get("KAULI_PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


def _complete_auth_session(request: Request, session, next: str, marketing_consent: bool = False,
                            login_kind: str = "password") -> RedirectResponse:
    """The real provisioning that has to happen once Supabase confirms who
    someone is, regardless of which door they came through - shared by
    /login, /signup, and the Google OAuth callback below (extracted from
    what used to be near-identical copies inside /login and /signup, which
    is exactly the kind of thing that quietly drifts apart over time -
    Google sign-in would have been a third copy). marketing_consent only
    ever comes from the real signup checkbox; Google/email-login callers
    just don't have one to set. login_kind is only for the security_events
    row below - "password", "password_mfa", "google_oauth", or
    "google_one_tap" - so an audit trail can distinguish how someone
    actually got in."""
    email = session.user.email.strip().lower()
    role, is_admin = _resolve_role_and_admin(email)
    user, was_new = db.get_or_create_user(
        session.user.id, email, default_role=role,
        marketing_consent=marketing_consent, consent_ip=request.client.host if request.client else None,
        referred_by_blog_post_id=request.session.get("last_blog_post_id"),
    )
    db.claim_guest_orders(email, user["id"])                     # a short file tried before signing up becomes theirs
    db.log_security_event(
        "signup" if was_new else "login_success", user_id=user["id"], email=email,
        ip_address=rate_limit.client_ip(request), user_agent=request.headers.get("user-agent"),
        detail=login_kind)
    # GA4's own recommended event names (sign_up/login) - scoped to real
    # clients only, same reasoning as the welcome-message/notification
    # block below: a handful of known staff/voice-actor accounts logging
    # in isn't a marketing conversion, and counting it as one would just
    # be noise in the real acquisition funnel this exists to make visible.
    if role == "client":
        ga_client_id = ga_events.client_id_from_request(request)
        ga_events.send_event(ga_client_id, "sign_up" if was_new else "login", {"method": login_kind})
    # Not gated on was_new - an existing account can also accept a team
    # invite sent to their email; this just needs to run once per login,
    # which is exactly what it does (accept_team_invite is a no-op once
    # there's no more 'pending' row for this email).
    db.accept_team_invite(user["id"], email)
    if was_new:
        if is_admin:
            db.set_user_admin(user["id"], True)
        if role == "staff" and db.is_invited_staff(email):
            db.remove_staff_invite(email)  # consumed - the invite did its job
            db.log_security_event("staff_role_granted", user_id=user["id"], email=email,
                                   ip_address=rate_limit.client_ip(request), detail="invite accepted at signup")
        if role == "voice_actor":
            db.link_voice_actor_user(email, user["id"])  # consumed - same idea, see that function's comment
        if role == "client":  # the welcome message is written for a client, not a new staff account
            _queue_welcome_message(user, base_url=str(request.base_url))
            _notify_internal_new_signup(user, base_url=str(request.base_url))
            _capture_signup_ip_intel(user["id"], request)
            account_hub.record_referral(user, request.cookies.get("kauli_ref"))
            site_analytics.attribute_signup(user["id"], rate_limit.client_ip(request), request.headers.get("user-agent", ""))
            # Price card for this account: US/international vs East Africa,
            # from Cloudflare's visitor-country header (absent when hit
            # directly -> the international card). Fixed from here on; only
            # staff can change it (see /staff/clients/{id}/market).
            db.set_billing_market(user["id"], billing.market_for_country(request.headers.get("cf-ipcountry")))
    request.session["user_id"] = user["id"]
    # Their language follows them in: the translated site they came from (cookie) wins, else what the account
    # already had. Either way the cookie and the account end up agreeing.
    cookie_lang = request.cookies.get(LANG_COOKIE)
    try:
        saved_lang = user["ui_lang"]
    except (KeyError, IndexError):
        saved_lang = None
    keep_lang = cookie_lang if ui_text.valid(cookie_lang) else (saved_lang if ui_text.valid(saved_lang) else None)
    if keep_lang and keep_lang != saved_lang:
        db.set_ui_lang(user["id"], keep_lang)
    # Clicking a deep link from an email (an order, a receipt, billing)
    # while logged out used to always land back on the generic dashboard
    # after signing in, losing where they actually meant to go - this
    # sends them back to the real destination instead, same pattern every
    # real app uses. _safe_next already rejected anything that isn't a
    # real in-app path, so "/" (the normal role-based landing page) is
    # the only fallback, never an open redirect.
    resp = RedirectResponse(_safe_next(next), status_code=303)
    return _set_lang_cookie(resp, keep_lang) if keep_lang else resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, mode: str = "signin", notice: str | None = None, next: str | None = None,
                error: str | None = None):
    # error was a real, pre-existing gap: /login/google's own "not
    # configured" failure already redirected here with ?error=..., but this
    # route never read it, so that message silently never reached anyone -
    # same bug the new Google One Tap failure path below would otherwise
    # have quietly inherited.
    display_notice = None
    if notice == "account_closed":
        display_notice = "Your account has been closed."
    elif notice == "password_reset":
        display_notice = "Your password has been reset - sign in with your new password."
    elif notice == "session_expired":
        display_notice = "You were signed out after a period of inactivity. Please sign in again to continue."
    return templates.TemplateResponse(request, "login.html", {
        "error": error, "notice": display_notice, "mode": "signup" if mode == "signup" else "signin",
        "next": _safe_next(next) if next else "",
    })


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request, sent: str | None = None):
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": sent == "1"})


@app.post("/forgot-password")
def forgot_password_submit(request: Request, email: str = Form("")):
    email = email.strip().lower()
    # Same IP-keyed limit shape as the callback form - public,
    # unauthenticated, no CAPTCHA.
    allowed, retry_after = rate_limit.check(f"forgot:{rate_limit.client_ip(request)}", limit=5, window_s=600)
    if allowed and "@" in email:
        redirect_to = _public_base_url(request) + "/reset-password"
        supabase_auth.request_password_reset(email, redirect_to)
    # Always the same response whether the email exists, was rate-limited,
    # or Supabase errored - see request_password_reset's docstring for why.
    return RedirectResponse("/forgot-password?sent=1", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request):
    return templates.TemplateResponse(request, "reset_password.html", {"error": None})


@app.post("/reset-password")
def reset_password_submit(request: Request, access_token: str = Form(""), refresh_token: str = Form(""),
                           password: str = Form(""), confirm_password: str = Form("")):
    # IP-keyed, same shape as /forgot-password just above it - the tokens
    # themselves come from Supabase's own emailed link (not guessable), but
    # this still calls out to Supabase's API on every submit and is public/
    # unauthenticated, so it's worth the same guard against being hammered.
    allowed, retry_after = rate_limit.check(f"reset:{rate_limit.client_ip(request)}", limit=5, window_s=600)
    if not allowed:
        return templates.TemplateResponse(request, "reset_password.html", {
            "error": f"Too many attempts - try again in {retry_after // 60 + 1} minute(s).",
        }, status_code=429)
    if not access_token or not refresh_token:
        return templates.TemplateResponse(request, "reset_password.html", {
            "error": "That reset link is invalid or has expired. Request a new one.",
        }, status_code=400)
    if password != confirm_password:
        return templates.TemplateResponse(request, "reset_password.html", {"error": "Passwords don't match."},
                                           status_code=400)
    policy_errors = supabase_auth.password_policy_errors(password)
    if policy_errors:
        return templates.TemplateResponse(request, "reset_password.html", {
            "error": "Password needs " + ", ".join(policy_errors) + ".",
        }, status_code=400)
    ok, error, reset_email = supabase_auth.set_new_password(access_token, refresh_token, password)
    if not ok:
        return templates.TemplateResponse(request, "reset_password.html", {
            "error": error or "That reset link is invalid or has expired. Request a new one.",
        }, status_code=400)
    db.log_security_event("password_changed", email=reset_email, ip_address=rate_limit.client_ip(request),
                           user_agent=request.headers.get("user-agent"), detail="via reset link")
    return RedirectResponse("/login?notice=password_reset", status_code=303)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    email = email.strip().lower()
    # Brute-force guard: keyed on the email being attempted, not just IP -
    # protects one account from being hammered from many IPs, and many
    # accounts from being hammered from one. Doesn't touch the client-side
    # password policy, this is purely about attempt rate.
    allowed, retry_after = rate_limit.check(f"login:{email}", limit=8, window_s=60)
    if not allowed:
        # Real alerting for a real brute-force pattern - throttled to at
        # most once per 10 minutes per targeted email (its own separate
        # rate_limit key), so a sustained attack sends staff one email, not
        # one per blocked attempt for as long as it keeps going.
        alert_allowed, _ = rate_limit.check(f"login-alert:{email}", limit=1, window_s=600)
        if alert_allowed:
            notifications.notify_staff(
                "Kauli: repeated failed logins",
                f"More than 8 failed login attempts in 60s for {email} "
                f"(latest from {rate_limit.client_ip(request)}) - could be a real brute-force attempt, "
                "or someone who forgot their password and is retyping it fast.",
            )
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Too many attempts - try again in {retry_after}s.", "notice": None, "mode": "signin",
            "next": _safe_next(next),
        }, status_code=429)
    session, error = supabase_auth.sign_in(email, password)
    if error or not session:
        db.log_security_event("login_failed", email=email, ip_address=rate_limit.client_ip(request),
                               user_agent=request.headers.get("user-agent"), detail=error)
        return templates.TemplateResponse(request, "login.html", {
            "error": error or "Wrong email or password.", "notice": None, "mode": "signin",
            "next": _safe_next(next),
        })
    totp_factor_id = supabase_auth.verified_totp_factor_id(session)
    if totp_factor_id:
        # Password alone isn't enough for this account - the actual login
        # (setting request.session["user_id"], via _complete_auth_session)
        # is deliberately NOT done yet. Everything needed to finish once
        # the code checks out lives server-side (pending_auth_state - see
        # its own table comment), keyed by a small opaque token in the
        # cookie, not the tokens themselves.
        login_token = db.create_pending_auth_state("mfa_login", {
            "user_id": session.user.id, "email": session.user.email, "factor_id": totp_factor_id,
            "access_token": session.access_token, "refresh_token": session.refresh_token,
            "next": _safe_next(next),
        })
        request.session["_mfa_login_token"] = login_token
        return RedirectResponse("/login/mfa", status_code=303)
    return _complete_auth_session(request, session, next)


@app.get("/login/mfa", response_class=HTMLResponse)
def login_mfa_form(request: Request, error: str | None = None):
    pending = db.get_pending_auth_state(request.session.get("_mfa_login_token"), "mfa_login")
    if not pending:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "login_mfa.html", {"error": error, "email": pending["email"]})


@app.post("/login/mfa")
def login_mfa_submit(request: Request, code: str = Form(...)):
    login_token = request.session.get("_mfa_login_token")
    pending = db.get_pending_auth_state(login_token, "mfa_login")
    if not pending:
        return RedirectResponse("/login")
    # Keyed on the pending user, not IP - same reasoning as the password
    # login limit above. Tighter window (5, not 8): a TOTP code is only 6
    # digits (1 in a million per guess, but that's still brute-forceable
    # fast without a real limit) and only valid for ~30s, so there's no
    # legitimate reason for many rapid guesses in a row.
    allowed, retry_after = rate_limit.check(f"mfa-login:{pending['user_id']}", limit=5, window_s=60)
    if not allowed:
        return templates.TemplateResponse(request, "login_mfa.html", {
            "error": f"Too many attempts - try again in {retry_after}s.", "email": pending["email"],
        }, status_code=429)
    code = code.strip()
    # Format-detected, not a separate field/toggle - a backup code always
    # has a dash (XXXX-XXXX), a TOTP code never does.
    if "-" in code:
        ok = db.consume_backup_code(pending["user_id"], _hash_backup_code(code))
        error = None if ok else "That backup code is wrong or already used."
    else:
        ok, error = supabase_auth.mfa_challenge_and_verify(
            pending["access_token"], pending["refresh_token"], pending["factor_id"], code)
    if not ok:
        db.log_security_event("mfa_login_failed", user_id=pending["user_id"], email=pending["email"],
                               ip_address=rate_limit.client_ip(request))
        return templates.TemplateResponse(request, "login_mfa.html", {
            "error": error or "Wrong code - try again.", "email": pending["email"],
        }, status_code=400)
    db.delete_pending_auth_state(login_token)
    request.session.pop("_mfa_login_token", None)
    fake_session = types.SimpleNamespace(
        user=types.SimpleNamespace(id=pending["user_id"], email=pending["email"]))
    return _complete_auth_session(request, fake_session, pending["next"], login_kind="password_mfa")


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           marketing_consent: str = Form(""), next: str = Form("")):
    email = email.strip().lower()
    # IP-keyed, not email-keyed - the thing worth bounding here is one
    # visitor spamming Supabase's sign_up API with lots of *different*
    # emails (real accounts or throwaway ones), not one email retrying its
    # own signup a few times.
    allowed, retry_after = rate_limit.check(f"signup:{rate_limit.client_ip(request)}", limit=6, window_s=600)
    if not allowed:
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Too many signup attempts - try again in {retry_after // 60 + 1} minute(s).",
            "notice": None, "mode": "signup", "email": email,
            "marketing_consent": bool(marketing_consent), "next": _safe_next(next),
        }, status_code=429)
    policy_errors = supabase_auth.password_policy_errors(password, email=email)
    if policy_errors:
        # Rejected before ever calling Supabase - no point spending an API
        # call on a password that fails our own rules regardless of what
        # Supabase's own (looser) minimum would have allowed. email/
        # marketing_consent are threaded back through so a rejected password
        # doesn't also cost them everything else they'd already typed -
        # found via real client testing, not a hypothetical.
        return templates.TemplateResponse(request, "login.html", {
            "error": "Password needs " + ", ".join(policy_errors) + ".",
            "notice": None, "mode": "signup", "email": email,
            "marketing_consent": bool(marketing_consent), "next": _safe_next(next),
        })
    session, error = supabase_auth.sign_up(email, password)
    if error:
        return templates.TemplateResponse(request, "login.html", {
            "error": error, "notice": None, "mode": "signup", "email": email,
            "marketing_consent": bool(marketing_consent), "next": _safe_next(next),
        })
    if session is None:
        # Supabase's "Confirm email" setting is on - account exists but
        # can't log in until the confirmation link is clicked. Normal.
        return templates.TemplateResponse(request, "login.html", {
            "error": None,
            "notice": f"Account created for {email}. Check your email to confirm it, then log in.",
            "mode": "signin",  # once confirmed, they'll sign in - land them there
        })
    return _complete_auth_session(request, session, next, marketing_consent=bool(marketing_consent))



# ---------------------------------------------------------- magic-link sign-in and the try-before-signup flow ----
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GUEST_TRIAL_MAX_MINUTES = float(billing.FREE_MINUTES_PER_MONTH)         # the free minutes: a guest can send up to this much
GUEST_TRIAL_MAX_BYTES = 100 * 1024 * 1024
GUEST_TRIALS_PER_DAY = int(os.environ.get("KAULI_GUEST_TRIALS_PER_DAY", "60"))


def _magic_redirect(request: Request, next_url: str) -> str:
    url = _public_base_url(request) + "/auth/google/callback?via=magic"          # the callback already on the sign-in redirect list
    return url + (f"&next={quote(_safe_next(next_url), safe='')}" if next_url else "")


@app.post("/login/magic")
def login_magic(request: Request, email: str = Form(...), next: str = Form("")):
    """One email field: we send a link that signs them in (or makes the account). The answer is the same whether or not the address has an account."""
    email = email.strip().lower()
    ip = rate_limit.client_ip(request)
    ok_user, _ = rate_limit.check(f"magic:{email}", limit=3, window_s=600)
    ok_ip, _ = rate_limit.check(f"magic-ip:{ip}", limit=10, window_s=600)
    if not (ok_user and ok_ip):
        return templates.TemplateResponse(request, "login.html", {"error": "Too many requests - try again in a few minutes.", "notice": None, "mode": "signin",
                                                                  "next": _safe_next(next)}, status_code=429)
    if _EMAIL_RE.match(email):
        supabase_auth.send_magic_link(email, _magic_redirect(request, next))
        db.log_security_event("magic_link_sent", email=email, ip_address=ip, user_agent=request.headers.get("user-agent"))
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "notice": "If that email address is valid, a sign-in link is on its way. It works once and expires soon.", "mode": "signin", "next": _safe_next(next)})


@app.post("/try/start")
def guest_trial_start(request: Request, audio: UploadFile = File(...), source_lang: str = Form("en")):
    """Try Kauli before signing up: up to the free minutes (30) of a file are transcribed with no account. The result is only ever shown inside an
    account: the guest page asks for an email, and the transcript appears once they open the link (see /client/save-guest). Limited per address
    and per day, and the file is the only thing asked for."""
    user = current_user(request)
    if user and not (user["is_guest"] if "is_guest" in user.keys() else 0):
        return JSONResponse({"redirect": "/client#new-project"})
    ip = rate_limit.client_ip(request)
    allowed, _ = rate_limit.check(f"guest-trial:{ip}", limit=3, window_s=86400)
    if not allowed or db.guest_trials_today() >= GUEST_TRIALS_PER_DAY:
        return JSONResponse({"error": "We have reached today's limit for trying without an account. Create a free account to continue.", "signup": "/login?mode=signup"}, status_code=429)
    if source_lang not in LANGUAGE_NAMES:
        source_lang = "en"
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="guest-"))
    try:
        name = Path(audio.filename or "recording").name
        src = tmp / name
        size = 0
        with open(src, "wb") as out:
            while chunk := audio.file.read(1 << 20):
                size += len(chunk)
                if size > GUEST_TRIAL_MAX_BYTES:
                    return JSONResponse({"error": "That file is over 100 MB. Create a free account to upload larger files."}, status_code=413)
                out.write(chunk)
        try:
            minutes = probe_duration_minutes(str(src))
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "We could not read that recording. Try an MP3, WAV, M4A or MP4 file."}, status_code=400)
        final = src
        if minutes > GUEST_TRIAL_MAX_MINUTES:
            final = tmp / "free-minutes.mp3"                                        # only the free minutes are transcribed
            subprocess.run(["ffmpeg", "-y", "-i", str(src), "-vn", "-t", str(int(GUEST_TRIAL_MAX_MINUTES * 60)), "-ac", "1", "-ar", "16000", "-b:a", "64k", str(final)],
                           capture_output=True, timeout=120, check=True)
        guest_id = db.create_guest_user()
        guest = dict(db.get_user(guest_id))
        guest["client_scope_id"] = guest_id
        request.state.api_user = guest
        from starlette.datastructures import UploadFile as _Upload
        with open(final, "rb") as fh:
            upload = _Upload(file=fh, filename=final.name if final is not src else name)
            try:
                resp = create_order(
                    request, audio=upload, upload_key="", youtube_url="", source_lang=source_lang, target_lang=source_lang, service_level="transcribe",
                    addon_video_deliverables="", addon_human_voice_over="", addon_premium_voice="", premium_voice_id="", addon_audio_cleanup="",
                    instr_speaker_ids="", instr_verbatim_level="clean_read", instr_transcribe_lyrics="", instr_use_italics="", instr_existing_subs="ignore",
                    instr_no_audio="tag", instr_wrong_language="tag", instr_instrumental_only="tag", instr_notes="", style_guide=None, idempotency_key="",
                    folder_name="", voice_clone_consent="", rush="", review_tier="ai")
            except ApiOrderError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        loc = resp.headers.get("location", "") if isinstance(resp, RedirectResponse) else ""
        if not re.match(r"^/client/orders/[0-9a-f]+$", loc):
            return JSONResponse({"error": "We could not start that transcript. Create a free account to try again.", "signup": "/login?mode=signup"}, status_code=400)
        request.session["user_id"] = guest_id
        ga_events.send_event(ga_events.client_id_from_request(request), "guest_trial_started", {"minutes": round(min(minutes, GUEST_TRIAL_MAX_MINUTES), 1)})
        return JSONResponse({"redirect": loc, "trimmed": minutes > GUEST_TRIAL_MAX_MINUTES})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/client/save-guest")
def guest_save(request: Request, email: str = Form(...), next: str = Form("/client")):
    """A guest gives an email to keep and download their transcript: we send a sign-in link, and their orders move to the account it opens."""
    user = current_user(request)
    if not user or not (user["is_guest"] if "is_guest" in user.keys() else 0):
        return RedirectResponse("/client", status_code=303)
    email = email.strip().lower()
    ip = rate_limit.client_ip(request)
    ok_user, _ = rate_limit.check(f"magic:{email}", limit=3, window_s=600)
    ok_ip, _ = rate_limit.check(f"magic-ip:{ip}", limit=10, window_s=600)
    back = _safe_next(next)
    if not _EMAIL_RE.match(email):
        return RedirectResponse(f"{back}?error={quote('Please enter a valid email address.')}", status_code=303)
    if not (ok_user and ok_ip):
        return RedirectResponse(f"{back}?error={quote('Too many requests - try again in a few minutes.')}", status_code=303)
    db.add_guest_claim(email, user["id"])
    supabase_auth.send_magic_link(email, _magic_redirect(request, back))
    return RedirectResponse(f"{back}?notice={quote('We sent a link to ' + email + '. Open it and your transcript is saved to your account.')}", status_code=303)


@app.post("/client/exit-reason")
async def exit_reason(request: Request):
    """The review step's "Not ready? Tell us why": one tap, optional note. It explains the drop-off between review and submit."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Please sign in."}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Bad request."}, status_code=400)
    reason = str(body.get("reason") or "")[:40]
    if reason not in ("price", "accuracy", "language", "speed", "just_looking", "other"):
        return JSONResponse({"error": "Choose one of the reasons."}, status_code=400)
    db.add_exit_reason(user["id"], str(body.get("step") or "review")[:20], reason, str(body.get("note") or "").strip()[:500])
    return JSONResponse({"ok": True})


@app.get("/login/google")
def login_google(request: Request, next: str = ""):
    """Redirects to Supabase's own OAuth entry point, not Google directly -
    Supabase (GoTrue) owns the actual OAuth exchange with Google (the real
    client ID/secret live in the Supabase dashboard, never in this app's
    own .env), then bounces back to our callback below with a real
    session. Requires Google actually enabled as a provider in the
    Supabase project's Authentication > Providers settings, and this app's
    /auth/google/callback added to its Redirect URLs allowlist - real
    external setup this route can't do by itself, and fails honestly with
    a real error page (Supabase's own) rather than a silent loop if it
    isn't done yet."""
    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        return RedirectResponse(f"/login?error={quote('Sign-in service is not configured.')}", status_code=303)
    callback_url = _public_base_url(request) + "/auth/google/callback"
    if next:
        callback_url += f"?next={quote(_safe_next(next), safe='')}"
    authorize_url = f"{supabase_url}/auth/v1/authorize?provider=google&redirect_to={quote(callback_url, safe='')}"
    return RedirectResponse(authorize_url, status_code=303)


@app.get("/auth/google/callback", response_class=HTMLResponse)
def google_callback_page(request: Request):
    """Supabase hands the real session back as a URL fragment
    (#access_token=...&refresh_token=...), which never reaches this server
    on its own - the exact same real mechanism reset_password.html already
    documents and handles for the password-recovery link. This page's own
    JS reads the fragment (and the ?next= this app added when it built the
    redirect above) and forwards both to the POST route below as plain
    form fields."""
    return templates.TemplateResponse(request, "oauth_callback.html", {"error": None})


@app.post("/auth/google/callback")
def google_callback_submit(request: Request, access_token: str = Form(""), refresh_token: str = Form(""),
                            next: str = Form(""), via: str = Form("")):
    if not access_token or not refresh_token:
        return templates.TemplateResponse(request, "oauth_callback.html", {
            "error": "That didn't come back from Google correctly - please try again.",
        }, status_code=400)
    session, error = supabase_auth.complete_oauth_session(access_token, refresh_token)
    if error or not session:
        return templates.TemplateResponse(request, "oauth_callback.html", {
            "error": error or "That sign-in didn't work - please try again.",
        }, status_code=400)
    return _complete_auth_session(request, session, next, login_kind="magic_link" if via == "magic" else "google_oauth")


@app.post("/auth/google/one-tap")
def google_one_tap_submit(request: Request, credential: str = Form(""), nonce: str = Form("")):
    """The Google One Tap prompt's callback (_one_tap.html) submits here as
    a plain form POST, not fetch/JSON - same reasoning as the regular
    Google OAuth callback just above: a real 303 redirect through
    _complete_auth_session is simpler and more consistent than a JS-side
    JSON response that then has to do its own navigation.

    Public and unauthenticated by design (a visitor isn't signed in yet),
    so it's IP-rate-limited like every other public auth endpoint - a
    forged/replayed credential still fails Supabase's own verification
    below, but there's no reason to let that verification be hammered."""
    allowed, retry_after = rate_limit.check(f"onetap:{rate_limit.client_ip(request)}", limit=10, window_s=60)
    if not allowed:
        return RedirectResponse(f"/login?error={quote('Too many attempts - try again shortly.')}", status_code=303)
    if not credential:
        return RedirectResponse(
            f"/login?error={quote('That sign-in did not come back from Google correctly - please try again.')}",
            status_code=303)
    session, error = supabase_auth.sign_in_with_google_id_token(credential, nonce or None)
    if error or not session:
        return RedirectResponse(
            f"/login?error={quote(error or 'That sign-in did not work - please try again.')}", status_code=303)
    return _complete_auth_session(request, session, "", login_kind="google_one_tap")


# ---------------------------------------------- client YouTube connect ----
@app.get("/client/youtube/connect")
def youtube_connect_start(request: Request, next: str = ""):
    """A real, separate OAuth grant to the client's OWN YouTube channel -
    see webapp/youtube_captions.py's own docstring for why this can't
    reuse the sign-in Google OAuth. Gated behind GOOGLE_YOUTUBE_CLIENT_ID/
    SECRET being set, same "inert until configured" pattern as every other
    optional integration in this app."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if not youtube_captions.youtube_oauth_configured():
        return RedirectResponse(_safe_next(next))
    state = uuid.uuid4().hex
    request.session["youtube_oauth_state"] = state
    request.session["youtube_oauth_next"] = _safe_next(next)
    redirect_uri = _public_base_url(request) + "/client/youtube/callback"
    return RedirectResponse(youtube_captions.build_authorize_url(redirect_uri, state), status_code=303)


@app.get("/client/youtube/callback", response_class=HTMLResponse)
def youtube_connect_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    next_url = request.session.pop("youtube_oauth_next", "/client")
    expected_state = request.session.pop("youtube_oauth_state", None)
    if error:
        return RedirectResponse(f"{next_url}?youtube_error={quote('YouTube connection was cancelled.')}", status_code=303)
    if not code or not state or state != expected_state:
        return RedirectResponse(f"{next_url}?youtube_error={quote('That connection request expired - please try again.')}", status_code=303)
    try:
        redirect_uri = _public_base_url(request) + "/client/youtube/callback"
        tokens = youtube_captions.exchange_code_for_tokens(code, redirect_uri)
        expires_at = time.time() + tokens.get("expires_in", 3600)
        channel_id, channel_title = youtube_captions.get_channel_info(tokens["access_token"])
        db.save_youtube_connection(user["id"], channel_id, channel_title,
                                    tokens["access_token"], tokens["refresh_token"], expires_at)
    except Exception as exc:  # noqa: BLE001 - a real, specific message beats a stack trace for the client
        return RedirectResponse(f"{next_url}?youtube_error={quote(str(exc)[:200])}", status_code=303)
    return RedirectResponse(f"{next_url}?youtube_connected=1", status_code=303)


@app.post("/client/youtube/disconnect")
def youtube_disconnect(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    db.delete_youtube_connection(user["id"])
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@app.post("/client/orders/{order_id}/push-captions-youtube")
def push_captions_to_youtube(request: Request, order_id: str):
    """Real captions.insert against the client's own connected channel -
    see webapp/youtube_captions.py. Only ever pushes the subtitle file
    this client already has real, human-verified access to download
    (order_detail.html's own /download/srt) - never a draft/unreviewed
    transcript."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if _order_locked(order):
        return RedirectResponse(f"/client/orders/{order_id}?youtube_error={quote('Choose a plan to unlock downloads and publishing.')}", status_code=303)
    if not order["source_youtube_id"]:
        return RedirectResponse(f"/client/orders/{order_id}?youtube_error={quote('This order was not sourced from a YouTube video.')}", status_code=303)
    srt_path = Path(order["outdir"]) / f"subs_{order['target_lang']}.srt"
    if not srt_path.exists():
        return RedirectResponse(f"/client/orders/{order_id}?youtube_error={quote('Subtitles are not ready yet.')}", status_code=303)
    connection = db.get_youtube_connection(user["id"])
    if not connection:
        return RedirectResponse(f"/client/orders/{order_id}?youtube_error={quote('Connect your YouTube channel first.')}", status_code=303)
    try:
        access_token = youtube_captions.ensure_fresh_token(connection)
        youtube_captions.upload_caption(
            access_token, order["source_youtube_id"], order["target_lang"], str(srt_path),
            # Label honestly: only a Human-reviewed order has been checked by a person.
            name=(f"{order['target_lang'].upper()} (Kauli, AI translation)" if order["review_tier"] == "ai"
                  else f"{order['target_lang'].upper()} (Kauli, human-verified)"),
        )
    except Exception as exc:  # noqa: BLE001 - a real, specific message beats a silent failure
        return RedirectResponse(f"/client/orders/{order_id}?youtube_error={quote(str(exc)[:250])}", status_code=303)
    return RedirectResponse(f"/client/orders/{order_id}?youtube_pushed=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ------------------------------------------------------- notifications ----
_READY_TITLE = re.compile(r"^(.+) is ready$")


def _notification_titles(titles: list[str], lang: str) -> dict[str, str]:
    """{stored English title: title in the reader's language}. Titles are saved in English when the event happens, so they are translated as they
    are shown. "<file name> is ready" is translated as a sentence with the file name kept exactly; the rest are translated whole."""
    if lang == "en" or not page_translate.configured():
        return {}
    templates_needed: list[str] = []
    plain: list[str] = []
    for t in dict.fromkeys(titles):
        (templates_needed if _READY_TITLE.match(t or "") else plain).append(t)
    out: dict[str, str] = {}
    try:
        tr, _ = page_translate.translate_many(["{name} is ready"] + plain, lang, 3.0)
        ready = tr.get("{name} is ready")
        for t in templates_needed:
            if ready and "{name}" in ready:
                out[t] = ready.replace("{name}", _READY_TITLE.match(t).group(1))
        out.update({t: tr[t] for t in plain if tr.get(t)})
    except Exception:  # noqa: BLE001 - an untranslated title is better than none
        pass
    return out


@app.get("/notifications/recent")
def notifications_recent(request: Request):
    """JSON, polled by the bell in base.html - a real, persistent notification
    the user hasn't dismissed yet, not a client-side guess. See db.notify_all_staff
    for what actually creates these."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = db.list_recent_notifications(user["id"], limit=10)
    lang = _ui_lang(request, user)
    titles = _notification_titles([r["title"] for r in rows], lang)
    return JSONResponse({
        "unread_count": db.count_unread_notifications(user["id"]),
        "items": [
            {"id": r["id"], "title": titles.get(r["title"], r["title"]), "link": r["link"], "kind": r["kind"],
             "created_at": r["created_at"], "unread": r["read_at"] is None}
            for r in rows
        ],
    })


@app.post("/notifications/{notification_id}/read")
def notifications_mark_read(request: Request, notification_id: str):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.mark_notification_read(notification_id, user["id"])
    return JSONResponse({"ok": True})


@app.post("/notifications/mark-all-read")
def notifications_mark_all_read(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.mark_all_notifications_read(user["id"])
    return JSONResponse({"ok": True})


# ------------------------------------------------------------- settings ----
_SETTINGS_TABS = ("general", "defaults", "notifications", "integrations", "team", "security")


@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, tab: str = "general", team_notice: str | None = None,
                   team_error: str | None = None, defaults_saved: bool = False,
                   password_error: str | None = None, password_saved: bool = False,
                   saved: bool = False, error: str | None = None, webhook_error: str | None = None,
                   slack_error: str | None = None, slack_notice: str | None = None,
                   notion_error: str | None = None, notion_notice: str | None = None,
                   teams_error: str | None = None, teams_notice: str | None = None,
                   mfa_step: str | None = None, mfa_error: str | None = None, mfa_notice: str | None = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if tab not in _SETTINGS_TABS:
        tab = "general"
    # is_team_owner: this client is NOT themselves an accepted member of
    # someone else's team - only the real account owner can invite/remove
    # people, never a teammate (who might not even be trusted with who
    # else is on the account, let alone able to remove them).
    is_team_owner = user["role"] == "client" and user["client_scope_id"] == user["id"]
    team_members = db.list_team_members(user["id"]) if is_team_owner else []
    team_owner_name = None
    if user["role"] == "client" and not is_team_owner:
        owner = db.get_user(user["client_scope_id"])
        team_owner_name = owner["display_name"] or owner["email"] if owner else None
    # API keys/webhooks are account-level, same ownership scope as team
    # management above - only the real account owner manages them.
    api_key_row = db.get_client_api_key(user["id"]) if is_team_owner else None
    webhook_row = db.get_client_webhook(user["id"]) if is_team_owner else None
    webhook_deliveries = db.list_webhook_deliveries(user["id"], limit=8) if is_team_owner else []
    # Shown once, right after generation - popped from the session so a
    # page refresh (or anyone else who later views this page) never sees
    # the real key again, only its stored hash can ever confirm a match.
    new_api_key = request.session.pop("_new_api_key", None)
    # Same one-shot-reveal for the two MFA session states: the pending
    # enrollment's QR/secret (only relevant mid-setup, mfa_step=verify) and
    # a freshly generated batch of backup codes (mfa_step=backup_codes) -
    # neither is ever popped except by actually rendering this page, so a
    # page reload before finishing setup still shows the SAME QR code
    # rather than silently generating a new, different one server-side.
    mfa_pending = (
        db.get_pending_auth_state(request.session.get("_mfa_enroll_token"), "mfa_enroll")
        if mfa_step == "verify" else None
    )
    new_backup_codes = request.session.pop("_mfa_new_backup_codes", None) if mfa_step == "backup_codes" else None
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": saved, "error": error, "tab": tab,
        "theme": "dark" if user["role"] == "staff" else "light",
        "is_team_owner": is_team_owner, "team_members": team_members, "team_owner_name": team_owner_name,
        "team_notice": team_notice, "team_error": team_error,
        "api_key_row": api_key_row, "new_api_key": new_api_key,
        "webhook_row": webhook_row, "webhook_deliveries": webhook_deliveries,
        "defaults_saved": defaults_saved, "password_error": password_error, "password_saved": password_saved,
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(), "service_levels": billing.SERVICE_LEVELS,
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "deletion_requested": db.has_pending_deletion_request(user["id"]) if user["role"] == "client" else False,
        "webhook_error": webhook_error,
        "slack_row": db.get_client_slack(user["id"]) if is_team_owner else None,
        "slack_error": slack_error, "slack_notice": slack_notice,
        "notion_row": db.get_client_notion(user["id"]) if is_team_owner else None,
        "notion_error": notion_error, "notion_notice": notion_notice,
        "teams_row": db.get_client_teams(user["id"]) if is_team_owner else None,
        "teams_error": teams_error, "teams_notice": teams_notice,
        "mfa_step": mfa_step, "mfa_error": mfa_error, "mfa_notice": mfa_notice, "mfa_pending": mfa_pending,
        "new_backup_codes": new_backup_codes,
        "backup_codes_remaining": db.count_unused_backup_codes(user["id"]) if user["mfa_enabled_at"] else 0,
        # Per-user, not account-level like the API key/webhook/team blocks
        # above - any client (not just the team owner) may have their own
        # YouTube channel to push captions to on their own orders.
        "youtube_connection": db.get_youtube_connection(user["id"]) if user["role"] == "client" else None,
    })


@app.post("/settings/api-key/generate")
def settings_api_key_generate(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    raw_key = "kauli_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db.set_client_api_key(user["id"], key_hash, raw_key[:18])
    request.session["_new_api_key"] = raw_key
    db.log_security_event("api_key_generated", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@app.post("/settings/api-key/revoke")
def settings_api_key_revoke(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.revoke_client_api_key(user["id"])
    db.log_security_event("api_key_revoked", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    return RedirectResponse("/settings?tab=integrations", status_code=303)


def _notion_fernet():
    import base64
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(f"{_session_secret}:notion-token".encode()).digest())
    return Fernet(key)


def _notion_encrypt(token: str) -> str:
    return _notion_fernet().encrypt(token.encode()).decode()


def _notion_decrypt(token_enc: str) -> str | None:
    """None if the stored token can't be read (e.g. the session secret changed) -
    the customer is then asked to reconnect, never shown an error page."""
    try:
        return _notion_fernet().decrypt(token_enc.encode()).decode()
    except Exception:
        return None


def _notion_http(*args, **kwargs):
    return httpx.request(*args, **kwargs)        # looked up per call so tests can replace httpx.request


_NOTION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")
_NOTION_RUN_IN_BACKGROUND = True


@app.post("/settings/notion")
def settings_notion_save(request: Request, token: str = Form(""), url: str = Form(""), auto_send: str = Form("")):
    from urllib.parse import quote_plus
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")

    def back(kind: str, message: str):
        return RedirectResponse(f"/settings?tab=integrations&notion_{kind}={quote_plus(message)}#notion", status_code=303)

    existing = db.get_client_notion(user["id"])
    token, url = token.strip(), url.strip()
    if token:
        if not _NOTION_TOKEN_RE.match(token):
            return back("error", "That doesn't look like a Notion integration secret. Copy it from notion.so/my-integrations.")
        plain = token
    elif existing:
        plain = _notion_decrypt(existing["token_enc"])
        if not plain:
            return back("error", "The saved Notion token can't be read any more. Paste the integration secret again.")
    else:
        return back("error", "Paste your Notion integration secret.")
    if url:
        parent_id = notion_export.parse_notion_id(url)
        if not parent_id:
            return back("error", "That doesn't look like a Notion page or database link.")
    elif existing:
        parent_id = existing["parent_id"]
    else:
        return back("error", "Paste the link to the Notion page or database where transcripts should go.")
    try:
        info = notion_export.validate_connection(plain, parent_id, http=_notion_http)
    except notion_export.NotionError as exc:
        return back("error", str(exc))
    db.set_client_notion(user["id"], _notion_encrypt(plain), plain[-4:], parent_id, info["kind"],
                         info["title_prop"], info["bot_name"], bool(auto_send))
    return back("notice", f"Notion connected as {info['bot_name']}. Finished transcripts will appear as pages "
                          f"in your {info['kind']}.")


@app.post("/settings/notion/delete")
def settings_notion_delete(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.delete_client_notion(user["id"])
    return RedirectResponse("/settings?tab=integrations&notion_notice=Notion+disconnected#notion", status_code=303)


_NOTION_SERVICE_LABELS = {"transcribe": "Transcription", "translate": "Transcription + translation", "dub": "Full dub"}


def _notion_push_order(order, base_url: str) -> tuple[bool, str]:
    """Creates the transcript page in the customer's Notion. Returns (ok, page url or the reason).
    Needs the same things a download needs: a finished order and a chosen plan."""
    row = db.get_client_notion(order["client_id"])
    if not row:
        return False, "Notion isn't connected."
    if order["status"] not in db.TERMINAL_OK_STATUSES:
        return False, "This order isn't ready yet."
    if _order_locked(order):
        return False, "Unlock this order to send it to Notion."
    token = _notion_decrypt(row["token_enc"])
    if not token:
        return False, "Reconnect Notion in Settings: the saved token can't be read."
    if not db.claim_notion_export(order["id"], order["client_id"]):
        sent = db.get_notion_export(order["id"])
        return True, (sent["page_url"] if sent and sent["page_url"] else "")
    try:
        job = _load_job(order)
        lines = transcript_export._lines(job, True) if job else []
        if not lines:
            raise notion_export.NotionError("The transcript isn't ready yet.")
        src, tgt = order["source_lang"], order["target_lang"]
        langs = LANGUAGE_NAMES.get(src, src) + (f" to {LANGUAGE_NAMES.get(tgt, tgt)}" if tgt and tgt != src else "")
        meta = " | ".join(x for x in (
            order["original_filename"] or order["id"],
            _NOTION_SERVICE_LABELS.get(order["service_level"] or "transcribe", "Order"), langs,
            f"{order['duration_minutes']:.1f} min" if order["duration_minutes"] else "") if x)
        blocks = notion_export.build_blocks(meta, f"{base_url.rstrip('/')}/client/orders/{order['id']}", lines,
                                            ai_note=order["review_tier"] == "ai")
        title = f"Transcript - {order['original_filename'] or order['id']}"
        page = notion_export.create_page(token, row["parent_id"], row["parent_kind"], row["title_prop"], title,
                                         blocks, http=_notion_http)
        db.finish_notion_export(order["id"], page["id"], page["url"])
        db.log_webhook_delivery(order["client_id"], order["id"], "notion.order.ready", True, 200)
        return True, page["url"] or ""
    except notion_export.NotionError as exc:
        db.release_notion_export(order["id"])
        db.log_webhook_delivery(order["client_id"], order["id"], "notion.order.ready", False, None, str(exc)[:300])
        return False, str(exc)
    except Exception as exc:
        db.release_notion_export(order["id"])
        db.log_webhook_delivery(order["client_id"], order["id"], "notion.order.ready", False, None, str(exc)[:300])
        return False, "Something went wrong sending to Notion. Try again in a moment."


def _notify_notion_order_ready(request, order) -> None:
    row = db.get_client_notion(order["client_id"])
    if not row or not row["auto_send"]:
        return
    base = str(request.base_url)
    if _NOTION_RUN_IN_BACKGROUND:          # Notion calls take seconds: never hold up an approval
        threading.Thread(target=_notion_push_order, args=(order, base), daemon=True).start()
    else:
        _notion_push_order(order, base)


@app.post("/client/orders/{order_id}/send-to-notion")
def client_send_to_notion(request: Request, order_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    from urllib.parse import quote_plus
    ok, detail = _notion_push_order(order, str(request.base_url))
    if ok:
        msg = "Sent to Notion." if detail else "This order is already being sent to Notion."
        return RedirectResponse(f"/client/orders/{order_id}?notice={quote_plus(msg)}", status_code=303)
    return RedirectResponse(f"/client/orders/{order_id}?error={quote_plus(detail)}", status_code=303)


_TEAMS_HOST_SUFFIXES = (".logic.azure.com", ".webhook.office.com", ".powerplatform.com")


def _is_teams_webhook_url(url: str) -> bool:
    """Only a Microsoft workflow/connector webhook: https, no credentials in the
    URL, the default port, and a host that is a real subdomain of one of
    Microsoft's webhook domains. A free-form URL would let a customer point
    Kauli at our own network; this list cannot."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            return False
        if parsed.port not in (None, 443) or len(parsed.path) < 2:
            return False
        if host == "outlook.office.com":                    # the older connector address
            return parsed.path.startswith("/webhook/")
        return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in _TEAMS_HOST_SUFFIXES)
    except ValueError:
        return False


def _post_to_teams(client_id: str, order_id: str, event: str, url: str, payload: dict) -> tuple[bool, str | None]:
    """One real POST to the customer's Teams webhook, logged with their other
    deliveries. Workflows answer 202 Accepted, the older connectors 200."""
    try:
        resp = httpx.post(url, json=payload, timeout=10.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        db.log_webhook_delivery(client_id, order_id, event, False, None, str(exc)[:300])
        return False, str(exc)[:200]
    ok = resp.status_code in (200, 202)
    detail = None if ok else (resp.text or f"HTTP {resp.status_code}")[:200]
    db.log_webhook_delivery(client_id, order_id, event, ok, resp.status_code, detail)
    return ok, detail


def _teams_clean(text) -> str:
    """Teams renders a little markdown in card text: no link syntax, mentions or code from file names."""
    return re.sub(r"[\[\]<>`]", "", str(text))


def _teams_card(title: str, facts: list[tuple[str, str]], note: str | None, actions: list[tuple[str, str]]) -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": _teams_clean(v)} for k, v in facts]})
    if note:
        body.append({"type": "TextBlock", "text": note, "isSubtle": True, "wrap": True})
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": body}
    if actions:
        card["actions"] = [{"type": "Action.OpenUrl", "title": label, "url": link} for label, link in actions]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def _teams_order_ready_message(request, order, include_links: bool) -> dict:
    base = str(request.base_url).rstrip("/")
    src, tgt = order["source_lang"], order["target_lang"]
    langs = LANGUAGE_NAMES.get(src, src) + (f" to {LANGUAGE_NAMES.get(tgt, tgt)}" if tgt and tgt != src else "")
    actions = [("Open order", f"{base}/client/orders/{order['id']}")]
    if include_links:
        links = _order_to_api_dict(request, order)["deliverables"]
        for kind, label in (("txt", "Transcript (TXT)"), ("docx", "Transcript (Word)"), ("srt", "Subtitles (SRT)"),
                            ("vtt", "Subtitles (VTT)")):
            if kind in links and len(actions) < 4:
                actions.append((label, links[kind]))
    return _teams_card(
        "Your Kauli order is ready",
        [("File", order["original_filename"] or order["id"]),
         ("Service", _SLACK_SERVICE_LABELS.get(order["service_level"] or "transcribe", "Order")),
         ("Languages", langs), ("Order", order["id"])],
        None if not _order_locked(order) else "Unlock this order in Kauli to download it.", actions)


def _notify_teams_order_ready(request, order) -> None:
    row = db.get_client_teams(order["client_id"])
    if not row or not _is_teams_webhook_url(row["webhook_url"]):
        return
    _post_to_teams(order["client_id"], order["id"], "teams.order.ready", row["webhook_url"],
                   _teams_order_ready_message(request, order, bool(row["include_links"])))


@app.post("/settings/teams")
def settings_teams_save(request: Request, url: str = Form(""), include_links: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    url = url.strip()
    if not url and db.get_client_teams(user["id"]):          # only the checkbox changed
        db.set_client_teams_include_links(user["id"], bool(include_links))
        return RedirectResponse("/settings?tab=integrations&teams_notice=Teams+settings+saved#teams", status_code=303)
    if not _is_teams_webhook_url(url):
        return RedirectResponse(
            "/settings?tab=integrations&teams_error=That+isn%27t+a+Microsoft+Teams+workflow+webhook+URL.+Create+one+with+"
            "the+%22Post+to+a+channel+when+a+webhook+request+is+received%22+workflow#teams", status_code=303)
    db.set_client_teams(user["id"], url, bool(include_links))
    return RedirectResponse("/settings?tab=integrations&teams_notice=Teams+connected.+Send+a+test+message+to+check+it#teams",
                            status_code=303)


@app.post("/settings/teams/test")
def settings_teams_test(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    row = db.get_client_teams(user["id"])
    if not row:
        return RedirectResponse("/settings?tab=integrations&teams_error=Connect+Teams+first#teams", status_code=303)
    ok, detail = _post_to_teams(user["id"], "test", "teams.test", row["webhook_url"], _teams_card(
        "Kauli is connected to this channel", [], "You will get a message here when an order is ready.", []))
    if ok:
        return RedirectResponse("/settings?tab=integrations&teams_notice=Test+message+sent#teams", status_code=303)
    from urllib.parse import quote_plus
    return RedirectResponse(f"/settings?tab=integrations&teams_error={quote_plus('Teams refused the message: ' + str(detail))}#teams",
                            status_code=303)


@app.post("/settings/teams/delete")
def settings_teams_delete(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.delete_client_teams(user["id"])
    return RedirectResponse("/settings?tab=integrations&teams_notice=Teams+disconnected#teams", status_code=303)


@app.post("/settings/slack")
def settings_slack_save(request: Request, url: str = Form(""), include_links: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    url = url.strip()
    existing = db.get_client_slack(user["id"])
    if not url and existing:                       # only the checkbox changed
        db.set_client_slack_include_links(user["id"], bool(include_links))
        return RedirectResponse("/settings?tab=integrations&slack_notice=Slack+settings+saved#slack", status_code=303)
    if not _is_slack_webhook_url(url):
        return RedirectResponse(
            "/settings?tab=integrations&slack_error=That+isn%27t+a+Slack+Incoming+Webhook+URL.+It+should+look+like+"
            "https%3A%2F%2Fhooks.slack.com%2Fservices%2FT...%2FB...%2F...#slack", status_code=303)
    db.set_client_slack(user["id"], url, bool(include_links))
    return RedirectResponse("/settings?tab=integrations&slack_notice=Slack+connected.+Send+a+test+message+to+check+it#slack",
                            status_code=303)


@app.post("/settings/slack/test")
def settings_slack_test(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    row = db.get_client_slack(user["id"])
    if not row:
        return RedirectResponse("/settings?tab=integrations&slack_error=Connect+Slack+first#slack", status_code=303)
    ok, detail = _post_to_slack(user["id"], "test", "slack.test", row["webhook_url"], {
        "text": "Kauli is connected to this channel. You will get a message here when an order is ready."})
    if ok:
        return RedirectResponse("/settings?tab=integrations&slack_notice=Test+message+sent#slack", status_code=303)
    from urllib.parse import quote_plus
    return RedirectResponse(f"/settings?tab=integrations&slack_error={quote_plus('Slack refused the message: ' + str(detail))}#slack",
                            status_code=303)


@app.post("/settings/slack/delete")
def settings_slack_delete(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.delete_client_slack(user["id"])
    return RedirectResponse("/settings?tab=integrations&slack_notice=Slack+disconnected#slack", status_code=303)


@app.post("/settings/webhook")
def settings_webhook_save(request: Request, url: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    url = url.strip()
    if url and not _is_safe_webhook_url(url):
        return RedirectResponse(
            "/settings?tab=integrations&webhook_error=That+URL+isn%27t+allowed+-+use+a+real%2C+public+https%3A%2F%2F+address",
            status_code=303)
    db.set_client_webhook(user["id"], url)
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@app.post("/settings/webhook/regenerate-secret")
def settings_webhook_regenerate_secret(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    hook = db.get_client_webhook(user["id"])
    if hook:
        db.set_client_webhook(user["id"], hook["url"], secret=uuid.uuid4().hex)
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@app.post("/settings/webhook/delete")
def settings_webhook_delete(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.delete_client_webhook(user["id"])
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@app.post("/settings/order-defaults")
def settings_order_defaults(request: Request, source_lang: str = Form(""), target_lang: str = Form(""),
                             service_level: str = Form(""), rush: str = Form(""),
                             addon_video: str = Form("")):
    """Personal to whoever's logged in, not account-wide - see
    db.set_order_defaults's docstring. Only ever prefills the wizard (see
    client_dashboard below); an invalid/blank choice just means "no
    default", never an error - this is a convenience, not a required
    field anywhere."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    db.set_order_defaults(
        user["id"],
        source_lang=source_lang if source_lang in SOURCE_LANGUAGES else None,
        target_lang=target_lang if target_lang in ("en", "sw") else None,
        service_level=service_level if service_level in billing.SERVICE_LEVELS else None,
        rush=bool(rush), addon_video=bool(addon_video),
    )
    return RedirectResponse("/settings?tab=defaults&defaults_saved=1", status_code=303)


@app.post("/settings/change-password")
def settings_change_password(request: Request, current_password: str = Form(...),
                              new_password: str = Form(...), confirm_password: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    # Same brute-force guard as /login - this route re-authenticates with
    # the current password internally (see change_password's docstring),
    # so it's just as guessable a target as the login form itself.
    allowed, retry_after = rate_limit.check(f"login:{user['email']}", limit=8, window_s=60)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=security&password_error=Too+many+attempts+-+try+again+in+{retry_after}s",
            status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/settings?tab=security&password_error=New+passwords+don%27t+match",
                                 status_code=303)
    policy_errors = supabase_auth.password_policy_errors(new_password, email=user["email"])
    if policy_errors:
        return RedirectResponse(
            f"/settings?tab=security&password_error=Password+needs%3A+{quote(', '.join(policy_errors))}",
            status_code=303)
    ok, error = supabase_auth.change_password(user["email"], current_password, new_password)
    if not ok:
        return RedirectResponse(f"/settings?tab=security&password_error={quote(error or 'Something went wrong.')}",
                                 status_code=303)
    db.log_security_event("password_changed", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request), user_agent=request.headers.get("user-agent"))
    return RedirectResponse("/settings?tab=security&password_saved=1", status_code=303)


# --------------------------------------------------------------- MFA (TOTP) ----
_BACKUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L - easy to misread when written down


def _generate_backup_codes(n: int = 10) -> list[str]:
    """Real random codes, not a fixed pattern - secrets.choice, same real-
    randomness standard as the API key token above. XXXX-XXXX is long
    enough to not be guessable (32^8 combinations) and short enough to
    type by hand if someone's reading it off a printout."""
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def _hash_backup_code(code: str) -> str:
    # Uppercased/stripped before hashing so "abcd-1234" and "ABCD-1234" (a
    # real thing someone typing this by hand will do) both match.
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


@app.post("/settings/mfa/enroll")
def settings_mfa_enroll(request: Request, current_password: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    allowed, retry_after = rate_limit.check(f"login:{user['email']}", limit=8, window_s=60)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=security&mfa_error=Too+many+attempts+-+try+again+in+{retry_after}s", status_code=303)
    ok, error, data = supabase_auth.mfa_start_enroll(user["email"], current_password)
    if not ok:
        return RedirectResponse(f"/settings?tab=security&mfa_error={quote(error or 'Something went wrong.')}",
                                 status_code=303)
    # Held server-side (pending_auth_state - see its own table comment for
    # why NOT the session cookie: the QR code alone is a multi-KB SVG data
    # URI, plus a Supabase JWT access_token, comfortably over the browser's
    # ~4KB cookie limit). Only the small opaque token goes in the cookie.
    # Cleared the moment enrollment is confirmed, cancelled, or abandoned
    # for a new attempt.
    request.session["_mfa_enroll_token"] = db.create_pending_auth_state("mfa_enroll", data)
    return RedirectResponse("/settings?tab=security&mfa_step=verify", status_code=303)


@app.post("/settings/mfa/verify-enroll")
def settings_mfa_verify_enroll(request: Request, code: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    enroll_token = request.session.get("_mfa_enroll_token")
    pending = db.get_pending_auth_state(enroll_token, "mfa_enroll")
    if not pending:
        return RedirectResponse("/settings?tab=security&mfa_error=That+setup+attempt+expired+-+start+again.",
                                 status_code=303)
    allowed, retry_after = rate_limit.check(f"mfa-enroll:{user['id']}", limit=8, window_s=60)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=security&mfa_step=verify&mfa_error=Too+many+attempts+-+try+again+in+{retry_after}s",
            status_code=303)
    ok, error = supabase_auth.mfa_challenge_and_verify(
        pending["access_token"], pending["refresh_token"], pending["factor_id"], code)
    if not ok:
        return RedirectResponse(
            f"/settings?tab=security&mfa_step=verify&mfa_error={quote(error or 'Wrong code - try again.')}",
            status_code=303)
    db.delete_pending_auth_state(enroll_token)
    request.session.pop("_mfa_enroll_token", None)
    db.set_user_mfa_enabled(user["id"], True)
    backup_codes = _generate_backup_codes()
    db.replace_mfa_backup_codes(user["id"], [_hash_backup_code(c) for c in backup_codes])
    db.log_security_event("mfa_enabled", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    # Shown exactly once - same one-shot-reveal-via-session pattern as
    # new_api_key above (see settings_form's own comment on why).
    request.session["_mfa_new_backup_codes"] = backup_codes
    return RedirectResponse("/settings?tab=security&mfa_step=backup_codes", status_code=303)


@app.post("/settings/mfa/cancel-enroll")
def settings_mfa_cancel_enroll(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    db.delete_pending_auth_state(request.session.pop("_mfa_enroll_token", None))
    return RedirectResponse("/settings?tab=security", status_code=303)


@app.post("/settings/mfa/disable")
def settings_mfa_disable(request: Request, current_password: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    allowed, retry_after = rate_limit.check(f"login:{user['email']}", limit=8, window_s=60)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=security&mfa_error=Too+many+attempts+-+try+again+in+{retry_after}s", status_code=303)
    ok, error = supabase_auth.mfa_disable(user["email"], current_password)
    if not ok:
        return RedirectResponse(f"/settings?tab=security&mfa_error={quote(error or 'Something went wrong.')}",
                                 status_code=303)
    db.set_user_mfa_enabled(user["id"], False)
    db.delete_mfa_backup_codes(user["id"])
    db.log_security_event("mfa_disabled", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    return RedirectResponse("/settings?tab=security&mfa_notice=Two-factor+authentication+turned+off.",
                             status_code=303)


@app.post("/settings/mfa/regenerate-backup-codes")
def settings_mfa_regenerate_backup_codes(request: Request, current_password: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user["mfa_enabled_at"]:
        return RedirectResponse("/settings?tab=security", status_code=303)
    allowed, retry_after = rate_limit.check(f"login:{user['email']}", limit=8, window_s=60)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=security&mfa_error=Too+many+attempts+-+try+again+in+{retry_after}s", status_code=303)
    # Re-authenticates but doesn't actually need the resulting session for
    # anything beyond confirming the password is real - regenerating codes
    # is entirely local (webapp/db.py), no Supabase call involved.
    session, error = supabase_auth.sign_in(user["email"], current_password)
    if error or not session:
        return RedirectResponse(f"/settings?tab=security&mfa_error={quote('Your current password is incorrect.')}",
                                 status_code=303)
    backup_codes = _generate_backup_codes()
    db.replace_mfa_backup_codes(user["id"], [_hash_backup_code(c) for c in backup_codes])
    db.log_security_event("mfa_backup_codes_regenerated", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    request.session["_mfa_new_backup_codes"] = backup_codes
    return RedirectResponse("/settings?tab=security&mfa_step=backup_codes", status_code=303)


@app.post("/settings/team/invite")
def settings_team_invite(request: Request, email: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    # Only the real owner can invite - a team member's own client_scope_id
    # already points at the owner, not themselves, so this check alone is
    # enough to block a teammate from inviting people onto an account
    # that isn't theirs to manage.
    if user["client_scope_id"] != user["id"]:
        return RedirectResponse("/settings?tab=team", status_code=303)
    # Real email sent to a third party's address below - without a limit, a
    # logged-in account could be used to email-bomb any inbox it names,
    # over and over, using our own mailer/domain.
    allowed, retry_after = rate_limit.check(f"invite:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return RedirectResponse(
            f"/settings?tab=team&team_error=Too+many+invites+-+try+again+in+{retry_after // 60 + 1}+minute(s).",
            status_code=303)
    email = email.strip().lower()
    if not email or "@" not in email:
        return RedirectResponse("/settings?tab=team&team_error=Enter+a+real+email+address.", status_code=303)
    if email == user["email"].strip().lower():
        return RedirectResponse("/settings?tab=team&team_error=That%27s+your+own+email.", status_code=303)
    existing = [m for m in db.list_team_members(user["id"]) if m["invited_email"] == email]
    if existing:
        return RedirectResponse("/settings?tab=team&team_error=Already+invited.", status_code=303)
    db.create_team_invite(user["id"], email)
    # Real email, not just a DB row - an invite nobody's told about isn't
    # an invite. Reuses the same mailer/wrap_email_html every other
    # transactional email in this app already goes through.
    try:
        inner = (
            f"<p>{user['display_name'] or user['email']} invited you to their Kauli account, so you can "
            f"see and manage the same orders and billing together.</p>"
            f"<p>Sign up or log in with this email address ({email}) to accept.</p>"
        )
        html = mailer.wrap_email_html(inner, cta_text="Sign up for Kauli", cta_url=str(request.base_url).rstrip("/") + "/login",
                                       base_url=str(request.base_url))
        mailer.send_email(email, f"{user['display_name'] or 'A Kauli client'} invited you to their team", html, inner)
    except Exception:
        pass  # the invite row itself is what matters - acceptance is checked at login regardless of this email landing
    return RedirectResponse("/settings?tab=team&team_notice=Invite+sent.", status_code=303)


@app.post("/settings/team/remove")
def settings_team_remove(request: Request, member_id: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.remove_team_member(user["id"], member_id)
    return RedirectResponse("/settings?tab=team&team_notice=Removed.", status_code=303)


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """Real, specific answers to the actual states this app's orders/
    payments can be in - not a generic support-article template. See the
    account menu (base.html) for where clients reach this from."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "help.html", {
        "user": user, "theme": "dark" if user["role"] == "staff" else "light",
    })


@app.get("/docs/api", response_class=HTMLResponse)
def docs_api(request: Request):
    """API/webhook reference for the integrations feature (see /settings'
    Developer card) - real endpoints, real example payloads, nothing
    aspirational."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "docs_api.html", {
        "user": user, "theme": "dark" if user["role"] == "staff" else "light",
    })


@app.get("/staff/guide", response_class=HTMLResponse)
def staff_guide(request: Request):
    """The long-form editor guide + style guide - the Guide tab inside
    Ereri is the quick-reference version for while you're actually
    working a segment; this is the comprehensive one, reachable from
    anywhere in the staff portal (sidebar) so it isn't tied to any one
    order."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_guide.html", {
        "user": user, "theme": "dark",
    })


@app.get("/staff/handbook", response_class=HTMLResponse)
def staff_handbook(request: Request):
    """The whole-system reference, distinct from /staff/guide (which is
    Ereri-editing-mechanics only) - what a brand new contractor needs to
    understand roles, the real order lifecycle, billing, and common
    troubleshooting without having to ask the team every time. Written for
    "a staff member who just joined us," per the actual real request that
    created this page - not a generic onboarding template."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_handbook.html", {
        "user": user, "theme": "dark",
    })


STAFF_DOCS_DIR = WEBAPP_DIR / "staff_docs"


@app.get("/staff/resources", response_class=HTMLResponse)
def staff_resources(request: Request):
    """One real entry point for everything a new contractor or an
    investor conversation needs, instead of scattering links across the
    sidebar - reference material (guide/handbook, already elsewhere in
    the sidebar) plus the standalone documents built alongside this
    system: a technical overview, the pitch deck, and the independent
    contractor agreement template. All gated behind staff login - none
    of this is meant to be publicly reachable, unlike webapp/static/."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_resources.html", {
        "user": user, "theme": "dark",
    })


@app.get("/staff/resources/technical-overview", response_class=HTMLResponse)
def staff_resources_technical_overview(request: Request):
    """Served as its own real HTML file, not a Jinja template - it has a
    complete, self-contained design of its own (not the app's chrome),
    same content as the one published as a Claude artifact but hosted
    here too so access never depends on that artifact's own sharing
    settings - every staff login can always reach it, not just whoever
    the team has personally shared the artifact link with."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    html_path = STAFF_DOCS_DIR / "technical-overview.html"
    if not html_path.exists():
        return HTMLResponse("Not found.", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/staff/resources/pitch-deck")
def staff_resources_pitch_deck(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    path = STAFF_DOCS_DIR / "kauli-pitch-deck.pptx"
    if not path.exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(str(path), filename="Kauli Pitch Deck.pptx")


@app.get("/staff/resources/contractor-agreement")
def staff_resources_contractor_agreement(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    path = STAFF_DOCS_DIR / "kauli-independent-contractor-agreement.docx"
    if not path.exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(str(path), filename="Kauli Independent Contractor Agreement (template).docx")


@app.get("/staff/voice-actors", response_class=HTMLResponse)
def staff_voice_actors(request: Request, notice: str | None = None, error: str | None = None):
    """Voice-actor roster + payout ledger. Admin-gated, same reasoning as
    /staff/admin - this is money-adjacent (who's owed what) even though
    today's single-staff-role reality means that's not really restricting
    anyone yet. Assigning an already-onboarded actor to a specific order
    is a normal staff action (see staff_review.html / assign_voice_actor
    route below), separate from managing the roster/ledger itself."""
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_voice_actors.html", {
        "user": user, "theme": "dark", "notice": notice, "error": error,
        "actors": db.list_voice_actors(),
        "payouts_owed": db.list_payouts("owed"),
        "payouts_paid": db.list_payouts("paid")[:50],  # recent history, not the whole ledger forever
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
    })


@app.post("/staff/voice-actors")
def staff_voice_actors_create(
    request: Request, name: str = Form(...), languages: list[str] = Form([]),
    email: str = Form(""), phone: str = Form(""), bio: str = Form(""),
    rate_per_min_usd: str = Form(""), notes: str = Form(""),
):
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    if not name.strip() or not languages:
        return RedirectResponse("/staff/voice-actors?error=Name+and+at+least+one+language+are+required.",
                                 status_code=303)
    rate = None
    if rate_per_min_usd.strip():
        try:
            rate = round(float(rate_per_min_usd), 2)
            if rate < 0:
                raise ValueError
        except ValueError:
            return RedirectResponse("/staff/voice-actors?error=Rate+must+be+a+positive+number.",
                                     status_code=303)
    db.create_voice_actor(name=name, languages=languages, email=email, phone=phone,
                           bio=bio, rate_per_min_usd=rate, notes=notes)
    return RedirectResponse(f"/staff/voice-actors?notice=Added+{quote(name.strip())}.", status_code=303)


@app.post("/staff/voice-actors/{actor_id}/status")
def staff_voice_actor_set_status(request: Request, actor_id: str, status: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    if status not in ("active", "inactive"):
        return RedirectResponse("/staff/voice-actors?error=Unknown+status.", status_code=303)
    db.set_voice_actor_status(actor_id, status)
    return RedirectResponse("/staff/voice-actors?notice=Updated.", status_code=303)


def _notify_actor_of_assignment(actor: dict, order, base_url: str) -> None:
    """Real email, sent the moment an actor is actually cast onto an
    order (see staff_assign_actor) - not at roster-add time (see
    staff_voice_actors_create, which deliberately doesn't email anyone,
    same as a plain staff invite - nothing to act on yet at that point).
    Works whether this is the actor's first job ever (no account linked
    yet - db.link_voice_actor_user fills that in the moment they sign up
    with this same email, see _resolve_role_and_admin) or their tenth -
    one message either way, since the same /login page handles both."""
    if not actor.get("email") or not mailer.email_configured():
        return
    login_url = f"{base_url.rstrip('/')}/login?next=%2Factor"
    has_account = bool(actor.get("user_id"))
    action_line = ("Log in to your Kauli account" if has_account
                   else "Create your free Kauli account (use this same email address)")
    subject = "New voice-over job on Kauli"
    text = (
        f"Hi {actor['name']},\n\n"
        f"You've been cast on a new job: \"{order['original_filename']}\".\n\n"
        f"{action_line} to see the script and upload your recording once it's ready:\n{login_url}\n\n"
        f"Any questions, just reply to this email."
    )
    inner = (
        f'<p style="margin:0 0 14px;">Hi {actor["name"]},</p>'
        + mailer.text_to_html_paragraphs(f'You\'ve been cast on a new job: "{order["original_filename"]}".')
        + f'<p style="margin:0 0 14px;">{action_line} to see the script and upload your recording once '
          f'it\'s ready.</p>'
    )
    html = mailer.wrap_email_html(inner, cta_text=action_line, cta_url=login_url, base_url=base_url)
    mailer.send_email(actor["email"], subject, html, text)


@app.post("/staff/orders/{order_id}/assign-actor")
def staff_assign_actor(request: Request, order_id: str, actor_id: str = Form("")):
    """Any staff member can cast an already-onboarded actor onto an order -
    this is normal day-to-day order work, not roster/ledger management
    (see the admin gate on the routes above)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    actor_id = actor_id.strip() or None
    db.assign_voice_actor(order_id, actor_id)
    if actor_id:
        actor = db.get_voice_actor(actor_id)
        if actor:
            _notify_actor_of_assignment(actor, order, str(request.base_url))
    return RedirectResponse(f"/staff/orders/{order_id}?notice=Voice+actor+updated.", status_code=303)


HUMAN_RECORDING_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".ogg")


def _human_recording_path(order) -> Path | None:
    """The voice actor's own take, kept in a stable slot separate from
    whatever's currently the ACTIVE delivered dub (see
    _activate_human_recording) - so switching the dub-voice picker back to
    an AI voice and later back to "human" never loses the real recording.
    Returns the existing file's path, or None if no human take has ever
    been uploaded for this order."""
    outdir = Path(order["outdir"])
    for ext in HUMAN_RECORDING_EXTS:
        p = outdir / f"dub_{order['target_lang']}_human{ext}"
        if p.exists():
            return p
    return None


def _activate_human_recording(order_id: str, order, ext: str, content: bytes) -> None:
    """Saves a freshly uploaded human take to its own permanent slot AND
    makes it the active delivered dub - same real effect
    staff_human_voice_upload/actor_upload_recording always had (the
    client's download link doesn't change, what's behind it does), just
    now paired with db.set_dub_voice so the editor's voice picker actually
    reflects "human" as the current choice instead of going stale the
    moment someone picks a Piper voice and comes back."""
    outdir = Path(order["outdir"])
    for other_ext in HUMAN_RECORDING_EXTS:
        if other_ext != ext:
            (outdir / f"dub_{order['target_lang']}_human{other_ext}").unlink(missing_ok=True)
    (outdir / f"dub_{order['target_lang']}_human{ext}").write_bytes(content)

    dest = outdir / f"dub_{order['target_lang']}{ext}"
    dest.write_bytes(content)
    # If a stale file from an earlier AI dub is still sitting there under a
    # different extension than what just got uploaded, the download route
    # would find both and the old one could still win - remove it so the
    # human recording is unambiguously what gets delivered.
    for other_ext in HUMAN_RECORDING_EXTS:
        if other_ext != ext:
            (outdir / f"dub_{order['target_lang']}{other_ext}").unlink(missing_ok=True)
    db.set_dub_voice(order_id, "human", job_status=None)


@app.post("/staff/orders/{order_id}/human-voice-upload")
async def staff_human_voice_upload(request: Request, order_id: str, recording: UploadFile = File(...)):
    """Drops a voice actor's finished, already-synced recording straight
    in as the delivered dub file, replacing whatever the AI TTS produced -
    the client's download link doesn't change, what's behind it does.
    Deliberately does NOT attempt any automatic timeline-fitting - a full
    human take doesn't decompose into per-segment slots the way TTS audio
    does, and pretending to auto-sync it would be a real, silent quality
    risk. Getting the recording actually synced to the source video before
    it's uploaded here is real editorial work someone does outside this
    system (whatever audio tool they're already using) - this is only
    where the FINISHED result gets delivered from."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if not order["voice_actor_id"]:
        return RedirectResponse(
            f"/staff/orders/{order_id}?error=Assign+a+voice+actor+to+this+order+first.", status_code=303)
    ext = Path(recording.filename or "").suffix.lower() or ".wav"
    if ext not in HUMAN_RECORDING_EXTS:
        return RedirectResponse(
            f"/staff/orders/{order_id}?error=Unsupported+audio+format+-+use+wav%2C+mp3%2C+m4a%2C+flac+or+ogg.",
            status_code=303)
    content = await recording.read()
    _activate_human_recording(order_id, order, ext, content)
    return RedirectResponse(
        f"/staff/orders/{order_id}?notice=Human+voice-over+uploaded+-+it+will+be+what+the+client+downloads.",
        status_code=303)


@app.post("/staff/orders/{order_id}/create-payout")
def staff_create_payout(request: Request, order_id: str):
    """Records what's owed the assigned actor for this order, at THEIR
    rate (not the client's addon price - see billing.ADDONS'
    human_voice_over comment on why those are different numbers). Does
    not move any money - see mark_payout_paid for the only thing that
    does that, and even that's just a staff member confirming a real
    transfer they already sent by hand."""
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if not order["voice_actor_id"]:
        return RedirectResponse(
            f"/staff/orders/{order_id}?error=No+voice+actor+assigned+to+this+order.", status_code=303)
    actor = db.get_voice_actor(order["voice_actor_id"])
    if not actor:
        return RedirectResponse(f"/staff/orders/{order_id}?error=Assigned+actor+not+found.", status_code=303)
    if not actor["rate_per_min_usd"]:
        return RedirectResponse(
            f"/staff/orders/{order_id}?error=Set+a+rate+for+{quote(actor['name'])}+on+the+"
            "Voice+Talent+page+first.", status_code=303)
    minutes = order["duration_minutes"] or 0
    if minutes <= 0:
        return RedirectResponse(
            f"/staff/orders/{order_id}?error=Order+has+no+known+duration+yet.", status_code=303)
    db.create_payout(actor["id"], order_id, minutes, actor["rate_per_min_usd"])
    return RedirectResponse(
        f"/staff/orders/{order_id}?notice=Payout+of+${minutes * actor['rate_per_min_usd']:.2f}+recorded+"
        f"for+{quote(actor['name'])}+-+see+the+Voice+Talent+page+to+mark+it+paid+once+sent.",
        status_code=303)


@app.post("/staff/payouts/{payout_id}/mark-paid")
def staff_mark_payout_paid(request: Request, payout_id: str, reference: str = Form("")):
    """The one action in this whole module that means real money actually
    moved - and even this doesn't move it, it just records that a staff
    member confirms they already sent it themselves (M-Pesa, bank
    transfer, whatever) outside this system. There is no payment-API
    integration here to automate this with."""
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    db.mark_payout_paid(payout_id, user["id"], reference)
    return RedirectResponse("/staff/voice-actors?notice=Marked+paid.", status_code=303)


# ------------------------------------------------------- voice actor portal ----
# The actor-facing half of the human-voice-over feature: a real, minimal
# self-service account, not just a staff-managed roster row - see
# db.is_invited_voice_actor / link_voice_actor_user (the login/signup
# machinery) and _resolve_role_and_admin for how an actor's account
# actually gets this role. Deliberately thin - a job list and an upload
# form, nothing an actor doesn't need, matching how little Ereri itself
# exposes to a role that only has one real job to do here.
def _require_voice_actor(request: Request):
    user = current_user(request)
    if not user or user["role"] != "voice_actor":
        return None, None
    actor = db.get_voice_actor_by_user_id(user["id"])
    return user, actor


@app.get("/actor", response_class=HTMLResponse)
def actor_dashboard(request: Request, notice: str | None = None, error: str | None = None):
    user, actor = _require_voice_actor(request)
    if not user:
        return RedirectResponse("/login")
    if not actor:
        # Real edge case, not a made-up one: the roster row this account
        # was linked to could have been deleted since - nothing left to
        # show, but don't crash over it.
        return HTMLResponse(
            "Your voice-actor profile wasn't found - contact us.", status_code=404)
    orders = db.list_orders_for_voice_actor(actor["id"])
    # The script the actor actually needs to read - real segment text,
    # same source _build_target_cells/to_srt use, not a re-derived copy.
    # None for an order whose job hasn't been processed yet (real gap,
    # shown honestly rather than an empty script pretending it's ready).
    jobs = {o["id"]: _load_job(o) for o in orders}
    return templates.TemplateResponse(request, "actor_dashboard.html", {
        "user": user, "actor": actor, "orders": orders, "jobs": jobs, "notice": notice, "error": error,
    })


@app.post("/actor/orders/{order_id}/upload")
async def actor_upload_recording(request: Request, order_id: str, recording: UploadFile = File(...)):
    """The actor's own version of staff_human_voice_upload - same real
    delivery mechanism (drops straight in as the delivered dub file, no
    auto-sync attempted - see that function's own docstring for why), the
    only difference is the ownership check: an actor may only ever
    upload to an order actually cast to THEM, not any order id they can
    guess or type into the URL."""
    user, actor = _require_voice_actor(request)
    if not user:
        return RedirectResponse("/login")
    if not actor:
        return HTMLResponse("Your voice-actor profile wasn't found - contact us.", status_code=404)
    order = db.get_order(order_id)
    if not order or order["voice_actor_id"] != actor["id"]:
        return HTMLResponse("Order not found.", status_code=404)
    ext = Path(recording.filename or "").suffix.lower() or ".wav"
    if ext not in HUMAN_RECORDING_EXTS:
        return RedirectResponse(
            f"/actor?error=Unsupported+audio+format+-+use+wav%2C+mp3%2C+m4a%2C+flac+or+ogg.", status_code=303)
    content = await recording.read()
    _activate_human_recording(order_id, order, ext, content)
    notifications.notify_staff(
        f"Kauli: {actor['name']} uploaded a recording for order {order_id}",
        f"Order {order_id} ({order['original_filename']}) - the human voice-over has been uploaded and "
        f"is now what the client will download. Give it a listen before the order ships.\n\n"
        f"See /staff/orders/{order_id}.",
    )
    return RedirectResponse(
        f"/actor?notice=Uploaded+-+that%27s+now+what+the+client+will+download.", status_code=303)


@app.post("/settings")
def settings_save(request: Request, display_name: str = Form(...),
                   avatar: UploadFile | None = File(None)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    display_name = display_name.strip()
    if not display_name:
        empty_name_msg = "Name can't be empty."
        return RedirectResponse(f"/settings?tab=general&error={quote(empty_name_msg)}", status_code=303)

    avatar_path = None
    if avatar is not None and avatar.filename:
        # Content-Type is a client-supplied header, not proof of anything -
        # a raw request can claim "image/png" for any bytes it likes.
        # Save to a temp spot first, sniff the real magic bytes, only THEN
        # decide the extension and move it into place.
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dest = AVATAR_DIR / f"{user['id']}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            _size, _sha256, head = upload_security.stream_save_with_limits(
                avatar, tmp_dest, upload_security.MAX_AVATAR_BYTES)
        except upload_security.UploadRejected as exc:
            return RedirectResponse(f"/settings?tab=general&error={quote(str(exc))}", status_code=303)
        mime = upload_security.sniff_image_type(head)
        ext = ALLOWED_AVATAR_TYPES.get(mime)
        if not ext:
            tmp_dest.unlink(missing_ok=True)
            return RedirectResponse(
                f"/settings?tab=general&error={quote('Photo must be a real PNG, JPEG, or WebP file.')}",
                status_code=303)
        # Clear any previous photo under a different extension first, so
        # switching from a .png to a .jpg doesn't leave the old one behind
        # for /avatar/{id} to find first.
        # Real bug this fixes, confirmed live: AVATAR_DIR.glob(f"{id}.*")
        # also matches the tmp file itself (tmp_dest is named "{id}.tmp-
        # xxxxxxxx" - glob's `*` matches the dot inside "tmp-xxxxxxxx" too,
        # verified directly), so this loop deleted the just-written upload
        # before the rename below could run, throwing a real
        # FileNotFoundError on every single avatar upload. Explicit known
        # extensions instead of a wildcard can never match the tmp file.
        for ext_ in set(ALLOWED_AVATAR_TYPES.values()):
            (AVATAR_DIR / f"{user['id']}{ext_}").unlink(missing_ok=True)
        dest = AVATAR_DIR / f"{user['id']}{ext}"
        tmp_dest.rename(dest)
        avatar_path = str(dest)

    db.update_profile(user["id"], display_name, avatar_path)
    return RedirectResponse("/settings?tab=general&saved=1", status_code=303)


@app.post("/settings/marketing-consent")
def settings_marketing_consent(request: Request, marketing_consent: str = Form("")):
    """The "Preference Center" action - opt in or out any time, logged
    with a fresh timestamp/IP each change (see db.set_marketing_consent).
    Never touches account_status or any transactional email - those keep
    going regardless, this is only the newsletter-style opt-in."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    db.set_marketing_consent(user["id"], bool(marketing_consent),
                              request.client.host if request.client else None)
    return RedirectResponse("/settings?tab=notifications&saved=1", status_code=303)


@app.post("/settings/close-account")
def settings_close_account(request: Request, confirm_text: str = Form("")):
    """Client-only on purpose - staff/admin accounts don't get a self-
    service close button here (too easy to accidentally lock yourself, or
    the only admin, out). Requires typing the account's own email as the
    confirmation, not just a click - the same "hard to do by accident"
    bar as any other irreversible-feeling action."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if confirm_text.strip().lower() != user["email"].strip().lower():
        return RedirectResponse(
            f"/settings?tab=security&error={quote('Type your email exactly to confirm closing your account.')}",
            status_code=303)
    db.log_security_event("account_closed", user_id=user["id"], email=user["email"],
                           ip_address=rate_limit.client_ip(request))
    db.close_account(user["id"])
    request.session.clear()
    return RedirectResponse("/login?notice=account_closed", status_code=303)


@app.post("/settings/request-deletion")
def settings_request_deletion(request: Request):
    """Never auto-deletes anything - see db.create_deletion_request's own
    docstring for why (real accounting/dispute-resolution reasons to keep
    order/payment records around for a while). This creates a real,
    staff-visible request instead - see /staff/leads."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    db.create_deletion_request(user["id"])
    return RedirectResponse("/settings?tab=security", status_code=303)


@app.get("/avatar/{user_id}")
def avatar(request: Request, user_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    target = db.get_user(user_id)
    if not target or not target["avatar_path"] or not Path(target["avatar_path"]).exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(target["avatar_path"])


# -------------------------------------------------------------- client ----
@app.get("/client/files", response_class=HTMLResponse)
def client_files(request: Request, type: str = "", q: str = "", page: int = 1):
    """A real cross-order file view - every order's source file plus
    whatever's actually deliverable for it, in one place, instead of
    having to open each order individually to find a download link.
    Reuses the exact same /client/orders/{id}/download/{kind} routes and
    the same real gating order_detail.html already uses (free-preview
    orders and anything not yet delivered get no download links here
    either) - no new file-serving logic, purely an aggregated view over
    what already exists per order."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    rows = []
    for o in db.list_orders_for_client(user["client_scope_id"]):
        deliverables = []
        if o["status"] in ("ready_for_delivery", "delivered") and not _order_locked(o):
            outdir = Path(o["outdir"]) if o["outdir"] else None
            deliverables = [
                {"kind": "audio", "label": "Dubbed audio (.wav)"},
                {"kind": "srt", "label": "Subtitles (.srt)"},
                {"kind": "vtt", "label": "Subtitles (.vtt)"},
                {"kind": "transcript", "label": "Source transcript (.srt)"},
            ]
            if outdir and (outdir / f"burned_captions_{o['target_lang']}.mp4").exists():
                deliverables.append({"kind": "burned", "label": "Video with burned-in captions"})
            if outdir and (outdir / f"dubbed_video_{o['target_lang']}.mp4").exists():
                deliverables.append({"kind": "dubbed", "label": "Dubbed video"})
        source_size = None
        if o["audio_path"] and Path(o["audio_path"]).exists():
            source_size = Path(o["audio_path"]).stat().st_size
        ext = (o["original_filename"] or "").rsplit(".", 1)[-1].lower() if "." in (o["original_filename"] or "") else ""
        kind = "video" if ext in ("mp4", "mov", "mkv", "webm", "avi", "m4v", "mpeg", "mpg", "3gp") or o["source_youtube_id"] else \
            "audio" if ext in ("mp3", "wav", "m4a", "flac", "ogg", "opus", "aac", "wma") else "other"
        rows.append({"order": o, "deliverables": deliverables, "source_size": source_size, "kind": kind})
    counts = {"": len(rows), **{k: sum(1 for r in rows if r["kind"] == k) for k in ("audio", "video", "other")}}
    type = type if type in ("audio", "video", "other") else ""
    needle = (q or "").strip().lower()[:100]
    shown = [r for r in rows if (not type or r["kind"] == type) and (not needle or needle in (r["order"]["original_filename"] or "").lower())]
    page, pages, shown = _paginate(shown, page, LIST_PAGE_SIZE)
    return templates.TemplateResponse(request, "client_files.html", {"user": user, "rows": shown, "counts": counts, "type": type, "q": needle,
                                                                      "all_count": len(rows), "page": page, "pages": pages})


@app.get("/client/messages", include_in_schema=False)
def client_messages(request: Request):
    """The separate Messages page is gone: replies from the team arrive in the assistant's inbox (the button at the bottom right) and on
    the order itself. Old links and bookmarks land on the dashboard."""
    return RedirectResponse("/client", status_code=303)


# ------------------------------------------------------------------ Kauli in your meetings
def _meeting_gate(user) -> tuple[bool, str, float]:
    """Whether this client may send Kauli to a meeting right now: on a paid plan, with enough minutes left (the plan's
    included Instant AI minutes plus any prepaid credit) to cover a meeting of a useful length."""
    cid = user["client_scope_id"]
    sub = db.get_subscription_current(cid)
    market = db.get_billing_market(cid)
    ai_rate = billing.rate_for("transcribe", market, "ai") or 0.12
    minutes = billing.free_minutes_remaining(sub) + billing.credits_to_usd(db.wallet_credits_remaining(cid)) / ai_rate
    is_tester = (user["email"] or "").strip().lower() in billing.test_client_emails()
    if billing._plan_in_force(sub) == "free" and not is_tester:
        return False, "Kauli can join your meetings on the Pro and Premium plans.", minutes
    if minutes < meeting_bot.MIN_MEETING_MINUTES and not is_tester:
        return False, (f"You need at least {int(meeting_bot.MIN_MEETING_MINUTES)} minutes left to send Kauli to a meeting, and you have {int(minutes)}. "
                       "Add prepaid credit, or your minutes renew next month."), minutes
    return True, "", minutes


def _fail_meeting(m, message: str) -> None:
    db.update_meeting(m["id"], status="failed", error=message)


def _finish_meeting(m, link: str, request: Request) -> None:
    """The meeting ended and its recording is ready: turn it into an ordinary Instant AI order for that client. Charged
    exactly like any other order (included minutes first, then credit, then a payment link for any overage)."""
    if not db.claim_meeting(m["id"]):
        return
    row = db.get_user(m["client_id"])
    if not row:
        _fail_meeting(m, "The account for this meeting no longer exists.")
        return
    user = dict(row)
    user["client_scope_id"] = m["client_id"]
    request.state.api_user = user
    tmp_dir = Path(tempfile.mkdtemp(prefix="kauli-meeting-"))
    tmp = tmp_dir / "meeting.mp4"
    try:
        meeting_bot.download(link, str(tmp))
        from starlette.datastructures import Headers
        with open(tmp, "rb") as fh:
            upload = UploadFile(file=fh, filename=f"{(m['title'] or 'Meeting').strip()[:80]}.mp4", headers=Headers({"content-type": "video/mp4"}))
            resp = create_order(
                request, audio=upload, upload_key="", youtube_url="", source_lang=m["source_lang"], target_lang=m["source_lang"],
                service_level="transcribe", addon_video_deliverables="", addon_human_voice_over="", addon_premium_voice="",
                premium_voice_id="", addon_audio_cleanup="", instr_speaker_ids="1", instr_verbatim_level="clean_read",
                instr_transcribe_lyrics="", instr_use_italics="", instr_existing_subs="ignore", instr_no_audio="tag",
                instr_wrong_language="tag", instr_instrumental_only="tag", instr_notes="Recorded by Kauli in an online meeting.",
                style_guide=None, idempotency_key=f"meeting-{m['id']}", folder_name="Meetings", voice_clone_consent="",
                rush="", review_tier="ai")
    except ApiOrderError as exc:
        _fail_meeting(m, f"The recording could not be turned into a transcript: {exc}")
        return
    except Exception:  # noqa: BLE001 - a bad recording must not take the webhook or the page down
        logging_setup.get_logger("meetings").warning("meeting import failed", extra={"job_id": m["id"]}, exc_info=True)
        _fail_meeting(m, "The recording could not be turned into a transcript. Please contact us and we will sort it out.")
        return
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    loc = resp.headers.get("location", "") if isinstance(resp, RedirectResponse) else ""
    found = re.match(r"^/client/orders/([0-9a-f]+)", loc)
    if not found:
        _fail_meeting(m, "The recording could not be turned into a transcript.")
        return
    db.update_meeting(m["id"], status="done", order_id=found.group(1))


def _refresh_meeting(m, request: Request) -> None:
    """Ask the meeting service where a bot has got to and move our record along; import the recording once it exists."""
    if m["status"] not in ("scheduled", "joining", "recording", "processing") or not m["provider_bot_id"]:
        return
    bot = meeting_bot.get_bot(m["provider_bot_id"])
    if "error" in bot:
        return
    status, problem = meeting_bot.status_from_bot(bot)
    link = meeting_bot.recording_url(bot)
    if status in ("processing", "done") and link:
        _finish_meeting(m, link, request)
        return
    if status == "failed":
        _fail_meeting(m, problem or "Kauli could not join or record this meeting.")
    elif status != m["status"]:
        db.update_meeting(m["id"], status="processing" if status == "done" else status)


_MEETING_STATUS_TEXT = {"scheduled": "Scheduled", "joining": "Joining", "recording": "Recording", "processing": "Getting the recording",
                        "importing": "Making the transcript", "done": "Transcript ready", "failed": "Did not work", "cancelled": "Cancelled"}


@app.get("/client/meetings", response_class=HTMLResponse)
def client_meetings(request: Request, error: str | None = None, notice: str | None = None, page: int = 1):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    enabled = meeting_bot.configured()
    meetings = db.list_meetings_for_client(user["client_scope_id"]) if enabled else []
    for m in meetings:
        _refresh_meeting(m, request)
    if enabled:
        meetings = db.list_meetings_for_client(user["client_scope_id"])
    ok, why, minutes = _meeting_gate(user) if enabled else (False, "", 0.0)
    page, pages, meetings = _paginate(list(meetings), page, LIST_PAGE_SIZE)
    return templates.TemplateResponse(request, "client_meetings.html", {
        "user": user, "enabled": enabled, "meetings": meetings, "page": page, "pages": pages, "can_send": ok, "why_not": why, "minutes_left": int(minutes),
        "min_minutes": int(meeting_bot.MIN_MEETING_MINUTES), "status_text": _MEETING_STATUS_TEXT, "platform_names": meeting_bot.PLATFORM_NAMES,
        "languages": sorted(LANGUAGE_NAMES.items(), key=lambda kv: kv[1]), "error": error, "notice": notice,
    })


@app.post("/client/meetings")
def client_meeting_create(request: Request, meeting_url: str = Form(""), title: str = Form(""), source_lang: str = Form("en"),
                          join_at: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")

    def back(kind: str, text: str):
        return RedirectResponse(f"/client/meetings?{kind}={quote(text)}", status_code=303)

    if not meeting_bot.configured():
        return back("error", "Meetings are not switched on yet.")
    url = (meeting_url or "").strip()
    platform = meeting_bot.platform_for(url)
    if not platform:
        return back("error", "Paste a Zoom, Google Meet or Microsoft Teams meeting link that starts with https.")
    if source_lang not in LANGUAGE_NAMES:
        return back("error", "Pick the language that will be spoken.")
    ok, why, minutes = _meeting_gate(user)
    if not ok:
        return back("error", why)
    cid = user["client_scope_id"]
    if db.count_active_meetings(cid) >= meeting_bot.MAX_ACTIVE_MEETINGS:
        return back("error", f"You already have {meeting_bot.MAX_ACTIVE_MEETINGS} meetings in progress. Wait for one to finish or cancel it.")
    allowed, _retry = rate_limit.check(f"meeting:{cid}", limit=10, window_s=600)
    if not allowed:
        return back("error", "Too many requests just now. Try again in a few minutes.")
    join_ts, join_iso = None, None
    if join_at.strip():
        try:
            when = datetime.fromisoformat(join_at.strip().replace("Z", "+00:00"))
            join_ts = when.timestamp()
        except ValueError:
            return back("error", "That start time was not understood.")
        if join_ts < time.time() - 300 or join_ts > time.time() + meeting_bot.MAX_SCHEDULE_DAYS * 86400:
            return back("error", f"Choose a start time within the next {meeting_bot.MAX_SCHEDULE_DAYS} days.")
        join_iso = datetime.fromtimestamp(join_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if join_ts > time.time() + 60 else None
    made = meeting_bot.create_bot(url, join_iso)
    if "error" in made:
        return back("error", made["error"])
    db.create_meeting(uuid.uuid4().hex, cid, url, platform, (title or "").strip()[:120], source_lang, made["id"], join_ts, minutes)
    return back("notice", "Kauli will join your meeting. Tell everyone in it that the meeting is being recorded.")


@app.post("/client/meetings/{meeting_id}/cancel")
def client_meeting_cancel(request: Request, meeting_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    m = db.get_meeting(meeting_id)
    if not m or m["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Not found.", status_code=404)
    if m["status"] in ("scheduled", "joining", "recording"):
        if m["provider_bot_id"]:
            meeting_bot.leave_bot(m["provider_bot_id"])
        db.update_meeting(m["id"], status="cancelled")
    return RedirectResponse("/client/meetings?notice=" + quote("Meeting cancelled."), status_code=303)


@app.post("/webhooks/recall")
async def recall_webhook(request: Request):
    """The meeting service telling us a bot changed state. Signature-checked; anything unsigned or for a bot we did not
    send is ignored."""
    body = await request.body()
    if not meeting_bot.verify_webhook(request.headers, body):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "bad body"}, status_code=400)
    data = payload.get("data") or {}
    bot_id = (data.get("bot") or {}).get("id") or data.get("bot_id")
    m = db.get_meeting_by_provider(str(bot_id)) if bot_id else None
    if m:
        await run_in_threadpool(_refresh_meeting, m, request)
    return JSONResponse({"ok": True})


def _attention_kind(order) -> str:
    """Why an unfinished order is on the Needs attention page: "pay", "input" and "charge" are waiting on the customer; "review" and
    "progress" are on their way."""
    if order["status"] == "pending_payment":
        return "pay"
    if order["status"] == "returned_to_client":
        return "input"
    if order["difficulty_surcharge_status"] == "pending_approval":
        return "charge"
    return "review" if order["status"] in ("awaiting_review", "editor_returned") else "progress"


@app.get("/client/pending", response_class=HTMLResponse)
def client_pending(request: Request, page: int = 1, tab: str = ""):
    """Orders that are not finished yet, with the one thing each is waiting for."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    rows = [(o, _attention_kind(o)) for o in db.list_pending_orders(user["client_scope_id"])]
    counts = {"": len(rows), "needs": sum(1 for _o, k in rows if k in ("pay", "input", "charge")), "progress": sum(1 for _o, k in rows if k in ("review", "progress"))}
    tab = tab if tab in ("needs", "progress") else ""
    shown_rows = [r for r in rows if not tab or (r[1] in ("pay", "input", "charge")) == (tab == "needs")]
    page, pages, shown = _paginate(shown_rows, page, FILES_PER_PAGE)
    return templates.TemplateResponse(request, "client_pending.html", {
        "user": user, "rows": shown, "page": page, "pages": pages, "tab": tab, "counts": counts,
    })


LIST_PAGE_SIZE = 6   # every list in a customer account shows at most this many rows, then numbered pages
FILES_PER_PAGE = LIST_PAGE_SIZE


def _paginate(items: list, page: int, per_page: int) -> tuple[int, int, list]:
    """(the page to show, how many pages there are, that page's items). A page past the end shows the last one."""
    pages = max(1, -(-len(items) // per_page))
    page = min(max(int(page or 1), 1), pages)
    return page, pages, items[(page - 1) * per_page: page * per_page]


@app.get("/client/home", response_class=HTMLResponse)
def client_home(request: Request, page: int = 1, q: str = "", status: str = ""):
    """The client portal's real landing page - summary stats + a preview
    of recent orders, separate from /client (the full order list +
    submission wizard, kept at its existing URL so none of the 'back to
    my orders' links elsewhere break). Every number here is a real,
    freshly-computed count from the client's own orders/payments - no
    placeholder/demo data."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    orders = db.list_orders_for_client(user["client_scope_id"])
    stats = db.client_dashboard_stats(user["client_scope_id"])
    view = _orders_view(orders, status, "", q[:100], page, per_page=FILES_PER_PAGE)
    counts = {"": len(orders), **{k: sum(1 for o in orders if o["status"] in v) for k, v in _ORDER_BUCKETS.items()}}
    hour = datetime.now().hour
    greeting = "greet_morning" if hour < 12 else "greet_afternoon" if hour < 18 else "greet_evening"
    return templates.TemplateResponse(request, "client_home.html", {
        "user": user, "greeting": greeting,
        "stats": stats, "recent_orders": view["orders"], "page": view["page"], "pages": view["pages"], "q": view["q"], "status": view["status"], "counts": counts,
        "free_minutes_left": billing.free_minutes_remaining(db.get_subscription_current(user["client_scope_id"])),
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
        "winback_offer": winback.open_offer(user["id"]) if user["client_scope_id"] == user["id"] else None,
        "next_step": engagement.next_step(user["id"]) if user["client_scope_id"] == user["id"] else None,
    })


ORDERS_PER_PAGE = LIST_PAGE_SIZE
_ORDER_BUCKETS = {
    "processing": ("queued", "processing"),
    "in_review": ("awaiting_review", "editor_returned"),
    "in_progress": ("queued", "processing", "awaiting_review", "editor_returned"),
    "ready": ("ready_for_delivery", "delivered"),
    "needs_you": ("returned_to_client", "pending_payment"),
}


def _orders_view(orders: list, status: str = "", folder: str = "", q: str = "", page: int = 1, per_page: int = ORDERS_PER_PAGE) -> dict:
    """The order list as the customer asked to see it: one filter, one folder, one search, one numbered page. The parameters are
    kept in the page links so moving between pages never loses the filter."""
    status = status if status in _ORDER_BUCKETS else ""
    needle = (q or "").strip().lower()
    shown = [o for o in orders
             if (not status or o["status"] in _ORDER_BUCKETS[status])
             and (not folder or (o["folder_name"] or "") == folder)
             and (not needle or needle in (o["original_filename"] or "").lower() or needle in (o["folder_name"] or "").lower()
                  or needle.lstrip("#") in o["id"].lower() or needle in (o["service_level"] or "").lower())]
    page, pages, items = _paginate(shown, page, per_page)
    counts = {"": len(orders), **{k: sum(1 for o in orders if o["status"] in v) for k, v in _ORDER_BUCKETS.items()}}
    return {"orders": items, "page": page, "pages": pages, "total": len(shown), "all_count": len(orders), "counts": counts,
            "status": status, "folder": folder, "q": (q or "").strip(), "params": {"status": status, "folder": folder, "q": (q or "").strip()}}


@app.get("/client", response_class=HTMLResponse)
def client_dashboard(request: Request, reorder_youtube_url: str | None = None, reorder_source_lang: str | None = None,
                     page: int = 1, status: str = "", folder: str = "", q: str = ""):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    orders = db.list_orders_for_client(user["client_scope_id"])
    order_list = _orders_view(orders, status, folder, q, page)
    unread = db.unread_order_ids(user["client_scope_id"], include_internal=False)
    anthropic_available = bool(os.environ.get("ANTHROPIC_API_KEY"))
    subscription = db.get_subscription_current(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    # "Process this in another language" from order_detail.html - only for
    # a YouTube-sourced original (re-fetches the same real video, not a
    # fake/reused upload); pre-fills the same wizard fields the error path
    # already uses, so submitting is the one real click of picking a
    # different target language.
    # Real per-user defaults (see /settings' "defaults" tab) prefill the
    # wizard, same as any real form remembering your last real choices - a
    # reorder's own explicit values (from order_detail.html's "process in
    # another language") still win over these, they're just the starting
    # point when there's nothing more specific in play.
    reorder_form_values = {}
    if user["default_source_lang"]:
        reorder_form_values["source_lang"] = user["default_source_lang"]
    if user["default_target_lang"]:
        reorder_form_values["target_lang"] = user["default_target_lang"]
    # Nothing chosen in Settings: learn it from the languages this client keeps ordering, so a repeat client
    # lands on their usual pair instead of the site default. Only ever a starting point they can change.
    languages_learned = False
    if not user["default_source_lang"] and not user["default_target_lang"]:
        usual = db.usual_languages(user["client_scope_id"])
        if usual and usual[0] in LANGUAGE_NAMES:
            reorder_form_values["source_lang"] = usual[0]
            if usual[1]:
                reorder_form_values["target_lang"] = usual[1]
            languages_learned = True
    if user["default_service_level"]:
        reorder_form_values["service_level"] = user["default_service_level"]
    if user["default_rush"]:
        reorder_form_values["rush"] = "1"
    if user["default_addon_video"]:
        reorder_form_values["addon_video_deliverables"] = "1"
    if reorder_youtube_url:
        reorder_form_values["youtube_url"] = reorder_youtube_url
        reorder_form_values["source_lang"] = reorder_source_lang or "sw"
    return templates.TemplateResponse(request, "client_dashboard.html", {
        "user": user, "orders": order_list["orders"], "order_list": order_list, "unread": unread, "anthropic_available": anthropic_available,
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "review_tiers": billing.REVIEW_TIERS,
        "price_card": billing.price_card_for(db.get_billing_market(user["client_scope_id"])),
        "default_review_tier": "ai",          # Instant AI is the default for every account (older ones used to open on Human review, so their orders waited for staff)
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "form_values": reorder_form_values, "languages_learned": languages_learned and not reorder_youtube_url,
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["client_scope_id"]),
        "youtube_pending_imports": db.list_pending_imports(user["client_scope_id"]),
        "show_tour": not user["onboarding_tour_seen_at"],
        "premium_voice_available": _elevenlabs_configured(),
        "audio_cleanup_available": _audio_cleanup_available(), "audio_cleanup_ai_rate": billing.AUDIO_CLEANUP_AI_RATE,
        "studio_available": _studio_available(),
    })


@app.post("/client/tour/seen")
def client_tour_seen(request: Request):
    """Fired once, whether the tour was finished or skipped - either way
    it's real, informed "don't show me this again", not just a page
    unload. Never blocks on this - a client's dashboard doesn't wait on
    it, see static/tour.js."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.set_tour_seen(user["id"])
    return JSONResponse({"ok": True})


def _client_dashboard_error(request: Request, user, error: str, form_values: dict | None = None,
                             exception_context: str | None = None):
    """Shared by every failure path in create_order() below - keeps the
    context (orders, unread badges, tier availability) consistent instead
    of each error branch building its own slightly-different version.
    form_values, when given, is what the client actually submitted - see
    create_order()'s own comment on why this gets threaded back through
    instead of every field silently resetting to its default.
    exception_context, when given, shows a real "Think this is a mistake?"
    trigger under the error - only set it for limits a staff-granted
    exception can actually lift (see grant_trusted_submitter), not for
    every error message on this page."""
    if getattr(request.state, "api_user", None) is not None:
        raise ApiOrderError(error)
    subscription = db.get_subscription_current(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    return templates.TemplateResponse(request, "client_dashboard.html", {
        "user": user,
        "orders": _orders_view(db.list_orders_for_client(user["client_scope_id"]))["orders"],
        "order_list": _orders_view(db.list_orders_for_client(user["client_scope_id"])),
        "unread": db.unread_order_ids(user["client_scope_id"], include_internal=False),
        "anthropic_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "review_tiers": billing.REVIEW_TIERS,
        "price_card": billing.price_card_for(db.get_billing_market(user["client_scope_id"])),
        "default_review_tier": "ai",          # Instant AI is the default for every account (older ones used to open on Human review, so their orders waited for staff)
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "error": error,
        "exception_context": exception_context,
        "form_values": form_values or {},
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["client_scope_id"]),
        "youtube_pending_imports": db.list_pending_imports(user["client_scope_id"]),
        "show_tour": not user["onboarding_tour_seen_at"],
        "premium_voice_available": _elevenlabs_configured(),
        "audio_cleanup_available": _audio_cleanup_available(), "audio_cleanup_ai_rate": billing.AUDIO_CLEANUP_AI_RATE,
        "studio_available": _studio_available(),
    })


@app.post("/client/request-exception")
def client_request_exception(request: Request, context: str = Form(...), note: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    db.create_exception_request(user["id"], context, note.strip() or None)
    notifications.notify_staff(
        "Kauli: a client is asking for a limit exception",
        f"{user['display_name']} ({user['email']}) hit a limit ({context}) and asked to proceed.\n\n"
        + (f"Their note: {note.strip()}\n\n" if note.strip() else "")
        + "Review on /staff/exceptions.",
    )
    return RedirectResponse("/client?exception_requested=1", status_code=303)


@app.get("/feedback/{context}/{user_id}/{rating}", response_class=HTMLResponse)
def record_feedback(request: Request, context: str, user_id: str, rating: str):
    """One click from an email link (see _queue_first_payment_message) - no
    login required, same as any other real-world "rate your experience"
    email link. Not a security boundary: user_id here is trusted the same
    way an order ID or receipt ID is elsewhere in this app - a real,
    non-sequential id, not something worth signing for a satisfaction
    rating. 'needs_work' is the actual churn-risk signal, so staff get a
    real alert for it immediately rather than finding it later in a
    report."""
    if rating not in ("great", "good", "needs_work"):
        return HTMLResponse("Unknown rating.", status_code=404)
    # Public, unauthenticated, no login wall - same shape as the callback/
    # forgot-password guards above. Real abuse case this closes: hitting a
    # "needs_work" link on repeat to spam staff with churn-risk alerts.
    allowed, retry_after = rate_limit.check(f"feedback:{rate_limit.client_ip(request)}", limit=20, window_s=600)
    if not allowed:
        return HTMLResponse("Too many requests - try again shortly.", status_code=429)
    user = db.get_user(user_id)
    if not user:
        return HTMLResponse("Account not found.", status_code=404)
    db.record_client_feedback(user_id, context, rating)
    if rating == "needs_work":
        notifications.notify_staff(
            f"Kauli: {user['display_name']} said their first order needs work",
            f"{user['display_name']} ({user['email']}) rated their first-payment experience "
            f"\"Needs work\" - worth reaching out before this turns into churn.",
        )
    return HTMLResponse(
        "<div style=\"font-family:sans-serif; max-width:480px; margin:80px auto; text-align:center;\">"
        "<h1>Thanks for the feedback</h1><p>The team reads every one of these personally.</p></div>"
    )


@app.post("/client/youtube-watches")
def client_add_youtube_watch(request: Request, channel_url: str = Form(""), label: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if not youtube_poll.youtube_polling_configured():
        return RedirectResponse("/client?yt_error=not_configured", status_code=303)
    # Each add below calls out to the real YouTube Data API, which is
    # quota-limited on our side, not just rate-limited on theirs - bound
    # how fast one account can burn through that shared quota.
    allowed, retry_after = rate_limit.check(f"ytwatch:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return RedirectResponse("/client?yt_error=rate_limited", status_code=303)
    parsed = youtube_poll.extract_channel_or_playlist_id(channel_url.strip())
    if not parsed:
        return RedirectResponse("/client?yt_error=unrecognized", status_code=303)
    try:
        playlist_id = youtube_poll.resolve_uploads_playlist(*parsed)
    except Exception as exc:  # noqa: BLE001 - surface a real reason, not a silent failure
        api_log.warning("youtube watch resolve failed", extra={"error": str(exc)})
        return RedirectResponse("/client?yt_error=resolve_failed", status_code=303)
    db.create_youtube_watch(user["id"], playlist_id, label.strip() or None)
    return RedirectResponse("/client?yt_added=1", status_code=303)


@app.post("/client/youtube-watches/{watch_id}/remove")
def client_remove_youtube_watch(request: Request, watch_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    watches = {w["id"]: w for w in db.list_youtube_watches(client_id=user["client_scope_id"])}
    if watch_id not in watches:
        return HTMLResponse("Not found.", status_code=404)
    db.set_youtube_watch_active(watch_id, False)
    return RedirectResponse("/client", status_code=303)


@app.post("/client/youtube-imports/{import_id}/dismiss")
def client_dismiss_youtube_import(request: Request, import_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    # No ownership check needed beyond "is a client" - the import_id itself
    # isn't guessable (uuid4 hex) and dismissing someone else's has no
    # effect beyond a wasted request; list_pending_imports already scopes
    # what each client actually sees.
    db.set_pending_import_status(import_id, "dismissed")
    return RedirectResponse("/client", status_code=303)


class _LocalFileAdapter:
    """Makes an already-downloaded local file (one that arrived via a
    presigned direct-to-R2 upload, see r2_uploads.py) look enough like a
    FastAPI UploadFile that it passes straight into
    upload_security.validate_media_upload/stream_save_with_limits
    unchanged - only `.file` and `.filename` are ever touched by that
    pipeline. This is deliberate: an R2-sourced upload gets the EXACT
    same 7-step validation (extension check, size cap, magic-byte sniff,
    ffprobe, ClamAV, content-safety scan, metadata strip) as one posted
    straight to this app - never a second, parallel, potentially
    drifting copy of that logic for the R2 path."""

    def __init__(self, path: Path, filename: str):
        self.file = open(path, "rb")
        self.filename = filename


@app.post("/client/wizard-event")
def client_wizard_event(request: Request, step: str = Form(...)):
    """Real friction signal, nothing else in the app already covers: which
    step of the order wizard a client actually reached before leaving,
    logged from static/client_wizard.js's showStep()/finalizeOrder().
    Fire-and-forget by design (sendBeacon/no-op on failure on the JS
    side) - a dropped or delayed log entry should never block or slow
    down the wizard itself. Staff-only aggregate view: db.client_funnel_
    stats, surfaced on /staff/ops. Silently accepts an unknown step
    string rather than 400ing - a future wizard step added on the
    frontend shouldn't need a matching backend change just to not error."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"ok": False}, status_code=401)
    step = (step or "").strip()[:20]
    if not step:
        return JSONResponse({"ok": False}, status_code=400)
    db.log_wizard_step(user["client_scope_id"], step)
    return JSONResponse({"ok": True})


@app.post("/client/orders/presign-upload")
def client_presign_upload(request: Request, filename: str = Form(...), content_type: str = Form("")):
    """Step 1 of the direct-to-R2 upload flow: issue a short-lived,
    write-only URL for exactly one new object key, so the client's
    browser can PUT the raw file straight to R2 - never through this
    app, never through the Cloudflare Tunnel it sits behind. That
    matters because Cloudflare's own documented limit for a
    Tunnel-proxied hostname is 100MB per request body (Free/Pro plan);
    a bigger file posted the old way could stall before ever reaching
    this app at all (confirmed live against a real stuck client
    upload). The extension is checked here, before any URL is handed
    out - the size itself still gets the real, authoritative check
    later, once the file is actually on disk (see create_order /
    client_resume_order), because R2 has no way to enforce our app's
    byte cap on the client's raw PUT."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    if not r2_uploads.r2_configured():
        return JSONResponse({"error": "Direct upload isn't configured on this server yet."}, status_code=503)
    ext = Path(filename or "").suffix.lower()
    if ext not in upload_security.ALLOWED_MEDIA_EXTENSIONS:
        allowed = ", ".join(sorted(upload_security.ALLOWED_MEDIA_EXTENSIONS))
        return JSONResponse({"error": f"That file type isn't accepted. Allowed: {allowed}"}, status_code=400)
    key = r2_uploads.new_upload_key(filename)
    put_url = r2_uploads.generate_presigned_put(key, content_type)
    if not put_url:
        return JSONResponse({"error": "Couldn't prepare the upload right now - try again shortly."}, status_code=502)
    return JSONResponse({"upload_key": key, "put_url": put_url})


def _resolve_uploaded_audio(upload_key: str, order_upload_dir: Path, client_id: str = ""):
    """Step 2: the file the client already PUT to R2 gets pulled down to
    local disk and run through the real validation pipeline - see
    _LocalFileAdapter. Always deletes the R2 staging object afterward
    (success or failure) - it's transient staging, not permanent
    storage; the real, permanent copy is the local file, same as any
    other order's audio_path. Raises upload_security.UploadRejected on
    any hard failure, exactly like the direct-upload path does."""
    if upload_key.startswith("import:"):
        # A file the customer picked from Google Drive, OneDrive or Dropbox (webapp/cloud_import.py): already on this server in
        # a private folder of theirs, so it only needs the same checks as any other upload.
        staged = cloud_import.staged_file(upload_key, client_id)
        if staged is None:
            raise upload_security.UploadRejected("That import wasn't found - it may have expired. Please import the file again.")
        original_filename = cloud_import.display_name(upload_key)
        try:
            adapter = _LocalFileAdapter(staged, original_filename)
            try:
                audit = upload_security.validate_media_upload(adapter, order_upload_dir / "placeholder", original_filename)
            finally:
                adapter.file.close()
            return audit, original_filename
        finally:
            staged.unlink(missing_ok=True)
    size = r2_uploads.head_object_size(upload_key)
    if size is None:
        raise upload_security.UploadRejected(
            "That upload wasn't found - it may have expired. Please try uploading again.")
    if size > upload_security.MAX_UPLOAD_BYTES:
        r2_uploads.delete_object(upload_key)
        raise upload_security.UploadRejected(
            f"File is larger than the {upload_security.MAX_UPLOAD_BYTES // (1024 * 1024 * 1024)}GB limit.")
    original_filename = upload_key.rsplit("/", 1)[-1]
    staged_path = order_upload_dir / f"_r2_staged_{uuid.uuid4().hex}{Path(original_filename).suffix}"
    if not r2_uploads.download_object(upload_key, staged_path):
        raise upload_security.UploadRejected(
            "Couldn't retrieve that upload - please try again.")
    try:
        adapter = _LocalFileAdapter(staged_path, original_filename)
        try:
            audit = upload_security.validate_media_upload(adapter, order_upload_dir / "placeholder", original_filename)
        finally:
            adapter.file.close()
        return audit, original_filename
    finally:
        staged_path.unlink(missing_ok=True)
        r2_uploads.delete_object(upload_key)


@app.post("/client/orders")
def create_order(
    request: Request,
    audio: UploadFile | None = File(None),
    upload_key: str = Form(""),
    youtube_url: str = Form(""),
    source_lang: str = Form("sw"),
    target_lang: str = Form(""),
    service_level: str = Form("transcribe"),
    addon_video_deliverables: str = Form(""),
    addon_human_voice_over: str = Form(""),
    addon_premium_voice: str = Form(""),
    premium_voice_id: str = Form(""),
    addon_audio_cleanup: str = Form(""),
    instr_speaker_ids: str = Form(""),
    instr_verbatim_level: str = Form("clean_read"),
    instr_transcribe_lyrics: str = Form(""),
    instr_use_italics: str = Form(""),
    instr_existing_subs: str = Form("ignore"),
    instr_no_audio: str = Form("tag"),
    instr_wrong_language: str = Form("tag"),
    instr_instrumental_only: str = Form("tag"),
    instr_notes: str = Form(""),
    style_guide: UploadFile | None = File(None),
    idempotency_key: str = Form(""),
    folder_name: str = Form(""),
    voice_clone_consent: str = Form(""),
    rush: str = Form(""),
    review_tier: str = Form(""),
    reuse_order_id: str = Form(""),
):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    # Every rejection below re-renders this same form (see
    # _client_dashboard_error) - without this, a client who filled out all
    # three steps and hit a validation error on the last one got bounced
    # back to a totally blank step 1, language/service/instructions and
    # all, and had to redo the whole wizard just to fix the one thing that
    # was wrong. The uploaded file itself can't be restored this way
    # (browsers won't let a server pre-fill a file input) - that's the one
    # real re-do left after this.
    form_values = {
        "youtube_url": youtube_url, "source_lang": source_lang, "target_lang": target_lang,
        "service_level": service_level, "addon_video_deliverables": addon_video_deliverables,
        "addon_human_voice_over": addon_human_voice_over,
        "addon_premium_voice": addon_premium_voice, "premium_voice_id": premium_voice_id,
        "addon_audio_cleanup": addon_audio_cleanup,
        "instr_speaker_ids": instr_speaker_ids, "instr_verbatim_level": instr_verbatim_level,
        "instr_transcribe_lyrics": instr_transcribe_lyrics, "instr_use_italics": instr_use_italics,
        "instr_existing_subs": instr_existing_subs, "instr_no_audio": instr_no_audio,
        "instr_wrong_language": instr_wrong_language, "instr_instrumental_only": instr_instrumental_only,
        "instr_notes": instr_notes, "folder_name": folder_name,
        "voice_clone_consent": voice_clone_consent, "rush": rush, "review_tier": review_tier,
    }
    # Real ASR/MT/TTS work behind every accepted submission - a much
    # tighter budget than the global per-IP limit above, keyed on the
    # account rather than IP (a legitimate client could switch networks
    # mid-session; the account is what actually owns the processing cost).
    # A staff-granted trusted_submitter window (see grant_trusted_submitter)
    # raises this rather than removing it outright - still a real ceiling,
    # just one sized for a legitimate high-volume client instead of 10/10min.
    is_trusted = bool(user["trusted_submitter_until"]) and user["trusted_submitter_until"] > time.time()
    submit_limit = 60 if is_trusted else 10
    allowed, retry_after = rate_limit.check(f"submit:{user['id']}", limit=submit_limit, window_s=600)
    if not allowed:
        return _client_dashboard_error(
            request, user, f"Too many submissions recently - try again in {retry_after // 60 + 1} minute(s).",
            form_values=form_values, exception_context="order_submission_rate_limit")
    # Checked before anything else touches disk or billing - a slow
    # connection + an impatient double-click on Submit used to mean two
    # real uploads and two real charges for one file. The client sends the
    # same key on a resubmit, so this just returns the order that's
    # already there instead of creating a second one.
    if idempotency_key.strip():
        existing = db.get_order_by_idempotency_key(user["client_scope_id"], idempotency_key.strip())
        if existing:
            return RedirectResponse(f"/client/orders/{existing['id']}", status_code=303)
    if service_level not in billing.SERVICE_LEVELS:
        return _client_dashboard_error(request, user, "Unknown service level.", form_values=form_values)
    if source_lang not in LANGUAGE_NAMES:
        return _client_dashboard_error(request, user, "Unknown source language.", form_values=form_values)
    # "Same language" (or nothing chosen) means a plain transcript. Any OTHER language means the client wants the words in that language,
    # so a Transcription order with different languages is taken as a Transcription + translation order, and priced as one.
    if target_lang in ("", "same"):
        target_lang = source_lang
    if service_level == "transcribe" and target_lang != source_lang:
        service_level = "translate"
        form_values["service_level"] = "translate"
    form_values["target_lang"] = target_lang
    if target_lang not in LANGUAGE_NAMES or target_lang == "ki":
        return _client_dashboard_error(request, user, "Unknown target language.", form_values=form_values)
    # Market pricing: the price card is the TEAM OWNER's (fixed at signup,
    # staff-changeable) - never something the submitter picks per order.
    # review_tier is the one thing they do pick: 'ai' (instant, no human
    # review) or 'human' (the original reviewed flow). A missing/unknown
    # value means the old behaviour (human), so any older client of this
    # endpoint keeps working exactly as before.
    billing_market = db.get_billing_market(user["client_scope_id"])
    # Studio = a human-reviewed order that also gets ElevenLabs' best: audio clean-up, Scribe recognition with
    # speaker labels and, on a dub, a premium voice. It is expressed as the existing add-ons, so every part is
    # priced, refunded and guarded exactly like the same add-on bought on its own. English and Swahili only:
    # those are the languages with both human reviewers and ElevenLabs support (nothing speaks Kikuyu).
    studio = review_tier == "studio"
    if studio:
        review_tier = "human"
        if service_level != "dub":
            return _client_dashboard_error(request, user, "Studio is made for dubbing. For a transcript or translation, choose Human-reviewed.", form_values=form_values)
        if not _studio_available():
            return _client_dashboard_error(request, user, "The Studio tier isn't available right now.", form_values=form_values)
        if source_lang not in _studio_languages() or target_lang not in _studio_languages():
            return _client_dashboard_error(request, user, "Studio isn't available for that language pair.", form_values=form_values)
        addon_audio_cleanup = "1"
        if service_level == "dub":
            addon_premium_voice = "1"
    if review_tier not in billing.REVIEW_TIERS:
        review_tier = "human"
    if source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # A person has to transcribe these by hand (no ASR model) - there is
        # no such thing as an instant, no-human version of that.
        review_tier = "human"
    # Instant AI can transcribe the ~74 wider languages and, when Lara
    # Translate is connected, translate between them. Dubbing and human review
    # stay with Swahili, Kikuyu and English (there is no reviewer or voice for
    # the rest).
    ai_translation_ok = _lara_configured()
    extras_on = _review_extra_languages()
    # Chinese, German and Spanish may also be human-reviewed (outside contractors) when switched on.
    if source_lang in AI_ONLY_LANGUAGES and not (source_lang in extras_on and review_tier == "human"):
        allowed_services = ("transcribe", "translate") if ai_translation_ok else ("transcribe",)
        if service_level not in allowed_services or review_tier != "ai":
            what = "transcription and translation" if ai_translation_ok else "transcription"
            return _client_dashboard_error(
                request, user,
                f"{AI_ONLY_LANGUAGES[source_lang]} is available as Instant AI {what} only - choose "
                "'Instant AI' and " + ("'Transcription only' or 'Transcription + translation'" if ai_translation_ok
                                       else "'Transcription only'") +
                ", or pick Swahili, English or Kikuyu for dubbing and human review.", form_values=form_values)
    if target_lang not in ("en", "sw"):
        # A wider-language TARGET is Instant AI translation only (never a dub,
        # never human-reviewed), and never Kikuyu without a human in the loop -
        # except the human-reviewed extras (Chinese, German, Spanish) when switched on.
        extra_target_ok = (target_lang in extras_on and review_tier == "human" and service_level in ("translate", "dub"))
        if not extra_target_ok and not (target_lang in AI_ONLY_LANGUAGES and service_level == "translate"
                                        and review_tier == "ai" and ai_translation_ok):
            return _client_dashboard_error(
                request, user,
                f"Translating into {LANGUAGE_NAMES[target_lang]} is only available as Instant AI "
                "'Transcription + translation'. For dubbing and human review choose English or Swahili.",
                form_values=form_values)
    if service_level != "transcribe" and target_lang == source_lang:
        return _client_dashboard_error(request, user, "Choose a different target language than the source.",
                                        form_values=form_values)
    addons = ["video_deliverables"] if addon_video_deliverables else []
    # Only meaningful on a full dub - there's no voice to record on a
    # transcription/translation-only order. Enforced here, not just by
    # hiding the checkbox client-side - a submitted form field for the
    # wrong service level is simply ignored, not trusted. Also human-tier
    # only: a human voice actor is a human step, which the instant tier skips.
    wants_human_voice_over = bool(addon_human_voice_over) and service_level == "dub" and review_tier == "human"
    if wants_human_voice_over:
        addons.append("human_voice_over")
    # Premium AI voice (ElevenLabs): a full dub into English or Swahili only - no ElevenLabs model
    # speaks Kikuyu - using one of its premade voices. Enforced here, not just in the wizard.
    wants_premium_voice = bool(addon_premium_voice)
    # Premium and Enterprise include realistic voices: a dub into English or Swahili gets one automatically (the customer can still pick the
    # voice). It is never a reason to refuse an order: if ElevenLabs is unavailable or out of credits the standard voice takes it (see below).
    auto_voice = False
    if (not wants_premium_voice and service_level == "dub" and target_lang in _studio_languages() and _elevenlabs_configured()
            and billing.effective_plan(user, db.get_subscription_current(user["client_scope_id"])) in billing.ADDONS["premium_voice"]["included_in"]):
        voices = _premium_voices()
        if voices:
            wants_premium_voice, auto_voice, addon_premium_voice, premium_voice_id = True, True, "1", voices[0]["id"]
    if wants_premium_voice:
        if service_level != "dub" or target_lang not in _studio_languages():
            return _client_dashboard_error(
                request, user, "Premium voice is available on a full dub into English or Swahili.", form_values=form_values)
        if not _elevenlabs_configured():
            return _client_dashboard_error(request, user, "Premium voices aren't available right now.", form_values=form_values)
        if premium_voice_id not in {v["id"] for v in _premium_voices()}:
            return _client_dashboard_error(request, user, "Choose one of the listed premium voices.", form_values=form_values)
        addons.append("premium_voice")
    wants_audio_cleanup = bool(addon_audio_cleanup)
    if wants_audio_cleanup:
        if not (_audio_cleanup_available() and (review_tier != "ai" or billing.audio_cleanup_ai_margin_ok())):
            return _client_dashboard_error(request, user, "Audio clean-up isn't available right now.", form_values=form_values)
        addons.append("audio_cleanup")
    if studio:
        addons.append("premium_asr")
    # Human review by an outside specialist for Chinese/German/Spanish, only where it still clears the cost floor
    # for this account's pricing region; a dub into one of them also needs a premium voice (no standard voice speaks it).
    if review_tier == "human" and (source_lang in extras_on or target_lang in extras_on):
        if not _extra_review_ok(billing_market, service_level):
            return _client_dashboard_error(
                request, user, "Human review for that language isn't available in your pricing region yet. "
                               "Instant AI is, or contact us.", form_values=form_values)
        if service_level == "dub" and target_lang in extras_on and not wants_premium_voice:
            return _client_dashboard_error(
                request, user, f"Dubbing into {LANGUAGE_NAMES[target_lang]} needs a premium voice: choose Studio, "
                               "or add Premium AI voice.", form_values=form_values)
        addons.append("review_extra")
    if source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # Not a checkbox the client toggles - this is a real cost every
        # service level incurs for this language (every SERVICE_LEVELS tier
        # needs the ASR stage), so it's applied automatically the same way
        # the service level's own rate is, not offered as an opt-in upsell.
        addons.append("manual_transcription")
    if instr_verbatim_level not in db.VERBATIM_LEVELS:
        instr_verbatim_level = "clean_read"
    if review_tier == "ai":
        # These are instructions for a human editor; an Instant AI order has none, so whatever the form carried is dropped.
        instr_verbatim_level, instr_existing_subs, instr_no_audio, instr_wrong_language, instr_instrumental_only = "clean_read", "ignore", "tag", "tag", "tag"
        instr_transcribe_lyrics, instr_use_italics = "", ""
        if not idempotency_key.startswith("meeting-"):      # the meeting notetaker identifies its own speakers; a customer cannot ask for labels
            instr_speaker_ids = ""                  # speaker labels (NARRATOR:, SPEAKER 1:) are an editor's work: Instant AI never gets them
    if instr_existing_subs not in db.EXISTING_SUBS_OPTIONS:
        instr_existing_subs = "ignore"
    if instr_no_audio not in db.CONTENT_HANDLING_OPTIONS:
        instr_no_audio = "tag"
    if instr_wrong_language not in db.CONTENT_HANDLING_OPTIONS:
        instr_wrong_language = "tag"
    if instr_instrumental_only not in db.CONTENT_HANDLING_OPTIONS:
        instr_instrumental_only = "tag"

    subscription = db.get_subscription_current(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    plan_mt = billing.PLANS[plan]["mt"]
    if plan_mt == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        plan_mt = "local"  # degrade gracefully rather than block a paying client

    order_id = uuid.uuid4().hex[:10]
    order_upload_dir = UPLOAD_DIR / order_id

    # No video file to scan for a YouTube-sourced order - _download_youtube
    # only ever fetches audio (see its own docstring), so
    # content_safety_flagged just stays False for that path.
    content_safety_flagged, content_safety_detail = False, None
    youtube_url = youtube_url.strip()
    youtube_video_id = None
    # "Do more with this file": a new order made from a file the client already sent us, with no second upload (see repurpose.py).
    # A video that came from YouTube is simply fetched again; an uploaded file is linked from the earlier order's folder.
    reuse_src = None
    reuse_order_id = reuse_order_id if isinstance(reuse_order_id, str) else ""        # the API and meeting code call this directly, without the form field
    if reuse_order_id.strip() and not youtube_url and not (audio is not None and audio.filename) and not upload_key.strip():
        reuse_src = db.get_order(reuse_order_id.strip())
        if not reuse_src or reuse_src["client_id"] != user["client_scope_id"]:
            return _client_dashboard_error(request, user, "We could not find that earlier order. Upload the file again to continue.", form_values=form_values)
        if reuse_src["source_youtube_id"]:
            youtube_url, reuse_src = f"https://www.youtube.com/watch?v={reuse_src['source_youtube_id']}", None
        elif not reuse_src["audio_path"] or not Path(reuse_src["audio_path"]).is_file():
            return _client_dashboard_error(request, user, "That file is no longer stored. Upload it again to continue.", form_values=form_values)
    if reuse_src is not None:
        order_upload_dir.mkdir(parents=True, exist_ok=True)
        audio_path = order_upload_dir / Path(reuse_src["audio_path"]).name
        try:
            os.link(reuse_src["audio_path"], audio_path)                         # no second copy on disk
        except OSError:
            shutil.copy2(reuse_src["audio_path"], audio_path)
        original_filename = reuse_src["original_filename"]
        content_safety_flagged = bool(reuse_src["content_safety_flagged"])       # it was scanned when it first arrived
        content_safety_detail = "carried over from an earlier order" if content_safety_flagged else None
    elif youtube_url:
        try:
            audio_path, original_filename, youtube_video_id = _download_youtube(youtube_url, order_upload_dir)
        except Exception as exc:
            api_log.warning("youtube_fetch_failed", extra={"trace_id": getattr(request.state, "trace_id", None),
                                                           "url": youtube_url[:200], "error": str(exc)[:500]})
            return _client_dashboard_error(request, user, explain_youtube_failure(exc), form_values=form_values)
    elif audio is not None and audio.filename:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            audit = upload_security.validate_media_upload(audio, order_upload_dir / "placeholder", audio.filename)
        except upload_security.UploadRejected as exc:
            db.log_upload_audit(user["id"], None,
                                 {"original_filename": audio.filename, "rejected": True, "reject_reason": str(exc)},
                                 client_ip, user_agent)
            return _client_dashboard_error(request, user, str(exc), form_values=form_values)
        db.log_upload_audit(user["id"], None, audit, client_ip, user_agent)
        audio_path = Path(audit["final_path"])
        original_filename = audio.filename
        content_safety_flagged = bool(audit.get("content_safety_flagged"))
        content_safety_detail = audit.get("content_safety_detail")
    elif upload_key.strip():
        # The file already landed in R2 via a presigned PUT (see
        # client_presign_upload) - this app never saw the raw bytes go
        # by, so the Cloudflare Tunnel's 100MB body cap never applied.
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            audit, original_filename = _resolve_uploaded_audio(upload_key.strip(), order_upload_dir, user["client_scope_id"])
        except upload_security.UploadRejected as exc:
            db.log_upload_audit(user["id"], None,
                                 {"original_filename": upload_key.strip(), "rejected": True, "reject_reason": str(exc)},
                                 client_ip, user_agent)
            return _client_dashboard_error(request, user, str(exc), form_values=form_values)
        db.log_upload_audit(user["id"], None, audit, client_ip, user_agent)
        audio_path = Path(audit["final_path"])
        content_safety_flagged = bool(audit.get("content_safety_flagged"))
        content_safety_detail = audit.get("content_safety_detail")
    else:
        return _client_dashboard_error(request, user, "Choose a file or paste a YouTube link.",
                                        form_values=form_values)

    try:
        minutes = probe_duration_minutes(str(audio_path))
    except Exception:
        return _client_dashboard_error(request, user,
            "Couldn't read that file's duration - it may not be a valid audio/video file.",
            form_values=form_values)

    # Nothing is transcribed twice: if this account already received a transcript of this same file (the "Do more with this file" button,
    # the same file sent again, or the same YouTube video), whatever is ordered now starts from that delivered transcript, with the
    # customer's own corrections, and goes straight to the new work (see transcript_reuse.py).
    source_sha = transcript_reuse.file_sha256(audio_path) if not youtube_video_id else None
    reuse_from = transcript_reuse.pick(user["client_scope_id"], source_lang, explicit=reuse_src, sha256=source_sha, youtube_id=youtube_video_id)
    if wants_audio_cleanup and not _audio_cleanup_capacity_ok(minutes):
        return _client_dashboard_error(
            request, user, "Audio clean-up is at capacity right now. Continue without it, or try again later.",
            form_values=form_values)
    if wants_premium_voice and not _premium_capacity_ok(minutes):
        if not auto_voice:
            return _client_dashboard_error(
                request, user, "Premium voices are at capacity right now. Choose the standard voice, or try again later.",
                form_values=form_values)
        wants_premium_voice = False                                  # included with the plan: the standard voice takes this one
        addons = [a for a in addons if a != "premium_voice"]

    # Validated before db.create_order() below, on purpose - a rejection
    # here needs to bail out cleanly with nothing written to the orders
    # table, not leave a half-created order behind because an optional
    # attachment failed a scan after the row already existed.
    style_guide_path, style_guide_filename = None, None
    if style_guide is not None and style_guide.filename:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            sg_audit = upload_security.validate_generic_upload(
                style_guide, order_upload_dir / "placeholder", style_guide.filename,
                upload_security.ALLOWED_STYLE_GUIDE_EXTENSIONS)
        except upload_security.UploadRejected as exc:
            db.log_upload_audit(user["id"], None,
                                 {"original_filename": style_guide.filename, "rejected": True, "reject_reason": str(exc)},
                                 client_ip, user_agent)
            return _client_dashboard_error(request, user, f"Style guide: {exc}", form_values=form_values)
        db.log_upload_audit(user["id"], None, sg_audit, client_ip, user_agent)
        style_guide_path = sg_audit["final_path"]
        style_guide_filename = style_guide.filename

    outdir = OUTPUT_DIR / order_id
    outdir.mkdir(parents=True, exist_ok=True)

    level = billing.SERVICE_LEVELS[service_level]
    if not level["asr"]:
        asr = "stub"
    elif source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        asr = "manual"  # no ASR model recognizes this language - see the constant's comment
    else:
        # Transkriptor first (real paid transcription, better on real
        # Kenyan Swahili than local Whisper - see kauli/providers/asr.py's
        # TranskriptorASR), with local faster-whisper as an automatic,
        # built-in fallback on any failure or plan-limit hit - that
        # fallback lives INSIDE the provider itself, not here, so this one
        # value is the whole policy. Falls straight to faster-whisper with
        # no attempt at all if no key is configured yet.
        asr = ("transkriptor" if (os.environ.get("TRANSKRIPTOR_API_KEY") or os.environ.get("TRANSKRIPTOR_API_KEY_BACKUP")
                                  or os.environ.get("TRANSKRIPTOR_API_KEY_BACKUP_2"))
               else "faster-whisper")
    if studio and level["asr"]:
        asr = "elevenlabs-scribe"                   # fell back to faster-whisper in the pipeline if ElevenLabs fails
    if not level["mt"]:
        mt = "stub"
    else:
        # See _pick_mt: Lara first, with the Kikuyu/Claude rule and the old
        # local/Azure guards (a Swahili-to-English-only local model must never
        # be fed another direction - confirmed live to produce nonsense)
        # kept as fallbacks for when Lara isn't connected.
        mt, mt_error = _pick_mt(source_lang, target_lang, plan_mt, review_tier, minutes=minutes)
        if mt_error:
            return _client_dashboard_error(request, user, mt_error, form_values=form_values)
    # Real bug this fixes: this used to be tts = "piper" if level["tts"]
    # else "stub", with zero regard for target_lang - so every en->sw
    # full-dub order was read by an ENGLISH Piper voice attempting
    # Swahili text (Piper has no real Swahili voice in this app's own
    # PIPER_VOICES set), not real Swahili speech at all. Same refuse-
    # rather-than-ship-it-wrong pattern as the Kikuyu MT case above:
    # Azure's real sw-KE-ZuriNeural/RafikiNeural voices are what this
    # actually needs (see kauli/providers/tts.py:AzureTTS) - if that
    # isn't configured yet, hold the order instead of silently
    # delivering garbled audio a client already paid for.
    if not level["tts"]:
        tts = "stub"
    elif wants_premium_voice:
        tts = "elevenlabs"
    elif provider_health.default_for("tts", source_lang, target_lang):
        tts = provider_health.default_for("tts", source_lang, target_lang)       # staff's default voice provider, when it can take this language
    elif target_lang == "sw":
        if not provider_health.usable("azure"):
            return _client_dashboard_error(request, user,
                "Swahili dubbing needs our voice provider connected first, which isn't done yet - "
                "contact us and we'll handle this one manually in the meantime.", form_values=form_values)
        tts = "azure"
    else:
        # our own free voice first; Azure only while Piper is set aside after a failure
        tts = "azure" if provider_health.blocked("piper") and provider_health.usable("azure") else "piper"

    # The monthly allowance covers Instant AI transcription on every plan and Instant AI translation on the paid
    # plans (billing.allowance_covers); a dub or a human-reviewed order pays the full rate from its first minute.
    #
    # Reserved atomically here (not just read via free_minutes_remaining)
    # so two near-simultaneous submissions can't both read the same "X
    # remaining" snapshot and each get the full free allowance applied -
    # see db.reserve_free_minutes's docstring for the actual race this
    # closes. Whatever's granted here is real and already spent the moment
    # this call returns, whether or not the rest of this order ends up
    # needing payment.
    if billing.allowance_covers(plan, service_level, review_tier):
        # (The free trial only ever buys the INSTANT AI tier now - it's
        # "see the AI transcription quality for yourself", and it must not
        # cost us a human reviewer's time.)
        # No card check: free minutes can be used straight away, and subscribing
        # to the Free plan (pay as you go, which charges for the order being
        # opened) is what unlocks downloads and editing.
        free_cap = billing.plan_included_minutes(plan) + (
            (subscription["bonus_minutes"] or 0.0) + billing._reward_minutes(subscription) if subscription else 0.0)
        free_minutes_for_this_order = db.reserve_free_minutes(user["client_scope_id"], minutes, free_cap)
    else:
        free_minutes_for_this_order = 0.0
    # A paid plan also includes some human-reviewed transcription minutes each month: they take the place of the per-minute human-review
    # charge for as many minutes as are left, and anything beyond them is priced at the normal human-review rate.
    human_included = 0.0
    if billing.human_allowance_covers(plan, service_level, review_tier):
        human_included = db.reserve_human_minutes(user["client_scope_id"], minutes, billing.plan_human_minutes(plan))
        free_minutes_for_this_order = human_included
    # Rush = the team manually jumping the review queue - meaningless for an
    # instant AI order, which has no queue.
    is_rush = bool(rush) and review_tier == "human"
    # Same double-spend concern as free minutes above, for prepaid credits:
    # reserve atomically rather than read-then-consume-later. The ceiling a
    # reservation could possibly need is a pure function of minutes/rate/
    # plan/discount, none of which touch the wallet - so it's safe to price
    # the order once with wallet_credits_available=0 just to learn that
    # ceiling, reserve up to it (see db.reserve_wallet_credits), then price
    # it again for real with whatever was actually granted.
    preliminary_cost = billing.order_cost_usd(minutes, service_level, plan, free_minutes_for_this_order,
                                               addons=addons, wallet_credits_available=0.0, rush=is_rush,
                                               market=billing_market, review_tier=review_tier)
    credits_ceiling = billing.usd_to_credits(preliminary_cost["gross_usd"] - preliminary_cost["discount_usd"])
    reserved_credits = db.reserve_wallet_credits(user["client_scope_id"], credits_ceiling)
    cost = billing.order_cost_usd(minutes, service_level, plan, free_minutes_for_this_order,
                                   addons=addons, wallet_credits_available=reserved_credits, rush=is_rush,
                                   market=billing_market, review_tier=review_tier)
    # A new customer's first Instant AI order is never held for payment: it is processed and delivered, they read the first part of the
    # transcript, and subscribing to the Free plan (pay as you go, charged for that order) unlocks the rest. What it would have cost is kept in the
    # breakdown. After this first order, anything beyond the free minutes is pay as you go as usual.
    if (cost["total_usd"] > 0 and review_tier == "ai" and not studio and service_level in ("transcribe", "translate")
            and minutes <= min(billing.FIRST_ORDER_FREE_MAX_MINUTES, onboarding.first_order_cap(db.get_user(user["client_scope_id"])))
            and not _plan_chosen(user["client_scope_id"]) and not db.list_orders_for_client(user["client_scope_id"])):
        cost = {**cost, "first_order_courtesy_usd": cost["total_usd"], "total_usd": 0.0}
    # order_cost_usd silently drops any addon the plan already includes -
    # reflect that back so we never store/charge for one that didn't apply.
    applied_addons = [line["key"] for line in cost["addons"]]
    # Free-tier only, never a wallet/real-money $0 order (those already
    # paid for their credits in a top-up) - this is what gates downloads
    # below and in order_detail.html. A rush order is never a free preview -
    # the surcharge alone (see billing.order_cost_usd) already makes
    # total_usd > 0, but spelled out here too for clarity.
    cost["human_minutes_included"] = round(human_included, 2)
    is_free_preview = ((cost["free_minutes_applied"] > 0 or bool(cost.get("first_order_courtesy_usd")))
                        and cost["credits_applied"] <= 0 and review_tier == "ai" and cost["total_usd"] <= 0 and not is_rush)

    # Free-allowance orders (the free plan, fully covered, Instant AI) go to the low-cost providers; everyone who pays keeps Transkriptor.
    free_covered = (plan == "free" and review_tier == "ai" and cost["free_minutes_applied"] > 0
                    and cost["billable_minutes"] <= 0 and cost["total_usd"] <= 0 and not studio)
    # The first thing a new account uploads, if it is short, is transcribed by Transkriptor whatever the plan: that is the
    # impression of our quality that wins the sign-up. Their later free minutes go to the low-cost providers, automatically.
    # (The order is not created yet, so an empty list means this is their first.)
    first_impression = (free_covered and minutes <= billing.FIRST_ORDER_QUALITY_MINUTES
                        and not db.list_orders_for_client(user["client_scope_id"]))
    # On a paid plan the first "quality minutes" of each month are Transkriptor's; the rest of the included allowance is served by
    # the low-cost providers, which is how thousands of included minutes stay affordable. Anything the customer pays for
    # per minute beyond the allowance stays on Transkriptor.
    used_before = (subscription["minutes_used_this_period"] or 0.0) if subscription else 0.0
    bulk_allowance = (plan != "free" and review_tier == "ai" and cost["free_minutes_applied"] > 0 and cost["billable_minutes"] <= 0
                      and not studio and used_before + minutes > billing.PLANS[plan].get("quality_minutes", 0))
    # Instant AI (free minutes included) goes to our own model first where it is accurate, then the low-cost providers (Groq, AssemblyAI),
    # with Transkriptor behind them; human-reviewed orders keep Transkriptor first.
    instant_ai = review_tier == "ai" and not studio
    asr = _route_asr(asr, source_lang=source_lang, free_covered=(free_covered and not first_impression) or bulk_allowance or instant_ai,
                     minutes=minutes)
    if reuse_from is not None:
        asr = "stub"                                  # no speech recognition will run: the earlier transcript is used

    db.create_order(
        order_id=order_id,
        client_id=user["client_scope_id"], original_filename=original_filename,
        audio_path=str(audio_path), source_lang=source_lang, target_lang=target_lang,
        tier=plan, asr=asr, mt=mt, tts=tts, outdir=str(outdir),
        source_youtube_id=youtube_video_id,
        idempotency_key=idempotency_key.strip() or None,
        folder_name=folder_name.strip() or None,
        wants_human_voice_over=wants_human_voice_over,
    )
    if reuse_from is not None or source_sha:
        db.set_order_reuse(order_id, reuse_from["id"] if reuse_from is not None else None, source_sha)
    if wants_premium_voice:
        db.set_dub_voice(order_id, f"elevenlabs:{premium_voice_id}", job_status=None)
    if youtube_video_id:
        # Closes the loop on the auto-import flow, if this happens to be
        # one of those videos - a real order now exists for it, so it's no
        # longer "pending".
        db.mark_pending_import_ordered(user["id"], youtube_video_id)
    db.set_order_billing(order_id, service_level, minutes, cost["total_usd"], addons=applied_addons,
                          cost_breakdown=cost, is_rush=is_rush, review_tier=review_tier,
                          billing_market=billing_market)
    if is_free_preview:
        db.set_order_free_preview(order_id, True)
    if content_safety_flagged:
        db.set_order_content_safety_flag(order_id, True, content_safety_detail)
    if voice_clone_consent:
        # Logged with the real submitting IP - a genuine, timestamped,
        # attributable action, not a silent default. See
        # staff_set_dub_voice's server-side check for the part that
        # actually enforces this - a client can grant this later too, see
        # client_grant_voice_clone_consent below.
        db.set_voice_clone_consent(order_id, request.client.host if request.client else None)
    if cost["credits_applied"] > 0:
        # Already deducted above by db.reserve_wallet_credits, atomically,
        # at the moment it was granted - not repeated here (see that call's
        # comment for the double-spend race this avoids). Just the
        # low-balance check left to do.
        if mailer.email_configured() and db.wallet_low_alert_needed(user["id"]):
            remaining = db.wallet_credits_remaining(user["client_scope_id"])
            name = (user["display_name"] or user["email"].split("@")[0]).strip()
            billing_url = f"{str(request.base_url).rstrip('/')}/client/billing"
            body = (
                f"Hi {name},\n\n"
                f"You're down to about {remaining:.0f} prepaid credits - just a heads up so an upcoming "
                f"order doesn't get held up waiting on a top-up.\n\n"
                "Questions? Just reply to this email.\n\n"
                f"Talk soon,\n{TEAM_NAME}"
            )
            inner = (
                f'<p style="margin:0 0 14px;">Hi {name},</p>'
                f'<p style="margin:0 0 14px;">You\'re down to about {remaining:.0f} prepaid credits - just '
                f'a heads up so an upcoming order doesn\'t get held up waiting on a top-up.</p>'
                f'<p style="margin:0 0 14px;">Questions? '
                f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">Reply to this email</a>.</p>'
                f'<p style="margin:0;">Talk soon,<br>{TEAM_NAME}</p>'
            )
            html = mailer.wrap_email_html(inner, cta_text="Top up your credits", cta_url=billing_url,
                                           base_url=str(request.base_url))
            mailer.send_email(user["email"], "Running low on prepaid Kauli credits", html, body)
            db.mark_wallet_low_alert_sent(user["id"])

    db.set_job_instructions(
        order_id,
        speaker_ids=bool(instr_speaker_ids), verbatim_level=instr_verbatim_level,
        transcribe_lyrics=bool(instr_transcribe_lyrics), use_italics=bool(instr_use_italics),
        existing_subs=instr_existing_subs, no_audio=instr_no_audio, wrong_language=instr_wrong_language,
        instrumental_only=instr_instrumental_only, notes=instr_notes.strip() or None,
        style_guide_path=style_guide_path, style_guide_filename=style_guide_filename,
    )
    # Custom event, not a GA4 e-commerce one - a submitted order isn't
    # revenue yet (it might be fully covered by the free allowance, or
    # still waiting on payment below); the real "purchase" event fires
    # once money actually clears (see _activate_payment).
    ga_events.send_event(ga_events.client_id_from_request(request), "order_submitted", {
        "service_level": service_level, "is_rush": is_rush, "value": round(cost["total_usd"], 2),
    })
    if cost["free_minutes_applied"] > 0 and not (user["is_guest"] if "is_guest" in user.keys() else 0):
        try:
            usage_prompts.check(user["client_scope_id"], str(request.base_url), _queue_and_send)
        except Exception as exc:  # noqa: BLE001 - a kind word must never stop an order
            api_log.warning("usage prompt failed", extra={"error": str(exc)})

    if cost["total_usd"] <= 0:
        # Fully covered by this month's free allowance - no payment step,
        # straight to processing, same as the old free experience. Usage
        # was already recorded above by db.reserve_free_minutes at the
        # moment it was granted - not repeated here.
        if review_tier == "human":  # an instant AI order has no human turnaround to promise or watch
            deadlines = tat.compute_deadlines(plan, service_level, minutes, rush=is_rush)
            db.set_order_deadlines(order_id, deadlines["start_at"], deadlines["internal_deadline_at"],
                                    deadlines["deadline_at"])
        worker.submit_job(order_id)
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)

    # Real cost beyond the free allowance - hold the order for payment.
    # worker.submit_job() is only ever called once a payment webhook
    # confirms it (see billing_checkout / the webhook handlers below).
    db.update_order_status(order_id, "pending_payment")
    return RedirectResponse(f"/client/orders/{order_id}/pay", status_code=303)


_PROGRESS_STEPS = [
    ("submitted", "Submitted"),
    ("processing", "Processing"),
    ("staff_review", "Staff review"),
    ("delivered", "Delivered"),
]
# Which real order.status values map onto which step - queued/processing
# are both "processing" from the client's point of view (the distinction
# between them is a staff/ops concern, not something worth a whole extra
# step here). editor_returned still counts as "staff review" - the order
# is back with a Kauli editor, not sitting on the client.
_STEP_FOR_STATUS = {
    "pending_payment": "submitted", "queued": "processing", "processing": "processing",
    "awaiting_review": "staff_review", "editor_returned": "staff_review",
    "returned_to_client": "staff_review", "ready_for_delivery": "delivered", "delivered": "delivered",
    "failed": "processing", "dead_letter": "processing",
}


_PROC_STAGES = [
    ("prepare", "Preparing your file"), ("clean", "Cleaning up the audio"), ("transcribe", "Turning speech into text"),
    ("translate", "Translating the text"), ("voice", "Creating the dubbed voice"), ("mix", "Mixing the final audio"),
    ("finalize", "Getting your files ready"),
]


MAX_EDIT_CHARS = 5000
MAX_EDITS_PER_SAVE = 600


def _rewrite_caption_files(order, job) -> None:
    """After a transcript edit: the subtitle and transcript files are rebuilt from the job so every download matches."""
    out = Path(order["outdir"])
    (out / f"subs_{order['target_lang']}.srt").write_text(to_srt(job), encoding="utf-8")
    (out / f"subs_{order['target_lang']}.vtt").write_text(to_vtt(job), encoding="utf-8")
    (out / f"transcript_{order['source_lang']}.srt").write_text(to_srt(job, source=True), encoding="utf-8")


@app.post("/client/orders/{order_id}/transcript")
def client_edit_transcript(request: Request, order_id: str, body: dict = Body(...)):
    """A client corrects their own Instant AI transcript. Edits are stored beside the original text (never over it), the caption
    files are rebuilt, and an empty text puts a line back as it was. Human-reviewed transcripts are not edited here: a person
    signed them off, so changes go through a revision request."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "sign in"}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _order_locked(order):
        return JSONResponse({"error": "Subscribe to the Free plan (pay as you go), or unlock this order, to edit your transcript."}, status_code=403)
    if order["review_tier"] != "ai":
        return JSONResponse({"error": "This transcript was checked by a person, so changes go through a revision request."}, status_code=403)
    if order["status"] not in db.TERMINAL_OK_STATUSES:
        return JSONResponse({"error": "The transcript is not ready yet."}, status_code=409)
    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "transcript not found"}, status_code=404)
    edits = body.get("edits")
    if not isinstance(edits, list) or len(edits) > MAX_EDITS_PER_SAVE:
        return JSONResponse({"error": "nothing to save"}, status_code=400)
    by_id = {s.segment_id: s for s in job.segments if s.segment_type == "speech"}
    same_language = order["service_level"] == "transcribe" or order["source_lang"] == order["target_lang"]
    changed = 0
    for e in edits:
        if not isinstance(e, dict) or str(e.get("segment_id")) not in by_id:
            continue
        seg = by_id[str(e["segment_id"])]
        texts = {}
        for field in ("source", "target"):
            if field in e:
                if not isinstance(e[field], str) or len(e[field]) > MAX_EDIT_CHARS:
                    return JSONResponse({"error": "a line is too long to save"}, status_code=400)
                texts[field] = " ".join(e[field].split())
        before = (seg.source_final_text, seg.final_text)
        if same_language and texts:
            text = texts.get("source", texts.get("target"))
            seg.source_edited_transcript = text or None
            seg.edited_text = text or None
        else:
            if "source" in texts:
                seg.source_edited_transcript = texts["source"] or None
                if "target" not in texts and seg.source_final_text != before[0]:
                    seg.translation_stale = True            # the translation no longer matches the text beside it
            if "target" in texts:
                seg.edited_text = texts["target"] or None
                seg.translation_stale = False
        if (seg.source_final_text, seg.final_text) != before:
            changed += 1
    if changed:
        _rewrite_caption_files(order, job)
        job.save(str(Path(order["outdir"]) / "manifest.json"))
        db.set_order_ai_summary(order_id, "")               # the summaries are written again, on request, from the corrected text
        db.clear_order_summaries(order_id)
    return JSONResponse({"ok": True, "changed": changed}, headers={"Cache-Control": "no-store"})


def _speaker_lines(job) -> dict:
    """Per speech line, what the customer reads: the speaker's name and the text without the name typed at its start. The name an
    editor added replaces the recogniser's SPK_n label; the stored text is untouched (the dubbing step still reads the tag)."""
    segs = [s for s in (getattr(job, "segments", None) or []) if getattr(s, "segment_type", "speech") == "speech"]
    src = label_speakers([(getattr(s, "speaker_id", None), getattr(s, "source_final_text", "") or "") for s in segs])
    tgt = label_speakers([(getattr(s, "speaker_id", None), getattr(s, "final_text", "") or "") for s in segs])
    return {getattr(s, "segment_id", ""): {"speaker": a[0] or b[0], "source": a[1], "source_tag": a[2], "target": b[1], "target_tag": b[2]}
            for s, a, b in zip(segs, src, tgt)}


def _caption_cues(job) -> list[dict]:
    """The live captions for the order preview. A job that cannot be laid out shows none, and never breaks the page."""
    try:
        return cue_dicts(job) if job and job.segments else []
    except (AttributeError, TypeError):
        return []


# The widely spoken languages the low-cost providers handle well. Anything else (Swahili and the smaller languages Kauli is known
# for) stays on Transkriptor even for free minutes: the quality there is the product.
CHEAP_ASR_LANGUAGES = {"en", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "tr", "zh", "ja", "ko", "hi", "ar", "sv", "da", "no",
                       "fi", "cs", "el", "hu", "ro", "uk", "id", "vi"}


OWN_ASR_DEFAULT_LANGUAGES = "en,es,fr,de,pt,it,nl"


def _own_asr_first(source_lang: str, minutes: float = 0.0) -> bool:
    """Our own transcription model goes first for the languages it was measured accurate in (kauli/providers/own_model_quality.json), on short
    and medium files. It is on unless KAULI_OWN_ASR=off or staff turned it off on the Providers page; KAULI_OWN_ASR_LANGUAGES overrides the
    measured list. Swahili and the rest stay with the providers built for them until the model is proven there."""
    if os.environ.get("KAULI_OWN_ASR", "").lower() in ("off", "0", "false", "no") or not provider_health.is_enabled("own"):
        return False
    raw = os.environ.get("KAULI_OWN_ASR_LANGUAGES")
    langs = {x.strip() for x in raw.split(",") if x.strip()} if raw else own_quality.asr_languages()
    return source_lang in langs and minutes <= OWN_ASR_MAX_MINUTES


def _route_asr(default_asr: str, *, source_lang: str, free_covered: bool, minutes: float = 0.0) -> str:
    """Which transcription provider (or chain of them) an order uses: see _route_asr_outside, with our own model in front where it is
    reliable. If our own model is not confident about a recording the chain hands it to the next provider; if every other provider
    fails, our own draft is delivered rather than lost."""
    pinned = provider_health.default_for("asr", source_lang, source_lang)          # staff's default on the Providers page, when it can take this job
    if pinned:
        return pinned
    outside = _route_asr_outside(default_asr, source_lang=source_lang, free_covered=free_covered)
    if not free_covered or not _own_asr_first(source_lang, minutes) or default_asr not in ("transkriptor", "faster-whisper"):
        return outside
    parts = outside[len("chain:"):].split(",") if outside.startswith("chain:") else [outside]
    parts = [p for p in parts if p not in ("faster-whisper", "own")]        # our own model is the local one now
    return "chain:" + ",".join(["own"] + parts)


def _route_asr_outside(default_asr: str, *, source_lang: str, free_covered: bool) -> str:
    """Which transcription provider (or chain of them) an order uses.

    Transkriptor stays the go-to for everyone who pays. The low-cost providers (Groq, AssemblyAI) take the orders that the
    free allowance covers in full, in the widely spoken languages, which keeps the free offer at a few cents a user; and they
    stand behind Transkriptor as the next stop if it fails or runs out, before local Whisper. KAULI_CHEAP_ASR=off turns
    all of it off. With no low-cost key configured nothing changes."""
    cheap = [p for p in cheap_providers_configured() if provider_health.is_enabled(p)]
    if default_asr == "transkriptor" and not provider_health.is_enabled("transkriptor"):
        default_asr = "faster-whisper"                      # staff turned Transkriptor off
    if os.environ.get("KAULI_CHEAP_ASR", "").lower() == "off" or not cheap or default_asr not in ("transkriptor", "faster-whisper"):
        return default_asr
    has_transkriptor = default_asr == "transkriptor"
    mode = provider_health.mode()                           # staff's choice on the Providers page: saver, balanced (today) or quality
    cheap_first = source_lang in CHEAP_ASR_LANGUAGES and mode != "quality" and (free_covered or mode == "saver")
    if cheap_first:
        chain = cheap + (["transkriptor"] if has_transkriptor else [])
    elif has_transkriptor:
        chain = ["transkriptor"] + cheap
    else:
        chain = cheap
    return "chain:" + ",".join(chain + ["faster-whisper"])


def _processing_state(order, lang: str = "en") -> dict:
    """What a running order is doing right now, for the live panel: the steps THIS order goes through (a transcript
    has fewer than a dub), which one is current, and a percentage that only ever moves forward. The percentage is
    the step reached plus a slow creep inside the step, capped so it never claims to be finished before it is."""
    level = order["service_level"] or "transcribe"
    wanted = {"prepare", "transcribe", "finalize"}
    if level in ("translate", "dub"):
        wanted.add("translate")
    if level == "dub":
        wanted |= {"voice", "mix"}
    if db.order_has_addon(order, "audio_cleanup"):
        wanted.add("clean")
    steps = [(k, ui_text.translate(lang, "stage_" + k)) for k, _ in _PROC_STAGES if k in wanted]
    order_keys = [k for k, _ in _PROC_STAGES]
    stage = (order["processing_stage"] if "processing_stage" in order.keys() else None) or "prepare"
    reached = order_keys.index(stage) if stage in order_keys else 0
    idx = next((i for i, (k, _) in enumerate(steps) if order_keys.index(k) >= reached), len(steps) - 1)
    started = (order["processing_stage_at"] if "processing_stage_at" in order.keys() else None) or order["created_at"] or time.time()
    elapsed = max(0.0, time.time() - started)
    queued = order["status"] == "queued"
    done = order["status"] not in ("queued", "processing")
    creep = 1 - math.exp(-elapsed / 45.0)
    pct = 2 if queued else int(min(97, max(3, (idx + 0.85 * creep) / len(steps) * 100)))
    out = [{"key": k, "label": label, "state": "done" if (done or i < idx) else ("current" if i == idx and not queued else "pending")}
           for i, (k, label) in enumerate(steps)]
    what = {"transcribe": "transcript", "translate": "translation", "dub": "dub"}.get(level, "transcript")
    return {"done": done, "status": order["status"], "queued": queued, "pct": 100 if done else pct, "steps": out,
            "what": what,
            "title": ui_text.translate(lang, "live_queue") if queued else ui_text.translate(lang, "live_title", what=ui_text.translate(lang, "what_" + what)),
            "sub": ui_text.translate(lang, "live_sub_q" if queued else "live_sub"),
            "elapsed_s": int(max(0, time.time() - (order["created_at"] or time.time())))}


@app.get("/client/orders/{order_id}/progress")
def client_order_progress(request: Request, order_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "sign in"}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_processing_state(order, _ui_lang(request, user)), headers={"Cache-Control": "no-store"})


def _order_progress_steps(status: str, review_tier: str = "human") -> list[dict]:
    """Real step states for order_detail.html's progress tracker - never
    a fixed animation, always derived from the actual order.status. Two
    real statuses don't fit a clean "moving forward" story and get their
    own marker instead of silently advancing: returned_to_client (needs
    the CLIENT's action, not further progress) and failed/dead_letter
    (something broke) both mark their step 'attention' instead of
    'current' or 'done'."""
    current_key = _STEP_FOR_STATUS.get(status, "submitted")
    # An instant AI order never goes through staff review - don't show a
    # "Staff review" step it skips. (If a content-safety flag DID route it to
    # review, its status is awaiting_review and the step is needed.)
    step_defs = _PROGRESS_STEPS
    if review_tier == "ai" and status not in ("awaiting_review", "editor_returned", "returned_to_client"):
        step_defs = [s for s in _PROGRESS_STEPS if s[0] != "staff_review"]
    current_idx = next((i for i, (k, _) in enumerate(step_defs) if k == current_key), 0)
    attention = status in ("returned_to_client", "failed", "dead_letter")
    # Ready (or delivered) is the end of the road for the client: every step, Delivered included, is ticked. An
    # Instant AI order gets there on its own the moment processing finishes - nobody has to approve it.
    finished = status in ("ready_for_delivery", "delivered")
    steps = []
    for i, (key, label) in enumerate(step_defs):
        if finished or i < current_idx:
            state = "done"
        elif i == current_idx:
            state = "attention" if attention else "current"
        else:
            state = "pending"
        steps.append({"key": key, "label": label, "state": state})
    return steps


@app.get("/client/orders/{order_id}", response_class=HTMLResponse)
def client_order_detail(request: Request, order_id: str, error: str | None = None, notice: str | None = None,
                         youtube_error: str | None = None, youtube_connected: str | None = None,
                         youtube_pushed: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if "is_guest" in user.keys() and user["is_guest"]:
        return templates.TemplateResponse(request, "guest_order.html", {
            "user": user, "order": order, "ready": order["status"] in ("ready_for_delivery", "delivered"),
            "failed": order["status"] in ("failed", "dead_letter"), "error": error, "notice": notice,
            "max_minutes": int(GUEST_TRIAL_MAX_MINUTES), "minutes": round(order["duration_minutes"] or 0.0, 1)})

    job = _load_job(order)
    # include_internal=False here is load-bearing, not decorative - this is
    # the one line standing between a client and staff-only notes.
    messages = db.list_messages(order_id, include_internal=False)
    db.mark_read(user["id"], order_id)

    outdir = Path(order["outdir"])
    # Real bug this fixes: the downloads list used to show every possible
    # deliverable (dubbed audio, subtitles) on EVERY order regardless of
    # what was actually purchased - a transcription-only client (mt=tts=
    # stub by design, see billing.SERVICE_LEVELS) would see "Dubbed audio"
    # and subtitle links for a translation/dub that never happened. Same
    # billing.SERVICE_LEVELS lookup already used to gate the Ereri stage-
    # picker and the workflow stepper - one source of truth for "what did
    # this order actually include."
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    # Same real entitlement/source checks _maybe_auto_render_video_
    # deliverables uses to decide whether to auto-generate these - lets
    # the page tell "still generating, check back" (entitled, has a real
    # video, just not rendered yet) apart from "not included" (never
    # true), instead of one generic "ask us" for both.
    has_video_deliverables = (billing.PLANS.get(order["tier"], {}).get("video_deliverables")
                               or db.order_has_addon(order, "video_deliverables"))
    has_video_source = is_video_file(order["audio_path"]) or bool(order["source_youtube_id"])
    return templates.TemplateResponse(request, "order_detail.html", {
        "user": user, "order": order, "job": job, "role": "client", "messages": messages,
        "has_translation": order_level["mt"], "has_dub": order_level["tts"],
        "has_video_deliverables": has_video_deliverables, "has_video_source": has_video_source,
        "source_is_video": (not order["source_youtube_id"]) and is_video_file(order["audio_path"] or ""),
        "source_media_ready": bool(order["audio_path"]) and Path(order["audio_path"]).is_file(),
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "subs_ready": (outdir / f"subs_{order['target_lang']}.srt").exists(),
        "caption_looks": [{"key": k, "label": v, "state": _captioned_state(order, k), "pct": _captioned_progress(order, k)} for k, v in CAPTION_LOOKS.items()],
        "summaries": [{"template_id": r["template_id"], "name": summary_templates.get(r["template_id"])["name"],
                       "html": order_ai_assist.render_summary_html(r["body"]), "when": datetime.fromtimestamp(r["created_at"]).strftime("%b %d, %H:%M")}
                      for r in db.list_order_summaries(order_id)],
        "summary_catalog": summary_templates.catalog(),
        "receipt": db.get_receipt_for_order(order_id),
        "error": error, "notice": notice,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "progress_steps": _order_progress_steps(order["status"], order["review_tier"]),
        "live": _processing_state(order, _ui_lang(request, user)) if order["status"] in ("queued", "processing") else None,
        "caption_cues": _caption_cues(job),
        "csat_due": order["status"] in ("ready_for_delivery", "delivered") and onboarding.csat_due(order["client_id"], order["id"]),
        "order_locked": _order_locked(order), "plan_subscribed": _plan_chosen(order["client_id"]),
        "free_plan_charge": _free_plan_charge(order["client_id"], order["id"]) if _order_locked(order) else None,
        "speaker_lines": _speaker_lines(job),
        "can_edit_transcript": order["review_tier"] == "ai" and order["status"] in db.TERMINAL_OK_STATUSES,
        "can_export_transcript": bool(job) and _deliverable_denied_reason(order, "txt") is None,
        "same_language": order["service_level"] == "transcribe" or order["source_lang"] == order["target_lang"],
        "youtube_error": youtube_error, "youtube_connected": youtube_connected, "youtube_pushed": youtube_pushed,
        "youtube_connection": db.get_youtube_connection(user["id"]),
        "upsell": _order_upsell_prices(order),
        "human_check": _human_check_offer(order),
        "paystack_configured": billing.paystack_configured(),
        "transcript_teaser": (_transcript_teaser(job, order["id"])
                              if job and order["status"] in db.TERMINAL_OK_STATUSES
                              and _order_locked(order) else None),
        "notion_row": db.get_client_notion(user["client_scope_id"]),
        "notion_export": db.get_notion_export(order["id"]),
    })


def _scramble_like(text: str, rng) -> str:
    """Same length and word shapes as `text`, but random letters: nothing of the
    real words survives, so it is safe to put in a page that is then blurred."""
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") if ch.isalpha() else ch for ch in text)


def _transcript_teaser(job, order_id: str,
                       fraction: float = billing.TRANSCRIPT_TEASER_VISIBLE_FRACTION) -> dict:
    """What an account that has not chosen a plan may read of a finished
    transcript: the first `fraction` of it as real text, and the remainder as
    scrambled placeholder text of the same shape. The hidden part is replaced
    HERE, on the server - blurring the real text with CSS would leave it in the
    page for anyone to read."""
    import random
    paras = []
    for seg in (job.segments if job else []):
        if getattr(seg, "segment_type", "speech") != "speech":
            continue
        text = (getattr(seg, "source_final_text", "") or "").strip()
        if text:
            paras.append(text)
    total = sum(len(p) for p in paras)
    if not total:
        return {"visible": [], "hidden": [], "hidden_pct": 0}
    budget = total * fraction
    visible, hidden_real, used = [], [], 0
    for p in paras:
        if hidden_real:
            hidden_real.append(p)
        elif used + len(p) <= budget:
            visible.append(p)
            used += len(p)
        else:
            cut = p.rfind(" ", 0, max(int(budget - used), 0) + 1)   # split on a word boundary
            head, tail = (p[:cut].strip(), p[cut:].strip()) if cut > 0 else ("", p)
            if head:
                visible.append(head)
                used += len(head)
            hidden_real.append(tail)
    rng = random.Random(f"{order_id}:teaser")
    return {"visible": visible, "hidden": [_scramble_like(p, rng) for p in hidden_real],
            "hidden_pct": round(100 * (total - used) / total)}


def _transcript_text_for_order(job, order_level: dict) -> str:
    """The real, final, human-reviewed text for this order - the target-
    language translation where one actually happened, the source
    transcript otherwise (a transcription-only order has no final_text at
    all). Never the raw AI draft - source_final_text/final_text are only
    ever set once a segment's been through the real review pipeline, same
    fields the existing transcript preview table above already reads."""
    if not job or not job.segments:
        return ""
    use_translation = bool(order_level.get("mt"))
    lines = []
    for seg in job.segments:
        if getattr(seg, "segment_type", "speech") != "speech":
            continue
        text = (seg.final_text if use_translation else seg.source_final_text) or ""
        text = text.strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


@app.post("/client/orders/{order_id}/ai-summary")
def client_order_ai_summary(request: Request, order_id: str, template: str = Form("")):
    """Made only when the customer asks, in the template they chose, and kept: asking again for the same template shows the saved
    one. (Older note: lazy-generated, cached forever after - see the ai_summary_text
    migration note in db.py and order_ai_assist.py's own module docstring
    for why this is a real Claude call grounded in the order's own
    transcript, not something pulled from Transkriptor (their API doesn't
    provide one)."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"ok": False, "error": "Not signed in."}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return JSONResponse({"ok": False, "error": "Order not found."}, status_code=404)
    if _order_locked(order):
        return JSONResponse({"ok": False, "error": "Unlock this order to use this: subscribe to the Free plan (pay as you go) or pay for the order."},
                             status_code=403)
    chosen = summary_templates.get(template)
    saved = next((r for r in db.list_order_summaries(order_id) if r["template_id"] == chosen["id"]), None)
    if saved:
        return JSONResponse({"ok": True, "summary": saved["body"], "html": order_ai_assist.render_summary_html(saved["body"]),
                             "template": chosen["id"], "name": chosen["name"]})
    # Keyed on the account, not the order - a real result is cached forever
    # right below, so this only ever fires repeatedly on a *failing*
    # generation; without a limit that's an unbounded retry loop straight
    # onto a real, billed Claude call every time.
    allowed, retry_after = rate_limit.check(f"ai-summary:{user['client_scope_id']}", limit=10, window_s=600)
    if not allowed:
        return JSONResponse({"ok": False, "error": f"Too many requests - try again in {retry_after}s."},
                             status_code=429)
    job = _load_job(order)
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    text = _transcript_text_for_order(job, order_level)
    result = order_ai_assist.generate_templated_summary(text, chosen)
    if result["ok"]:
        db.save_order_summary(order_id, chosen["id"], result["summary"])
        if not order["ai_summary_text"]:                    # integrations that include "the summary" use the first one made
            db.set_order_ai_summary(order_id, order_ai_assist.plain_summary(result["summary"]))
        result.update(html=order_ai_assist.render_summary_html(result["summary"]), template=chosen["id"], name=chosen["name"])
    return JSONResponse(result)


@app.post("/client/orders/{order_id}/ai-ask")
async def client_order_ai_ask(request: Request, order_id: str):
    """Stateless per-question Q&A, not a persisted chat - each call is a
    fresh grounded-in-the-real-transcript request, no conversation memory
    kept server-side. Every call is a real, billed Claude request, so it's
    rate-limited per account below - the gap this docstring used to flag
    as unaddressed."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"ok": False, "error": "Not signed in."}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return JSONResponse({"ok": False, "error": "Order not found."}, status_code=404)
    if _order_locked(order):
        return JSONResponse({"ok": False, "error": "Unlock this order to use this: subscribe to the Free plan (pay as you go) or pay for the order."},
                             status_code=403)
    allowed, retry_after = rate_limit.check(f"ai-ask:{user['client_scope_id']}", limit=20, window_s=600)
    if not allowed:
        return JSONResponse({"ok": False, "error": f"Too many questions - try again in {retry_after}s."},
                             status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    question = (body.get("question") or "").strip()
    job = _load_job(order)
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    text = _transcript_text_for_order(job, order_level)
    result = order_ai_assist.answer_question_about_order(text, question)
    return JSONResponse(result)


@app.post("/client/orders/{order_id}/messages")
def client_send_message(request: Request, order_id: str, body: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if order["review_tier"] == "ai":
        return RedirectResponse(f"/client/orders/{order_id}?error=" + quote("Instant AI orders are not reviewed by a person, so messages and "
                                "revisions are not available. Edit the transcript yourself, or add human review."), status_code=303)
    if body.strip():
        # visibility is hardcoded here, never read from the request - a
        # client has no form field that could set it to 'internal'.
        db.create_message(order_id, user["id"], "client", body)
        db.mark_read(user["id"], order_id)
    return RedirectResponse(f"/client/orders/{order_id}", status_code=303)


@app.post("/client/orders/{order_id}/resume")
def client_resume_order(request: Request, order_id: str, audio: UploadFile | None = File(None),
                         upload_key: str = Form("")):
    """The other way a returned order gets moving again - no brand-new
    order, no second payment. Most returns just need the client's reply
    on the message thread above (already handled by client_send_message);
    a replacement file here is only for the cases that genuinely need
    one - a real, corrected upload validated the same way as any other
    submission, on the same order and outdir it already had."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if order["status"] != "returned_to_client":
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)

    new_audio_path, new_original_filename, new_duration = None, None, None
    if audio is not None and audio.filename:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        order_upload_dir = UPLOAD_DIR / order_id
        try:
            audit = upload_security.validate_media_upload(audio, order_upload_dir / "placeholder", audio.filename)
        except upload_security.UploadRejected as exc:
            db.log_upload_audit(user["id"], order_id,
                                 {"original_filename": audio.filename, "rejected": True, "reject_reason": str(exc)},
                                 client_ip, user_agent)
            return RedirectResponse(f"/client/orders/{order_id}?error={quote(str(exc))}", status_code=303)
        db.log_upload_audit(user["id"], order_id, audit, client_ip, user_agent)
        new_audio_path = audit["final_path"]
        new_original_filename = audio.filename
        if audit.get("content_safety_flagged"):
            db.set_order_content_safety_flag(order_id, True, audit.get("content_safety_detail"))
        try:
            new_duration = probe_duration_minutes(new_audio_path)
        except Exception:
            return RedirectResponse(
                f"/client/orders/{order_id}?error=Couldn%27t+read+that+file%27s+duration+-+it+may+not+be+a+valid+audio%2Fvideo+file.",
                status_code=303)
    elif upload_key.strip():
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        order_upload_dir = UPLOAD_DIR / order_id
        try:
            audit, new_original_filename = _resolve_uploaded_audio(upload_key.strip(), order_upload_dir, user["client_scope_id"])
        except upload_security.UploadRejected as exc:
            db.log_upload_audit(user["id"], order_id,
                                 {"original_filename": upload_key.strip(), "rejected": True, "reject_reason": str(exc)},
                                 client_ip, user_agent)
            return RedirectResponse(f"/client/orders/{order_id}?error={quote(str(exc))}", status_code=303)
        db.log_upload_audit(user["id"], order_id, audit, client_ip, user_agent)
        new_audio_path = audit["final_path"]
        if audit.get("content_safety_flagged"):
            db.set_order_content_safety_flag(order_id, True, audit.get("content_safety_detail"))
        try:
            new_duration = probe_duration_minutes(new_audio_path)
        except Exception:
            return RedirectResponse(
                f"/client/orders/{order_id}?error=Couldn%27t+read+that+file%27s+duration+-+it+may+not+be+a+valid+audio%2Fvideo+file.",
                status_code=303)

    db.resume_returned_order(order_id, new_audio_path, new_original_filename, new_duration)
    db.create_message(
        order_id, user["id"], "client",
        "Resumed this order" + (" with a replacement file." if new_audio_path else "."),
    )
    worker.submit_job(order_id)
    return RedirectResponse(f"/client/orders/{order_id}?notice=Order+resumed+-+back+in+the+queue.", status_code=303)


@app.post("/client/orders/{order_id}/folder")
def client_move_order_folder(request: Request, order_id: str, folder_name: str = Form("")):
    """Re-files an order that's already been submitted - folder was
    previously a one-time choice at upload, with no way back into a
    different (or new) project after the fact."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    db.set_order_folder(order_id, user["id"], folder_name)
    return RedirectResponse(f"/client/orders/{order_id}?notice=Folder+updated.", status_code=303)


@app.post("/staff/orders/{order_id}/resume")
def staff_resume_order(request: Request, order_id: str):
    """Staff-side equivalent - for when the client clarified things over
    WhatsApp or email rather than through the message thread, and staff
    just needs to pick the same order back up without waiting on a client
    click. No file here - if the fix genuinely needs a new upload, that
    still has to come from the client's own account."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["status"] != "returned_to_client":
        return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)
    db.resume_returned_order(order_id)
    db.create_message(order_id, user["id"], "internal", "Resumed by staff - back in the queue.")
    worker.submit_job(order_id)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/client/orders/{order_id}/grant-voice-clone-consent")
def client_grant_voice_clone_consent(request: Request, order_id: str):
    """The other way to grant this, alongside the submission-time checkbox
    (client_dashboard.html) - for someone who didn't check it up front and
    changes their mind. Always the client's own explicit action on their
    own order, logged with a real timestamp + IP - see
    staff_set_dub_voice's server-side check for what this actually
    unlocks."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    db.set_voice_clone_consent(order_id, request.client.host if request.client else None)
    return RedirectResponse(f"/client/orders/{order_id}", status_code=303)


# -------------------------------------------------------------- billing ----
@app.get("/client/billing", response_class=HTMLResponse)
def billing_page(request: Request, upgrade_for: str | None = None, notice: str | None = None,
                  error: str | None = None, choose_plan: str | None = None, payments_page: int = 1):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    subscription = db.get_subscription(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    is_test_account = plan == "enterprise" and user["email"].strip().lower() in billing.test_client_emails()
    bonus_minutes = (subscription["bonus_minutes"] or 0.0) if subscription else 0.0
    payments = list(db.list_payments_for_user(user["client_scope_id"]))
    payments_total = len(payments)
    pay_page, pay_pages, payments = _paginate(payments, payments_page, LIST_PAGE_SIZE)
    receipts_by_payment = {
        r["payment_id"]: r for r in db.list_receipts_for_client(user["client_scope_id"])
    }
    # Real, not decorative: total_spent is the same all-time confirmed-
    # payments figure client_home.html's dashboard card shows (one source
    # of truth); outstanding is what's actually sitting unpaid right now
    # across every order, so a client with several pending_payment orders
    # sees the real total they owe in one number instead of adding pills
    # up themselves order by order.
    client_stats = db.client_dashboard_stats(user["client_scope_id"])
    outstanding_usd = sum(
        (o["cost_usd"] or 0) for o in db.list_orders_for_client(user["client_scope_id"]) if o["status"] == "pending_payment"
    )
    return templates.TemplateResponse(request, "billing.html", {
        "user": user, "plans": billing.PLANS, "current_plan": plan,
        "plan_is_chosen": _plan_chosen(user["client_scope_id"]), "choose_plan": choose_plan, "addons": billing.ADDONS,
        "free_plan_charge": None if _plan_chosen(user["client_scope_id"]) else _free_plan_charge(user["client_scope_id"]),
        "subscription": subscription, "is_test_account": is_test_account,
        "payments": payments, "payments_total": payments_total, "pay_page": pay_page, "pay_pages": pay_pages, "receipts_by_payment": receipts_by_payment,
        "total_spent_usd": client_stats["total_spent_usd"], "outstanding_usd": outstanding_usd,
        "upgrade_for": upgrade_for, "notice": notice, "error": error,
        "paystack_configured": billing.paystack_configured(),
        "mpesa_configured": billing.mpesa_configured(),
        "free_minutes_base": billing.FREE_MINUTES_PER_MONTH,
        "bonus_minutes": bonus_minutes,
        "free_minutes_total": billing.FREE_MINUTES_PER_MONTH + bonus_minutes,
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "credit_packages": billing.CREDIT_PACKAGES,
        "service_levels": billing.SERVICE_LEVELS,
        "price_card": billing.price_card_for(db.get_billing_market(user["client_scope_id"])),
        "review_tiers": billing.REVIEW_TIERS,
        "recurring": bool(subscription) and billing._is_recurring(subscription),
    })


@app.post("/client/billing/cancel-subscription")
def billing_cancel_subscription(request: Request):
    """Stop a recurring Paystack subscription from renewing. The client keeps
    the plan until the period they already paid for ends. Team owner only,
    like every other billing action."""
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    subscription = db.get_subscription(user["id"])
    if not subscription or not billing._is_recurring(subscription) or not subscription["paystack_email_token"]:
        return RedirectResponse("/client/billing?error=No+auto-renewing+subscription+to+cancel.", status_code=303)
    result = billing.paystack_disable_subscription(subscription["paystack_subscription_code"],
                                                   subscription["paystack_email_token"])
    if not result["ok"]:
        return RedirectResponse(f"/client/billing?error={quote(result['error'])}", status_code=303)
    db.set_paystack_subscription(user["id"], None, None)
    dunning.resolve(user["id"], "downgraded")
    return RedirectResponse(
        "/client/billing?notice=Auto-renewal+cancelled.+You+keep+your+plan+until+the+period+you+paid+for+ends.",
        status_code=303)


def _order_locked(order) -> bool:
    """True while an order's results can be read but not downloaded or edited: the account has not subscribed to the Free plan (pay as you
    go) yet, or the order was made with free minutes on the Free plan and has not been paid for. Free minutes cover processing and reading
    only. Orders made under a paid plan are never locked by this."""
    if not _plan_chosen(order["client_id"]):
        return True
    return bool(order["is_free_preview"]) and (order["tier"] or "free") == "free" and _unlock_price(order) > 0


def _unlock_price(order) -> float:
    """What it costs to unlock an order made with free minutes: its full cost at pay-as-you-go prices, as if no free minutes had been used."""
    try:
        cost = json.loads(order["cost_breakdown_json"]) if order["cost_breakdown_json"] else {}
    except (json.JSONDecodeError, TypeError):
        cost = {}
    addons = [line["key"] for line in (cost.get("addons") or []) if isinstance(line, dict) and line.get("key")]
    try:
        full = billing.order_cost_usd(float(order["duration_minutes"] or 0.0), order["service_level"] or "transcribe", "free", 0.0, addons=addons,
                                      market=order["billing_market"], review_tier=order["review_tier"] or "ai")
        return round(float(full["total_usd"]), 2)
    except Exception:  # noqa: BLE001 - never block a page on a price that cannot be worked out
        return round(float(cost.get("first_order_courtesy_usd") or 0.0), 2)


def _free_plan_charge(client_id: str, order_id: str | None = None) -> dict | None:
    """What opening a locked order costs: its full pay-as-you-go price (see _unlock_price). For a new account this is what subscribing to the
    Free plan charges; once that payment succeeds the account is on the Free plan. None when there is no locked finished order."""
    orders = [o for o in db.list_orders_for_client(client_id) if o["status"] in db.TERMINAL_OK_STATUSES and _order_locked(o)]
    if order_id:
        orders = [o for o in orders if o["id"] == order_id] or orders
    if not orders:
        return None
    order = orders[0]
    return {"order_id": order["id"], "name": order["original_filename"], "order": order, "amount": _unlock_price(order)}


@app.post("/client/billing/choose-free")
def billing_choose_free(request: Request, order_id: str = Form(""), phone: str = Form("")):
    """Subscribing to the Free plan (pay as you go). The cost of the order being opened is worked out and charged; once that payment
    succeeds the account is on the Free plan and downloads and editing open (any real payment marks the plan as chosen, see
    _activate_payment). If the free minutes already cover the order there is nothing to charge and it opens straight away."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if user["client_scope_id"] != user["id"]:
        return RedirectResponse("/client/billing?error=Only+the+account+owner+can+choose+the+plan.", status_code=303)
    if user["plan_chosen_at"]:
        return RedirectResponse("/client/billing", status_code=303)
    charge = _free_plan_charge(user["id"], order_id or None)
    if charge and charge["amount"] > 0:
        if not billing.paystack_configured():
            return RedirectResponse("/client/billing?error=Payment+isn%27t+configured+yet+-+contact+us.", status_code=303)
        order = charge["order"]
        back = f"/client/orders/{order['id']}"
        return _checkout(request, user, "paystack", order["tier"], charge["amount"], order["id"], phone, back, back, payment_kind="preview_unlock")
    db.set_plan_chosen(user["id"])
    if charge:
        db.set_order_free_preview(charge["order_id"], False)                  # nothing to pay for this order: it opens
    return RedirectResponse("/client/billing?notice=You%27re+on+the+Free+plan+%28pay+as+you+go%29.+Downloads+and+editing+are+unlocked.", status_code=303)


@app.get("/client/verify-trial")
def client_verify_trial_retired():
    """The $1 card confirmation no longer exists; old links and emails land where a plan is chosen."""
    return RedirectResponse("/client/billing?choose_plan=1", status_code=303)


@app.post("/client/billing/wallet")
def buy_wallet_credits(request: Request, package: str = Form(""), provider: str = Form(...),
                        phone: str = Form(""), custom_minutes: str = Form("")):
    """custom_minutes is still the form field name (and still means "how
    many dub-rate minutes of value do you want", the same reference point
    the fixed packages use for their discount ladder) - only the balance
    it actually buys is now denominated in credits, see
    billing.custom_credit_price."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if custom_minutes.strip():
        try:
            minutes_equiv = float(custom_minutes.strip())
        except ValueError:
            return RedirectResponse("/client/billing?error=Enter+a+real+number+of+minutes.", status_code=303)
        if not (billing.CREDIT_CUSTOM_MIN_MINUTES_EQUIV <= minutes_equiv <= billing.CREDIT_CUSTOM_MAX_MINUTES_EQUIV):
            return RedirectResponse(
                f"/client/billing?error=Enter+between+{billing.CREDIT_CUSTOM_MIN_MINUTES_EQUIV}+and+"
                f"{billing.CREDIT_CUSTOM_MAX_MINUTES_EQUIV}+minutes.", status_code=303)
        pkg = billing.custom_credit_price(minutes_equiv)
    elif package in billing.CREDIT_PACKAGES:
        pkg = billing.CREDIT_PACKAGES[package]
    else:
        return RedirectResponse("/client/billing?error=Unknown+credit+package.", status_code=303)
    return _checkout(request, user, provider, "wallet", pkg["price_usd"],
                      None, phone, "/client/billing", "/client/billing",
                      payment_kind=f"credits_topup:{pkg['credits']}")


def _order_rate_per_min(order) -> float:
    """What this order's per-minute rate actually was - the rate frozen at
    creation (cost_breakdown_json), not today's price card, so a later price
    change can't alter an existing order's surcharge math. Orders from before
    the breakdown existed fall back to the original service-level rate."""
    try:
        if order["cost_breakdown_json"]:
            return float(json.loads(order["cost_breakdown_json"])["rate_per_min"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])["rate_per_min"]


def _order_receipt_line_items(order) -> list[dict] | None:
    """Real per-service breakdown for an order's receipt, straight from the
    billing.order_cost_usd() result frozen onto the order at creation time
    (db.set_order_billing's cost_breakdown_json) - never recomputed, so it
    can't drift from what was actually charged even if rates or the
    client's plan change later. Returns None for an order placed before
    this column existed, or with no service level on file - the receipt
    just falls back to its one-line description in that case."""
    if not order or not order["cost_breakdown_json"]:
        return None
    try:
        cost = json.loads(order["cost_breakdown_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    level = billing.SERVICE_LEVELS.get(order["service_level"])
    if not level:
        return None
    minutes, billable, rate = cost["minutes"], cost["billable_minutes"], cost["rate_per_min"]
    if billable < minutes:
        detail = (f"{billable:.1f} of {minutes:.1f} min billed (the rest covered by free minutes) "
                   f"× ${rate:.2f}/min")
    else:
        detail = f"{minutes:.1f} min × ${rate:.2f}/min"
    tier_label = ("Instant AI, not human-reviewed" if order["review_tier"] == "ai"
                   else "Human-reviewed")
    lines = [{"label": f"{level['name']} - {tier_label}", "detail": detail, "amount_usd": cost["gross_usd"]}]
    if cost.get("discount_pct"):
        lines.append({"label": f"Plan discount ({cost['discount_pct'] * 100:.0f}%)",
                       "detail": None, "amount_usd": -cost["discount_usd"]})
    if cost.get("credits_applied_usd"):
        lines.append({"label": "Prepaid credits applied",
                       "detail": f"{cost['credits_applied']:.0f} credits × $0.10",
                       "amount_usd": -cost["credits_applied_usd"]})
    for addon in cost.get("addons", []):
        lines.append({"label": addon["name"], "amount_usd": addon["cost_usd"],
                       "detail": f"{minutes:.1f} min × ${addon['rate_per_min']:.2f}/min"})
    if cost.get("rush_surcharge_usd"):
        lines.append({"label": "Rush processing", "amount_usd": cost["rush_surcharge_usd"],
                       "detail": f"+{billing.RUSH_SURCHARGE_PCT * 100:.0f}% for priority queue handling"})
    return lines


def _activate_payment(payment, provider_reference: str, base_url: str = "") -> bool:
    """The one place a confirmed payment turns into something real -
    either an order gets released to the worker (usage charge) or a plan
    subscription gets activated (plan purchase). db.complete_payment does
    the idempotency check (already-completed / duplicate provider_reference
    both short-circuit here), so this never runs twice for the same
    payment even if a webhook fires more than once.

    base_url (e.g. from str(request.base_url)) is only used to build the
    receipt link in the auto-sent email below - every caller has a real
    request in scope, so this is never fabricated; if a caller genuinely
    can't supply one, the receipt is simply still created and viewable,
    just not auto-emailed with a working link, same as before this existed."""
    if not db.complete_payment(payment["id"], provider_reference, billing.PLAN_PERIOD_DAYS):
        return False
    if not payment["order_id"]:
        dunning.resolve(payment["user_id"], "paid")        # a plan payment went through: any failed-payment recovery is over
    # Any real payment confirms the card and counts as choosing a plan.
    db.set_plan_chosen(db.get_team_owner_for_member(payment["user_id"]) or payment["user_id"])
    payer = db.get_user(payment["user_id"])
    if payer and payer["onboarding_status"] != "activated":
        # First real money from this account, of any kind (order, plan,
        # wallet top-up) - the actual "this is a real customer now" moment.
        # See _queue_first_payment_message - auto-sent now that Brevo is
        # configured, same as the welcome message.
        _queue_first_payment_message(payer, base_url=base_url)
        db.set_onboarding_status(payer["id"], "activated")
    try:
        meta = json.loads(payment["meta"]) if payment["meta"] else {}
    except (json.JSONDecodeError, AttributeError):
        meta = {}
    payment_kind = meta.get("kind", "order")
    # Captured at checkout time, not here - see _checkout's own comment on
    # why (this function runs from a payment webhook, which carries none
    # of the customer's cookies to read a client_id from directly).
    ga_client_id = meta.get("ga_client_id")
    line_items = None
    if payment["order_id"] and payment_kind == "difficulty_surcharge":
        # Already processed and delivered-ready - this only clears the
        # surcharge-approval gate, never re-queues the job.
        db.approve_difficulty_surcharge(payment["order_id"])
        description = f"Additional work on order {payment['order_id']}"
    elif payment["order_id"] and payment_kind == "preview_unlock":
        # A free-trial preview paid for after the fact: same file, now
        # downloadable. Nothing re-queued - it was already processed.
        db.set_order_free_preview(payment["order_id"], False)
        order = db.get_order(payment["order_id"])
        description = f"Unlocked transcript - {order['original_filename'] if order else payment['order_id']}"
    elif payment["order_id"] and payment_kind == "review_upgrade":
        # Instant AI -> Human-reviewed: it now waits for a dedicated editor like
        # any human-tier order, with a real deadline and a staff alert.
        db.set_order_review_tier(payment["order_id"], "human")
        db.update_order_status(payment["order_id"], "awaiting_review")
        order = db.get_order(payment["order_id"])
        if order:
            deadlines = tat.compute_deadlines(order["tier"], order["service_level"],
                                               order["duration_minutes"] or 0, rush=False)
            db.set_order_deadlines(payment["order_id"], deadlines["start_at"],
                                    deadlines["internal_deadline_at"], deadlines["deadline_at"])
            order = db.get_order(payment["order_id"])
            worker.notify_staff_needs_review(order)
        description = f"Human review upgrade - {order['original_filename'] if order else payment['order_id']}"
    elif payment["order_id"]:
        order = db.get_order(payment["order_id"])
        if order and order["status"] == "pending_payment":
            # NOT db.add_usage_minutes here - that column tracks the
            # monthly FREE-trial allowance specifically (see
            # billing.free_minutes_remaining), already credited at
            # submission time via cost["free_minutes_applied"] for
            # whatever portion of THIS order was actually free. Adding the
            # full paid duration here as well double-counted it - a
            # client's free trial was silently reading as exhausted the
            # moment they paid for one big order, even though they'd never
            # touched their free minutes. Found and fixed 2026-08-23.
            db.update_order_status(payment["order_id"], "queued")
            if order["review_tier"] != "ai":  # instant AI orders have no human turnaround to promise
                deadlines = tat.compute_deadlines(order["tier"], order["service_level"],
                                                   order["duration_minutes"] or 0, rush=bool(order["is_rush"]))
                db.set_order_deadlines(payment["order_id"], deadlines["start_at"],
                                        deadlines["internal_deadline_at"], deadlines["deadline_at"])
            order = db.get_order(payment["order_id"])  # re-fetch with the deadline fields now set
            worker.submit_job(payment["order_id"])
        level = billing.SERVICE_LEVELS.get(order["service_level"]) if order else None
        description = (f"{level['name']} - {order['original_filename']}" if order and level
                        else f"Order {payment['order_id']} - {payment['plan']} plan")
        line_items = _order_receipt_line_items(order) if order else None
    elif payment_kind.startswith("credits_topup:"):
        credits = float(payment_kind.split(":", 1)[1])
        db.add_wallet_credits(payment["user_id"], credits)
        description = f"Prepaid credits top-up - {credits:.0f} credits"
    elif payment_kind == "trial_verification":
        # A $1 verification charge made before that step was retired becomes real wallet credit, not a fee. Same
        # add_wallet_credits call a real top-up uses, so it shows up and
        # spends exactly the same way on their next real order.
        db.add_wallet_credits(payment["user_id"], billing.usd_to_credits(payment["amount_usd"]))
        db.set_trial_verified(payment["user_id"])
        description = "Free trial verification - credited to your account"
    else:
        yearly = payment_kind in ("plan_year", "subscription_renewal_year")
        db.set_subscription_plan(payment["user_id"], payment["plan"], billing.PLAN_YEAR_DAYS if yearly else billing.PLAN_PERIOD_DAYS)
        description = f"{billing.PLANS[payment['plan']]['name']} plan subscription" + (" (yearly)" if yearly else "")
        account_hub.on_plan_paid(payment["user_id"], payment["plan"])
    # A real receipt for every completed payment, whatever it was for -
    # not conditional on provider (Paystack/M-Pesa/bank all get one),
    # since this is Kauli's own receipt, not a copy of theirs.
    receipt_id = db.create_receipt(payment["id"], payment["user_id"], description, payment["amount_usd"],
                                    payment["amount_local"], payment["currency"], payment["provider"],
                                    provider_reference, line_items=line_items)
    # Auto-sent the moment it exists if a real mailer is configured (see
    # mailer.py) - no staff step in between. Falls back to the existing
    # "queued, staff forwards it manually" state if BREVO_API_KEY isn't
    # set, or if Brevo itself rejects/errors the send (email_send_detail
    # then holds the real reason, visible on /staff/billing).
    if mailer.email_configured() and payer:
        receipt_url = f"{base_url.rstrip('/')}/receipts/{receipt_id}" if base_url else None
        # Real invoice-style rows - the SAME line_items already computed
        # above (or a single description/total row for a plan/wallet
        # payment, which only ever has one real line anyway), not a
        # second, separately-maintained copy of the breakdown.
        rows = line_items or [{"label": description, "detail": None, "amount_usd": payment["amount_usd"]}]
        detail_span_style = f"font-size:12px; color:{mailer.BRAND_MUTED};"
        cell_style = f"padding:8px 0; border-bottom:1px solid {mailer.BRAND_BORDER};"
        amount_style = cell_style + " text-align:right; white-space:nowrap;"
        row_parts = []
        for r in rows:
            label_html = r["label"]
            if r.get("detail"):
                label_html += f'<br><span style="{detail_span_style}">{r["detail"]}</span>'
            sign = "-" if r["amount_usd"] < 0 else ""
            row_parts.append(
                f'<tr><td style="{cell_style}">{label_html}</td>'
                f'<td style="{amount_style}">{sign}${abs(r["amount_usd"]):.2f}</td></tr>'
            )
        row_html = "".join(row_parts)
        inner = (
            f'<p style="margin:0 0 18px;">Hi {payer["display_name"]}, your Kauli payment has been received.</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;">'
            f'<tr><th style="text-align:left; padding-bottom:8px; border-bottom:2px solid {mailer.BRAND_INK}; font-size:12px; '
            f'text-transform:uppercase; letter-spacing:0.04em; color:{mailer.BRAND_MUTED};">Description</th>'
            f'<th style="text-align:right; padding-bottom:8px; border-bottom:2px solid {mailer.BRAND_INK}; font-size:12px; '
            f'text-transform:uppercase; letter-spacing:0.04em; color:{mailer.BRAND_MUTED};">Amount</th></tr>'
            f'{row_html}'
            f'<tr><td style="padding:12px 0 0; font-weight:700;">Total paid</td>'
            f'<td style="padding:12px 0 0; font-weight:700; text-align:right;">${payment["amount_usd"]:.2f}</td></tr>'
            f'</table>'
        )
        html = mailer.wrap_email_html(
            inner, cta_text="View your receipt →" if receipt_url else None, cta_url=receipt_url,
            footer_note=f'Billing questions? Just <a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_MUTED};">reply</a> to this email.',
            base_url=base_url,
        )
        text = (f"Hi {payer['display_name']},\n\nYour Kauli payment has been received.\n"
                f"{description} - Total: ${payment['amount_usd']:.2f}\n"
                + (f"View your receipt: {receipt_url}\n" if receipt_url else ""))
        ok, detail = mailer.send_email(payer["email"], "Your Kauli receipt", html, text)
        db.set_receipt_email_result(receipt_id, ok, detail)
    # GA4's own recommended e-commerce event - the real "money actually
    # cleared" moment, not just a checkout attempt. transaction_id is this
    # payment's own real, unique id, so GA's dedup logic can't double-
    # count it even if a webhook somehow fired twice (though
    # db.complete_payment's idempotency check above already means this
    # whole function only ever runs once per real payment anyway).
    ga_events.send_event(ga_client_id, "purchase", {
        "transaction_id": payment["id"], "value": payment["amount_usd"], "currency": "USD",
        "payment_kind": payment_kind,
    })
    return True


def _note_payment_failure(client_id: str, order_id: str | None, provider: str, reason: str) -> None:
    """Real, automatic paper trail on a payment failure - never silently
    dropped, never left for staff to notice on their own. Appends to the
    same staff_notes field the client detail page's Settings tab already
    shows (see /staff/clients/{id}), so this shows up exactly where staff
    would already look, next to whatever they've manually noted about
    this account - never overwrites it. Counts recent failures so a
    one-off card decline and a client genuinely stuck read differently at
    a glance, without a separate escalation system to build and
    maintain."""
    recent = db.count_recent_failed_payments(client_id, hours=48)
    order_ref = f" (order {order_id})" if order_id else ""
    persists = f" - {recent} failed attempts in the last 48h, worth reaching out" if recent >= 2 else ""
    db.append_client_staff_note(
        client_id, f"Payment failed via {provider}{order_ref}: {reason}.{persists}")


def _checkout(request: Request, user, provider: str, plan: str, amount_usd: float,
              order_id: str | None, phone: str, success_redirect: str, back_url: str,
              payment_kind: str = "order"):
    """Shared by the plan-purchase and per-order checkout routes - same
    three providers, same idempotent-payment-record pattern either way.
    payment_kind rides along in meta so _activate_payment knows what
    completing this payment should actually do - "order" (the normal
    pay-before-processing flow) vs "difficulty_surcharge" (an
    already-delivered order's approved surcharge, which must NOT
    re-queue the job - see _activate_payment)."""
    if order_id:
        # The actual double-payment guard - not "can this order be
        # processed twice" (already handled in _activate_payment), but
        # "can the client's money actually be taken twice for it". A
        # recent unresolved payment blocks a new one; a stale one (client
        # abandoned the first attempt) doesn't, so nobody gets stuck.
        existing = db.get_active_pending_payment_for_order(order_id)
        if existing:
            minutes_ago = max(1, int((time.time() - existing["created_at"]) / 60))
            return RedirectResponse(
                f"{back_url}?error=A+payment+for+this+order+is+already+in+progress+"
                f"(started+{minutes_ago}+minute(s)+ago+via+{existing['provider']}).+"
                f"Give+it+a+few+minutes%2C+or+contact+us+if+it+seems+stuck.",
                status_code=303)
    payment_id = billing.new_reference()  # ours, generated before the provider ever hears about this
    # Captured here, not at _activate_payment - a payment webhook (Paystack/
    # M-Pesa's own servers) carries none of the customer's cookies, so this
    # is the last point a real client_id is available at all. Stashed on
    # the payment's own meta for _activate_payment to read back once the
    # webhook actually confirms the charge (see ga_events.client_id_from_
    # request's docstring).
    ga_client_id = ga_events.client_id_from_request(request)

    # Hiding a method in the page is not enough - refuse it here as well.
    if provider == "bank" and not _bank_transfer_ok(user):
        return RedirectResponse(f"{back_url}?error=Bank+transfer+is+not+available+on+this+account.+Please+pay+by+card.", status_code=303)
    if provider == "mpesa" and not _mobile_money_ok(request, user):
        return RedirectResponse(f"{back_url}?error=M-Pesa+is+only+available+in+Kenya.+Please+pay+by+card.", status_code=303)

    if provider == "paystack":
        if not billing.paystack_configured():
            return RedirectResponse(f"{back_url}?error=Paystack+isn%27t+configured+yet.", status_code=303)
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, None, "USD", "paystack", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind, "ga_client_id": ga_client_id}))
        callback_url = str(request.base_url).rstrip("/") + f"/billing/callback/paystack?payment_id={payment_id}"
        # A plan purchase (no order) on a plan that has a Paystack plan code
        # configured becomes a RECURRING subscription; anything else (orders,
        # top-ups, plans without a code) stays a one-off charge.
        charge_currency = billing.paystack_currency_for_market(db.get_billing_market(user["client_scope_id"]), request.headers.get("cf-ipcountry"))
        if order_id is None and payment_kind in ("order", "plan_year"):
            plan_code = billing.paystack_plan_code(plan, charge_currency, yearly=(payment_kind == "plan_year"))
        else:
            plan_code = None
        result = billing.paystack_initialize(user["email"], amount_usd, payment_id, callback_url,
                                              plan_code=plan_code, currency=charge_currency)
        if "error" in result:
            db.fail_payment(payment_id)
            _note_payment_failure(user["client_scope_id"], order_id, "Paystack", result["error"])
            return RedirectResponse(f"{back_url}?error={result['error']}", status_code=303)
        # Stashed so a client who lands back on this page while this
        # payment is still "pending" (see order_pay_page) gets a real
        # "Resume payment" link straight back to the SAME Paystack page,
        # not just a countdown telling them to wait - Paystack's checkout
        # page for a given reference stays open well past our own 15-
        # minute pending window, so re-using it is safe and correct.
        db.update_payment_meta(payment_id, json.dumps({
            "kind": payment_kind, "authorization_url": result["authorization_url"], "ga_client_id": ga_client_id,
        }))
        return RedirectResponse(result["authorization_url"], status_code=303)

    if provider == "mpesa":
        if not billing.mpesa_live():
            return RedirectResponse(f"{back_url}?error=M-Pesa+direct+is+coming+soon+-+not+live+yet.", status_code=303)
        if not phone.strip():
            return RedirectResponse(f"{back_url}?error=Enter+the+M-Pesa+phone+number.", status_code=303)
        amount_kes, rate_source = billing.usd_to_kes(amount_usd)
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, amount_kes, "KES", "mpesa",
                           meta=json.dumps({"phone": phone.strip(), "rate_source": rate_source,
                                             "kind": payment_kind, "ga_client_id": ga_client_id}),
                           order_id=order_id)
        callback_url = str(request.base_url).rstrip("/") + "/webhooks/mpesa"
        result = billing.mpesa_stk_push(phone.strip(), amount_kes, payment_id, callback_url)
        if "error" in result:
            db.fail_payment(payment_id)
            _note_payment_failure(user["client_scope_id"], order_id, "M-Pesa", result["error"])
            return RedirectResponse(f"{back_url}?error={result['error']}", status_code=303)
        db.update_payment_meta(payment_id, json.dumps({
            "phone": phone.strip(), "rate_source": rate_source,
            "checkout_request_id": result["checkout_request_id"], "kind": payment_kind,
            "ga_client_id": ga_client_id,
        }))
        return RedirectResponse(
            f"{back_url}?notice=Check+your+phone+({phone.strip()})+for+the+M-Pesa+PIN+prompt+-+"
            f"KES+{amount_kes:,.2f}.", status_code=303)

    if provider == "bank":
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, None, "USD", "bank", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind, "ga_client_id": ga_client_id}))
        # order_pay.html has always promised "We'll email transfer
        # instructions" - a real gap until now, nothing ever actually
        # sent one. bank_details is a real, deliberate KAULI_BANK_DETAILS
        # env var (account name/number/bank, set by the account owner) -
        # never fabricated here; if it isn't set yet, the email still
        # goes out honestly saying a follow-up is coming, rather than
        # inventing account details that don't exist.
        bank_details = os.environ.get("KAULI_BANK_DETAILS", "").strip()
        details_html = (
            f"<p style=\"white-space:pre-wrap;\">{bank_details}</p>" if bank_details
            else "<p>We'll follow up shortly with the account details to transfer to.</p>"
        )
        client_html = mailer.wrap_email_html(
            f"<p>Hi {user['display_name']}, thanks - we've got your bank transfer request for "
            f"${amount_usd:.2f}.</p>{details_html}"
            f"<p>Please include reference <strong>{payment_id}</strong> on the transfer so we can match it "
            f"to your account quickly. We confirm bank transfers manually once they land - you'll get a "
            f"real receipt the moment it's confirmed.</p>",
            base_url=str(request.base_url),
        )
        mailer.send_email(user["email"], f"Kauli bank transfer - reference {payment_id}", client_html)
        staff_html = mailer.wrap_email_html(
            f"<p>{user['display_name']} ({user['email']}) requested a bank transfer for ${amount_usd:.2f} - "
            f"reference {payment_id}. Watch for it and confirm at /staff/billing once it lands.</p>",
            base_url=str(request.base_url),
        )
        mailer.send_email(CONTACT_EMAIL, f"New bank transfer expected - {payment_id}", staff_html)
        db.notify_all_staff("payment_received", f"{user['display_name']} requested a bank transfer (${amount_usd:.2f})",
                             link="/staff/billing")
        return RedirectResponse(f"{back_url}?notice=Bank+transfer+request+received+-+"
                                 f"check+your+email+for+instructions%2C+reference+{payment_id}.", status_code=303)

    return RedirectResponse(back_url, status_code=303)


@app.post("/client/billing/checkout")
def billing_checkout(request: Request, plan: str = Form(...), provider: str = Form(...),
                      phone: str = Form(""), interval: str = Form("month")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if plan not in billing.PLANS or plan == "free":
        return RedirectResponse("/client/billing", status_code=303)
    if plan == "enterprise":
        return RedirectResponse(
            "/client/billing?notice=" +
            "Enterprise is set up by an account manager, not self-serve checkout - "
            "send us a message from any order page and we'll reach out.",
            status_code=303,
        )
    yearly = interval == "year"
    return _checkout(request, user, provider, plan, billing.PLANS[plan]["price_usd_year" if yearly else "price_usd"],
                      None, phone, "/client/billing", "/client/billing", payment_kind="plan_year" if yearly else "order")


def _order_upsell_prices(order) -> dict:
    """What it costs to (a) unlock a free-trial preview and (b) upgrade a paid
    Instant AI order to human review - from the order's own FROZEN rate and
    market, never today's price card. Either is None when not applicable."""
    prices = {"unlock": None, "upgrade": None}
    try:
        cost = json.loads(order["cost_breakdown_json"]) if order["cost_breakdown_json"] else None
    except (json.JSONDecodeError, TypeError):
        cost = None
    if not cost or order["review_tier"] != "ai" or order["status"] not in db.TERMINAL_OK_STATUSES:
        return prices
    if order["is_free_preview"]:
        # Free minutes cover processing and reading only: downloading or editing the result means paying for the order, at its full
        # pay-as-you-go price. No human-review upsell is priced against an order that has not been paid for.
        prices["unlock"] = (_unlock_price(order) or None) if (order["tier"] or "free") == "free" else None
        return prices
    else:
        market = order["billing_market"] or "legacy"
        diff = (billing.rate_for(order["service_level"], market, "human") - cost["rate_per_min"])
        upgrade = round(diff * (order["duration_minutes"] or 0), 2)
        prices["upgrade"] = upgrade if upgrade > 0 else None
    return prices


def _human_check_offer(order) -> dict | None:
    """The offer on a delivered Instant AI translation: have a dedicated editor verify it against the audio. Priced as the difference between
    what the order was charged and the human-reviewed rate (see _order_upsell_prices), so the customer pays only for the extra work. None when
    it does not apply: not a paid, delivered Instant AI translation, or human review cannot be ordered for that language pair."""
    if order["service_level"] != "translate" or order["review_tier"] != "ai" or order["status"] not in db.TERMINAL_OK_STATUSES:
        return None
    price = _order_upsell_prices(order)["upgrade"]
    if not price:
        return None
    extras = _review_extra_languages()
    if order["source_lang"] not in ({"en", "sw", "ki"} | extras) or order["target_lang"] not in ({"en", "sw"} | extras):
        return None
    minutes = float(order["duration_minutes"] or 0)
    hours = tat.compute_deadlines(order["tier"], "translate", minutes)["client_tat_hours"]
    if hours <= 24:
        turnaround = f"within about {max(1, math.ceil(hours))} hour{'s' if math.ceil(hours) > 1 else ''}"
    else:
        days = math.ceil(hours / 24)
        turnaround = f"within about {days} day{'s' if days > 1 else ''}"
    return {"price": price, "minutes": round(minutes, 1), "per_min": round(price / minutes, 2) if minutes else None, "turnaround": turnaround,
            "target": LANGUAGE_NAMES.get(order["target_lang"], order["target_lang"])}


def _upsell_checkout(request: Request, order_id: str, provider: str, phone: str, kind: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    back = f"/client/orders/{order_id}"
    amount = _order_upsell_prices(order)["unlock" if kind == "preview_unlock" else "upgrade"]
    if not amount:
        return RedirectResponse(f"{back}?error=That+option+is+not+available+for+this+order.", status_code=303)
    return _checkout(request, user, provider, order["tier"], amount, order_id, phone, back, back,
                      payment_kind=kind)


@app.post("/client/orders/{order_id}/unlock")
def order_unlock_preview(request: Request, order_id: str, provider: str = Form("paystack"), phone: str = Form("")):
    return _upsell_checkout(request, order_id, provider, phone, "preview_unlock")


@app.post("/client/orders/{order_id}/upgrade-review")
def order_upgrade_review(request: Request, order_id: str, provider: str = Form("paystack"), phone: str = Form("")):
    return _upsell_checkout(request, order_id, provider, phone, "review_upgrade")


@app.get("/client/orders/{order_id}/pay", response_class=HTMLResponse)
def order_pay_page(request: Request, order_id: str, notice: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    # Real seconds left before get_active_pending_payment_for_order (the
    # actual guard - see _checkout) stops blocking a retry - drives the
    # live countdown on the "already in progress" error (order_pay.html),
    # computed from the same real payment row and the same constant the
    # guard itself uses, not a client-side guess re-parsed out of the
    # error string.
    pending_retry_after_s = None
    resume_url = None
    existing_payment = db.get_active_pending_payment_for_order(order_id)
    if existing_payment:
        pending_retry_after_s = max(0, round(
            db.PENDING_PAYMENT_MAX_AGE_S - (time.time() - existing_payment["created_at"])))
        if existing_payment["provider"] == "paystack" and existing_payment["meta"]:
            resume_url = json.loads(existing_payment["meta"]).get("authorization_url")
    return templates.TemplateResponse(request, "order_pay.html", {
        "user": user, "order": order, "notice": notice, "error": error,
        "pending_retry_after_s": pending_retry_after_s, "resume_url": resume_url,
        "paystack_configured": billing.paystack_configured(),
        "mpesa_configured": billing.mpesa_configured(),
        "mpesa_live": billing.mpesa_live(),
        "has_video_addon": db.order_has_addon(order, "video_deliverables"),
    })


@app.post("/client/orders/{order_id}/pay")
def order_pay_checkout(request: Request, order_id: str, provider: str = Form(...), phone: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if order["status"] != "pending_payment":
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)
    back_url = f"/client/orders/{order_id}/pay"
    return _checkout(request, user, provider, order["tier"], order["cost_usd"],
                      order_id, phone, back_url, back_url)


@app.get("/client/orders/{order_id}/difficulty-surcharge", response_class=HTMLResponse)
def order_surcharge_page(request: Request, order_id: str, notice: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    # Same real countdown as order_pay.html - see that route's comment.
    pending_retry_after_s = None
    resume_url = None
    existing_payment = db.get_active_pending_payment_for_order(order_id)
    if existing_payment:
        pending_retry_after_s = max(0, round(
            db.PENDING_PAYMENT_MAX_AGE_S - (time.time() - existing_payment["created_at"])))
        if existing_payment["provider"] == "paystack" and existing_payment["meta"]:
            resume_url = json.loads(existing_payment["meta"]).get("authorization_url")
    return templates.TemplateResponse(request, "order_surcharge_pay.html", {
        "user": user, "order": order, "notice": notice, "error": error,
        "pending_retry_after_s": pending_retry_after_s, "resume_url": resume_url,
        "paystack_configured": billing.paystack_configured(),
        "mpesa_configured": billing.mpesa_configured(),
        "reason_label": db.EXTRA_CHARGE_REASONS.get(order["difficulty_surcharge_reason"], "Additional work"),
    })


@app.post("/client/orders/{order_id}/difficulty-surcharge/pay")
def order_surcharge_checkout(request: Request, order_id: str, provider: str = Form(...), phone: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if order["difficulty_surcharge_status"] != "pending_approval":
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)
    back_url = f"/client/orders/{order_id}/difficulty-surcharge"
    return _checkout(request, user, provider, order["tier"], order["difficulty_surcharge_usd"],
                      order_id, phone, back_url, back_url, payment_kind="difficulty_surcharge")


@app.get("/receipts/{receipt_id}", response_class=HTMLResponse)
def view_receipt(request: Request, receipt_id: str):
    """Real, itemized, printable - Kauli's own receipt for a completed
    payment, viewable any time (not something that only exists as an
    email that might get lost). Client who owns it, or any staff member,
    can view it; nobody else."""
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        return HTMLResponse("Receipt not found.", status_code=404)
    if user["role"] != "staff" and receipt["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Receipt not found.", status_code=404)
    client = db.get_user(receipt["client_id"])
    order = db.get_order(receipt["order_id"]) if receipt["order_id"] else None
    try:
        line_items = json.loads(receipt["line_items_json"]) if receipt["line_items_json"] else None
    except json.JSONDecodeError:
        line_items = None
    return templates.TemplateResponse(request, "receipt.html", {
        "user": user, "receipt": receipt, "client": client, "order": order, "line_items": line_items,
        "email": CONTACT_EMAIL,
    })


@app.get("/billing/callback/paystack", response_class=HTMLResponse)
def paystack_callback(request: Request, payment_id: str, reference: str | None = None):
    """Where Paystack sends the client's browser back to after checkout.
    This is a convenience for the client's own UX - the webhook below is
    the one actually trusted to activate anything, since a browser
    redirect can be skipped, retried, or never arrive at all."""
    result = billing.paystack_verify(payment_id)
    payment = db.get_payment(payment_id)
    dest = f"/client/orders/{payment['order_id']}" if payment and payment["order_id"] else "/client/billing"
    if result.get("status") == "success" and payment:
        _activate_payment(payment, result.get("reference", payment_id), base_url=str(request.base_url))
        try:
            unlocking = json.loads(payment["meta"] or "{}").get("kind") == "trial_verification"
        except (ValueError, TypeError):
            unlocking = False
        if unlocking:                                         # the one-time unlock: straight back to what they were reading, now in full
            latest = next(iter(db.list_orders_for_client(payment["user_id"])), None)
            return RedirectResponse((f"/client/orders/{latest['id']}" if latest else "/client/home")
                                    + "?notice=You%27re+all+set.+Everything+is+unlocked%2C+and+your+%241+is+in+your+wallet.", status_code=303)
        return RedirectResponse(f"{dest}?notice=Payment+confirmed.", status_code=303)
    return RedirectResponse(f"{dest}?error=Payment+wasn%27t+confirmed+yet+-+give+it+a+minute+and+check+again.",
                             status_code=303)


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request):
    """The trusted path. Verifies the HMAC signature before touching
    anything - see billing.paystack_verify_webhook_signature's docstring
    for why that's non-negotiable."""
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature")
    if not billing.paystack_verify_webhook_signature(raw_body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    event = json.loads(raw_body)
    kind = event.get("event")
    data = event.get("data") or {}
    base_url = str(request.base_url)
    if kind == "charge.success":
        payment_id = data["reference"]
        payment = db.get_payment(payment_id)
        if payment:
            _activate_payment(payment, data.get("id", payment_id), base_url=base_url)
        else:
            # Not a payment we initiated: a recurring subscription RENEWAL,
            # which Paystack charges itself under its own reference.
            _handle_subscription_renewal(data, base_url)
    elif kind == "subscription.create":
        # Gives us the code + email_token needed to cancel it later. The
        # subscriptions row already exists by now (the first charge.success
        # activated the plan), but webhook order isn't guaranteed - retry
        # briefly by simply writing whenever we can find the user.
        customer = (data.get("customer") or {})
        user = db.get_user_by_email((customer.get("email") or "").strip().lower())
        if user and data.get("subscription_code"):
            db.set_paystack_subscription(user["id"], data["subscription_code"], data.get("email_token"))
    elif kind in ("subscription.disable", "subscription.not_renew"):
        # Nothing to revoke - the client keeps what they paid for until
        # current_period_end, then effective_plan drops them to Free. Just
        # stop treating them as auto-renewing (no more grace period).
        customer = (data.get("customer") or {})
        user = db.get_user_by_email((customer.get("email") or "").strip().lower())
        if user and dunning._open(user["id"]):
            dunning.finish(user, base_url)                  # Paystack gave up on a failing card: pause kindly, keep everything
        elif user:
            db.set_paystack_subscription(user["id"], None, None)
            db.create_notification(user["id"], "subscription_ended",
                                    "Your Kauli subscription won't renew",
                                    link="/client/billing")
    elif kind == "invoice.payment_failed":
        customer = (data.get("customer") or {})
        user = db.get_user_by_email((customer.get("email") or "").strip().lower())
        if user:
            db.create_notification(user["id"], "subscription_payment_failed",
                                    "Your Kauli subscription payment failed - update your card",
                                    link="/client/billing")
            notifications.notify_staff("Kauli: a subscription payment failed",
                                        f"{user['email']}'s recurring subscription charge failed.")
            dunning.record_failure(user, base_url, str(data.get("gateway_response") or data.get("message") or ""))
    return JSONResponse({"ok": True})


def _handle_subscription_renewal(data: dict, base_url: str) -> None:
    """charge.success for a charge we didn't initiate = Paystack renewing a
    recurring subscription. Matches the customer by email and the plan by its
    Paystack plan code (PAYSTACK_PLAN_*), records a real payment row, and runs
    it through _activate_payment so the plan period extends, a receipt is
    issued and emailed - exactly like a first purchase. Anything that doesn't
    match a known plan code + known customer is ignored, never guessed at."""
    plan_code = ((data.get("plan") or {}).get("plan_code") or "").strip()
    if not plan_code:
        return
    plan = next((p for p in billing.PLANS if p != "free" and plan_code in billing.paystack_plan_codes(p)), None)
    user = db.get_user_by_email(((data.get("customer") or {}).get("email") or "").strip().lower())
    provider_ref = str(data.get("id") or data.get("reference") or "")
    if not (plan and user and provider_ref):
        return
    payment_id = billing.new_reference()
    yearly = billing.paystack_plan_is_yearly(plan_code)
    amount_usd = billing.PLANS[plan]["price_usd_year" if yearly else "price_usd"]
    db.create_payment(payment_id, user["id"], plan, amount_usd, None, "USD", "paystack",
                       meta=json.dumps({"kind": "subscription_renewal_year" if yearly else "subscription_renewal"}))
    payment = db.get_payment(payment_id)
    if payment and not _activate_payment(payment, provider_ref, base_url=base_url):
        db.fail_payment(payment_id)  # a webhook retry for a renewal already recorded - don't leave a pending orphan


@app.post("/webhooks/mpesa")
async def mpesa_webhook(request: Request):
    """Safaricom's STK push result callback. No signature scheme like
    Paystack's - the mitigation is matching against a CheckoutRequestID we
    already stashed on a payment WE created (see _checkout), so an
    attacker would have to guess a live, pending reference, not just POST
    arbitrary JSON to grant themselves a plan or release someone's order."""
    body = await request.json()
    try:
        stk = body["Body"]["stkCallback"]
        checkout_request_id = stk["CheckoutRequestID"]
        result_code = stk["ResultCode"]
    except (KeyError, TypeError):
        return JSONResponse({"error": "unrecognised payload"}, status_code=400)

    payment = db.find_pending_mpesa_payment(checkout_request_id)
    if payment is None:
        return JSONResponse({"ok": True})  # unknown/already-handled - not an error to Safaricom

    if result_code == 0:
        receipt = None
        for item in stk.get("CallbackMetadata", {}).get("Item", []):
            if item.get("Name") == "MpesaReceiptNumber":
                receipt = item.get("Value")
        _activate_payment(payment, str(receipt or checkout_request_id), base_url=str(request.base_url))
    else:
        db.fail_payment(payment["id"])
        _note_payment_failure(payment["user_id"], payment["order_id"], "M-Pesa",
                               stk.get("ResultDesc") or f"result code {result_code}")
    return JSONResponse({"ok": True})


@app.post("/webhooks/calendly")
async def calendly_webhook(request: Request):
    """Not connected to anything yet - there's no KAULI_CALENDLY_URL set,
    so the booking widget on the marketing page doesn't even render (see
    _marketing_context). This is real, working code for the moment a real
    Calendly account exists: once KAULI_CALENDLY_URL is set AND a webhook
    subscription pointing at this URL is added in Calendly's own dashboard
    (Calendly's side, not something settable from here), a booking creates
    a real lead automatically instead of someone having to notice it
    happened. Field extraction below matches Calendly's documented v2
    webhook payload shape (event: 'invitee.created', payload.email/
    payload.name) - not yet exercised against a real Calendly webhook call,
    so treat the exact field names as worth double-checking against your
    own account's actual payload the first time this fires for real.
    No signature verification here yet either - Calendly signs webhooks
    with an HMAC (a Signing Key from the same dashboard where the webhook
    subscription is created); add that check once you have the key, the
    same way billing.paystack_verify_webhook_signature does it."""
    body = await request.json()
    if body.get("event") != "invitee.created":
        return JSONResponse({"ok": True})  # scheduling-page views, cancellations, etc. - not a new lead
    payload = body.get("payload", {})
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    if not name or "@" not in email:
        return JSONResponse({"ok": True})  # malformed/unexpected shape - don't 500 on Calendly's retry
    notes_parts = [f"{qa.get('question', '')}: {qa.get('answer', '')}"
                   for qa in payload.get("questions_and_answers", []) if qa.get("answer")]
    lead_id = db.create_lead(
        name, email, phone=None, company=None,
        message="Booked a call via Calendly." + ("\n" + "\n".join(notes_parts) if notes_parts else ""),
        preferred_time=None, source="calendly")
    _notify_internal_new_lead(lead_id, name, email, None, None, base_url=str(request.base_url))
    return JSONResponse({"ok": True})


@app.get("/staff/billing", response_class=HTMLResponse)
def staff_billing(request: Request, notice: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_billing.html", {
        "user": user, "pending_bank_payments": db.list_pending_bank_payments(),
        "notice": notice, "error": error,
        "queued_receipts": db.list_receipts_needing_delivery(),
        "email_configured": mailer.email_configured(),
    })


@app.post("/staff/receipts/{receipt_id}/mark-sent")
def staff_mark_receipt_sent(request: Request, receipt_id: str):
    """No transactional email provider exists in this app - see
    db.create_receipt's docstring. This is the honest manual step: staff
    actually forwarded the receipt (email, WhatsApp, whatever) and marks
    it done, same pattern as onboarding_messages' mark-sent action."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db.mark_receipt_sent(receipt_id)
    return RedirectResponse("/staff/billing", status_code=303)


@app.post("/staff/billing/grant-trial")
def staff_grant_trial(request: Request, client_email: str = Form(...), minutes: float = Form(...)):
    """Onboarding lever: a real prospect gets a bigger free look (e.g. 10
    minutes instead of the standard 2) before paying anything - staff
    discretion, no separate approval step beyond a staff member doing it."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if minutes <= 0 or minutes > 120:
        return RedirectResponse("/staff/billing?error=Enter+a+reasonable+minute+amount+(1-120).", status_code=303)
    ok = db.grant_bonus_minutes(client_email, minutes, user["display_name"])
    if not ok:
        return RedirectResponse(
            f"/staff/billing?error=No+client+account+found+for+{client_email}+-+"
            f"they+need+to+sign+up+first.", status_code=303)
    return RedirectResponse(
        f"/staff/billing?notice=Granted+{minutes:.0f}+trial+minutes+to+{client_email}.", status_code=303)


@app.post("/staff/billing/confirm/{payment_id}")
def staff_confirm_bank_payment(request: Request, payment_id: str, staff_note: str = Form(""),
                                receipt: UploadFile | None = File(None)):
    """The account-manager-assisted path: no automated bank reconciliation
    exists (nor does one realistically for arbitrary Kenyan bank transfers,
    M-Pesa till slips, or cash), so staff manually confirms once the
    payment's actually landed - optionally attaching the real receipt
    (photo or PDF) as the audit trail, same upload security pipeline
    (magic-byte check, ClamAV) as every other file in this app, just
    stored locally since there's no S3/cloud storage here."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    payment = db.get_payment(payment_id)
    if payment and payment["provider"] == "bank":
        receipt_path = None
        if receipt is not None and receipt.filename:
            try:
                RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
                audit = upload_security.validate_generic_upload(
                    receipt, RECEIPT_DIR / "placeholder", receipt.filename,
                    upload_security.ALLOWED_STYLE_GUIDE_EXTENSIONS)
                receipt_path = audit["final_path"]
            except upload_security.UploadRejected as exc:
                return RedirectResponse(
                    f"/staff/billing?error=Receipt+not+saved:+{exc}", status_code=303)
        if receipt_path or staff_note.strip():
            db.set_payment_receipt(payment_id, receipt_path, staff_note.strip() or None)
        # Must be unique per payment, not just per staff member - this was
        # `f"bank-confirmed-by-{user['id']}"` with nothing else, which
        # collided with provider_reference's UNIQUE constraint on every
        # bank confirmation after a given staff member's first one ever,
        # silently no-op'ing (still redirecting as if it worked) instead
        # of actually completing the payment. Found while verifying the
        # wallet top-up flow - affects every bank-transfer confirmation
        # in the app (plan purchases, order payments, surcharges, wallet
        # top-ups), not just this one.
        _activate_payment(payment, f"bank-confirmed-by-{user['id']}-{payment_id}", base_url=str(request.base_url))
    return RedirectResponse("/staff/billing", status_code=303)


@app.get("/staff/billing/receipt/{payment_id}")
def staff_view_receipt(request: Request, payment_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    payment = db.get_payment(payment_id)
    if not payment or not payment["receipt_path"] or not Path(payment["receipt_path"]).exists():
        return HTMLResponse("No receipt on file.", status_code=404)
    return FileResponse(payment["receipt_path"])


# ---------------------------------------------------------------- crm -----
# Our lead pipeline. Website submissions land here automatically (see
# /contact/request-callback); anything sourced elsewhere - an Instagram DM,
# a WhatsApp inquiry, a referral - staff log by hand via "Add lead" below.
# There's no live API pull from any social platform: each one needs its own
# developer account/OAuth app (Meta Business for Instagram & Facebook leads,
# WhatsApp Business API, etc.) that only you can set up - this is the
# honest, fully-working version until/unless that gets built.
@app.get("/staff/leads", response_class=HTMLResponse)
def staff_leads(request: Request, status: str | None = None, source: str | None = None,
                 org_type: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    pipeline = db.leads_pipeline_summary()
    by_source_chart = charts.horizontal_bar_chart([
        {"label": row["source"], "value": row["n"], "note": f"{row['won']} won"}
        for row in pipeline["by_source"]
    ], label_width=140) if len(pipeline["by_source"]) > 1 else ""
    by_org_type_chart = charts.horizontal_bar_chart([
        {"label": (ot or "unknown").replace("_", " "), "value": n}
        for ot, n in sorted(pipeline["by_org_type"].items(), key=lambda kv: -kv[1])
    ], label_width=140) if len(pipeline["by_org_type"]) > 1 else ""
    lead_days = site_analytics._days_list(time.time(), 14)
    conn = db.get_conn()
    try:
        made = {r["d"]: r["n"] for r in conn.execute("SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS n FROM leads WHERE created_at >= ? GROUP BY d", (time.time() - 15 * 86400,)).fetchall()}
        won_by_day = {r["d"]: r["n"] for r in conn.execute("SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS n FROM leads WHERE status = 'won' AND created_at >= ? GROUP BY d", (time.time() - 15 * 86400,)).fetchall()}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "staff_leads.html", {
        "an": site_analytics, "lead_source_items": [(r["source"], r["n"]) for r in pipeline["by_source"]], "lead_spark_new": site_analytics.spark([made.get(d, 0) for d in lead_days], 120, 34),
        "lead_spark_won": site_analytics.spark([won_by_day.get(d, 0) for d in lead_days], 120, 34, "#10b981"),
        "user": user, "leads": db.list_leads(status=status, source=source, org_type=org_type),
        "filter_status": status, "filter_source": source, "filter_org_type": org_type, "error": error,
        "pipeline": pipeline, "by_source_chart": by_source_chart, "by_org_type_chart": by_org_type_chart,
        "stale_leads": db.stale_leads(threshold_hours=48.0),
        "lead_statuses": db.LEAD_STATUSES, "lead_sources": db.LEAD_SOURCES,
        "now_ts": time.time(),
        "pending_onboarding_messages": db.list_onboarding_messages(status="pending_send"),
        "clients_needing_nudge": db.clients_needing_activation_nudge(threshold_hours=48.0),
        "pending_deletion_requests": db.list_deletion_requests(status="pending"),
    })


@app.get("/staff/exceptions", response_class=HTMLResponse)
def staff_exceptions(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_exceptions.html", {
        "user": user, "requests": db.list_exception_requests(status="open"),
    })


@app.get("/staff/security", response_class=HTMLResponse)
def staff_security(request: Request, kind: str | None = None):
    """Real audit trail (security_events) - login attempts, password
    changes, staff role grants/revokes, API key generation. Written to
    from many call sites across app.py; this is the one place it's
    actually read back, without which logging it would be pure write-only
    ceremony. Admin-only, not just staff - this spans every account on the
    platform (IPs, emails, failed-login patterns), a materially bigger
    blast radius than the day-to-day staff tooling around it."""
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_security.html", {
        "user": admin, "events": db.list_security_events(kind=kind or None), "filter_kind": kind,
    })


@app.post("/staff/exceptions/{request_id}/grant")
def staff_grant_exception(request: Request, request_id: str, staff_note: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    req = db.get_exception_request(request_id)
    if not req:
        return HTMLResponse("Not found.", status_code=404)
    if req["context"] == "order_submission_rate_limit":
        db.grant_trusted_submitter(req["client_id"], hours=24.0)
    # Any other context: no automatic mechanism exists for it yet (e.g. a
    # free-minutes/plan exception still goes through the existing
    # grant_bonus_minutes flow on the client's billing page by hand) -
    # this still records the decision either way, real paper trail intact.
    db.resolve_exception_request(request_id, "granted", user["id"], staff_note.strip() or None)
    return RedirectResponse("/staff/exceptions", status_code=303)


@app.post("/staff/exceptions/{request_id}/decline")
def staff_decline_exception(request: Request, request_id: str, staff_note: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    req = db.get_exception_request(request_id)
    if not req:
        return HTMLResponse("Not found.", status_code=404)
    db.resolve_exception_request(request_id, "declined", user["id"], staff_note.strip() or None)
    return RedirectResponse("/staff/exceptions", status_code=303)


@app.post("/staff/deletion-requests/{request_id}/resolve")
def staff_resolve_deletion_request(request: Request, request_id: str, status: str = Form("done")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if status not in ("done", "declined"):
        status = "done"
    db.resolve_deletion_request(request_id, resolved_by=user["id"], status=status)
    return RedirectResponse("/staff/leads", status_code=303)


@app.post("/staff/onboarding/{message_id}/mark-sent")
def staff_mark_onboarding_sent(request: Request, message_id: str):
    """Staff sent this by hand (copied it into an email or WhatsApp) - this
    just records that it happened, same "real action, no fake automation"
    idea as everything else in this queue. See db.queue_onboarding_message's
    docstring for why this exists instead of an actual send button."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db.mark_onboarding_message_sent(message_id)
    return RedirectResponse("/staff/leads", status_code=303)


def _send_activation_nudge(client, base_url: str) -> None:
    """The actual nudge send, shared by the manual staff button and the
    automatic 72-hour sweep below - one real copy of this email, not two
    that quietly drift apart. Guarded by has_onboarding_message the same
    way either caller already was, so a client can never get double-
    nudged just because both paths happened to run near each other."""
    if db.has_onboarding_message(client["id"], "inactivity_nudge"):
        return
    name = (client["display_name"] or client["email"].split("@")[0]).strip()
    subject = "Still thinking it over?"
    body = (
        f"Hi {name},\n\n"
        f"Noticed you signed up for Kauli but haven't uploaded anything yet - your "
        f"{billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are still sitting there unused.\n\n"
        "No pressure at all, just wanted to check if anything's unclear or in the way. "
        "Happy to walk you through it - just reply to this email.\n\n"
        f"Talk soon,\n{TEAM_NAME}"
    )
    nudge_cta_url = f"{base_url.rstrip('/')}/client"
    html_inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">Noticed you signed up for Kauli but haven\'t uploaded anything '
        f'yet - your {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are still sitting there unused.</p>'
        f'<p style="margin:0 0 14px;">No pressure at all, just wanted to check if anything\'s unclear '
        f'or in the way. Happy to walk you through it - '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email.</p>'
        f'<p style="margin:0;">Talk soon,<br>{TEAM_NAME}</p>'
    )
    _queue_and_send(client, "inactivity_nudge", subject, body, base_url=base_url,
                     cta_text="Upload your first order for free", cta_url=nudge_cta_url, html_inner=html_inner)
    db.set_onboarding_status(client["id"], "nudged")


@app.post("/staff/onboarding/{user_id}/nudge")
def staff_queue_nudge(request: Request, user_id: str):
    """Manual override for a client staff wants to chase sooner than the
    automatic 72-hour sweep below would get to them - same real email,
    same has_onboarding_message guard, just triggered by a person instead
    of waiting on the clock."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(user_id)
    if client:
        _send_activation_nudge(client, str(request.base_url))
    return RedirectResponse("/staff/leads", status_code=303)


@app.post("/staff/leads/new")
def staff_create_lead(request: Request, name: str = Form(""), email: str = Form(""),
                       phone: str = Form(""), company: str = Form(""),
                       message: str = Form(""), source: str = Form("other")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    name, email = name.strip(), email.strip().lower()
    if not name or "@" not in email or source not in db.LEAD_SOURCES:
        return RedirectResponse("/staff/leads?error=Enter+a+name%2C+valid+email+and+source.", status_code=303)
    db.create_lead(name, email, phone.strip() or None, company.strip() or None,
                    message.strip() or None, None, source=source, created_by=user["id"])
    return RedirectResponse("/staff/leads", status_code=303)


@app.get("/staff/leads/{lead_id}", response_class=HTMLResponse)
def staff_lead_detail(request: Request, lead_id: str, notice: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    lead = db.get_lead(lead_id)
    if not lead:
        return HTMLResponse("Lead not found.", status_code=404)
    converted_user = db.get_user(lead["converted_user_id"]) if lead["converted_user_id"] else None
    return templates.TemplateResponse(request, "staff_lead_detail.html", {
        "user": user, "lead": lead, "notes": db.list_lead_notes(lead_id),
        "converted_user": converted_user, "lead_statuses": db.LEAD_STATUSES,
        "notice": notice, "email_configured": mailer.email_configured(),
        "playbook": lead_playbook.get_playbook_stage(lead["org_type"], lead["status"], lead["company"] or lead["name"]),
    })


@app.post("/staff/leads/{lead_id}/status")
def staff_update_lead_status(request: Request, lead_id: str, status: str = Form(...),
                              next: str = Form("/staff/leads")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if status in db.LEAD_STATUSES:
        db.set_lead_status(lead_id, status, changed_by=user["id"])
    return RedirectResponse(next if next.startswith("/staff/leads") else "/staff/leads", status_code=303)


@app.post("/staff/leads/{lead_id}/notes")
def staff_add_lead_note(request: Request, lead_id: str, body: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if body.strip():
        db.add_lead_note(lead_id, user["id"], body.strip())
    return RedirectResponse(f"/staff/leads/{lead_id}", status_code=303)


@app.post("/staff/leads/{lead_id}/email")
def staff_email_lead(request: Request, lead_id: str, subject: str = Form(...), body: str = Form(...)):
    """Real, one-to-one outreach to a real inbound lead - sent from the
    SAME Kauli Operations address/infrastructure every transactional
    email already uses (mailer.send_email), logged to the lead's own
    activity timeline either way so there's a real record of what was
    sent and when, success or failure."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    lead = db.get_lead(lead_id)
    if not lead:
        return HTMLResponse("Lead not found.", status_code=404)
    html = mailer.wrap_email_html(f"<p>{body}</p>".replace("\n", "</p><p>"), base_url=str(request.base_url))
    ok, detail = mailer.send_email(lead["email"], subject.strip(), html, body.strip())
    note = (
        f"Email sent - \"{subject.strip()}\"\n\n{body.strip()}" if ok
        else f"Email FAILED to send - \"{subject.strip()}\": {detail}"
    )
    db.add_lead_note(lead_id, user["id"], note)
    notice = "Email sent." if ok else f"Couldn't send: {detail}"
    return RedirectResponse(f"/staff/leads/{lead_id}?notice={quote(notice)}", status_code=303)


@app.get("/orders/{order_id}/style-guide")
def download_style_guide(request: Request, order_id: str):
    """The client's own reference file (terminology, standards, examples) -
    readable by the client who uploaded it and any staff member, same
    ownership check as everything else order-scoped."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Not found.", status_code=404)
    if user["role"] == "client" and order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Not found.", status_code=404)
    if not order["style_guide_path"] or not Path(order["style_guide_path"]).exists():
        return HTMLResponse("No style guide on this order.", status_code=404)
    return FileResponse(order["style_guide_path"], filename=order["style_guide_filename"])


# ----------------------------------------------------- staff: clients ----
# The real, existing-account counterpart to /staff/leads (leads are people
# who HAVEN'T signed up yet). Deliberately scoped down from the fuller
# mockup this was built against: no Organizations/Workload/SLAs/Analytics/
# Reports/Glossary/Style-guide-library/Audit-Log pages, since none of that
# exists yet as real, backing functionality - see this session's own
# reasoning at the time. What's here (Overview/Orders/Team/Billing/
# Settings) is real, built on data that already exists elsewhere in this
# app, just not previously assembled into one place for staff.
@app.get("/staff/clients", response_class=HTMLResponse)
def staff_clients(request: Request, q: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    # A handful of real accounts, not thousands - one subscription lookup
    # per row is fine here and keeps list_clients_for_staff's own query
    # simple, rather than joining a whole extra table for it.
    rows = []
    for c in db.list_clients_for_staff(search=q):
        c = dict(c)
        c["plan"] = billing.effective_plan(c, db.get_subscription_current(c["id"]))
        rows.append(c)
    return templates.TemplateResponse(request, "staff_clients.html", {
        "user": user, "clients": rows, "q": q or "", "plans": billing.PLANS, "now_ts": time.time(),
    })


_STAFF_CLIENT_TABS = ("overview", "orders", "team", "billing", "settings")


@app.get("/staff/clients/{client_id}", response_class=HTMLResponse)
def staff_client_detail(request: Request, client_id: str, tab: str = "overview",
                         notice: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(client_id)
    if not client or client["role"] != "client":
        return HTMLResponse("Client not found.", status_code=404)
    if tab not in _STAFF_CLIENT_TABS:
        tab = "overview"
    subscription = db.get_subscription_current(client_id)
    return templates.TemplateResponse(request, "staff_client_detail.html", {
        "user": user, "client": client, "tab": tab, "notice": notice, "error": error,
        "stats": db.client_dashboard_stats(client_id),
        "plan": billing.effective_plan(client, subscription), "plans": billing.PLANS,
        "wallet_credits": db.wallet_credits_remaining(client_id),
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "orders": db.list_orders_for_client(client_id) if tab in ("orders", "overview") else [],
        "now_ts": time.time(),
        "team_members": db.list_team_members(client_id) if tab == "team" else [],
        "payments": db.list_payments_for_user(client_id) if tab == "billing" else [],
        "api_key_row": db.get_client_api_key(client_id) if tab == "settings" else None,
        "webhook_row": db.get_client_webhook(client_id) if tab == "settings" else None,
        "trial_payment": db.get_trial_verification_payment(client_id) if tab == "settings" else None,
        "markets": billing.MARKETS, "client_market": db.get_billing_market(client_id),
    })


@app.post("/staff/clients/{client_id}/internal")
def staff_client_set_internal(request: Request, client_id: str, internal: str = Form("")):
    """Team, test and friends-and-family accounts are left out of the business numbers on Site analytics."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(client_id)
    if not client or client["role"] != "client":
        return HTMLResponse("Client not found.", status_code=404)
    db.set_user_internal(client_id, bool(internal))
    return RedirectResponse(f"/staff/clients/{client_id}?notice={quote('Marked as an internal account: left out of the business numbers.' if internal else 'Counted as a real customer again.')}", status_code=303)


@app.post("/staff/clients/{client_id}/market")
def staff_client_set_market(request: Request, client_id: str, market: str = Form(...)):
    """Move an account to another price card (US/international, Kenya & East
    Africa, or back to the legacy rates). Staff-only on purpose - the market
    is fixed at signup precisely so a client can't pick the cheaper card."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(client_id)
    if not client or client["role"] != "client":
        return HTMLResponse("Client not found.", status_code=404)
    if market not in billing.MARKETS:
        return RedirectResponse(f"/staff/clients/{client_id}?tab=settings&error=Unknown+market.", status_code=303)
    db.set_billing_market(client_id, market)
    db.log_security_event("billing_market_changed", user_id=user["id"], email=client["email"],
                           ip_address=rate_limit.client_ip(request), detail=f"-> {market}")
    return RedirectResponse(f"/staff/clients/{client_id}?tab=settings&notice=Price+card+updated.", status_code=303)


@app.post("/staff/clients/{client_id}/notes")
def staff_client_save_notes(request: Request, client_id: str, notes: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if not db.get_user(client_id):
        return HTMLResponse("Client not found.", status_code=404)
    db.set_client_staff_notes(client_id, notes)
    return RedirectResponse(f"/staff/clients/{client_id}?tab=settings&notice=Notes+saved.", status_code=303)


@app.post("/staff/clients/{client_id}/reopen")
def staff_client_reopen(request: Request, client_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if not db.get_user(client_id):
        return HTMLResponse("Client not found.", status_code=404)
    db.reopen_account(client_id)
    return RedirectResponse(f"/staff/clients/{client_id}?tab=settings&notice=Account+reopened.", status_code=303)


@app.post("/staff/clients/{client_id}/close")
def staff_client_close(request: Request, client_id: str):
    """The staff-initiated counterpart to a client's own self-service
    close (Settings > Security) - for when the request comes in by email/
    WhatsApp instead of through the account itself."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if not db.get_user(client_id):
        return HTMLResponse("Client not found.", status_code=404)
    db.close_account(client_id)
    return RedirectResponse(f"/staff/clients/{client_id}?tab=settings&notice=Account+closed.", status_code=303)


@app.post("/staff/clients/{client_id}/refund-trial-verification")
def staff_refund_trial_verification(request: Request, client_id: str):
    """A genuine last resort - real money back to a real client's card,
    for when onboarding hasn't worked out despite staff actually trying.
    Manual, staff-only, never automatic: there's no computable "we tried
    hard enough" signal, that's a real human judgment call. Refunds the
    $1 charge on Paystack's side, and claws back whatever's left of the
    matching wallet credit (never below zero - if they already spent it
    on a real order, that part just isn't recoverable, which is fine;
    the point is not double-crediting them, not chasing spent money)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(client_id)
    if not client:
        return HTMLResponse("Client not found.", status_code=404)
    payment = db.get_trial_verification_payment(client_id)
    if not payment:
        return RedirectResponse(
            f"/staff/clients/{client_id}?tab=settings&error=No+trial-verification+payment+found+for+this+client.",
            status_code=303)
    if payment["refunded_at"]:
        return RedirectResponse(
            f"/staff/clients/{client_id}?tab=settings&error=Already+refunded.", status_code=303)
    result = billing.paystack_refund(payment["provider_reference"])
    if not result["ok"]:
        return RedirectResponse(
            f"/staff/clients/{client_id}?tab=settings&error=Refund+failed%3A+{quote(result['error'])}",
            status_code=303)
    db.mark_payment_refunded(payment["id"], payment["provider_reference"])
    db.consume_wallet_credits(client_id, billing.usd_to_credits(payment["amount_usd"]))
    return RedirectResponse(
        f"/staff/clients/{client_id}?tab=settings&notice=Refund+submitted+to+Paystack+"
        f"({quote(result['status'])})+-+it+can+take+a+few+days+to+reach+their+card.",
        status_code=303)


# --------------------------------------------- public API / integrations ----
# Lets a client's own systems (a portal, a script, Zapier/Make) reach Kauli
# without a browser session: an API key for pulling order status, and a
# webhook for having Kauli push the moment an order's ready, instead of the
# caller having to poll. Read-only on purpose for this pass - order
# SUBMISSION still only happens through the upload wizard, whose pricing/
# free-minutes/wallet-credit logic (see the route above this comment block)
# is real, load-bearing, and not something to fork into a second
# implementation without a dedicated pass of its own.

# Derived from the session secret rather than a second required env var,
# but hashed with a fixed, distinct label first so a signed deliverable
# link's HMAC key is never literally the same bytes as the session-signing
# key - a leaked link signature reveals nothing usable against sessions.
_LINK_SECRET = hashlib.sha256(f"{_session_secret}:deliverable-link".encode()).digest()


def _sign_deliverable(order_id: str, kind: str, expires_at: int) -> str:
    msg = f"{order_id}:{kind}:{expires_at}".encode()
    return hmac.new(_LINK_SECRET, msg, hashlib.sha256).hexdigest()


def _deliverable_link(request: Request, order_id: str, kind: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    """A signed, time-limited download URL that needs no login - what the
    webhook payload and the /api/v1/orders/{id} response hand back instead
    of a link only a logged-in browser could use."""
    expires_at = int(time.time()) + ttl_seconds
    sig = _sign_deliverable(order_id, kind, expires_at)
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/deliverables/{order_id}/{kind}?expires={expires_at}&sig={sig}"


def _deliverable_filename(order, kind: str) -> str | None:
    return {
        "audio": f"dub_{order['target_lang']}.wav",
        "srt": f"subs_{order['target_lang']}.srt",
        "vtt": f"subs_{order['target_lang']}.vtt",
        "transcript": f"transcript_{order['source_lang']}.srt",
        "burned": f"burned_captions_{order['target_lang']}.mp4",
        "dubbed": f"dubbed_video_{order['target_lang']}.mp4",
    }.get(kind)


def _deliverable_denied_reason(order, kind: str) -> str | None:
    """None if `kind` is really part of what this order bought and is
    safe to hand over right now, else the real reason it isn't. Same
    substantive rules as download_deliverable below, re-derived from the
    order itself (order['tier'] - the plan it was actually billed under,
    not whatever the client's plan happens to be today) rather than a
    live user/session, since a signed link or an API key call has neither -
    a signature alone is never enough to serve something never bought."""
    if order["status"] not in db.TERMINAL_OK_STATUSES:
        return "This order isn't ready yet."
    if _order_locked(order):
        return "Subscribe to the Free plan (pay as you go), or unlock this order, to download your files."
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    if kind in ("srt", "vtt") and not order_level["mt"]:
        return "This order doesn't include translated subtitles."
    if kind in ("txt", "docx") and not order_level["asr"]:
        return "This order doesn't include a transcript."
    if kind == "audio" and not order_level["tts"]:
        return "This order doesn't include a dubbed audio deliverable."
    if kind in ("burned", "dubbed"):
        has_video = (billing.PLANS.get(order["tier"], {}).get("video_deliverables")
                     or db.order_has_addon(order, "video_deliverables"))
        if not order_level["tts"] or not has_video:
            return "This order doesn't include a video deliverable."
    return None


@app.get("/api/v1/deliverables/{order_id}/{kind}")
def api_signed_deliverable(request: Request, order_id: str, kind: str, expires: int, sig: str):
    """The URL _deliverable_link hands out - real, but only valid until
    `expires` and only for the exact order_id/kind it was signed for."""
    allowed, retry_after = rate_limit.check(f"deliverable:{rate_limit.client_ip(request)}", limit=60, window_s=60)
    if not allowed:
        return HTMLResponse(f"Too many requests - try again in {retry_after}s.", status_code=429)
    if time.time() > expires:
        return HTMLResponse("This link has expired.", status_code=403)
    if not hmac.compare_digest(_sign_deliverable(order_id, kind, expires), sig):
        return HTMLResponse("Invalid or tampered link.", status_code=403)
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Not found.", status_code=404)
    denied = _deliverable_denied_reason(order, kind)
    if denied:
        return HTMLResponse(denied, status_code=404)
    if kind in ("txt", "docx"):
        job = _load_job(order)
        if job is None:
            return HTMLResponse("Not ready yet.", status_code=404)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(order["original_filename"] or "transcript").stem).strip("_") or "transcript"
        title = f"Transcript - {order['original_filename'] or order['id']}"
        if kind == "txt":
            data, media = transcript_export.to_txt(job, title), "text/plain; charset=utf-8"
        else:
            data = transcript_export.to_docx(job, title)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return Response(content=data, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{stem}_transcript.{kind}"'})
    filename = _deliverable_filename(order, kind)
    if not filename:
        return HTMLResponse("Unknown file.", status_code=404)
    path = Path(order["outdir"]) / filename
    if not path.exists():
        return HTMLResponse("Not ready yet.", status_code=404)
    return FileResponse(str(path), filename=filename)


def _client_id_from_api_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    raw_key = auth[7:].strip()
    if not raw_key:
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    client_id = db.find_client_by_api_key_hash(key_hash)
    if client_id:
        db.touch_api_key_last_used(client_id)
    return client_id


def _order_to_api_dict(request: Request, order) -> dict:
    deliverables = {}
    if _deliverable_denied_reason(order, "transcript") is None:
        deliverables["transcript"] = _deliverable_link(request, order["id"], "transcript")
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    for kind in ("srt", "vtt", "audio", "burned", "dubbed"):
        if order_level["mt" if kind in ("srt", "vtt") else "tts"] and _deliverable_denied_reason(order, kind) is None:
            deliverables[kind] = _deliverable_link(request, order["id"], kind)
    for kind in ("txt", "docx"):
        if _deliverable_denied_reason(order, kind) is None:
            deliverables[kind] = _deliverable_link(request, order["id"], kind)
    out = {
        "id": order["id"], "status": order["status"], "original_filename": order["original_filename"],
        "service_level": order["service_level"], "review_tier": order["review_tier"],
        "source_lang": order["source_lang"], "target_lang": order["target_lang"],
        "duration_minutes": order["duration_minutes"], "cost_usd": order["cost_usd"],
        "created_at": order["created_at"],
        "ready": order["status"] in db.TERMINAL_OK_STATUSES, "deliverables": deliverables,
    }
    if order["status"] == "pending_payment":
        out["payment_required"] = True
        out["payment_url"] = f"{str(request.base_url).rstrip('/')}/client/orders/{order['id']}/pay"
    if _order_locked(order):
        out["plan_required"] = True
        out["choose_plan_url"] = f"{str(request.base_url).rstrip('/')}/client/billing?choose_plan=1"
    return out


def _api_rate_limited(request: Request) -> JSONResponse | None:
    """Shared guard for every /api/v1 route - keyed on IP since a request
    can arrive with no key at all (or a wrong one), same reasoning as the
    login brute-force guard above. Generous enough for real integration
    use (polling a handful of orders a minute) while still ruling out
    someone hammering the endpoint trying to guess a valid key."""
    allowed, retry_after = rate_limit.check(f"api:{rate_limit.client_ip(request)}", limit=120, window_s=60)
    if not allowed:
        return JSONResponse({"error": f"rate limited - try again in {retry_after}s"}, status_code=429)
    return None


@app.get("/api/v1/orders/{order_id}")
def api_get_order(request: Request, order_id: str):
    limited = _api_rate_limited(request)
    if limited:
        return limited
    client_id = _client_id_from_api_key(request)
    if not client_id:
        return JSONResponse({"error": "invalid or missing API key - use Authorization: Bearer <key>"},
                             status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != client_id:
        return JSONResponse({"error": "order not found"}, status_code=404)
    return JSONResponse(_order_to_api_dict(request, order))


@app.get("/api/v1/orders")
def api_list_orders(request: Request, limit: int = 25):
    limited = _api_rate_limited(request)
    if limited:
        return limited
    client_id = _client_id_from_api_key(request)
    if not client_id:
        return JSONResponse({"error": "invalid or missing API key - use Authorization: Bearer <key>"},
                             status_code=401)
    limit = max(1, min(limit, 100))
    rows = db.list_orders_for_client(client_id)[:limit]
    return JSONResponse({"orders": [_order_to_api_dict(request, o) for o in rows]})


@app.get("/api/v1/languages")
def api_languages():
    """Public (no key) - every language code the API accepts and what each can
    be ordered as, so an integration can build its own picker."""
    return JSONResponse({"languages": [
        {"code": code, "name": name,
         "transcription": True,
         "translation_target": code != "ki" and (code in ("en", "sw") or _lara_configured()),
         "human_review": code in ("sw", "en", "ki"), "dubbing": code in ("sw", "en")}
        for code, name in sorted(LANGUAGE_NAMES.items(), key=lambda kv: kv[1])]})


@app.post("/api/v1/orders")
def api_create_order(
    request: Request,
    audio: UploadFile | None = File(None),
    url: str = Form(""),
    source_lang: str = Form("en"),
    target_lang: str = Form(""),
    service_level: str = Form("transcribe"),
    review_tier: str = Form("ai"),
    idempotency_key: str = Form(""),
    folder_name: str = Form(""),
    notes: str = Form(""),
):
    """Create an order from a file (multipart `audio`) or a YouTube `url`.
    Runs the SAME create_order() the dashboard uses - same validation,
    pricing, free-minutes and wallet rules - so the API can never be a way
    around any of them. An order that still needs payment comes back 402 with
    a payment_url; nothing is downloadable until it is paid, as everywhere."""
    limited = _api_rate_limited(request)
    if limited:
        return limited
    client_id = _client_id_from_api_key(request)
    row = db.get_user(client_id) if client_id else None
    if not row or row["account_status"] == "closed":
        return JSONResponse({"error": "invalid or missing API key - use Authorization: Bearer <key>"},
                             status_code=401)
    user = dict(row)
    if user["role"] != "client":
        return JSONResponse({"error": "API keys can only create orders for client accounts"}, status_code=403)
    user["client_scope_id"] = client_id
    request.state.api_user = user
    idem = (idempotency_key or request.headers.get("idempotency-key", "")).strip()
    service_level = service_level.strip() or "transcribe"
    target = target_lang.strip() or (source_lang if service_level == "transcribe" else "en")
    base = str(request.base_url).rstrip("/")
    try:
        resp = create_order(
            request, audio=audio, upload_key="", youtube_url=url, source_lang=source_lang.strip(),
            target_lang=target, service_level=service_level, addon_video_deliverables="",
            addon_human_voice_over="", addon_premium_voice="", premium_voice_id="", addon_audio_cleanup="",
            instr_speaker_ids="", instr_verbatim_level="clean_read",
            instr_transcribe_lyrics="", instr_use_italics="", instr_existing_subs="ignore",
            instr_no_audio="tag", instr_wrong_language="tag", instr_instrumental_only="tag",
            instr_notes=notes, style_guide=None, idempotency_key=idem, folder_name=folder_name,
            voice_clone_consent="", rush="", review_tier=review_tier.strip())
    except ApiOrderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    loc = resp.headers.get("location", "") if isinstance(resp, RedirectResponse) else ""
    m = re.match(r"^/client/orders/([0-9a-f]+)(/pay)?$", loc)
    if not m:
        return JSONResponse({"error": "could not create the order"}, status_code=400)
    order = db.get_order(m.group(1))
    return JSONResponse(_order_to_api_dict(request, order), status_code=402 if m.group(2) else 201)


_MCP_TEXT_LIMIT = 200_000


def _mcp_owned_order(client_id: str, order_id: str):
    order = db.get_order(str(order_id or "").strip())
    if not order or order["client_id"] != client_id:
        return None
    return order


def _mcp_call_tool(request: Request, client_id: str, name: str, args: dict) -> dict:
    """What each MCP tool does. Every one goes through the same helpers as the
    REST API - same key check, same ownership rule, same payment gate - so the
    assistant can never see or do more than the customer's own key could."""
    text = mcp_server.text_result
    if name == "kauli_list_languages":
        return text(json.loads(api_languages().body))
    if name == "kauli_create_order":
        resp = api_create_order(
            request, audio=None, url=str(args.get("url", "")), source_lang=str(args.get("source_lang", "en")),
            target_lang=str(args.get("target_lang", "")), service_level=str(args.get("service_level", "transcribe")),
            review_tier=str(args.get("review_tier", "ai")), idempotency_key=str(args.get("idempotency_key", "")),
            folder_name="", notes="")
        body = json.loads(resp.body)
        return text(body, is_error=resp.status_code not in (200, 201, 402))
    if name == "kauli_get_order":
        order = _mcp_owned_order(client_id, args.get("order_id"))
        return text(_order_to_api_dict(request, order)) if order else text("Order not found.", is_error=True)
    if name == "kauli_list_orders":
        try:
            limit = max(1, min(int(args.get("limit", 10)), 100))
        except (TypeError, ValueError):
            limit = 10
        return text({"orders": [_order_to_api_dict(request, o) for o in db.list_orders_for_client(client_id)[:limit]]})
    if name == "kauli_get_transcript":
        order = _mcp_owned_order(client_id, args.get("order_id"))
        if not order:
            return text("Order not found.", is_error=True)
        fmt = str(args.get("format", "txt")).lower()
        if fmt not in ("txt", "srt", "vtt"):
            return text("format must be txt, srt or vtt.", is_error=True)
        if order["status"] == "pending_payment":
            return text("This order is waiting for payment, so there is no transcript to read yet.", is_error=True)
        outdir = Path(order["outdir"])
        if fmt == "txt":
            denied = _deliverable_denied_reason(order, "txt")
            job = None if denied else _load_job(order)
            if denied or job is None:
                return text(denied or "Not ready yet.", is_error=True)
            title = f"Transcript - {order['original_filename'] or order['id']}"
            content = transcript_export.to_txt(job, title).decode("utf-8")
        else:
            # Translated subtitles when the order bought translation, else the source-language SRT.
            kind = fmt if _deliverable_denied_reason(order, fmt) is None else (
                "transcript" if fmt == "srt" else None)
            denied = _deliverable_denied_reason(order, kind) if kind else (
                "VTT subtitles are only part of translation orders - ask for txt or srt instead.")
            filename = _deliverable_filename(order, kind) if kind else None
            if denied or not filename or not (outdir / filename).exists():
                return text(denied or "Not ready yet.", is_error=True)
            content = (outdir / filename).read_text(encoding="utf-8", errors="replace")
        if len(content) > _MCP_TEXT_LIMIT:
            content = content[:_MCP_TEXT_LIMIT] + "\n\n[Truncated - download the full file with kauli_get_order's links.]"
        return text(content)
    return text(f"Unknown tool: {name}", is_error=True)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """Kauli's remote MCP server (Streamable HTTP, JSON responses). Connect a
    client with the customer's API key as a bearer token."""
    limited = _api_rate_limited(request)
    if limited:
        return limited
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                              "error": {"code": mcp_server.PARSE_ERROR, "message": "Invalid JSON"}}, status_code=400)
    client_id = _client_id_from_api_key(request)
    row = db.get_user(client_id) if client_id else None
    if row and row["account_status"] == "closed":
        client_id = None
    batch = payload if isinstance(payload, list) else [payload]
    if not batch:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {
            "code": mcp_server.INVALID_REQUEST, "message": "Empty batch"}}, status_code=400)

    def run(msg):
        return mcp_server.handle_message(
            msg, bool(client_id), lambda name, args: _mcp_call_tool(request, client_id, name, args))

    responses = [r for r in [await run_in_threadpool(run, m) for m in batch] if r is not None]
    if not responses:
        return Response(status_code=202)                   # only notifications
    if any(r.get("error", {}).get("code") == -32001 for r in responses):
        return JSONResponse(responses[0] if len(responses) == 1 else responses, status_code=401,
                             headers={"WWW-Authenticate": 'Bearer realm="kauli"'})
    return JSONResponse(responses if isinstance(payload, list) else responses[0])


@app.get("/mcp")
def mcp_get():
    # No server-initiated stream: this server only answers POSTed requests.
    return Response(status_code=405, headers={"Allow": "POST"})


def _is_safe_webhook_url(url: str) -> bool:
    """Blocks the obvious SSRF path: a client pointing their webhook at
    something on Kauli's own network (localhost, the Docker gateway, an
    internal service, a cloud metadata endpoint) so that WE end up making
    a request to it on their behalf. Checks the hostname's resolved IP,
    not just the literal string, since "not localhost" alone is trivially
    dodged by anything that resolves there. Not fully DNS-rebinding-proof
    (the same hostname could resolve differently between this check and
    the actual send) - a real gap, worth a proper allow/deny-listing
    HTTP client if this ever needs to be airtight, but this closes the
    easy version of the attack, which is what actually matters here."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except (ValueError, OSError):
        return False


_SLACK_WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+$")


def _is_slack_webhook_url(url: str) -> bool:
    """Exactly Slack's Incoming Webhook shape on exactly hooks.slack.com - the
    host is fixed, so unlike a free-form webhook URL this can never be pointed
    at Kauli's own network."""
    return bool(_SLACK_WEBHOOK_RE.match(url or ""))


def _post_to_slack(client_id: str, order_id: str, event: str, url: str, payload: dict) -> tuple[bool, str | None]:
    """One real POST to the client's Slack webhook, logged in the same delivery
    list as their other webhook. Never follows redirects, never raises."""
    try:
        resp = httpx.post(url, json=payload, timeout=10.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        db.log_webhook_delivery(client_id, order_id, event, False, None, str(exc)[:300])
        return False, str(exc)[:200]
    ok = resp.status_code == 200
    detail = None if ok else (resp.text or f"HTTP {resp.status_code}")[:200]
    db.log_webhook_delivery(client_id, order_id, event, ok, resp.status_code, detail)
    return ok, detail


def _slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SLACK_SERVICE_LABELS = {"transcribe": "Transcription", "translate": "Transcription + translation", "dub": "Full dub"}


def _slack_order_ready_message(request, order, include_links: bool) -> dict:
    base = str(request.base_url).rstrip("/")
    name = _slack_escape(order["original_filename"] or order["id"])
    service = _SLACK_SERVICE_LABELS.get(order["service_level"] or "transcribe", "Order")
    src, tgt = order["source_lang"], order["target_lang"]
    langs = LANGUAGE_NAMES.get(src, src) + (f" \u2192 {LANGUAGE_NAMES.get(tgt, tgt)}" if tgt and tgt != src else "")
    buttons = [{"type": "button", "text": {"type": "plain_text", "text": "Open order"},
                "url": f"{base}/client/orders/{order['id']}", "style": "primary"}]
    if include_links:
        links = _order_to_api_dict(request, order)["deliverables"]
        for kind, label in (("txt", "Transcript (TXT)"), ("docx", "Transcript (Word)"), ("srt", "Subtitles (SRT)"),
                            ("vtt", "Subtitles (VTT)")):
            if kind in links and len(buttons) < 4:
                buttons.append({"type": "button", "text": {"type": "plain_text", "text": label}, "url": links[kind]})
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Your Kauli order is ready"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*File*\n{name}"},
            {"type": "mrkdwn", "text": f"*Service*\n{service}"},
            {"type": "mrkdwn", "text": f"*Languages*\n{_slack_escape(langs)}"},
            {"type": "mrkdwn", "text": f"*Order*\n{order['id']}"}]},
    ]
    if _order_locked(order):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Unlock this order in Kauli to download it."}]})
    blocks.append({"type": "actions", "elements": buttons})
    return {"text": f"Your Kauli order is ready: {order['original_filename'] or order['id']}", "blocks": blocks}


def _notify_slack_order_ready(request, order) -> None:
    row = db.get_client_slack(order["client_id"])
    if not row or not _is_slack_webhook_url(row["webhook_url"]):
        return
    _post_to_slack(order["client_id"], order["id"], "slack.order.ready", row["webhook_url"],
                   _slack_order_ready_message(request, order, bool(row["include_links"])))


def _fire_client_webhook(request: Request, order, event: str = "order.ready") -> None:
    """Real outbound POST, fired once, right when the client-facing 'order
    ready' email/notification already go out (see staff_approve below). No
    retry queue - this is a single solo-operator server, not infrastructure
    for one; a failed delivery is logged (see webhook_deliveries) and the
    client can always still see the same info by calling GET
    /api/v1/orders/{id} or just using the dashboard, so nothing is actually
    lost if a webhook attempt fails. Errors are swallowed - a client's
    misconfigured or offline endpoint must never break the real approval
    flow it's hanging off of."""
    try:                                   # Slack is independent of the signed webhook below
        if event == "order.ready":
            _notify_slack_order_ready(request, order)
    except Exception:
        pass
    try:                                   # so is Notion
        if event == "order.ready":
            _notify_notion_order_ready(request, order)
    except Exception:
        pass
    try:                                   # and the one-click connections (Google Docs, Slack)
        if event == "order.ready":
            connections.deliver_in_background(order, _public_base_url(request))
    except Exception:
        pass
    try:                                   # and Microsoft Teams
        if event == "order.ready":
            _notify_teams_order_ready(request, order)
    except Exception:
        pass
    hook = db.get_client_webhook(order["client_id"])
    if not hook:
        return
    # Re-checked at send time, not just when the URL was saved - closes
    # the gap where DNS for an already-saved hostname later resolves
    # somewhere internal (rebinding), or a URL saved before this check
    # existed never got validated at all.
    if not _is_safe_webhook_url(hook["url"]):
        db.log_webhook_delivery(order["client_id"], order["id"], event, False, None,
                                 "webhook URL failed the safety check at send time")
        return
    payload = {"event": event, "order": _order_to_api_dict(request, order)}
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(hook["secret"].encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    try:
        resp = httpx.post(
            hook["url"], content=body, timeout=10.0, follow_redirects=False,
            # follow_redirects=False on purpose - a redirect could otherwise
            # be used to steer this request somewhere internal after the
            # safety check above already passed on the original URL.
            headers={"Content-Type": "application/json", "X-Kauli-Timestamp": ts,
                     "X-Kauli-Signature": f"sha256={sig}"},
        )
        db.log_webhook_delivery(order["client_id"], order["id"], event, 200 <= resp.status_code < 300,
                                 resp.status_code)
    except httpx.HTTPError as exc:
        db.log_webhook_delivery(order["client_id"], order["id"], event, False, None, str(exc)[:300])


# ------------------------------------------------------------------------------------------------ transcript tools
# Download with options (format, which side, how it is split, timestamps, speaker names) and a read-only share link. Both only
# ever hand over what _deliverable_denied_reason already allows for the order, so a transcript that is not paid for, not ready
# or not part of the order is never served by either.
EXPORT_FORMATS = {
    "txt": ("text/plain; charset=utf-8", "txt"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "pdf": ("application/pdf", "pdf"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "srt": ("application/x-subrip; charset=utf-8", "srt"),
}


def _flag(value: str | None, default: bool = True) -> bool:
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _export_transcript(job, order, fmt: str, side: str, split: str, timestamps: bool, speakers: bool) -> tuple[bytes, str]:
    """(bytes, filename) for a transcript download; raises ValueError with a customer-readable reason."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError("Choose a format: Word, PDF, text, SRT or CSV.")
    side = side if side in ("source", "target") else "source"
    split = split if split in ("line", "speaker", "single") else "line"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(order["original_filename"] or "transcript").stem).strip("_") or "transcript"
    title = f"Transcript - {order['original_filename'] or order['id']}"
    if fmt == "txt":
        data = transcript_export.to_txt(job, title, timestamps, speakers, split, side)
    elif fmt == "docx":
        data = transcript_export.to_docx(job, title, timestamps, speakers, split, side)
    elif fmt == "pdf":
        data = transcript_export.to_pdf(job, title, timestamps, speakers, split, side)
    elif fmt == "csv":
        data = transcript_export.to_csv(job, timestamps, speakers, side)
    else:
        try:
            text = to_srt(job, source=(side != "target"))
        except (AttributeError, TypeError):
            text = ""
        if not text.strip():
            raise ValueError("There is nothing to put in a subtitle file yet.")
        data = text.encode("utf-8")
    return data, f"{stem}_transcript.{EXPORT_FORMATS[fmt][1]}"


def _own_finished_order(request: Request, order_id: str):
    """(user, order, None) for a signed-in client's own finished transcript, else (None, None, a ready error response)."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return None, None, JSONResponse({"error": "Sign in to continue."}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return None, None, JSONResponse({"error": "Order not found."}, status_code=404)
    denied = _deliverable_denied_reason(order, "txt")
    if denied:
        return None, None, JSONResponse({"error": denied}, status_code=403)
    return user, order, None


@app.get("/client/orders/{order_id}/export")
def client_export_transcript(request: Request, order_id: str, format: str = "docx", side: str = "source", split: str = "line",
                             timestamps: str | None = None, speakers: str | None = None):
    user, order, err = _own_finished_order(request, order_id)
    if err:
        return err
    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "Not ready yet."}, status_code=404)
    try:
        data, filename = _export_transcript(job, order, format.lower(), side, split, _flag(timestamps), _flag(speakers))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    account_hub.track_feature(request, "transcript_download", format=format.lower(), side=side, split=split)
    return Response(content=data, media_type=EXPORT_FORMATS[format.lower()][0],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@app.post("/client/orders/{order_id}/share")
async def client_share_transcript(request: Request, order_id: str):
    """Body {"action": "create" | "revoke" | "email", "to": "..."}. create returns the link and a WhatsApp link."""
    user, order, err = _own_finished_order(request, order_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") if isinstance(body, dict) else None) or "create"
    if action == "revoke":
        return JSONResponse({"ok": True, "revoked": db.revoke_order_shares(order_id, user["client_scope_id"])})
    allowed, retry_after = rate_limit.check(f"share:{user['id']}", limit=20, window_s=600)
    if not allowed:
        return JSONResponse({"error": f"Too many share requests. Try again in {retry_after}s."}, status_code=429)
    token = db.create_order_share(order_id, user["client_scope_id"])
    account_hub.track_feature(request, "transcript_share", method=action)
    url = f"{_site_base(request)}/s/{token}"
    name = order["original_filename"] or "a transcript"
    if action == "email":
        to = str(body.get("to") or "").strip()
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to) or len(to) > 254:
            return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
        if not mailer.email_configured():
            return JSONResponse({"error": "Email sending is not set up yet. Copy the link instead."}, status_code=503)
        sender = user["display_name"] or user["email"]
        html = mailer.wrap_email_html(
            mailer.text_to_html_paragraphs(f"{sender} shared the transcript of \"{name}\" with you. The link is read-only."),
            cta_text="Open the transcript", cta_url=url, base_url=_site_base(request))
        ok, detail = mailer.send_email(to, f"{sender} shared a transcript with you", html,
                                       text_body=f"{sender} shared the transcript of \"{name}\" with you: {url}", tags=["share"])
        if not ok:
            return JSONResponse({"error": "Could not send that email. Copy the link instead."}, status_code=502)
        return JSONResponse({"ok": True, "url": url, "emailed": to})
    return JSONResponse({"ok": True, "url": url,
                         "whatsapp": "https://wa.me/?text=" + quote(f"Transcript of {name}: {url}"),
                         "mailto": "mailto:?subject=" + quote(f"Transcript: {name}") + "&body=" + quote(url)})


@app.get("/s/{token}", response_class=HTMLResponse)
def shared_transcript(request: Request, token: str):
    allowed, retry_after = rate_limit.check(f"shared:{rate_limit.client_ip(request)}", limit=60, window_s=60)
    if not allowed:
        return HTMLResponse(f"Too many requests. Try again in {retry_after}s.", status_code=429)
    share = db.get_order_share(token)
    order = db.get_order(share["order_id"]) if share else None
    job = _load_job(order) if order and _deliverable_denied_reason(order, "txt") is None else None
    if not share or not order or job is None:
        return templates.TemplateResponse(request, "share_transcript.html", {"missing": True, "order": None, "rows": []}, status_code=404)
    db.count_order_share_view(token)
    same = order["service_level"] == "transcribe" or order["source_lang"] == order["target_lang"]
    return templates.TemplateResponse(request, "share_transcript.html", {
        "missing": False, "order": order, "same_language": same,
        "rows": transcript_export.rows(job, "source"),
        "target_by_start": {} if same else {r["start"]: r["text"] for r in transcript_export.rows(job, "target")},
        "clock": transcript_export._clock,
    })


@app.get("/client/orders/{order_id}/download/{kind}")
def download_deliverable(request: Request, order_id: str, kind: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Not found.", status_code=404)
    if user["role"] == "client" and order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Not found.", status_code=404)

    # The one hard rule: process, but don't download until it's paid for -
    # by the free monthly allowance or by real money, doesn't matter which.
    # An order's status says exactly that: 'pending_payment' means it
    # hasn't been covered yet; anything else means it has. Staff bypass
    # this (they need the files to do review work) - only gated for the
    # client who owns the order.
    if user["role"] == "client":
        if order["status"] == "pending_payment":
            return RedirectResponse(f"/client/orders/{order_id}/pay", status_code=303)
        if order["difficulty_surcharge_status"] == "pending_approval":
            return RedirectResponse(f"/client/orders/{order_id}/difficulty-surcharge", status_code=303)
        if _order_locked(order):
            # Nothing downloads until a plan is chosen - the Free plan (pay as
            # you go: it charges for the order being opened) is enough, and covers results
            # made with the free monthly minutes.
            return RedirectResponse("/client/billing?choose_plan=1", status_code=303)
        plan = billing.effective_plan(user, db.get_subscription(user["client_scope_id"]))
        has_video = billing.PLANS[plan]["video_deliverables"] or db.order_has_addon(order, "video_deliverables")
        if (kind in ("burned", "dubbed") or kind.startswith("captioned_")) and not has_video:
            return RedirectResponse("/client/billing?upgrade_for=video", status_code=303)
        # Real enforcement, not just a hidden UI button - a transcription-
        # only or translation-only order's mt/tts providers are set to
        # 'stub' by design (billing.SERVICE_LEVELS), but kauli.pipeline.run
        # still unconditionally WRITES subs_*.srt/.vtt and dub_*.wav either
        # way (stub content, not real translated subtitles or real dub
        # audio) - those files genuinely exist on disk and were
        # downloadable by direct URL even with the matching button removed
        # from order_detail.html. A client only ever gets a deliverable
        # that was actually part of what they bought.
        order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
        if kind in ("srt", "vtt") and not order_level["mt"]:
            return HTMLResponse("This order doesn't include translated subtitles.", status_code=404)
        if kind in ("audio", "burned", "dubbed") and not order_level["tts"]:
            return HTMLResponse("This order doesn't include a dubbed audio/video deliverable.", status_code=404)

    if kind in ("ttml", "sbv"):
        # Generated on demand: TTML (broadcast and many web players) and SBV (YouTube's uploader). Translated captions when the
        # order has a translation, otherwise the transcript itself.
        if order["status"] not in db.TERMINAL_OK_STATUSES and user["role"] == "client":
            return HTMLResponse("This order isn't ready yet.", status_code=404)
        job = _load_job(order)
        if job is None:
            return HTMLResponse("Not ready yet.", status_code=404)
        translated = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])["mt"]
        lang = order["target_lang"] if translated else order["source_lang"]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(order["original_filename"] or "captions").stem).strip("_") or "captions"
        if kind == "ttml":
            data, media = caption_formats.to_ttml(job, lang=lang, source=not translated), "application/ttml+xml; charset=utf-8"
        else:
            data, media = caption_formats.to_sbv(job, source=not translated), "text/plain; charset=utf-8"
        return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{stem}_{lang}.{kind}"'})

    if kind.startswith("captioned_") and kind[len("captioned_"):] in CAPTION_LOOKS:
        look = kind[len("captioned_"):]
        path = _captioned_path(order, look)
        if not path.exists():
            return HTMLResponse("Not ready yet.", status_code=404)
        return FileResponse(str(path), filename=path.name)

    if kind in ("txt", "docx"):
        # Generated on demand from the finished job - plain text and Word are
        # what most people expect from a transcription service, next to SRT.
        if order["status"] not in db.TERMINAL_OK_STATUSES and user["role"] == "client":
            return HTMLResponse("This order isn't ready yet.", status_code=404)
        job = _load_job(order)
        if job is None:
            return HTMLResponse("Not ready yet.", status_code=404)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(order["original_filename"] or "transcript").stem).strip("_") or "transcript"
        title = f"Transcript - {order['original_filename'] or order['id']}"
        if kind == "txt":
            data, media = transcript_export.to_txt(job, title), "text/plain; charset=utf-8"
        else:
            data = transcript_export.to_docx(job, title)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return Response(content=data, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{stem}_transcript.{kind}"'})

    if kind == "source":
        # The file the client uploaded, for the preview player on the order page (same payment and plan gates as
        # every other download above). Served inline, with range support, so video can be scrubbed.
        import mimetypes
        src = Path(order["audio_path"] or "")
        if not src.is_file():
            return HTMLResponse("The original file is no longer stored.", status_code=404)
        guessed = mimetypes.guess_type(src.name)[0] or ("video/mp4" if is_video_file(str(src)) else "audio/mpeg")
        return FileResponse(str(src), media_type=guessed, content_disposition_type="inline")

    files = {
        "audio": f"dub_{order['target_lang']}.wav",
        "srt": f"subs_{order['target_lang']}.srt",
        "vtt": f"subs_{order['target_lang']}.vtt",
        "transcript": f"transcript_{order['source_lang']}.srt",
        "burned": f"burned_captions_{order['target_lang']}.mp4",
        "dubbed": f"dubbed_video_{order['target_lang']}.mp4",
    }
    filename = files.get(kind)
    if not filename:
        return HTMLResponse("Unknown file.", status_code=404)
    path = Path(order["outdir"]) / filename
    if not path.exists():
        return HTMLResponse("Not ready yet.", status_code=404)
    return FileResponse(str(path), filename=filename)


def _render_video_deliverable_for_order(order, kind: str) -> None:
    """The actual render, shared by the manual staff button below and the
    automatic post-approval trigger (see _maybe_auto_render_video_
    deliverables). Raises on any real problem - callers decide what that
    means for them (an HTTP error for a human waiting on the button; a
    logged warning for the background auto-trigger, which must never take
    down the thread it runs on)."""
    outdir = Path(order["outdir"])
    if is_video_file(order["audio_path"]):
        video_path = Path(order["audio_path"])
    elif order["source_youtube_id"]:
        # The one place that ever fetches the actual YouTube video - see
        # fetch_youtube_video's own docstring - so this only happens for
        # an order that actually needs it (real video-deliverable
        # entitlement), not on every YouTube-sourced order.
        video_path = fetch_youtube_video(order["source_youtube_id"], outdir / "video_cache")
    else:
        raise ValueError("no video source - audio-only upload")

    if kind == "burned":
        srt_path = outdir / f"subs_{order['target_lang']}.srt"
        if not srt_path.exists():
            raise FileNotFoundError("subtitles aren't ready yet")
        render_burned_captions(video_path, srt_path, outdir / f"burned_captions_{order['target_lang']}.mp4")
    elif kind == "dubbed":
        dub_path = outdir / f"dub_{order['target_lang']}.wav"
        if not dub_path.exists():
            raise FileNotFoundError("dubbed audio isn't ready yet")
        render_dubbed_video(video_path, dub_path, outdir / f"dubbed_video_{order['target_lang']}.mp4")
    else:
        raise ValueError(f"unknown render kind: {kind}")


def _maybe_auto_render_video_deliverables(order) -> None:
    """Fires the moment an order's approved (see staff_approve) for any
    order that actually bought video deliverables and has a real video
    source - closes a real gap where approval could go through without
    staff remembering the separate manual "Generate burned-in captions"/
    "Generate dubbed video" buttons on staff_review, leaving a client
    stuck seeing "ask us" for something they already paid for.

    Runs in a background thread, not inline in the request: burned-in
    captions specifically re-encode the whole video (ffmpeg's subtitles
    filter can't use -c:v copy), which is real minutes for a longer file -
    approve shouldn't hang waiting on that, or risk a proxy timeout.
    order_detail.html shows an honest "still generating" state for
    burned/dubbed specifically until the file actually lands; the rest of
    the delivery (transcript/subtitles/dubbed audio) is available right
    away regardless."""
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    if not order_level["tts"]:
        return  # not a dub order - nothing to burn captions onto or dub in the first place
    has_video_deliverables = (billing.PLANS.get(order["tier"], {}).get("video_deliverables")
                               or db.order_has_addon(order, "video_deliverables"))
    if not has_video_deliverables:
        return
    if not (is_video_file(order["audio_path"]) or order["source_youtube_id"]):
        return  # audio-only upload - nothing to burn captions onto or dub

    outdir = Path(order["outdir"])
    pending = [
        kind for kind in ("burned", "dubbed")
        if not (outdir / f"{'burned_captions' if kind == 'burned' else 'dubbed_video'}_{order['target_lang']}.mp4").exists()
    ]
    if not pending:
        return  # staff already generated these manually during review

    def _run():
        for kind in pending:
            try:
                _render_video_deliverable_for_order(order, kind)
            except Exception as exc:  # noqa: BLE001 - one kind failing must never take the thread (or the other kind) down
                api_log.warning(f"auto video-deliverable render failed for order {order['id']} ({kind}): {exc}")

    threading.Thread(target=_run, daemon=True, name=f"video-render-{order['id']}").start()


# ------------------------------------------------- captioned video: choose a look ----
# Beyond one fixed style: the customer picks how burned-in captions look (see kauli/caption_formats.py), including the "social"
# look where each word lights up as it is spoken. Each look is its own file, made on request in the background.
CAPTION_LOOKS = caption_formats.STYLES
_CAPTION_RENDER_SLOT = threading.Semaphore(1)       # one re-encode at a time: the server is shared with everything else
_RENDER_STALE_SECONDS = 45 * 60


def _captioned_path(order, style: str) -> Path:
    return Path(order["outdir"]) / f"captioned_{style}_{order['target_lang']}.mp4"


def _captioned_state(order, style: str) -> str | None:
    """"ready", "rendering", "failed" or None, from the files the render leaves behind."""
    out = _captioned_path(order, style)
    if out.exists():
        return "ready"
    marker = out.with_suffix(".rendering")
    if marker.exists():
        if time.time() - marker.stat().st_mtime < _RENDER_STALE_SECONDS:
            return "rendering"
        marker.unlink(missing_ok=True)                  # the server restarted mid-render: let it be tried again
        return "failed"
    return "failed" if out.with_suffix(".failed").exists() else None


def _video_size(path: Path) -> tuple[int, int]:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                             capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:  # noqa: BLE001 - a size we cannot read just means the standard size
        return 1280, 720


def _video_seconds(path: Path) -> float:
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                    capture_output=True, text=True, timeout=60, check=True).stdout.strip() or 0)
    except Exception:  # noqa: BLE001 - without a length there is simply no percentage
        return 0.0


def render_styled_captions(video_path: Path, ass_path: Path, out_path: Path, progress=None) -> None:
    """Burn the captions in. progress(percent) is called as ffmpeg reports how far it has got, so the page can show a real percentage."""
    escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vf", f"ass='{escaped}'",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(out_path)]
    total = _video_seconds(video_path) if progress else 0.0
    if not (progress and total > 0):
        subprocess.run(cmd, check=True, timeout=3600)
        return
    proc = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):           # both are microseconds in practice
                try:
                    progress(min(99, int(float(line.split("=", 1)[1]) / 1_000_000 / total * 100)))
                except ValueError:
                    pass
        if proc.wait(timeout=3600) != 0:
            raise subprocess.CalledProcessError(proc.returncode, "ffmpeg", stderr=proc.stderr.read())
    finally:
        if proc.poll() is None:
            proc.kill()


def _captioned_progress(order, style: str) -> int | None:
    try:
        return int(_captioned_path(order, style).with_suffix(".progress").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _render_captioned_video(order, style: str) -> None:
    """The background job behind "Create captioned video". Leaves captioned_<look>_<lang>.mp4 on success or a .failed note on
    trouble, and always removes its .rendering marker."""
    out = _captioned_path(order, style)
    marker, failed = out.with_suffix(".rendering"), out.with_suffix(".failed")
    try:
        with _CAPTION_RENDER_SLOT:
            outdir = Path(order["outdir"])
            if is_video_file(order["audio_path"]):
                video_path = Path(order["audio_path"])
            elif order["source_youtube_id"]:
                video_path = fetch_youtube_video(order["source_youtube_id"], outdir / "video_cache")
            else:
                raise ValueError("no video source - audio-only upload")
            job = _load_job(order)
            if not job or not job.segments:
                raise FileNotFoundError("the transcript is not ready")
            if getattr(job, "top_ranges", None) is None:                              # not looked at yet: find the video's own text and graphics, so the captions keep clear of them
                job.top_ranges = screen_text.analyse(str(video_path), outdir) or None
            width, height = _video_size(video_path)
            has_translation = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])["mt"]
            ass = out.with_suffix(".ass")
            ass.write_text(caption_formats.to_ass(job, style, source=not has_translation, width=width, height=height), encoding="utf-8")
            part = out.with_suffix(".part.mp4")
            progress_file = out.with_suffix(".progress")
            render_styled_captions(video_path, ass, part, progress=lambda pct: progress_file.write_text(str(pct), encoding="utf-8"))
            part.replace(out)
            failed.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - recorded for the page, never raised into the thread
        failed.write_text(str(exc)[:300], encoding="utf-8")
        api_log.warning(f"captioned video render failed for order {order['id']} ({style}): {exc}")
    finally:
        marker.unlink(missing_ok=True)
        out.with_suffix(".progress").unlink(missing_ok=True)


@app.get("/client/orders/{order_id}/captioned-video/status")
def client_captioned_video_status(request: Request, order_id: str, style: str = "classic"):
    """The page asks this every few seconds while a captioned video is being made: {state, pct}."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "sign in"}, status_code=401)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"] or style not in CAPTION_LOOKS:
        return JSONResponse({"error": "not found"}, status_code=404)
    state = _captioned_state(order, style)
    return JSONResponse({"state": state, "pct": _captioned_progress(order, style) if state == "rendering" else (100 if state == "ready" else None)},
                        headers={"Cache-Control": "no-store"})


@app.post("/client/orders/{order_id}/captioned-video")
def client_make_captioned_video(request: Request, order_id: str, style: str = Form("classic")):
    from urllib.parse import quote_plus
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)

    def back(kind: str, message: str):
        return RedirectResponse(f"/client/orders/{order_id}?{kind}={quote_plus(message)}#captioned-video", status_code=303)

    if order["status"] not in db.TERMINAL_OK_STATUSES:
        return back("error", "This order is not ready yet.")
    if _order_locked(order):
        return RedirectResponse("/client/billing?choose_plan=1" if not _plan_chosen(user["client_scope_id"]) else f"/client/orders/{order_id}", status_code=303)
    if not (billing.PLANS.get(order["tier"], {}).get("video_deliverables") or db.order_has_addon(order, "video_deliverables")):
        return RedirectResponse("/client/billing?upgrade_for=video", status_code=303)
    if style not in CAPTION_LOOKS:
        return back("error", "Choose one of the caption looks.")
    if not (is_video_file(order["audio_path"]) or order["source_youtube_id"]):
        return back("error", "This order was submitted as audio only, so there is no video to put captions on.")
    job = _load_job(order)
    if not job or not job.segments:
        return back("error", "The transcript is not ready yet.")
    state = _captioned_state(order, style)
    if state == "ready":
        return back("notice", "That captioned video is already ready.")
    if state == "rendering":
        return back("notice", "That captioned video is already being made.")
    out = _captioned_path(order, style)
    out.with_suffix(".failed").unlink(missing_ok=True)
    out.with_suffix(".rendering").write_text(str(time.time()), encoding="utf-8")
    threading.Thread(target=_render_captioned_video, args=(order, style), daemon=True, name=f"captioned-{order_id}").start()
    return back("notice", "Creating your captioned video. Longer videos take a few minutes: this page updates by itself.")


@app.post("/staff/orders/{order_id}/render/{kind}")
def render_video_deliverable(request: Request, order_id: str, kind: str):
    """The manual staff button on staff_review - same underlying render
    as the automatic trigger above, just synchronous and HTTP-error-
    reporting since a human's waiting on it."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if kind not in ("burned", "dubbed"):
        return HTMLResponse("Unknown render kind.", status_code=404)
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)

    try:
        _render_video_deliverable_for_order(order, kind)
    except subprocess.CalledProcessError as exc:
        return HTMLResponse(f"ffmpeg render failed: {exc}", status_code=500)
    except (ValueError, FileNotFoundError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except Exception as exc:
        return HTMLResponse(f"Render failed: {exc}", status_code=500)

    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


# ------------------------------------------------------- staff admin ----
def _require_admin(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return None
    return user


@app.get("/staff/admin", response_class=HTMLResponse)
def staff_admin(request: Request, error: str | None = None, notice: str | None = None):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_admin.html", {
        "user": admin,
        "staff_users": db.list_staff_users(),
        "staff_invites": db.list_staff_invites(),
        "error": error, "notice": notice,
    })


@app.post("/staff/admin/invite")
def staff_admin_invite(request: Request, email: str = Form(...)):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/login")
    email = email.strip().lower()
    if not email or "@" not in email:
        return RedirectResponse("/staff/admin?error=Enter+a+valid+email.", status_code=303)
    existing = db.get_user_by_email(email)
    if existing and existing["role"] == "staff":
        return RedirectResponse("/staff/admin?error=That+email+is+already+staff.", status_code=303)
    if existing:
        # Already has a client account - promote directly rather than
        # queuing an invite that would never actually be consumed (invites
        # only apply at ACCOUNT CREATION - see _resolve_role_and_admin).
        db.promote_to_staff(existing["id"])
        db.log_security_event("staff_role_granted", user_id=existing["id"], actor_user_id=admin["id"],
                               email=email, ip_address=rate_limit.client_ip(request))
        return RedirectResponse(f"/staff/admin?notice=Promoted+the+existing+account+for+{email}+to+staff.", status_code=303)
    db.add_staff_invite(email, invited_by=admin["id"])
    db.log_security_event("staff_invite_sent", actor_user_id=admin["id"], email=email,
                           ip_address=rate_limit.client_ip(request))
    return RedirectResponse(
        f"/staff/admin?notice=Invited+{email}+-+they+get+staff+access+automatically+the+moment+they+sign+up.",
        status_code=303)


@app.post("/staff/admin/remove-invite")
def staff_admin_remove_invite(request: Request, email: str = Form(...)):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/login")
    db.remove_staff_invite(email.strip().lower())
    return RedirectResponse("/staff/admin", status_code=303)


@app.post("/staff/admin/demote/{user_id}")
def staff_admin_demote(request: Request, user_id: str):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/login")
    if user_id == admin["id"]:
        return RedirectResponse("/staff/admin?error=Can%27t+demote+your+own+account.", status_code=303)
    demoted = db.get_user(user_id)
    db.demote_from_staff(user_id)
    db.log_security_event("staff_role_revoked", user_id=user_id, actor_user_id=admin["id"],
                           email=demoted["email"] if demoted else None, ip_address=rate_limit.client_ip(request))
    return RedirectResponse("/staff/admin", status_code=303)


# ------------------------------------------------------------ staff blog ----
@app.get("/staff/newsletter", response_class=HTMLResponse)
def staff_newsletter_form(request: Request, notice: str | None = None):
    """Real staff-composed newsletter, sent manually - no AI auto-writing
    it, no cron auto-sending it on a schedule, matching this app's whole
    "a human decides what goes out under Kauli's name" principle
    (blog_ai_assist.py's own docstring lays out exactly this same
    reasoning for blog drafts). Only ever goes to marketing_consent=1
    clients - that field IS the recipient list, no separate one to keep
    in sync."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    history = db.list_newsletters()
    # Real open/click counts from Brevo's own event log (see mailer.
    # get_open_stats_for_tag's docstring) - only for the most recent few,
    # not the whole history on every page load: each one is 3 real API
    # calls, and Brevo's own event-report window only covers 90 days back
    # anyway, so an old newsletter's stats would come back empty regardless.
    open_stats = {}
    for n in history[:10]:
        stats = mailer.get_open_stats_for_tag(f"newsletter-{n['id']}")
        if stats is not None:
            open_stats[n["id"]] = stats
    return templates.TemplateResponse(request, "staff_newsletter.html", {
        "user": user, "notice": notice,
        "posts": db.list_blog_posts(published_only=True),
        "history": history, "open_stats": open_stats,
        "recipient_count": len(db.list_marketing_opted_in_clients()),
        "calendly_url": os.environ.get("KAULI_CALENDLY_URL"),
    })


@app.post("/staff/newsletter/send")
def staff_newsletter_send(request: Request, subject: str = Form(...), blog_post_id: str = Form(""),
                           feature_update: str = Form(""), industry_trend_text: str = Form(""),
                           industry_trend_url: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if not mailer.email_configured():
        return RedirectResponse("/staff/newsletter?notice=Email+isn%27t+configured+-+nothing+sent.", status_code=303)

    base_url = str(request.base_url).rstrip("/")
    blog_post = db.get_blog_post(blog_post_id) if blog_post_id else None

    sections = []
    if feature_update.strip():
        sections.append(
            '<p style="margin:0 0 6px; font-size:12px; font-weight:700; text-transform:uppercase; '
            f'letter-spacing:0.05em; color:{mailer.BRAND_ACCENT};">The Kauli Scoop</p>'
            f'<p style="margin:0 0 24px;">{feature_update.strip()}</p>'
        )
    post_url = None
    if blog_post:
        post_url = f"{base_url}/blog/{blog_post['slug']}"
        sections.append(
            '<p style="margin:0 0 6px; font-size:12px; font-weight:700; text-transform:uppercase; '
            f'letter-spacing:0.05em; color:{mailer.BRAND_ACCENT};">From the Blog</p>'
            f'<p style="margin:0 0 6px; font-weight:700;">{blog_post["title"]}</p>'
            f'<p style="margin:0 0 24px;">{blog_post["description"] or ""}</p>'
        )
    if industry_trend_text.strip():
        trend_link = (f' <a href="{industry_trend_url.strip()}" style="color:{mailer.BRAND_ACCENT};">More →</a>'
                       if industry_trend_url.strip() else "")
        sections.append(
            '<p style="margin:0 0 6px; font-size:12px; font-weight:700; text-transform:uppercase; '
            f'letter-spacing:0.05em; color:{mailer.BRAND_ACCENT};">Industry Trend</p>'
            f'<p style="margin:0 0 24px;">{industry_trend_text.strip()}{trend_link}</p>'
        )
    if not sections:
        return RedirectResponse("/staff/newsletter?notice=Add+at+least+one+section+before+sending.", status_code=303)

    calendly_url = os.environ.get("KAULI_CALENDLY_URL")
    cta_line = (f'Need help localizing your next project? '
                f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">Reply to this email</a>'
                + (f' or <a href="{calendly_url}" style="color:{mailer.BRAND_ACCENT};">book a 15-minute call</a>'
                   if calendly_url else "") + ".")
    inner = "".join(sections) + f'<p style="margin:24px 0 0; font-size:13px; color:{mailer.BRAND_MUTED};">{cta_line}</p>'

    # A real button, not just inline links - the featured post if one was
    # picked, otherwise the blog index, so there's always one clear
    # primary click.
    newsletter_cta_text, newsletter_cta_url = (
        ("Read the full article →", post_url) if post_url else ("Read our latest posts", f"{base_url}/blog")
    )

    # Generated before the send loop, not after - see db.create_newsletter_
    # record's own comment on why: this exact id is the real Brevo tag
    # every send below carries, which is what lets a later "how many
    # opened this newsletter" query find them again.
    newsletter_id = uuid.uuid4().hex[:12]
    send_tag = f"newsletter-{newsletter_id}"
    recipients = db.list_marketing_opted_in_clients()
    sent_count = 0
    for client in recipients:
        unsub_url = f"{base_url}/unsubscribe/{client['id']}"
        html = mailer.wrap_email_html(
            inner, cta_text=newsletter_cta_text, cta_url=newsletter_cta_url, base_url=base_url,
            footer_note=f'You\'re getting this because you opted in to Kauli updates. '
                        f'<a href="{unsub_url}" style="color:{mailer.BRAND_MUTED};">Unsubscribe</a> anytime.',
        )
        ok, _ = mailer.send_email(client["email"], subject, html, tags=[send_tag])
        if ok:
            sent_count += 1

    db.create_newsletter_record(subject, blog_post_id or None, feature_update.strip(),
                                 industry_trend_text.strip(), industry_trend_url.strip(),
                                 user["id"], sent_count, newsletter_id=newsletter_id)
    return RedirectResponse(
        f"/staff/newsletter?notice=Sent+to+{sent_count}+of+{len(recipients)}+opted-in+clients.", status_code=303)


@app.get("/unsubscribe/{user_id}", response_class=HTMLResponse)
def unsubscribe(request: Request, user_id: str):
    """No login needed - the same real-world pattern every newsletter
    unsubscribe link uses. Only ever touches marketing_consent - never
    closes the account or stops a real transactional email (order
    status, receipts), which aren't marketing and don't need this."""
    user = db.get_user(user_id)
    if not user:
        return HTMLResponse("Account not found.", status_code=404)
    db.set_marketing_consent(user_id, False, request.client.host if request.client else None)
    return HTMLResponse(
        "<div style=\"font-family:sans-serif; max-width:480px; margin:80px auto; text-align:center;\">"
        "<h1>You're unsubscribed</h1><p>You won't get Kauli newsletters anymore - "
        "order updates and receipts are unaffected.</p></div>"
    )


@app.get("/staff/blog", response_class=HTMLResponse)
def staff_blog_list(request: Request, error: str | None = None, notice: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_blog_list.html", {
        "user": user, "posts": db.list_blog_posts(published_only=False),
        "medium_configured": medium_publish.medium_configured(),
        "devto_configured": devto_publish.devto_configured(),
        "error": error, "notice": notice,
        "performance": db.blog_performance_report(),
        "reader_countries": db.blog_reader_countries(),
    })


@app.get("/staff/blog/new", response_class=HTMLResponse)
def staff_blog_new_form(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_blog_edit.html", {
        "user": user, "post": None, "error": None,
        "ai_assist_configured": blog_ai_assist.ai_assist_configured(),
    })


@app.post("/staff/blog/ai-draft")
async def staff_blog_ai_draft(request: Request):
    """AJAX endpoint the editor's "Draft with AI" button calls - returns a
    draft to fill the form with, never saves or publishes anything itself.
    See blog_ai_assist.py's own docstring for why this stays scoped to
    "draft text a human reviews," not an autonomous pipeline."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"ok": False, "error": "Not authorized."}, status_code=401)
    body = await request.json()
    topic = (body.get("topic") or "").strip()
    if not topic:
        return JSONResponse({"ok": False, "error": "Give it a topic first."})
    result = blog_ai_assist.draft_blog_post(topic, (body.get("notes") or "").strip())
    return JSONResponse(result)


@app.post("/staff/blog/new")
def staff_blog_create(request: Request, title: str = Form(...), slug: str = Form(""),
                       description: str = Form(""), body_html: str = Form(...),
                       status: str = Form("draft"), category: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    title = title.strip()
    slug = _slugify(slug.strip() or title)
    if not title or not body_html.strip():
        return templates.TemplateResponse(request, "staff_blog_edit.html", {
            "user": user, "post": None, "error": "Title and body are required.",
            "blog_categories": db.BLOG_CATEGORIES,
        })
    if db.slug_exists(slug):
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"  # real collision, not left to silently overwrite another post
    post_id = db.create_blog_post(slug, title, description.strip() or None, body_html,
                                   author_id=user["id"], status="published" if status == "published" else "draft",
                                   category=category.strip() or None)
    return RedirectResponse(f"/staff/blog/{post_id}/edit", status_code=303)


@app.get("/staff/blog/{post_id}/edit", response_class=HTMLResponse)
def staff_blog_edit_form(request: Request, post_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    post = db.get_blog_post(post_id)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    return templates.TemplateResponse(request, "staff_blog_edit.html", {
        "user": user, "post": post, "error": None,
        "ai_assist_configured": blog_ai_assist.ai_assist_configured(),
        "blog_categories": db.BLOG_CATEGORIES,
    })


@app.post("/staff/blog/{post_id}/edit")
def staff_blog_update(request: Request, post_id: str, title: str = Form(...), slug: str = Form(""),
                       description: str = Form(""), body_html: str = Form(...), category: str = Form(""),
                       cover_image: UploadFile | None = File(None), remove_cover_image: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    post = db.get_blog_post(post_id)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    title = title.strip()
    slug = _slugify(slug.strip() or title)
    if db.slug_exists(slug, exclude_post_id=post_id):
        return templates.TemplateResponse(request, "staff_blog_edit.html", {
            "user": user, "post": post, "error": f'The slug "{slug}" is already used by another post.',
            "blog_categories": db.BLOG_CATEGORIES,
        })
    db.update_blog_post(post_id, slug, title, description.strip() or None, body_html, category=category.strip() or None)
    # Same real magic-byte validation every other image upload in this app
    # gets (see /settings' avatar handling) - client-supplied Content-Type
    # is never trusted on its own.
    if cover_image is not None and cover_image.filename:
        BLOG_COVER_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dest = BLOG_COVER_DIR / f"{post_id}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            _size, _sha256, head = upload_security.stream_save_with_limits(
                cover_image, tmp_dest, upload_security.MAX_BLOG_COVER_BYTES)
        except upload_security.UploadRejected as exc:
            return RedirectResponse(f"/staff/blog/{post_id}/edit?error={quote(str(exc))}", status_code=303)
        mime = upload_security.sniff_image_type(head)
        ext = ALLOWED_IMAGE_TYPES.get(mime)
        if not ext:
            tmp_dest.unlink(missing_ok=True)
            return RedirectResponse(
                f"/staff/blog/{post_id}/edit?error={quote('Cover image must be a real PNG, JPEG, or WebP file.')}",
                status_code=303)
        # Explicit known extensions, not a wildcard glob - see the identical
        # real bug just fixed in /settings' avatar upload above:
        # BLOG_COVER_DIR.glob(f"{post_id}.*") also matched tmp_dest itself
        # ("{post_id}.tmp-xxxxxxxx"), deleting the just-written upload
        # before the rename below could run. Confirmed live - this is the
        # exact FileNotFoundError behind the "it's refusing" report.
        for ext_ in set(ALLOWED_IMAGE_TYPES.values()):
            (BLOG_COVER_DIR / f"{post_id}{ext_}").unlink(missing_ok=True)
        dest = BLOG_COVER_DIR / f"{post_id}{ext}"
        tmp_dest.rename(dest)
        db.set_blog_post_cover_image(post_id, str(dest))
    elif remove_cover_image == "1":
        if post["cover_image_path"]:
            Path(post["cover_image_path"]).unlink(missing_ok=True)
        db.set_blog_post_cover_image(post_id, None)
    return RedirectResponse(f"/staff/blog/{post_id}/edit?saved=1", status_code=303)


@app.get("/blog-cover/{post_id}")
def blog_cover_image(post_id: str):
    """Public - a blog post's cover image is meant to be seen (in the card,
    the post itself, an og:image tag), not an authenticated asset like an
    avatar. 404 rather than a broken image tag when a post has none."""
    post = db.get_blog_post(post_id)
    if not post or not post["cover_image_path"] or not Path(post["cover_image_path"]).exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(post["cover_image_path"])


@app.post("/staff/blog/{post_id}/publish")
def staff_blog_publish(request: Request, post_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db.set_blog_post_status(post_id, "published")
    return RedirectResponse("/staff/blog", status_code=303)


@app.post("/staff/blog/{post_id}/unpublish")
def staff_blog_unpublish(request: Request, post_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db.set_blog_post_status(post_id, "draft")
    return RedirectResponse("/staff/blog", status_code=303)


@app.post("/staff/blog/{post_id}/delete")
def staff_blog_delete(request: Request, post_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    db.delete_blog_post(post_id)
    return RedirectResponse("/staff/blog", status_code=303)


@app.post("/staff/blog/{post_id}/publish-medium")
def staff_blog_publish_medium(request: Request, post_id: str):
    """Cross-posts to Medium with a canonicalUrl pointing back at the real
    Kauli post - see medium_publish.py's own docstring for the honest
    caveats (untested against a real account, needs a real Integration
    Token). Only makes sense for an already-published post - Medium
    shouldn't get a live copy of something not even live here yet."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    post = db.get_blog_post(post_id)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    if post["status"] != "published":
        return RedirectResponse("/staff/blog?error=Publish+it+on+Kauli+first.", status_code=303)
    canonical_url = f"{_site_base(request)}/blog/{post['slug']}"
    result = medium_publish.publish_post(post["title"], post["body_html"], canonical_url)
    if not result["ok"]:
        return RedirectResponse(f"/staff/blog?error={result['error']}", status_code=303)
    db.set_blog_post_medium_url(post_id, result["url"])
    return RedirectResponse("/staff/blog?notice=Cross-posted+to+Medium.", status_code=303)


@app.post("/staff/blog/{post_id}/publish-devto")
def staff_blog_publish_devto(request: Request, post_id: str):
    """Same idea as the Medium route above, on DEV.to instead - see
    devto_publish.py for why that's the platform actually usable for free
    right now."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    post = db.get_blog_post(post_id)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    if post["status"] != "published":
        return RedirectResponse("/staff/blog?error=Publish+it+on+Kauli+first.", status_code=303)
    canonical_url = f"{_site_base(request)}/blog/{post['slug']}"
    result = devto_publish.publish_post(post["title"], post["body_html"], canonical_url, post["description"])
    if not result["ok"]:
        return RedirectResponse(f"/staff/blog?error={result['error']}", status_code=303)
    db.set_blog_post_devto_url(post_id, result["url"])
    return RedirectResponse("/staff/blog?notice=Cross-posted+to+DEV.to.", status_code=303)


# --------------------------------------------------------------- staff ----
_ACTIVITY_STATUS_LABELS = {
    "pending_payment": "is awaiting payment",
    "queued": "was queued for AI processing",
    "processing": "started AI processing",
    "awaiting_review": "is ready for human review",
    "editor_returned": "needs an ops decision",
    "ready_for_delivery": "is ready for delivery",
    "delivered": "was delivered",
    "returned_to_client": "was returned to the client",
    "failed": "failed processing",
    "dead_letter": "failed processing (retries exhausted)",
}


@app.get("/staff", response_class=HTMLResponse)
def staff_overview(request: Request):
    """Landing page for staff, replacing a bare queue table with an actual
    "what needs my attention right now" view - real KPI counts, a live
    slice of the queue, the human-review backlog, and recent activity, all
    from data that already exists (no fabricated metrics, no features that
    don't exist yet like per-staff assignment, notifications, or search -
    see the Aug 26 2026 staff-portal-spec conversation for what's
    deliberately deferred)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")

    now = time.time()
    DAY = 86400
    now_dt = datetime.fromtimestamp(now)
    month_start = datetime(now_dt.year, now_dt.month, 1).timestamp()
    if now_dt.month == 1:
        prev_month_start = datetime(now_dt.year - 1, 12, 1).timestamp()
    else:
        prev_month_start = datetime(now_dt.year, now_dt.month - 1, 1).timestamp()

    status_counts = db.orders_by_status()
    total_jobs = sum(status_counts.values())
    in_progress = status_counts.get("queued", 0) + status_counts.get("processing", 0)
    in_review = status_counts.get("awaiting_review", 0)
    ops_decision = status_counts.get("editor_returned", 0)
    completed_total = status_counts.get("delivered", 0)

    jobs_last_30 = db.orders_created_between(now - 30 * DAY, now)
    jobs_prev_30 = db.orders_created_between(now - 60 * DAY, now - 30 * DAY)
    completed_last_30 = db.orders_reaching_status_between("delivered", now - 30 * DAY, now)
    completed_prev_30 = db.orders_reaching_status_between("delivered", now - 60 * DAY, now - 30 * DAY)
    revenue_this_month = db.revenue_between(month_start, now)
    revenue_prev_month = db.revenue_between(prev_month_start, month_start)

    # Same "not real staff work yet" filter /staff/jobs already applies -
    # an unpaid order sitting in pending_payment doesn't belong on a "what
    # needs my attention" widget (real bug: without this, list_all_orders()'s
    # own sort - active/deadline first, else newest-first - could surface a
    # just-submitted, still-unpaid order ahead of real in-flight jobs).
    orders = [o for o in db.list_all_orders() if o["status"] != "pending_payment"]
    # Compact by design - "everything visible without scrolling" (a real
    # ask, not a nice-to-have) means this widget shows a short real slice
    # with a real link to the full, properly-paginated list at /staff/jobs,
    # not every order crammed into one tall table.
    active_jobs = orders[:5]
    review_queue = [o for o in orders if o["status"] == "awaiting_review"][:3]
    overdue_count = sum(
        1 for o in orders
        if (ts := tat.time_status(o["tat_start_at"], o["deadline_at"])) and ts["overdue"]
    )
    edited_pct = {}
    for o in active_jobs + review_queue:
        if o["id"] not in edited_pct:
            job = _load_job(o)
            edited_pct[o["id"]] = job.edited_pct if job else None

    # Activity feed: merged from two REAL sources (recent status
    # transitions, recent client-visible messages), not a fabricated
    # per-staff-member log - see recent_status_changes/recent_client_messages
    # docstrings for why (no audit-log table, single combined staff role).
    activity = []
    for o in db.recent_status_changes(limit=8):
        label = _ACTIVITY_STATUS_LABELS.get(o["status"], o["status"].replace("_", " "))
        activity.append({
            "actor": "System", "ts": o["status_changed_at"],
            "text": f"{o['original_filename']} ({o['client_name']}) {label}",
        })
    for m in db.recent_client_messages(limit=8):
        activity.append({
            "actor": m["staff_name"] or "Staff", "ts": m["created_at"],
            "text": f"sent an update to {m['client_name']} on {m['original_filename']}",
        })
    activity.sort(key=lambda a: a["ts"], reverse=True)
    activity = activity[:8]

    greeting = "morning" if now_dt.hour < 12 else ("afternoon" if now_dt.hour < 18 else "evening")
    return templates.TemplateResponse(request, "staff_overview.html", {
        "user": user, "now_ts": now, "greeting": greeting, "today_label": now_dt.strftime("%a, %b %d, %Y").replace(" 0", " "),
        "total_jobs": total_jobs, "jobs_pct_change": _pct_change(jobs_last_30, jobs_prev_30),
        "in_progress": in_progress, "in_review": in_review, "ops_decision": ops_decision,
        "completed_total": completed_total, "completed_pct_change": _pct_change(completed_last_30, completed_prev_30),
        "revenue_this_month": revenue_this_month, "revenue_pct_change": _pct_change(revenue_this_month, revenue_prev_month),
        "active_jobs": active_jobs, "review_queue": review_queue, "edited_pct": edited_pct, "overdue_count": overdue_count,
        "activity": activity, "top_clients": db.top_clients_by_usage(days=30, limit=5),
        "daily_trend_svg": _daily_trend_svg(db.daily_job_trend(days=30)),
        "stuck_threshold_seconds": tat.STUCK_STAGE_THRESHOLD_SECONDS,
    })


@app.get("/staff/search", response_class=HTMLResponse)
def staff_search(request: Request, q: str = ""):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    results = db.staff_search(q, limit=15) if q.strip() else {"orders": [], "clients": [], "leads": []}
    return templates.TemplateResponse(request, "staff_search.html", {
        "user": user, "q": q, "results": results,
    })


def _queue_file_kind(order) -> str:
    """Real, cheap classification for the queue's file-type icon - not a
    fabricated media-library feature, just "what should the icon look
    like" from data already on the order."""
    if order["source_youtube_id"]:
        return "youtube"
    return "video" if is_video_file(order["audio_path"]) else "audio"


def _queue_row_view(order, pct, ts, unread: bool, now: float, assignee) -> dict:
    """Everything staff_jobs.html needs for one row, computed once here
    instead of repeated ad-hoc Jinja logic - real derived values only:
    review_minutes from real duration_minutes x real edited_pct (no new
    per-minute tracking invented), sla_exceeded/needs_attention from the
    same real tat_status every deadline pill in the app already uses."""
    duration = order["duration_minutes"] or 0.0
    review_minutes = round(duration * (pct / 100), 1) if pct is not None else None
    sla_exceeded = bool(ts and ts["overdue"])
    needs_attention = (
        order["status"] in ("editor_returned", "dead_letter", "failed")
        or (ts is not None and ts["level"] in ("red", "yellow"))
    )
    # tat.time_status clamps remaining_s to >= 0 (it's built for a progress
    # bar, not a "how overdue" readout) - this recomputes the real signed
    # delta directly from internal_deadline_at so "Overdue by 23h" can show
    # an actual duration instead of just the word "Overdue".
    deadline_delta_s = (order["internal_deadline_at"] - now) if order["internal_deadline_at"] else None
    return {
        "order": order, "pct": pct, "ts": ts, "unread": unread,
        "file_kind": _queue_file_kind(order),
        "review_minutes": review_minutes, "duration_minutes": duration,
        "sla_exceeded": sla_exceeded, "needs_attention": needs_attention,
        "stage_seconds": now - (order["status_changed_at"] or order["created_at"]),
        "deadline_delta_s": deadline_delta_s,
        "assignee": assignee,
    }


@app.post("/staff/orders/{order_id}/mark-read")
def staff_mark_order_read(request: Request, order_id: str):
    """The queue's one real, safe bulk action (see the checkbox column in
    staff_dashboard.html) - clears the unread-message dot. Deliberately
    the only bulk action wired up for now: every other real status
    transition (approve, retry, return) has side effects (re-running a
    real pipeline, notifying a client) that shouldn't fire on a batch of
    orders from one careless click."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.mark_read(user["id"], order_id)
    return JSONResponse({"ok": True})


@app.get("/staff/jobs", response_class=HTMLResponse)
def staff_jobs(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    # Real "no payment, no pipeline" rule: an unpaid order was already
    # never sent to worker.submit_job (see create_order/_activate_payment)
    # - the AI/editor queue never touches one either way. What this line
    # closes is a real, separate gap: pending_payment orders were still
    # showing up IN this view, inflating the Total count and cluttering
    # the All tab with rows nobody can (or should) act on - there's
    # nothing for staff to do with an unpaid order; it's the client's own
    # dashboard that shows them the real "pay now"/retry flow. Left alone
    # in staff_ops's revenue reporting below (a different, legitimate use:
    # seeing how many checkouts got abandoned is a real ops metric).
    orders = [o for o in db.list_all_orders() if o["status"] != "pending_payment"]
    unread = db.unread_order_ids(user["id"], include_internal=True)
    now = time.time()
    # Real single-operator reality (see the "one staff role for now"
    # note elsewhere in this file) - every job's "assignee" is whichever
    # real staff account exists, never a fabricated name. None if somehow
    # no staff account exists at all (never happens in practice, but a
    # real None is correct there, not a placeholder person).
    staff_list = db.list_staff_users()
    the_assignee = staff_list[0] if staff_list else None

    rows = []
    for o in orders:
        job = _load_job(o)
        pct = job.edited_pct if job else None
        ts = tat.time_status(o["tat_start_at"], o["deadline_at"], now=now)
        rows.append(_queue_row_view(o, pct, ts, o["id"] in unread, now, the_assignee))

    needs_attention_n = sum(1 for r in rows if r["needs_attention"])
    overdue_n = sum(1 for r in rows if r["ts"] and r["ts"]["overdue"])
    due_today_n = sum(1 for r in rows if r["ts"] and not r["ts"]["overdue"] and r["ts"]["remaining_s"] <= 86400)
    in_progress_n = sum(1 for r in rows if r["order"]["status"] in ("queued", "processing"))
    awaiting_review_n = sum(1 for r in rows if r["order"]["status"] == "awaiting_review")
    exceptions_n = sum(1 for r in rows if r["order"]["status"] in ("editor_returned", "dead_letter", "failed", "returned_to_client"))
    ready_n = sum(1 for r in rows if r["order"]["status"] == "ready_for_delivery")
    delivered_n = sum(1 for r in rows if r["order"]["status"] == "delivered")
    today_start = datetime(datetime.fromtimestamp(now).year, datetime.fromtimestamp(now).month,
                            datetime.fromtimestamp(now).day).timestamp()
    delivered_today_n = sum(1 for r in rows if r["order"]["status"] == "delivered" and r["order"]["status_changed_at"] >= today_start)

    service_level_labels = {k: v["name"] for k, v in billing.SERVICE_LEVELS.items()}
    trend = db.daily_job_trend(days=28)
    recent, earlier = trend[-14:], trend[:-14]

    def _trend_card(key: str) -> dict:
        now_n, before_n = sum(d[key] for d in recent), sum(d[key] for d in earlier)
        return {"spark": site_analytics.spark([d[key] for d in recent], 120, 34) if recent else "", "change": _pct_change(now_n, before_n)}
    return templates.TemplateResponse(request, "staff_dashboard.html", {
        "trend_created": _trend_card("created"), "trend_delivered": _trend_card("delivered"),
        "user": user, "rows": rows, "now_ts": now,
        "stuck_threshold_seconds": tat.STUCK_STAGE_THRESHOLD_SECONDS,
        "service_level_labels": service_level_labels,
        "total_jobs": len(rows), "needs_attention_n": needs_attention_n,
        "overdue_n": overdue_n, "due_today_n": due_today_n, "in_progress_n": in_progress_n,
        "awaiting_review_n": awaiting_review_n, "exceptions_n": exceptions_n,
        "ready_n": ready_n, "delivered_n": delivered_n, "delivered_today_n": delivered_today_n,
    })


def _ops_bars_svg(labels: list[str], values: list[float], color: str, money: bool = False, width: int = 640, height: int = 230) -> str:
    """The bars for a Reports chart: one bar a day, a few value labels on the left and date ticks along the bottom. Real data only."""
    pad_l, pad_r, pad_t, pad_b = 40, 8, 10, 24
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    peak = max(values) if values and max(values) > 0 else 1
    step = 10 ** max(0, len(str(int(peak))) - 1)
    top = ((int(peak) + step - 1) // step) * step if peak >= 1 else 1
    n = max(1, len(values))
    slot = plot_w / n
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Daily values" xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">']
    for i in range(0, 5):
        v = top * i / 4
        y = pad_t + plot_h - (v / top) * plot_h
        label = f"${v:,.0f}" if money else f"{v:,.0f}"
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="currentColor" stroke-opacity="{0.2 if i == 0 else 0.08}"/>'
                   f'<text x="{pad_l - 6}" y="{y + 3.5:.1f}" font-size="10" fill="currentColor" opacity="0.6" text-anchor="end">{label}</text>')
    for i, v in enumerate(values):
        h = (v / top) * plot_h
        out.append(f'<rect x="{pad_l + i * slot + slot * 0.18:.1f}" y="{pad_t + plot_h - h:.1f}" width="{max(1.5, slot * 0.64):.1f}" height="{max(0.0, h):.1f}" rx="2" fill="{color}"><title>{labels[i]}: {v:,.2f}</title></rect>')
    for i in sorted({round(k * (n - 1) / 6) for k in range(7)}) if n > 1 else [0]:
        out.append(f'<text x="{pad_l + i * slot + slot / 2:.1f}" y="{height - 6}" font-size="10" fill="currentColor" opacity="0.6" text-anchor="middle">{_short_day(labels[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def _ops_series(days: int, now: float) -> dict:
    labels = site_analytics._days_list(now, days)
    start = now - days * 86400
    conn = db.get_conn()
    try:
        def daily(sql: str) -> dict:
            return {r["d"]: r["v"] for r in conn.execute(sql, (start,)).fetchall()}
        rev = daily("SELECT date(completed_at, 'unixepoch') AS d, SUM(amount_usd) AS v FROM payments WHERE status = 'completed' AND completed_at >= ? GROUP BY d")
        orders = daily("SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS v FROM orders WHERE created_at >= ? GROUP BY d")
        clients = daily("SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS v FROM users WHERE role = 'client' AND created_at >= ? GROUP BY d")
        leads_now = conn.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (start,)).fetchone()[0]
        leads_prev = conn.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ? AND created_at < ?", (start - days * 86400, start)).fetchone()[0]
    finally:
        conn.close()
    series = {"revenue": [float(rev.get(d, 0.0)) for d in labels], "orders": [float(orders.get(d, 0)) for d in labels], "clients": [float(clients.get(d, 0)) for d in labels]}
    return {"labels": labels, **series, "leads_now": leads_now, "leads_prev": leads_prev,
            "svg": {"revenue": _ops_bars_svg(labels, series["revenue"], "#2fd08a", money=True), "orders": _ops_bars_svg(labels, series["orders"], "#4c8dff"),
                    "clients": _ops_bars_svg(labels, series["clients"], "#a78bfa")}}


@app.get("/staff/ops/export.csv")
def staff_ops_export(request: Request, days: int = 30):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    days = max(1, min(days, 365))
    now = time.time()
    s = _ops_series(days, now)
    rev, margin = db.revenue_summary(days=days), db.margin_summary(days=days)
    import csv as _csv
    import io as _io
    out = _io.StringIO()
    w = _csv.writer(out)
    w.writerow(["Kauli reports", f"last {days} days", datetime.now().strftime("%Y-%m-%d %H:%M")])
    w.writerow([])
    for label, value in (("Revenue (period)", rev["total_period"]), ("Revenue (all time)", rev["total_all_time"]), ("Pending bank transfers", rev["pending_bank_amount"]),
                         ("Orders created", db.orders_created_since(days=days)), ("Minutes delivered", db.minutes_processed(days=days)),
                         ("Revenue billed", margin["total_revenue_usd"]), ("Est. AI cost", margin["total_est_ai_cost_usd"]), ("Est. margin", margin["total_est_margin_usd"])):
        w.writerow([label, round(float(value), 2)])
    w.writerow([])
    w.writerow(["Day", "Revenue", "Orders", "New clients"])
    for i, d in enumerate(s["labels"]):
        w.writerow([d, round(s["revenue"][i], 2), int(s["orders"][i]), int(s["clients"][i])])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="kauli-reports-{days}d.csv"'})


@app.get("/staff/ops", response_class=HTMLResponse)
def staff_ops(request: Request, days: int = 30):
    """Phase 7 of the roadmap: SLAs, workload, revenue - a read-only report
    built entirely from data that already exists (orders, payments). `days`
    controls the reporting window for revenue/turnaround/failure-rate;
    queue-health (current counts, stale orders) is always a live snapshot,
    not windowed."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    days = max(1, min(days, 365))  # a stray ?days= shouldn't be able to force a scan-the-whole-table query
    ai_spend_today = db.ai_spend_since(time.time() - 86400)
    # FinOps guard: no fixed "safe" number exists in the abstract - it's
    # only meaningful relative to what your own order volume would
    # explain. $5/day is a deliberately low starting trip-wire for a small
    # operation; raise it in .env once real usage tells you what a normal
    # day actually costs.
    ai_spend_alert_threshold = float(os.environ.get("KAULI_DAILY_AI_COST_ALERT_USD", "5.0"))
    margin = db.margin_summary(days=days)
    margin_chart = charts.horizontal_bar_chart([
        {"label": row["pair"], "value": row["est_margin_usd"],
         "color": charts.STATUS_CRITICAL if row["est_margin_usd"] < 0 else charts.STATUS_GOOD,
         "display": f"${row['est_margin_usd']:.2f}"}
        for row in margin["by_pair"]
    ], label_width=160) if len(margin["by_pair"]) > 1 else ""
    series = _ops_series(days, time.time())
    rev_prev = db.revenue_between(time.time() - 2 * days * 86400, time.time() - days * 86400)
    return templates.TemplateResponse(request, "staff_ops.html", {
        "series": series, "rev_change": _pct_change(db.revenue_summary(days=days)["total_period"], rev_prev),
        "rev_spark": site_analytics.spark(series["revenue"], 140, 40, "#2fd08a"), "leads_change": _pct_change(series["leads_now"], series["leads_prev"]),
        "user": user, "days": days, "now_ts": time.time(),
        "status_counts": db.orders_by_status(),
        "stale_orders": db.stale_active_orders(threshold_hours=24.0),
        "turnaround": db.turnaround_stats(days=days),
        "failures": db.failure_rate(days=days),
        "minutes_processed": db.minutes_processed(days=days),
        "orders_created": db.orders_created_since(days=days),
        "revenue": db.revenue_summary(days=days),
        "margin": margin, "margin_chart": margin_chart,
        "new_leads": db.count_new_leads(),
        "ops_triage": db.list_orders_needing_ops_triage(),
        "ai_spend_today": ai_spend_today,
        "ai_spend_alert_threshold": ai_spend_alert_threshold,
        "funnel": db.client_funnel_stats(days=days),
    })


@app.get("/staff/orders/{order_id}", response_class=HTMLResponse)
def staff_review(request: Request, order_id: str, error: str | None = None, notice: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)

    job = _load_job(order)
    all_messages = db.list_messages(order_id, include_internal=True)
    client_messages = [m for m in all_messages if m["visibility"] == "client"]
    internal_messages = [m for m in all_messages if m["visibility"] == "internal"]
    db.mark_read(user["id"], order_id)

    outdir = Path(order["outdir"])
    has_video_source = is_video_file(order["audio_path"]) or bool(order["source_youtube_id"])
    difficulty_rate = audio_difficulty_rate(job) if job else 0.0
    suggested_surcharge_usd = round(
        (order["duration_minutes"] or 0) * _order_rate_per_min(order) * DIFFICULTY_SURCHARGE_DEFAULT_PCT, 2)

    assigned_actor = db.get_voice_actor(order["voice_actor_id"]) if order["voice_actor_id"] else None
    order_client = db.get_user(order["client_id"])
    return templates.TemplateResponse(request, "staff_review.html", {
        "user": user, "order": order, "job": job, "order_client_email": order_client["email"] if order_client else None,
        "client_messages": client_messages, "internal_messages": internal_messages,
        "has_video_source": has_video_source,
        "voice_actors": db.list_voice_actors("active"),
        "assigned_actor": assigned_actor,
        "order_payouts": [p for p in db.list_payouts() if p["order_id"] == order_id],
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "return_reasons": db.RETURN_REASONS,
        "workflow_steps": workflow_steps_for_order(order),
        "difficulty_rate": difficulty_rate,
        "difficulty_threshold": DIFFICULTY_SURCHARGE_THRESHOLD,
        "suggested_surcharge_pct": DIFFICULTY_SURCHARGE_DEFAULT_PCT,
        "suggested_surcharge_usd": suggested_surcharge_usd,
        "extra_charge_reasons": db.EXTRA_CHARGE_REASONS,
        "error": error, "notice": notice,
        "provider_failure": provider_health.failure_panel(order),
    })


@app.post("/staff/orders/{order_id}/difficulty-surcharge")
def staff_difficulty_surcharge(request: Request, order_id: str, action: str = Form(...),
                                pct: float = Form(25), note: str = Form(""),
                                reason: str = Form("difficult_audio")):
    """pct arrives as a whole percentage from the form (e.g. 25, not
    0.25) - staff types a percent, not a fraction. reason generalizes
    this from "only audio difficulty" to any extra-work charge a job's
    workflow needed - see db.EXTRA_CHARGE_REASONS."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if action == "propose":
        if reason not in db.EXTRA_CHARGE_REASONS:
            reason = "other"
        pct = max(0.01, min(1.0, pct / 100))
        usd = round((order["duration_minutes"] or 0) * _order_rate_per_min(order) * pct, 2)
        db.propose_difficulty_surcharge(order_id, pct, usd, note.strip() or None, reason=reason)
        reason_label = db.EXTRA_CHARGE_REASONS[reason]
        db.create_message(
            order_id, user["id"], "client",
            f"This order needed additional work ({reason_label}) - we're proposing a "
            f"${usd:.2f} charge ({pct*100:.0f}%) before delivery. " + (note.strip() or ""),
        )
    elif action == "waive":
        db.waive_difficulty_surcharge(order_id)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/messages")
def staff_send_message(request: Request, order_id: str,
                        body: str = Form(...), visibility: str = Form(...), also_email: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if body.strip() and visibility in ("client", "internal"):
        db.create_message(order_id, user["id"], visibility, body)
        db.mark_read(user["id"], order_id)
        if visibility == "client":
            db.create_notification(order["client_id"], "staff_message",
                                    f"New message about {order['original_filename']}",
                                    link=f"/client/orders/{order_id}")
        # also_email is only ever meaningful on a client-visible message -
        # an internal note has no client to email, and the checkbox
        # doesn't even exist on that form, but the check here is real
        # defense, not just relying on the template never sending it.
        if also_email and visibility == "client":
            client = db.get_user(order["client_id"])
            if client and client["email"]:
                html = mailer.wrap_email_html(
                    f"<p>{body.strip()}</p>".replace("\n", "</p><p>"),
                    cta_text="View your order →", cta_url=str(request.base_url).rstrip("/") + f"/client/orders/{order_id}",
                    base_url=str(request.base_url),
                )
                mailer.send_email(client["email"], f"Update on your Kauli order - {order['original_filename']}",
                                   html, body.strip())
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/return")
def staff_return_order(request: Request, order_id: str, reason: str = Form(...), note: str = Form("")):
    """An editor hitting something the client's instructions say to
    escalate (or anything else needing a human call) sends it here instead
    of quietly guessing what to do - see db.flag_order_for_return."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if reason not in db.RETURN_REASONS:
        return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)
    if order["status"] in ("ready_for_delivery", "delivered", "editor_returned", "returned_to_client"):
        # Already finished, or already sitting in the triage queue - nothing
        # to flag. Matches what the editor UI already hides this control for.
        return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)
    db.flag_order_for_return(order_id, reason, note.strip() or None)
    db.create_message(
        order_id, user["id"], "internal",
        f"Returned for ops review - reason: {reason.replace('_', ' ')}." + (f" {note.strip()}" if note.strip() else ""),
    )
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/workflow-step")
def staff_toggle_workflow_step(request: Request, order_id: str, step: str = Form(...),
                                done: str = Form(""), next: str = Form("")):
    """The Ereri workflow stepper's manual checkmarks - only "source",
    "target" and "voice" are ever settable here; "deliverables" and
    "complete" are computed live (see workflow_steps_for_order) and any
    step key outside the fixed set is silently ignored rather than trusted."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if step in ("source", "target", "voice"):
        db.set_workflow_step(order_id, step, bool(done))
    destination = f"/staff/orders/{order_id}/editor" if next == "editor" else f"/staff/orders/{order_id}"
    return RedirectResponse(destination, status_code=303)


@app.post("/staff/orders/{order_id}/ops-decision")
def staff_ops_decision(request: Request, order_id: str, action: str = Form(...), message: str = Form("")):
    """What operations does with an editor-returned order: tell the client
    what's going on and keep working it (status stays editor_returned -
    still needs someone to follow up once the client replies), formally end
    the job and notify the client, or decide it was a false alarm and send
    it back into the review queue."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["status"] != "editor_returned":
        return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)

    if action == "contact_customer" and message.strip():
        db.create_message(order_id, user["id"], "client", message.strip())
    elif action == "return_to_client":
        db.update_order_status(order_id, "returned_to_client")
        if message.strip():
            db.create_message(order_id, user["id"], "client", message.strip())
        client = db.get_user(order["client_id"])
        if client and mailer.email_configured():
            link = f"{str(request.base_url).rstrip('/')}/client/orders/{order_id}"
            reason_text = f"\n\n{message.strip()}" if message.strip() else ""
            client_name = (client["display_name"] or client["email"].split("@")[0]).strip()
            body = (f"Hi {client_name},\n\n"
                    f"Your order ({order['original_filename']}) was returned without completing - "
                    f"we need something from you before we can continue.{reason_text}\n\n"
                    f"No need to submit a new order or pay again - reply on the order's message thread and, "
                    f"if we need a corrected file, attach it right there; we'll pick this same order back up "
                    f"as soon as it's sorted.\n\n"
                    "Or just reply to this email.\n\n"
                    f"Talk soon,\n{TEAM_NAME}")
            reason_html = f'<p style="margin:0 0 14px;">{message.strip()}</p>' if message.strip() else ""
            inner = (
                f'<p style="margin:0 0 14px;">Hi {client_name},</p>'
                f'<p style="margin:0 0 14px;">Your order ({order["original_filename"]}) was returned '
                f'without completing - we need something from you before we can continue.</p>'
                f'{reason_html}'
                f'<p style="margin:0 0 14px;">No need to submit a new order or pay again - reply on the '
                f'order\'s message thread and, if we need a corrected file, attach it right there; we\'ll '
                f'pick this same order back up as soon as it\'s sorted.</p>'
                f'<p style="margin:0 0 14px;">Or just '
                f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email.</p>'
                f'<p style="margin:0;">Talk soon,<br>{TEAM_NAME}</p>'
            )
            html = mailer.wrap_email_html(inner, cta_text="View order & resume", cta_url=link,
                                           base_url=str(request.base_url))
            mailer.send_email(client["email"], "Action needed on your Kauli order", html, body)
    elif action == "resume":
        db.update_order_status(order_id, "awaiting_review")
        db.create_message(order_id, user["id"], "internal", "Resolved - back in the review queue.")
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/segments/{segment_id}")
def staff_edit_segment(request: Request, order_id: str, segment_id: str,
                        edited_text: str = Form(...), resynthesize: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)

    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return HTMLResponse("Segment not found.", status_code=404)

    _apply_segment_edit(order, job, seg, edited_text, resynthesize == "on")
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/dub-voice")
def staff_set_dub_voice(request: Request, order_id: str, voice: str = Form(...)):
    """Re-render the whole dub track with a different voice: either a
    picked Piper preset (fast, runs inline) or "xtts" - an actual clone of
    the source speaker (slow, runs on a background thread; see
    _run_xtts_clone_job)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if order["tts"] == "stub":
        return HTMLResponse("This order has no dub voice to change (transcription/translation only).", status_code=400)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)

    if voice == "xtts":
        # The actual enforcement point - not the editor UI graying out the
        # option (that's just a courtesy; nothing stops a raw POST to this
        # route). No consent recorded on THIS order means no clone, full
        # stop, regardless of what the form submits or which staff member
        # submits it.
        if not order["voice_clone_consent_given_at"]:
            return HTMLResponse(
                "This order doesn't have voice-cloning consent on file yet - the client needs to grant "
                "it (from their order page) before a clone can run. Ask them, or check the order's "
                "message thread.", status_code=403)
        if order["dub_voice_job_status"] == "running":
            return RedirectResponse(f"/staff/orders/{order_id}/editor", status_code=303)
        db.set_dub_voice_job_status(order_id, "running")
        threading.Thread(target=_run_xtts_clone_job, args=(order_id,), daemon=True).start()
    elif voice.startswith("piper:") and voice[6:] in PIPER_VOICES:
        voice_key = voice[6:]
        voice_path = str(PROJECT_ROOT / PIPER_VOICES[voice_key]["path"])
        if not Path(voice_path).exists():
            return HTMLResponse(f"Voice model not downloaded: {voice_path}", status_code=400)
        _resynthesize_full_dub(order, job, "piper", voice_path)
        db.set_dub_voice(order_id, voice, job_status=None)
    elif voice == "human":
        # No re-render needed - the actor's take already exists in its own
        # permanent slot (see _human_recording_path/_activate_human_recording).
        # Switching the picker back to "human" just re-copies it back into
        # the active delivered slot, in case a Piper/xtts re-render since
        # overwrote that slot with AI audio.
        human_path = _human_recording_path(order)
        if not human_path:
            return HTMLResponse(
                "No voice-actor recording has been uploaded for this order yet.", status_code=400)
        _activate_human_recording(order_id, order, human_path.suffix, human_path.read_bytes())
    else:
        return HTMLResponse("Unknown voice.", status_code=400)

    return RedirectResponse(f"/staff/orders/{order_id}/editor", status_code=303)


@app.get("/staff/orders/{order_id}/dub-voice-status")
def staff_dub_voice_status(request: Request, order_id: str):
    """Tiny JSON poll target for the editor UI while an XTTS clone runs in
    the background - see _run_xtts_clone_job."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"voice": order["dub_voice"], "job_status": order["dub_voice_job_status"]})


@app.post("/staff/orders/{order_id}/retry")
def staff_retry_order(request: Request, order_id: str):
    """Manual retry for a 'dead_letter' order - see worker.py's automatic
    retry budget (2 attempts) that gave up before this. Resets the count
    so it gets its own fresh retry budget rather than immediately
    dead-lettering again after one more try.

    resume=True: the same real-cost fix as worker.py's own automatic
    retry - this order already has a manifest on disk from its last
    attempt, quite possibly with real, correctly-transcribed and
    -translated segments in it. A manual retry re-running ASR/MT from
    scratch would re-bill a paid provider (Transkriptor) for the whole
    file for no reason - see kauli.pipeline.run's resume docstring."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    db.reset_retry_count(order_id)
    db.update_order_status(order_id, "queued")
    worker.submit_job(order_id, resume=True)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/switch-provider")
def staff_switch_provider(request: Request, order_id: str, kind: str = Form(...), provider: str = Form(...)):
    """After a provider fails (out of quota, key rejected, down): point this order at another connected provider that can take
    its languages and retry it. resume=True keeps whatever the earlier steps already produced, so nothing is paid for twice."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    problem = provider_health.valid_switch(order, kind, provider)
    if problem:
        return RedirectResponse(f"/staff/orders/{order_id}?error={quote(problem)}", status_code=303)
    db.set_order_provider(order_id, kind, provider)
    db.reset_retry_count(order_id)
    db.update_order_status(order_id, "queued")
    db.create_message(order_id, user["id"], "internal",
                      f"Switched {kind.upper()} from {provider_health.NAMES.get(order[kind], order[kind])} to "
                      f"{provider_health.NAMES.get(provider, provider)} and retried.")
    worker.submit_job(order_id, resume=True)
    return RedirectResponse(f"/staff/orders/{order_id}?notice={quote('Switched to ' + provider_health.NAMES.get(provider, provider) + ' and retrying.')}", status_code=303)


@app.get("/staff/providers", response_class=HTMLResponse)
def staff_providers(request: Request):
    """What each outside provider has left, for staff deciding where to send work."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_providers.html", {
        "user": user, "resources": provider_health.resources(), "steps": provider_health.switchboard(), "modes": provider_health.MODES,
        "mode": provider_health.mode(), "estimates": provider_health.cost_estimates(), "notice": request.query_params.get("notice"),
        "defaults": {k: provider_health.default_name(k) for k in provider_health.DEFAULT_KEYS}, "kind_labels": provider_health.KIND_LABELS,
        "error": request.query_params.get("error")})


def _provider_log(user, detail: str) -> None:
    db.log_security_event("provider_setting_changed", user_id=user["id"], email=user["email"], detail=detail)


@app.post("/staff/providers/download-own-model")
def staff_download_own_model(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    started = provider_health.warm_own_model()
    _provider_log(user, "download own transcription model")
    return RedirectResponse("/staff/providers?notice=" + quote("Downloading our transcription model in the background. Refresh in a few minutes." if started
                                                                 else "It is already downloading."), status_code=303)


@app.post("/staff/providers/mode")
def staff_provider_mode(request: Request, mode: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if mode not in provider_health.MODES:
        return RedirectResponse("/staff/providers?error=" + quote("Pick one of the three modes."), status_code=303)
    db.set_flag("provider_mode", mode, by=user["id"])
    _provider_log(user, f"mode = {mode}")
    return RedirectResponse("/staff/providers?notice=" + quote(f"Mode set to {provider_health.MODES[mode][0]}. It applies to new orders."), status_code=303)


@app.post("/staff/providers/default")
def staff_provider_default(request: Request, kind: str = Form(""), name: str = Form("")):
    """Pin the default provider for one step (transcription, translation or dubbing), or go back to automatic. New orders use it when it can take
    the job, and the editor's provider popup opens on it."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    problem = provider_health.set_default(kind, name.strip(), by=user["id"])
    if problem:
        return RedirectResponse("/staff/providers?error=" + quote(problem), status_code=303)
    _provider_log(user, f"default {kind} = {name.strip() or 'automatic'}")
    label = provider_health.KIND_LABELS[kind]
    said = provider_health.NAMES.get(name.strip(), name.strip()) if name.strip() else "automatic (follows the mode above)"
    return RedirectResponse("/staff/providers?notice=" + quote(f"{label} default: {said}. It applies to new orders and to the editor's provider popup."), status_code=303)


@app.post("/staff/providers/set")
def staff_provider_set(request: Request, name: str = Form(""), action: str = Form(""), cost: str = Form("")):
    """Turn one provider on or off, or record what it really costs per audio-minute. Applies to new orders straight away."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    allowed = {n for names in provider_health.TOGGLEABLE.values() for n in names}
    if name not in allowed:
        return RedirectResponse("/staff/providers?error=" + quote("That provider cannot be changed here."), status_code=303)
    label = provider_health.NAMES.get(name, name)
    if action in ("on", "off"):
        db.set_provider_setting(name, enabled=(action == "on"), by=user["id"])
        _provider_log(user, f"{name} {action}")
        return RedirectResponse("/staff/providers?notice=" + quote(f"{label} is now {action}."), status_code=303)
    if action == "cost":
        text = cost.strip().lstrip("$")
        if not text:
            db.set_provider_setting(name, clear_cost=True, by=user["id"])
            _provider_log(user, f"{name} cost reset")
            return RedirectResponse("/staff/providers?notice=" + quote(f"{label}: back to the default cost."), status_code=303)
        try:
            value = float(text)
        except ValueError:
            value = -1.0
        if not (0 <= value <= 5):
            return RedirectResponse("/staff/providers?error=" + quote("Enter the cost in dollars per audio-minute, for example 0.0019."), status_code=303)
        db.set_provider_setting(name, cost_per_min=value, by=user["id"])
        _provider_log(user, f"{name} cost = {value}")
        return RedirectResponse("/staff/providers?notice=" + quote(f"{label} costs ${value:.4f} per audio-minute."), status_code=303)
    return RedirectResponse("/staff/providers", status_code=303)


@app.post("/staff/orders/{order_id}/approve")
def staff_approve(request: Request, order_id: str, next: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    # Staff clicking "Approve" is the real, final sign-off - not the %
    # edited figure. This used to hard-block approval below 100% edited;
    # a real complaint changed that: an editor/QA who's actually reviewed
    # the whole order and is confident in it gets to make that call
    # themselves, full stop, rather than the system second-guessing a
    # human's own explicit "I'm done" with an error page. %edited is still
    # real, still shown everywhere it always was (the queue, this page) -
    # just informational from here on, not a blocker. The button's own
    # confirm() (staff_review.html/editor.html) still surfaces the real
    # number before a genuinely low-%-edited approval goes through, so
    # it's a deliberate choice, not an accidental click.
    db.update_order_status(order_id, "ready_for_delivery")
    order = db.get_order(order_id)
    if order:
        worker.notify_client_order_ready(order, base_url=str(request.base_url))
        _fire_client_webhook(request, order, "order.ready")
        _maybe_auto_render_video_deliverables(order)
    if next == "next_job":
        nxt = db.next_order_needing_review(order_id)
        if nxt:
            return RedirectResponse(f"/staff/orders/{nxt['id']}/editor", status_code=303)
        return RedirectResponse("/staff", status_code=303)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


class _PublicBaseRequest:
    """Just enough of a Request for _order_to_api_dict/_deliverable_link
    (they only read .base_url) when there's no real HTTP request - i.e. the
    background worker finishing an instant-AI order."""
    def __init__(self, base: str):
        self.base_url = base.rstrip("/") + "/"


def _on_instant_ai_order_ready(order) -> None:
    """What staff_approve does for a human-approved order, for an instant-AI
    one that just auto-delivered: the client's order.ready webhook (needs a
    public base URL for the signed download links - skipped if
    KAULI_PUBLIC_BASE_URL isn't set) and any paid video-deliverable render."""
    if worker.PUBLIC_BASE_URL:
        _fire_client_webhook(_PublicBaseRequest(worker.PUBLIC_BASE_URL), order, "order.ready")
    _maybe_auto_render_video_deliverables(order)


worker.ai_order_ready_hook = _on_instant_ai_order_ready


# --------------------------------------------------------- word editor ----
# Kauli's own word-synced correction workspace: audio + word-level cells
# tied to their timestamps + keyboard-driven editing. Same core idea as
# tools like Trint/Descript/Otter (click a word, jump the audio there) -
# built fresh around kauli's own Segment/Word data model, not a copy of
# any specific vendor's tool.
@app.get("/staff/orders/{order_id}/editor", response_class=HTMLResponse)
def staff_editor(request: Request, order_id: str, notice: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)

    job = _load_job(order)
    if job is None:
        return HTMLResponse(
            "Job hasn't finished processing yet - nothing to edit.", status_code=404)
    opened_changes = _split_long_gaps(job) | _repair_leaked_translations(job)          # silences cut into seconds; leaked commentary cleaned
    if not job.speaker_paragraphs_applied:                                              # every new speaker starts a new paragraph, once: after that it is the editor's call
        apply_speaker_paragraphs(job.segments)
        job.speaker_paragraphs_applied = True
        opened_changes = True
    if opened_changes:
        job.save(str(Path(order["outdir"]) / "manifest.json"))

    segments_json = [
        {
            "segment_id": s.segment_id,
            "segment_type": s.segment_type,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "speaker_id": s.speaker_id,
            "source_transcript": s.source_transcript,
            "source_final_text": s.source_final_text,
            "source_confidence": s.source_confidence,
            "cultural_notes": s.cultural_notes,
            "review_flag": s.review_flag,
            "review_reasons": s.review_reasons,
            "fit_status": s.fit_status,
            "fit_ratio": s.fit_ratio,
            "final_text": s.final_text,
            "translation_confidence": s.translation_confidence,
            "translation_stale": s.translation_stale,
            "manual_pace_pct": s.manual_pace_pct,
            "spell_out": s.spell_out,
            "source_cells": _build_source_cells(s),
            "target_cells": _build_target_cells(s),
            # Real delivered-caption formatting - the SAME wrap/display-cap
            # logic kauli.subtitles.to_srt/to_vtt uses to build the actual
            # file a client downloads, not a second, JS-side approximation
            # of it. Powers the Preview tab's caption bar (editor.js's
            # bindPreviewCaptions) so what an editor sees there is what
            # actually ships, not just the raw un-wrapped text.
            "wrapped_caption": _wrap_caption_text(s.final_text.strip()) if s.final_text.strip() else "",
            "display_end_ms": _display_end_ms(s),
        }
        for s in job.segments
    ]

    dub_audio_path = Path(order["outdir"]) / f"dub_{order['target_lang']}.wav"
    # File existence alone isn't enough - the stub TTS provider (used for
    # transcribe/translate-only jobs) writes a placeholder file rather than
    # skipping output entirely, so a real dub preview also needs a real
    # voice behind it.
    dub_audio_ready = dub_audio_path.exists() and order["tts"] != "stub"
    dubbed_video_ready = (Path(order["outdir"]) / f"dubbed_video_{order['target_lang']}.mp4").exists()
    stale_translation_count = sum(1 for s in job.segments if s.translation_stale)
    piper_voices = {
        key: v for key, v in PIPER_VOICES.items() if (PROJECT_ROOT / v["path"]).exists()
    }
    human_recording_available = _human_recording_path(order) is not None
    # Real speakers actually present in this order - from Transkriptor's
    # diarization labels or a human's own set-speaker correction (see
    # editor_set_segment_speaker) - never a fabricated "Speaker 1/2/3"
    # list when nothing has actually tagged anyone. Sorted for a stable
    # display order, not insertion order (which would jump around as
    # segments get corrected).
    distinct_speakers = sorted({s.speaker_id for s in job.segments if s.speaker_id})
    # Real bug this fixes: the stage-picker's "2. {target} translation"
    # tab used to show unconditionally, on every order - for a real
    # transcription-only order (billing.SERVICE_LEVELS["transcribe"]["mt"]
    # is False, so MT never runs and every segment's spoken/literal is
    # untouched stub passthrough of the SOURCE text), that tab showed the
    # Swahili source verbatim mislabeled as an "English translation" -
    # confirmed live, this is exactly what workflow_steps_for_order
    # already gets right for the progress stepper (it only lists a
    # translation step when the service level actually includes one);
    # the stage-picker just never consulted the same fact.
    order_level = billing.SERVICE_LEVELS.get(order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])
    return templates.TemplateResponse(request, "editor.html", {
        "redo_kinds": provider_health.editor_kinds(order), "kind_labels": provider_health.KIND_LABELS,
        "user": user, "order": order, "job": job,
        "has_translation": order_level["mt"], "has_dub": order_level["tts"],
        "source_lang_name": LANGUAGE_NAMES.get(order["source_lang"], order["source_lang"]),
        "target_lang_name": LANGUAGE_NAMES.get(order["target_lang"], order["target_lang"]),
        # The alt+p pluralize shortcut (editor.js:pluralizeWord) is a real
        # English grammar-rules algorithm - it only ever produces a
        # correct result on English text, whichever STEP that happens to
        # be for this order (source for an en->sw job, target for sw->en).
        # This is the other language, whichever side it's actually on.
        "non_english_lang_name": (
            LANGUAGE_NAMES.get(order["target_lang"], order["target_lang"]) if order["source_lang"] == "en"
            else LANGUAGE_NAMES.get(order["source_lang"], order["source_lang"])
        ),
        "is_video": is_video_file(order["audio_path"]),
        "youtube_video_id": order["source_youtube_id"],
        # Jinja2Templates has no Flask-style `tojson` filter, so serialize
        # here and mark it safe in the template rather than double-escaping.
        "segments_json": json.dumps(segments_json).replace("</", "<\\/"),
        "youtube_video_id_json": json.dumps(order["source_youtube_id"]),
        "return_reasons": db.RETURN_REASONS,
        "workflow_steps": workflow_steps_for_order(order),
        "dub_audio_ready": dub_audio_ready,
        "dubbed_video_ready": dubbed_video_ready,
        "stale_translation_count": stale_translation_count,
        "piper_voices": piper_voices,
        "human_recording_available": human_recording_available,
        "distinct_speakers": distinct_speakers,
        "speaker_voices": job.speaker_voices,
        "speaker_voice_names": job.speaker_voice_names,
        "speaker_subtitle_included": job.speaker_subtitle_included,
        "azure_voice_labels": _AZURE_VOICE_LABELS,
        "notice": notice,
        # Real raw deliverable text, not a re-rendering of it - the exact
        # bytes-minus-encoding a client's .srt/.vtt download will contain,
        # so an editor can catch a real formatting/timestamp problem here
        # before it ships, not after a client reports it.
        "target_srt": to_srt(job), "target_vtt": to_vtt(job),
        "source_srt": to_srt(job, source=True),
    })


@app.get("/staff/orders/{order_id}/source-audio")
def staff_source_audio(request: Request, order_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or not Path(order["audio_path"]).exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(order["audio_path"])


@app.post("/staff/orders/{order_id}/segments/{segment_id}/save")
def editor_save_segment(request: Request, order_id: str, segment_id: str,
                         body: dict = Body(...)):
    """JSON save endpoint used by the editor's JS (fetch), distinct from the
    form-POST one the plain review screen uses - same underlying logic via
    _apply_segment_edit either way."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)

    seg.para_start_target = bool(body.get("para_start"))              # a paragraph break right before this segment's first word
    _apply_segment_edit(order, job, seg, body.get("text", ""), bool(body.get("resynthesize")))
    return JSONResponse({
        "ok": True,
        "final_text": seg.final_text,
        "rendered_duration_ms": seg.rendered_duration_ms,
        # The Preview tab's caption bar (editor.js's bindPreviewCaptions)
        # closes over the segments array from page load, not segmentMeta -
        # without this, a correction here never reached that preview until
        # a full page reload, exactly the "whatever's in the English
        # transcript should be in the dubbed English transcript, live" bug.
        "wrapped_caption": _wrap_caption_text(seg.final_text.strip()) if seg.final_text.strip() else "",
        "display_end_ms": _display_end_ms(seg),
    })


@app.post("/staff/orders/{order_id}/segments/{segment_id}/flag")
def editor_toggle_flag(request: Request, order_id: str, segment_id: str):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)

    manual_reason = "staff_flagged"
    if manual_reason in seg.review_reasons:
        seg.review_reasons.remove(manual_reason)
    else:
        seg.review_reasons.append(manual_reason)
    seg.review_flag = bool(seg.review_reasons)

    job.save(str(Path(order["outdir"]) / "manifest.json"))
    return JSONResponse({"ok": True, "review_flag": seg.review_flag,
                          "review_reasons": seg.review_reasons})


def _retranslate_and_resync(order, job: Job, seg, mt_provider=None) -> None:
    """The actual "translate from the corrected source" guarantee: re-run
    MT on the current seg.source_final_text (a human correction always
    wins there, same as final_text), using the exact same
    translate_segment() the main pipeline uses - not a second, drifting
    copy of that logic - then re-render this segment's audio in the dub's
    current voice so the delivered dub track is never left speaking a
    stale translation. Skips the audio step for a gap segment or a
    stub-TTS (transcription/translation-only) order, same guard
    _apply_segment_edit uses.

    mt_provider is optional so a caller retranslating MANY segments in one
    request (editor_retranslate_all) can build one provider instance and
    reuse it - both ClaudeMT's total_cost_usd and LaraMT's
    total_chars_used only actually accumulate across calls on the SAME
    instance; a fresh one per segment would silently reset that tracking
    every time. A single-segment caller just leaves this unset."""
    if getattr(seg, "segment_type", "speech") == "gap":
        # A gap's "text" is a caption tag ([Applause], [Makofi]) an editor
        # typed on the Swahili side - there's no real language to
        # translate (run()'s own TTS/MT loops already skip gaps entirely
        # for exactly this reason), so a real MT provider call here would
        # just waste a real request and risk garbling a caption
        # convention into something that doesn't even look like a tag any
        # more. Mirror the exact source text across instead - that's the
        # actual fix for "I don't want to retag sounds again in English
        # once it's already done in Swahili." Into literal/spoken, never
        # edited_text, so a LATER source correction keeps auto-mirroring
        # too (same reason a real translate_segment result never touches
        # edited_text either) - an editor who writes their own English
        # caption directly (editor_save_target, which DOES set
        # edited_text) still permanently wins, exactly like a real
        # hand-edited translation would.
        #
        # What an editor typed there is a caption in the SOURCE language, and a translation reads it in the target language ([MUZIKI YACHEZA]
        # reads [MUSIC PLAYING], [MUSIC PLAYING] reads [MUSIK SPIELT]), the same as a tag inside a sentence. A tag that cannot be translated, or an order
        # with no translation step (a transcript), keeps the editor's own wording.
        tag = (seg.source_final_text or "").strip()
        translated = tag
        if tag and order["mt"] != "stub" and order["source_lang"] != order["target_lang"]:
            try:
                translated = translate_gap_tag(tag, mt_provider or get_mt(order["mt"]), order["source_lang"], order["target_lang"]) or tag
            except Exception:  # noqa: BLE001 - a tag that will not translate stays as the editor wrote it
                translated = tag
        seg.literal = translated
        seg.spoken = translated
        seg.translation_stale = False
        return
    mt_provider = mt_provider or get_mt(order["mt"])
    cps = timing.DEFAULT_CPS.get(order["target_lang"], 14.0)
    translate_segment(seg, mt_provider, order["source_lang"], order["target_lang"], cps,
                       all_segments=job.segments)
    seg.translation_stale = False  # back in sync - this translation IS of the current source
    if order["tts"] != "stub" and getattr(seg, "segment_type", "speech") != "gap":
        _resynthesize_one_segment(order, job, seg)


@app.post("/staff/orders/{order_id}/segments/{segment_id}/source")
def editor_save_source(request: Request, order_id: str, segment_id: str, body: dict = Body(...)):
    """Step 1 of the editor's workflow: correct the Swahili ASR transcript
    itself. If the English side hasn't been hand-finalized yet, this now
    ALSO re-translates (and re-renders the dub audio) from the corrected
    text immediately - see _retranslate_and_resync - so a corrected source
    reliably produces a corrected translation without a second, easy-to-
    forget manual step. A human who already reviewed/edited the English
    directly is never silently overwritten here - that still just gets
    flagged stale, same as before, and only clears via an explicit
    Re-translate or their own edit."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)

    text = body.get("text", "").strip()
    changed = (text or None) != seg.source_final_text
    seg.source_edited_transcript = text or None
    seg.para_start_source = bool(body.get("para_start"))              # a paragraph break right before this segment's first word
    auto_retranslated = False
    retranslate_error = None
    if changed:
        if seg.edited_text is None and not seg.approved:
            # Nobody has hand-finalized the English yet - safe to just
            # redo it from the corrected source, no human work at risk.
            try:
                _retranslate_and_resync(order, job, seg)
                auto_retranslated = True
            except Exception as exc:  # noqa: BLE001 - the source correction itself must
                # still save even if the MT provider is down/misconfigured - fall back to
                # the old flag-it-stale behavior rather than losing the save entirely.
                traceback.print_exc()
                seg.translation_stale = True
                retranslate_error = str(exc)
        else:
            # The English was already reviewed/edited directly - don't
            # silently clobber that; flag it and let a human decide
            # (Re-translate, or leave their own edit as-is).
            seg.translation_stale = True
    # Regenerate the actual delivered transcript file too, not just the
    # manifest - this endpoint didn't used to, so a saved Swahili
    # correction never reached transcript_{lang}.srt until someone
    # happened to trigger a full re-render some other way.
    (Path(order["outdir"]) / f"transcript_{order['source_lang']}.srt").write_text(
        to_srt(job, source=True), encoding="utf-8")
    # Real bug this used to have: when a source correction auto-
    # retranslates the English side (see _retranslate_and_resync above),
    # that changes seg.final_text - but this endpoint never rewrote the
    # actual DELIVERED subs_<lang>.srt/.vtt files, only the manifest. A
    # client's downloaded captions could silently drift from what Ereri
    # showed, forever, unless some unrelated later save happened to touch
    # them. _apply_segment_edit already does this on every target-side
    # save; this auto-retranslate path needs the exact same regeneration.
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(
        to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(
        to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    return JSONResponse({
        "ok": True, "source_final_text": seg.source_final_text,
        "translation_stale": seg.translation_stale, "auto_retranslated": auto_retranslated,
        "retranslate_error": retranslate_error,
        "final_text": seg.final_text, "target_cells": _build_target_cells(seg),
        # See editor_save_segment's own comment - the Preview tab's caption
        # bar needs this refreshed too, not just the on-screen cells.
        "wrapped_caption": _wrap_caption_text(seg.final_text.strip()) if seg.final_text.strip() else "",
        "display_end_ms": _display_end_ms(seg),
    })


def _editor_provider(order, kind: str, body) -> tuple[str | None, str | None]:
    """(the provider the editor chose for this redo, or None for the order's own; an error message, if the choice is not allowed)."""
    chosen = ((body or {}).get("provider") or "").strip() if isinstance(body, dict) else ""
    if not chosen or chosen == order[kind]:
        return None, None
    problem = provider_health.valid_switch(order, kind, chosen)
    return (None, problem) if problem else (chosen, None)


def _editor_log(user, order_id: str, kind: str, provider: str, detail: str) -> None:
    db.log_security_event("editor_provider_redo", user_id=user["id"], email=user["email"], detail=f"{order_id} {kind} {provider} {detail}")


@app.get("/staff/orders/{order_id}/provider-options")
def editor_provider_options(request: Request, order_id: str, kind: str = "asr"):
    """The editor's provider popup: who could do this step for this order, what each costs us, and which one is the default."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)
    if kind not in provider_health.COLUMN:
        return JSONResponse({"error": "unknown step"}, status_code=400)
    return JSONResponse(provider_health.editor_options(order, kind))


@app.post("/staff/orders/{order_id}/use-provider")
def editor_use_provider(request: Request, order_id: str, body: dict = Body(...)):
    """A whole-file redo moves the order itself to the chosen provider, so later edits and redos carry on with it."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)
    kind = (body.get("kind") or "").strip()
    chosen = (body.get("provider") or "").strip()
    if kind not in provider_health.COLUMN:
        return JSONResponse({"error": "unknown step"}, status_code=400)
    if chosen != order[kind]:
        problem = provider_health.valid_switch(order, kind, chosen)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        db.set_order_provider(order_id, kind, chosen)
        _editor_log(user, order_id, kind, chosen, "order moved to this provider")
    return JSONResponse({"ok": True, "provider": chosen})


@app.post("/staff/orders/{order_id}/redub")
def editor_redub(request: Request, order_id: str, body: dict = Body(...)):
    """Dubbing redo with a chosen voice provider: re-renders the audio of the chosen segments (or every segment) and rebuilds the dub track.
    Only the chosen segments are paid for. Redoing the whole file also moves the order to that provider."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)
    if order["tts"] == "stub":
        return JSONResponse({"error": "This order has no dub to redo."}, status_code=400)
    if order["dub_voice"] == "human":
        return JSONResponse({"error": "This order's dub is the voice actor's own recording, so there is no AI audio to redo."}, status_code=400)
    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    chosen, problem = _editor_provider(order, "tts", body)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    whole = bool(body.get("all"))
    wanted = set(body.get("segment_ids") or [])
    if not whole and not wanted:
        return JSONResponse({"error": "Choose a section first."}, status_code=400)
    tts_name, voice_id = (chosen, None) if chosen else _current_tts_route(order)
    try:
        tts_provider = get_tts(tts_name)
        redone = 0
        for seg in job.segments:
            if not (whole or seg.segment_id in wanted):
                continue
            if getattr(seg, "segment_type", "speech") == "gap" and not whole:
                continue
            _render_segment_audio(order, job, seg, tts_provider, voice_id)
            redone += 1
        track = build_timeline([(s.start_ms, s.audio_path) for s in job.segments],
                               job.source_duration_ms or (job.segments[-1].end_ms if job.segments else 0), sample_rate=tts_provider.sample_rate)
        write_wav_mono(str(Path(order["outdir"]) / f"dub_{order['target_lang']}.wav"), track, tts_provider.sample_rate)
    except Exception as exc:  # noqa: BLE001 - a real provider failure comes back as a message the editor can show
        traceback.print_exc()
        return JSONResponse({"error": f"{tts_name} provider failed: {exc}"}, status_code=502)
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    if whole and chosen:
        db.set_order_provider(order_id, "tts", chosen)
    _editor_log(user, order_id, "tts", chosen or order["tts"], f"{redone} segment(s){' (whole file)' if whole else ''}")
    return JSONResponse({"ok": True, "redone": redone, "provider": chosen or order["tts"]})


@app.post("/staff/orders/{order_id}/segments/{segment_id}/retranslate")
def editor_retranslate(request: Request, order_id: str, segment_id: str, body: dict | None = Body(None)):
    """Step 1 -> step 2 of the editor's workflow: the manual trigger for
    the same real re-translate-and-resync _retranslate_and_resync does
    automatically on a source save now - kept as its own action for
    forcing a fresh translation without re-saving the source text (e.g.
    after switching the order's MT provider, or to discard a stale hand
    edit and start over from MT)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)

    chosen, problem = _editor_provider(order, "mt", body)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    try:
        _retranslate_and_resync(order, job, seg, mt_provider=get_mt(chosen) if chosen else None)
    except Exception as exc:  # noqa: BLE001 - a real provider failure (bad/missing key,
        # network error) should come back as a real error the editor UI can show, not a
        # raw 500 with no message.
        traceback.print_exc()
        return JSONResponse({"error": f"{chosen or order['mt']} provider failed: {exc}"}, status_code=502)
    _editor_log(user, order_id, "mt", chosen or order["mt"], f"segment {segment_id}")

    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(
        to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(
        to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))

    return JSONResponse({
        "ok": True,
        "final_text": seg.final_text,
        "translation_confidence": seg.translation_confidence,
        "review_flag": seg.review_flag,
        "review_reasons": seg.review_reasons,
        "translation_stale": seg.translation_stale,
        "target_cells": _build_target_cells(seg),
        # See editor_save_segment's own comment - the Preview tab's caption
        # bar needs this refreshed too, not just the on-screen cells.
        "wrapped_caption": _wrap_caption_text(seg.final_text.strip()) if seg.final_text.strip() else "",
        "display_end_ms": _display_end_ms(seg),
    })


@app.post("/staff/orders/{order_id}/segments/{segment_id}/retranscribe")
def editor_retranscribe(request: Request, order_id: str, segment_id: str, body: dict | None = Body(None)):
    """Alt+T: re-run ASR on just this segment's own audio window, instead of
    fixing a transcription miss by hand. Real, not a re-run of the whole
    file - slices exactly [start_ms, end_ms] out of the order's source audio
    (same ffmpeg window-cut extract_audio_window already does for a dub's
    non-speech gap bed) and sends only that clip to the order's ASR
    provider, the same one the original pass used.

    A clip this short can come back as more than one provider-side segment
    (a genuine pause inside it) or as several near-duplicate low-confidence
    guesses - this always joins whatever text comes back into ONE flat
    transcript for THIS segment, on purpose: a chunk redo is defined as
    "reprocess what's already here", not "let the ASR provider silently
    re-draw segment/timing boundaries out from under the editor and the
    translation/dub already built on top of them."

    Writes into source_transcript (a fresh ASR result) and clears any prior
    source_edited_transcript - a human correction of the OLD wrong text
    would be nonsensical to keep layered on top of a fresh transcript that
    supersedes it. Real per-word timing comes back from the provider too
    (offset from clip-relative back to the segment's real position in the
    source audio), so _build_source_cells's click-to-seek stays accurate -
    exactly what a manual correction can never give it (see that function's
    own docstring on the approx-timing fallback this avoids).

    Then reuses the exact same "auto-retranslate if the English hasn't been
    hand-finalized yet, else just flag it stale" rule editor_save_source
    already applies to a human's own source correction - a fresh ASR result
    is no different from one for that purpose."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)
    if getattr(seg, "segment_type", "speech") == "gap":
        return JSONResponse({"error": "This is a non-speech gap - there's no speech here to re-transcribe."}, status_code=400)
    chosen, problem = _editor_provider(order, "asr", body)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = str(Path(tmpdir) / "clip.wav")
        if not extract_audio_window(order["audio_path"], seg.start_ms, seg.end_ms, clip_path, sample_rate=16000):
            return JSONResponse({"error": "Could not extract this segment's audio (ffmpeg unavailable, or the window is empty)."}, status_code=502)
        try:
            asr_provider = get_asr(chosen or order["asr"])
            clip_segments = asr_provider.transcribe(clip_path, language=order["source_lang"])
        except Exception as exc:  # noqa: BLE001 - a real provider failure should come back
            # as a real error the editor UI can show, not a raw 500 with no message.
            traceback.print_exc()
            return JSONResponse({"error": f"{chosen or order['asr']} provider failed: {exc}"}, status_code=502)
        _editor_log(user, order_id, "asr", chosen or order["asr"], f"segment {segment_id}")

    if not clip_segments:
        return JSONResponse({"error": "The ASR provider found no speech in this window - it may genuinely be silence or non-speech audio."}, status_code=422)

    # Every returned word's timing is relative to the CLIP (starts at 0) -
    # shift it back by seg.start_ms so it lines up with the real source
    # audio timeline, same as every other timestamp in the manifest.
    new_words = []
    for cs in clip_segments:
        for w in cs.words:
            new_words.append(Word(text=w.text, start_ms=seg.start_ms + w.start_ms,
                                   end_ms=seg.start_ms + w.end_ms, confidence=w.confidence))
    new_text = " ".join(cs.source_transcript.strip() for cs in clip_segments if cs.source_transcript.strip())
    confidences = [cs.source_confidence for cs in clip_segments if cs.source_transcript.strip()]

    seg.words = new_words
    seg.source_transcript = new_text
    seg.source_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    seg.source_edited_transcript = None  # supersedes any prior hand correction of the old text

    auto_retranslated = False
    retranslate_error = None
    if seg.edited_text is None and not seg.approved:
        try:
            _retranslate_and_resync(order, job, seg)
            auto_retranslated = True
        except Exception as exc:  # noqa: BLE001 - the fresh transcript must still save even
            # if the MT provider is down/misconfigured - fall back to flag-it-stale.
            traceback.print_exc()
            seg.translation_stale = True
            retranslate_error = str(exc)
    else:
        seg.translation_stale = True

    (Path(order["outdir"]) / f"transcript_{order['source_lang']}.srt").write_text(
        to_srt(job, source=True), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(
        to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(
        to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))

    return JSONResponse({
        "ok": True, "source_final_text": seg.source_final_text,
        "source_cells": _build_source_cells(seg), "source_confidence": seg.source_confidence,
        "auto_retranslated": auto_retranslated, "retranslate_error": retranslate_error,
        "review_flag": seg.review_flag, "review_reasons": seg.review_reasons,
        "translation_confidence": seg.translation_confidence, "translation_stale": seg.translation_stale,
        "final_text": seg.final_text, "target_cells": _build_target_cells(seg),
        "wrapped_caption": _wrap_caption_text(seg.final_text.strip()) if seg.final_text.strip() else "",
        "display_end_ms": _display_end_ms(seg),
    })


@app.post("/staff/orders/{order_id}/segments/{segment_id}/voice-direction")
def editor_set_voice_direction(request: Request, order_id: str, segment_id: str, body: dict = Body(...)):
    """The editor doing final touches on the dub voice directs how THIS
    segment gets spoken - slower/faster than the automatic fit-to-slot
    pace, or spelled out letter-by-letter instead of read as a word (see
    Segment.manual_pace_pct/spell_out and _render_segment_audio) - then
    re-renders just this segment and the mixed dub track from it. A no-op
    on the audio side (but the direction still saves) whenever the order's
    dub is currently a human recording - see _resynthesize_one_segment's
    own guard on that."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = db.get_order(order_id)
    if not order:
        return JSONResponse({"error": "order not found"}, status_code=404)
    if order["tts"] == "stub":
        return JSONResponse({"error": "This order has no dub voice (transcription/translation only)."},
                             status_code=400)

    job = _load_job(order)
    if job is None:
        return JSONResponse({"error": "job not processed yet"}, status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if seg is None:
        return JSONResponse({"error": "segment not found"}, status_code=404)
    if getattr(seg, "segment_type", "speech") == "gap":
        return JSONResponse({"error": "A gap segment has no voice to direct."}, status_code=400)

    try:
        pace_pct = float(body.get("pace_pct") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "pace_pct must be a number"}, status_code=400)
    pace_pct = max(-50.0, min(100.0, pace_pct))  # matches time_stretch's own 0.5x-2.0x clamp
    seg.manual_pace_pct = pace_pct
    seg.spell_out = bool(body.get("spell_out"))
    if not pace_pct and "manual_pace_override" in seg.review_reasons:
        seg.review_reasons.remove("manual_pace_override")
        seg.review_flag = bool(seg.review_reasons)

    _resynthesize_one_segment(order, job, seg)

    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(
        to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(
        to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))

    return JSONResponse({
        "ok": True,
        "manual_pace_pct": seg.manual_pace_pct,
        "spell_out": seg.spell_out,
        "rendered_duration_ms": seg.rendered_duration_ms,
        "review_flag": seg.review_flag,
        "review_reasons": seg.review_reasons,
        "dub_voice": order["dub_voice"],
    })


@app.post("/staff/orders/{order_id}/detect-gaps")
def editor_detect_gaps(request: Request, order_id: str):
    """Retrofits real gap segments onto an order that was processed BEFORE
    kauli.pipeline._insert_non_speech_segments existed - every order run
    through the pipeline since gets these automatically at the ASR step;
    this is the one-time catch-up for one that predates it. Existing
    segments (and any corrections already made to them) are untouched -
    this only ever ADDS new gap-type segments into stretches that
    currently have no segment at all, using the exact same real function
    a fresh pipeline run uses, not a second copy of the gap-finding logic."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)

    before = len(job.segments)
    job.segments = _insert_non_speech_segments(job.segments, job.source_duration_ms)
    added = len(job.segments) - before

    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(to_vtt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"transcript_{order['source_lang']}.srt").write_text(
        to_srt(job, source=True), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    return RedirectResponse(
        f"/staff/orders/{order_id}/editor?notice=Added+{added}+gap+cell(s)+for+non-speech+stretches.",
        status_code=303)


@app.post("/staff/orders/{order_id}/retranslate-all")
def editor_retranslate_all(request: Request, order_id: str):
    """Bulk version of _retranslate_and_resync - re-translates (and
    re-renders audio for) EVERY segment that hasn't been hand-finalized on
    the English side, from its current (possibly corrected) source text.
    For fixing a whole order that was translated with a weaker MT
    provider before a better one was available (e.g. before
    ANTHROPIC_API_KEY was set) or before a batch of source corrections
    went in - one click instead of retranslating each segment by hand.
    Segments a human has already reviewed/edited directly are skipped,
    same "never silently overwrite finished human work" rule as the
    auto-retranslate-on-source-save path."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)

    # One provider instance for the whole batch, not one per segment - see
    # _retranslate_and_resync's docstring on why that matters for
    # ClaudeMT's total_cost_usd / LaraMT's total_chars_used.
    mt_provider = get_mt(order["mt"])
    retranslated = 0
    skipped = 0
    failed_error = None
    for seg in job.segments:
        if getattr(seg, "segment_type", "speech") == "gap" and not (seg.source_final_text or "").strip():
            continue                                                          # an empty silent stretch has no tag to translate
        if seg.edited_text is not None or seg.approved:
            skipped += 1
            continue
        try:
            _retranslate_and_resync(order, job, seg, mt_provider=mt_provider)
        except Exception as exc:  # noqa: BLE001 - a real provider failure (bad/missing key,
            # network error) shouldn't lose whatever DID succeed before it, or crash to a
            # raw 500 with no way back into the editor.
            traceback.print_exc()
            failed_error = str(exc)
            break
        retranslated += 1

    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(to_vtt(job), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    usage_note = ""
    if hasattr(mt_provider, "total_chars_used") and mt_provider.total_chars_used:
        usage_note = f"+-+{mt_provider.total_chars_used}+real+characters+used+this+run"
    elif hasattr(mt_provider, "total_cost_usd") and mt_provider.total_cost_usd:
        usage_note = f"+-+%24{mt_provider.total_cost_usd:.4f}+real+spend+this+run"
    if failed_error:
        return RedirectResponse(
            f"/staff/orders/{order_id}/editor?notice=Retranslated+{retranslated}+segment(s)+before+"
            f"hitting+an+error+with+the+{quote(order['mt'])}+provider+-+{quote(failed_error[:200])}"
            f"{usage_note}.",
            status_code=303)
    return RedirectResponse(
        f"/staff/orders/{order_id}/editor?notice=Retranslated+{retranslated}+segment(s)"
        f"+({skipped}+already+hand-finalized,+left+alone){usage_note}.",
        status_code=303)


@app.post("/staff/orders/{order_id}/segments/{segment_id}/set-speaker")
def editor_set_segment_speaker(request: Request, order_id: str, segment_id: str,
                                speaker_id: str = Form("")):
    """Manual speaker correction/assignment for ONE segment - the human
    half of multi-speaker support. Transkriptor's real diarization labels
    (TranskriptorASR) populate seg.speaker_id automatically for orders
    that use it; faster-whisper never does, and even a real diarization
    label can be wrong (two segments split from one person, or one
    segment actually crossing a speaker change). An editor is already
    listening to every segment for QA anyway - correcting or assigning a
    speaker by ear costs nothing extra and is what actually makes "cast
    each character a distinct voice" (see assign-speaker-voice below)
    possible on an order that has no automatic diarization at all.
    Doesn't re-synthesize by itself - Ctrl+Shift+S or a fresh voice
    assignment on the (corrected) speaker picks it up."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)
    seg = next((s for s in job.segments if s.segment_id == segment_id), None)
    if not seg:
        return HTMLResponse("Segment not found.", status_code=404)
    seg.speaker_id = speaker_id.strip() or None
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    return RedirectResponse(f"/staff/orders/{order_id}/editor?notice=Speaker+updated.", status_code=303)


@app.post("/staff/orders/{order_id}/assign-speaker-voice")
def editor_assign_speaker_voice(request: Request, order_id: str, speaker_id: str = Form(...),
                                 voice_key: str = Form(...)):
    """Assigns one detected/tagged speaker a specific, distinct Piper
    voice - the actual "same character, same voice throughout" guarantee
    (see Job.speaker_voices' own comment), achieved with the voices
    already installed, not a new diarization/casting ML pipeline. Only
    re-renders THAT speaker's segments (only_speaker_id), not the whole
    order - assigning a second character's voice shouldn't waste time
    re-doing a first one that's already right.

    Piper only, on purpose: XTTS voice cloning is a single reference
    speaker cloned from the source audio (see kauli.pipeline.run's own
    comment - "multi-speaker sources need diarization, not built yet"),
    it doesn't extend to "clone several different speakers at once"
    without real per-speaker reference-clip isolation, which is a bigger,
    separate piece of work this doesn't attempt."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)
    if voice_key not in PIPER_VOICES:
        return RedirectResponse(f"/staff/orders/{order_id}/editor?notice=Unknown+voice.", status_code=303)
    voice_path = str(PROJECT_ROOT / PIPER_VOICES[voice_key]["path"])
    if not Path(voice_path).exists():
        return RedirectResponse(
            f"/staff/orders/{order_id}/editor?notice=Voice+model+not+downloaded%3A+{quote(voice_path)}",
            status_code=303)
    job.speaker_voices[speaker_id] = voice_path
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    if order["tts"] != "stub":
        _resynthesize_full_dub(order, job, "piper", voice_path, only_speaker_id=speaker_id)
    return RedirectResponse(
        f"/staff/orders/{order_id}/editor?notice=Assigned+{quote(PIPER_VOICES[voice_key]['label'])}+to+"
        f"{quote(speaker_id)}.", status_code=303)


_AZURE_VOICE_LABELS = {"sw-KE-ZuriNeural": "Zuri (female)", "sw-KE-RafikiNeural": "Rafiki (male)"}


@app.post("/staff/orders/{order_id}/assign-speaker-voice-azure")
def editor_assign_speaker_voice_azure(request: Request, order_id: str, speaker_id: str = Form(...),
                                       voice_name: str = Form(...)):
    """The Azure-voice sibling of editor_assign_speaker_voice above - same
    per-speaker guarantee, for the two real Azure Kiswahili voices instead
    of a Piper model. This is the manual override half of multi-speaker
    voice assignment; kauli.pipeline.run's own TTS stage already assigns a
    default automatically from each detected speaker's pitch (see
    kauli.speaker_gender) - a human correcting that by ear here always
    wins, permanently, same as a Piper assignment does."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)
    if voice_name not in _AZURE_VOICE_LABELS:
        return RedirectResponse(f"/staff/orders/{order_id}/editor?notice=Unknown+voice.", status_code=303)
    job.speaker_voice_names[speaker_id] = voice_name
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    if order["tts"] == "azure":
        _resynthesize_full_dub(order, job, "azure", voice_name, only_speaker_id=speaker_id)
    return RedirectResponse(
        f"/staff/orders/{order_id}/editor?notice=Assigned+{quote(_AZURE_VOICE_LABELS[voice_name])}+to+"
        f"{quote(speaker_id)}.", status_code=303)


@app.post("/staff/orders/{order_id}/speaker-subtitle-inclusion")
def editor_set_speaker_subtitle_inclusion(request: Request, order_id: str, speaker_id: str = Form(...),
                                           included: str = Form(...)):
    """Real request: a documentary with a Swahili subject and an English
    narrator often only wants the SUBJECT subtitled, not the narrator -
    on-screen text for the local speaker's own words, nothing cluttering
    the screen while the narrator (already understandable to an English
    audience) is talking. This is a real filter applied at export time
    (see kauli.subtitles.to_srt/to_vtt), never a re-transcription or a
    dropped segment - the segment, its text, and its audio are untouched;
    only whether it appears in the delivered subs_*.srt/.vtt changes.
    Deliberately NOT automatic: Kauli doesn't yet detect which language
    each individual speaker is using (source_lang/target_lang are per-
    ORDER, not per-segment), so there's no reliable signal to guess this
    from - a human decides, same as every other speaker-level choice in
    Ereri."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    job = _load_job(order)
    if job is None:
        return HTMLResponse("Job not processed yet.", status_code=404)
    job.speaker_subtitle_included[speaker_id] = (included == "1")
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.srt").write_text(to_srt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt").write_text(to_vtt(job), encoding="utf-8")
    (Path(order["outdir"]) / f"transcript_{order['source_lang']}.srt").write_text(
        to_srt(job, source=True), encoding="utf-8")
    verb = "Included" if included == "1" else "Excluded"
    return RedirectResponse(
        f"/staff/orders/{order_id}/editor?notice={verb}+{quote(speaker_id)}+in+subtitles.", status_code=303)


# ------------------------------------------------------- youtube polling ----
YOUTUBE_POLL_INTERVAL_S = 20 * 60  # 20 min - playlistItems.list is 1 quota
# unit/call; even a handful of watches checked this often stays nowhere
# near the default 10,000/day cap.


def _youtube_poll_once() -> None:
    for watch in db.list_youtube_watches(active_only=True):
        try:
            videos = youtube_poll.list_recent_public_videos(watch["playlist_id"])
            for v in videos:
                if not db.video_already_seen(watch["id"], v["video_id"]):
                    db.create_pending_import(watch["id"], v["video_id"], v["title"], None)
            db.record_youtube_poll_result(watch["id"], error=None)
        except Exception as exc:  # noqa: BLE001 - one bad watch (deleted channel,
            # revoked key, network blip) must not kill polling for everyone else's.
            api_log.warning("youtube poll failed", extra={"watch_id": watch["id"], "error": str(exc)})
            db.record_youtube_poll_result(watch["id"], error=str(exc))


def _youtube_poll_loop() -> None:
    while True:
        time.sleep(YOUTUBE_POLL_INTERVAL_S)
        if youtube_poll.youtube_polling_configured():
            _youtube_poll_once()


if youtube_poll.youtube_polling_configured():
    threading.Thread(target=_youtube_poll_loop, daemon=True).start()


# ------------------------------------------------------ deadline watch ----
# The real, right-sized version of the doc's "Timekeeper" cron: no Redis,
# no separate scheduler process - same daemon-thread pattern as the
# YouTube poller above, since this app already has one background worker
# thread and no distributed infra to run a real cron against. Runs
# regardless of whether the mailer is configured (notifications.notify_staff
# already no-ops silently if it isn't) - the *_sent_at columns just dedupe
# so each order is only ever alerted on once per threshold, not every 15
# minutes for as long as it stays overdue. The dashboard/queue page itself
# (sorted and color-coded by deadline) is the real source of truth either
# way - this is the proactive nudge on top of that, not a replacement for it.
DEADLINE_WATCH_INTERVAL_S = 15 * 60
DEADLINE_WARNING_WINDOW_S = 2 * 3600  # matches the doc's "within 2 hours" check


def _system_sender_id() -> str | None:
    """A real staff account id to send an automated, staff-authored
    message under - client-facing message templates already show any
    staff sender as just "Kauli" (see staff_review.html), so which real
    staff account this is doesn't matter, only that it's a genuine one.
    None (and the caller skips sending) on the - currently impossible -
    case of no staff account existing at all, rather than crashing the
    background deadline-watch loop over it."""
    staff = db.list_staff_users()
    return staff[0]["id"] if staff else None


def _deadline_watch_once() -> None:
    now = time.time()
    for order in db.list_orders_needing_deadline_check():
        if order["internal_deadline_at"] <= now:
            if not order["deadline_missed_alert_sent_at"]:
                notifications.notify_staff(
                    f"Kauli: order {order['id']} has MISSED its internal deadline",
                    f"Order {order['id']} ({order['original_filename']}) - status: {order['status']} - "
                    f"was due internally at "
                    f"{datetime.fromtimestamp(order['internal_deadline_at']).strftime('%B %d, %H:%M')} and "
                    f"hasn't shipped yet.\n\nSee /staff/orders/{order['id']}.",
                )
                db.mark_deadline_missed_alert_sent(order["id"])
        elif (order["internal_deadline_at"] - now <= DEADLINE_WARNING_WINDOW_S
              and not order["deadline_warning_sent_at"]):
            hours_left = round((order["internal_deadline_at"] - now) / 3600, 1)
            notifications.notify_staff(
                f"Kauli: order {order['id']} is due in under {DEADLINE_WARNING_WINDOW_S // 3600} hours",
                f"Order {order['id']} ({order['original_filename']}) - status: {order['status']} - "
                f"has about {hours_left} hour(s) left on its internal deadline.\n\n"
                f"See /staff/orders/{order['id']}.",
            )
            db.mark_deadline_warning_sent(order["id"])

        # Enterprise SLA credit (billing.ENTERPRISE_SLA_CREDIT_PCT) - a
        # separate, later threshold than the internal-deadline alert
        # above: deadline_at is the real CLIENT-FACING promise (already
        # includes tat.py's 20% buffer specifically so a normal day
        # delivers early), so this only fires once that promise itself
        # has actually been broken, not just the tighter internal one
        # staff works against. Real money (wallet credit), so this stays
        # narrowly scoped: enterprise tier only, one credit per order
        # ever (sla_credit_issued_at), and only when a real charge
        # (cost_usd) exists to take a percentage of.
        if (order["tier"] == "enterprise" and order["deadline_at"] and order["deadline_at"] <= now
                and not order["sla_credit_issued_at"] and (order["cost_usd"] or 0) > 0):
            credit_usd = round(order["cost_usd"] * billing.ENTERPRISE_SLA_CREDIT_PCT, 2)
            db.add_wallet_credits(order["client_id"], billing.usd_to_credits(credit_usd))
            db.mark_sla_credit_issued(order["id"])
            sender_id = _system_sender_id()
            if sender_id:
                db.create_message(
                    order["id"], sender_id, "client",
                    f"This order missed the delivery window we promised you - that's on us, not you. "
                    f"We've credited ${credit_usd:.2f} ({billing.ENTERPRISE_SLA_CREDIT_PCT:.0%} of this "
                    f"order's cost) to your account as wallet credit, automatically, no need to ask. "
                    f"We're still finishing this order and will let you know the moment it's ready.",
                )
            notifications.notify_staff(
                f"Kauli: SLA credit auto-issued on order {order['id']}",
                f"Order {order['id']} ({order['original_filename']}) missed its client-facing deadline - "
                f"${credit_usd:.2f} was automatically credited to the client's wallet. "
                f"See /staff/orders/{order['id']}.",
            )


def _deadline_watch_loop() -> None:
    while True:
        time.sleep(DEADLINE_WATCH_INTERVAL_S)
        try:
            _deadline_watch_once()
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not kill the loop forever
            api_log.warning("deadline watch sweep failed", extra={"error": str(exc)})


threading.Thread(target=_deadline_watch_loop, daemon=True).start()


# ---------------------------------------------------- activation nudge ----
# Automatic version of the staff-triggered nudge above (see
# _send_activation_nudge) - a real, deliberate decision to switch from
# "a human decides when a real client gets chased" (this file's own prior
# comment) to a fixed 72-hour timer, made explicitly by the user rather
# than assumed. Same daemon-thread pattern as the YouTube poller/deadline
# watch above; checking hourly against a 72-hour threshold is plenty
# granular - being an hour late to a 3-day-old signup changes nothing.
# base_url comes from KAULI_PUBLIC_BASE_URL (see _public_base_url's own
# comment on why - the same real, deployed value worker.py already reads
# for its own no-request-object contexts), not a request object, since
# nothing triggered this sweep.
ACTIVATION_NUDGE_INTERVAL_S = 60 * 60
ACTIVATION_NUDGE_THRESHOLD_HOURS = 72.0


def _activation_nudge_sweep_once() -> None:
    try:
        onboarding.alert_stuck_first_orders()              # a new customer's first order that is not moving: staff hear about it the same day
    except Exception as exc:  # noqa: BLE001
        api_log.warning("stuck first order alert failed", extra={"error": str(exc)})
    base_url = os.environ.get("KAULI_PUBLIC_BASE_URL", "").rstrip("/") or "https://kauli-forgemedia.com"
    for client in db.clients_needing_activation_nudge(threshold_hours=ACTIVATION_NUDGE_THRESHOLD_HOURS):
        _send_activation_nudge(client, base_url)


def _activation_nudge_loop() -> None:
    while True:
        time.sleep(ACTIVATION_NUDGE_INTERVAL_S)
        try:
            _activation_nudge_sweep_once()
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not kill the loop forever
            api_log.warning("activation nudge sweep failed", extra={"error": str(exc)})


threading.Thread(target=_activation_nudge_loop, daemon=True).start()


def _winback_loop() -> None:
    """Once a day: take back finished free months, and (only when KAULI_WINBACK_AUTOSEND is switched on) offer a new one to
    customers who paid and went quiet. Sleeps first, like the loops above."""
    while True:
        time.sleep(winback.SWEEP_INTERVAL_S)
        try:
            _base = os.environ.get("KAULI_PUBLIC_BASE_URL", "").rstrip("/") or "https://kauli-forgemedia.com"
            winback.sweep_once(_base)
            dunning.expire_backstop()
            site_analytics.prune()
            engagement.sweep_once(_base)
            weekly_kpi.send_if_due(_base, mailer.send_email, mailer.wrap_email_html)
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not kill the loop forever
            api_log.warning("winback sweep failed", extra={"error": str(exc)})


threading.Thread(target=_winback_loop, daemon=True).start()

account_hub.register(sys.modules[__name__])
blog_audio.register(sys.modules[__name__])
cloud_import.register(sys.modules[__name__])
connections.register(sys.modules[__name__])
staff_integrations.register(sys.modules[__name__])
account_closure.register(sys.modules[__name__])
winback.register(sys.modules[__name__])
dunning.register(sys.modules[__name__])
engagement.register(sys.modules[__name__])
onboarding.register(sys.modules[__name__])
repurpose.register(sys.modules[__name__])
model_stats.register(sys.modules[__name__])
calendar_feed.register(sys.modules[__name__])        # calendar links and sending Kauli to meetings        # import from Google Drive, OneDrive and Dropbox          # narrated blog posts and the podcast feed
blog_audio.mark_stale_jobs_failed()         # notes, analytics, workspace, vocabulary, plans, rewards, integrations, assistant
