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
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from datetime import datetime
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
from kauli import timing  # noqa: E402
from kauli.models import Job, Word, split_off_speaker_tag  # noqa: E402
from kauli.mixer import build_timeline, write_wav_mono, extract_reference_clip, extract_audio_window, time_stretch  # noqa: E402
from kauli.pipeline import translate_segment, apply_stretch_fit, resolve_voice_for_segment, _insert_non_speech_segments, spell_out_text, render_gap_audio, strip_bracket_tags_for_tts  # noqa: E402
from kauli.providers import get_asr, get_mt, get_tts  # noqa: E402
from kauli.providers.tts import PIPER_VOICES  # noqa: E402
from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES  # noqa: E402
from . import locales as site_locales, seo_languages  # noqa: E402
from kauli.subtitles import to_srt, to_vtt, _wrap_caption_text, _display_end_ms  # noqa: E402

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
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
         "-vf", f"subtitles='{escaped}'",
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
    if path in ("/client/orders", "/settings", "/api/v1/orders"):
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
    "https://www.youtube.com https://www.googletagmanager.com https://accounts.google.com; "
    # style-src/font-src: api.fontshare.com serves the @font-face CSS itself
    # (style-src), cdn.fontshare.com serves the actual woff2/woff/ttf files
    # it points at (font-src) - same split as the existing Google Fonts
    # pair (fonts.googleapis.com serves CSS, fonts.gstatic.com serves files).
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://api.fontshare.com "
    "https://accounts.google.com; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.fontshare.com; "
    "img-src 'self' data: https://i.ytimg.com; "
    # google-analytics.com/analytics.google.com: real bug found and fixed
    # 2026-08-30 - GA's own script (script-src above) was allowed to load,
    # but the actual hit it sends on every pageview was still silently
    # blocked here the whole time, on every visitor who'd accepted
    # analytics cookies. Confirmed live via a real CSP violation in the
    # console before this fix, confirmed gone after it.
    "connect-src 'self' https://cloudflareinsights.com https://static.cloudflareinsights.com "
    "https://calendly.com https://*.calendly.com https://*.r2.cloudflarestorage.com "
    "https://www.google-analytics.com https://*.google-analytics.com https://*.analytics.google.com "
    "https://accounts.google.com; "
    "frame-src https://calendly.com https://*.calendly.com https://www.youtube.com https://accounts.google.com; "
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
# Unambiguous long form for formal documents (receipts, invoices) - "Aug 22,
# 15:48" is fine for an activity feed, not for something someone might file
# for their own records.
templates.env.filters["timestamp_to_date"] = lambda ts: datetime.fromtimestamp(ts).strftime("%B %d, %Y")


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


def _daily_trend_svg(daily_trend: list[dict], width: int = 760, height: int = 200) -> str:
    """Hand-rolled inline SVG line chart, real data only (see
    db.daily_job_trend) - no charting library, two series (created vs
    delivered per day). currentColor for the "created" line so it follows
    the page's own text color in both themes; the accent color is reserved
    for "delivered" since that's the one number worth drawing the eye to."""
    if not daily_trend:
        return ""
    n = len(daily_trend)
    pad_l, pad_r, pad_t, pad_b = 8, 8, 14, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    max_val = max(1, max(max(d["created"], d["delivered"]) for d in daily_trend))
    x_step = plot_w / max(1, n - 1)

    def points(key: str) -> str:
        pts = []
        for i, d in enumerate(daily_trend):
            x = pad_l + i * x_step
            y = pad_t + plot_h - (d[key] / max_val) * plot_h
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    first_day = daily_trend[0]["day"]
    last_day = daily_trend[-1]["day"]
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Jobs created and delivered, last {n} days" '
        f'xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">'
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" y2="{pad_t + plot_h}" '
        f'stroke="currentColor" stroke-opacity="0.15"/>'
        f'<polyline points="{points("created")}" fill="none" stroke="currentColor" stroke-opacity="0.45" stroke-width="2"/>'
        f'<polyline points="{points("delivered")}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>'
        f'<text x="{pad_l}" y="{height - 4}" font-size="10" fill="currentColor" opacity="0.6">{first_day}</text>'
        f'<text x="{width - pad_r}" y="{height - 4}" font-size="10" fill="currentColor" opacity="0.6" text-anchor="end">{last_day}</text>'
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


def _load_job(order) -> Job | None:
    manifest_path = Path(order["outdir"]) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return Job.load(str(manifest_path))
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
    target_name = SOURCE_LANGUAGES.get(order["target_lang"], order["target_lang"])
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
templates.env.globals["plan_chosen"] = lambda u: bool(u) and (u["role"] != "client" or _plan_chosen(u["client_scope_id"]))


def _lara_configured() -> bool:
    """Lara Translate is Kauli's translation engine for everything that is not
    a premium plan's Claude upgrade. Instant AI translation across the wider
    language list is only offered when its keys are set - never advertised in
    the order form otherwise."""
    return bool(os.environ.get("LARA_ACCESS_KEY_ID") and os.environ.get("LARA_ACCESS_KEY_SECRET"))


def _pick_mt(source_lang: str, target_lang: str, plan_mt: str, review_tier: str) -> tuple[str | None, str | None]:
    """(mt provider name, None) or (None, user-facing error). Lara first
    (the chosen engine); a priority plan's Claude upgrade, the Kikuyu
    Claude-first rule, and the original local/Azure fallbacks are kept for
    anything Lara can't take - so nothing changes for an account until Lara's
    keys are actually set."""
    lara_ok = _lara_configured()
    if source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # Kikuyu: Claude if connected (as before), otherwise Lara (which
        # supports ki-KE) - always followed by a human review.
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "claude", None
        if lara_ok:
            return "lara", None
        return None, ("Kikuyu translation needs our AI translation provider connected first, "
                      "which isn't done yet - contact us and we'll handle this one manually "
                      "in the meantime.")
    if review_tier == "ai" and lara_ok:
        return "lara", None
    if plan_mt == "claude":
        return "claude", None  # a Premium/Enterprise plan's priority-translation upgrade
    if lara_ok:
        return "lara", None
    # Lara not connected: the original behaviour, unchanged.
    mt = plan_mt
    if mt == "local" and not (source_lang == "sw" and target_lang == "en"):
        if os.environ.get("AZURE_TRANSLATOR_KEY"):
            return "azure-translate", None
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
MARKETING_FAQ = [
    {"q": "How much does it cost?",
     "a": f"From ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per audio-minute for Instant AI "
          f"transcription and ${billing.PRICE_CARD['intl']['transcribe']['human']:.2f} with human review "
          f"(US and international; lower in Kenya and East Africa). Your first {billing.FREE_MINUTES_PER_MONTH:.0f} "
          "minutes of Instant AI transcription are free.",
     "link": ("#pricing", "See all prices")},
    {"q": "What is the difference between Instant AI and Human-reviewed?",
     "a": "Instant AI is generated by machine and ready in minutes, and it is labelled as not reviewed by a "
          "person. Human-reviewed adds a real editor who checks the result against the audio before delivery "
          "(Swahili, Kikuyu and English).",
     "link": ("/blog/ai-vs-human-transcription-which-do-you-need", "How to choose")},
    {"q": "How long does an order take?",
     "a": "Instant AI orders are usually ready within minutes of upload, and longer files take longer. "
          "Human-reviewed orders depend on length and the review queue, and you see an estimated delivery "
          "time when you order."},
    {"q": "Which languages do you support?",
     "a": f"Instant AI transcribes {len(AI_ONLY_LANGUAGES) + len(SOURCE_LANGUAGES)} languages and translates "
          "between them (except Kikuyu). Dubbing and human review are available for Swahili, Kikuyu and English.",
     "link": ("/languages", "See every language")},
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
    "ngos": {
        # Title has no leading "Kauli" - base.html's <title> block always
        # appends " - Kauli" itself (see marketing.html's own title, which
        # follows the same convention); every title in this dict used to
        # start with "Kauli" too, which meant every solution page rendered
        # a real, live, duplicate "... - Kauli - Kauli" title tag (confirmed
        # via curl against the actual rendered <title> before this fix).
        "title": "Swahili and English Localization for NGOs, Without Agency Rates",
        "meta_description": "Transcription, translation and dubbing between Swahili, Kikuyu and English for "
                             "field reports, training videos and campaigns - transparent per-minute pricing, "
                             "optional human review before delivery.",
        "kicker": "For NGOs and civil-society organizations",
        "h1": "Reach Swahili and English-speaking audiences, without an agency budget or a six-week wait",
        "intro": "Field reports, training videos and awareness campaigns need to speak to the people they're "
                 "actually about. Kauli transcribes, translates and dubs between Swahili, Kikuyu and English at "
                 "a transparent per-minute rate, with a real editor checking every line before it reaches you on Human-reviewed orders.",
        "why_heading": "Built for real field and campaign work",
        "points": [
            {"title": "Human-reviewed when you choose it",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio line by line "
                      "before it's marked ready - it matters when the content is about real people and real "
                      "communities."},
            {"title": "Consent required before any voice is cloned",
             "body": "If a dub needs to sound like the original speaker - an interview subject, a community "
                      "leader - we require your explicit confirmation of rights and consent before that ever "
                      "runs. Never assumed."},
            {"title": "A real delivery estimate, not a guess",
             "body": "Every Human-reviewed order gets a real turnaround estimate the moment it's confirmed, based on its "
                      "actual length and service level, visible on your dashboard - so you know if it'll be "
                      "ready before your event or publication date."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits, no surprise tiers. Your first few minutes "
                      "are free to test the quality yourself."},
        ],
        "cta_heading": "See the quality for yourself, on your own material",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-translation", "video-transcription", "captioning"],
        "tour": {"mode": "upload", "filename": "field_report_march2026.mp4"},
        "faq": [
            {"q": "Will our field footage and recordings stay confidential?",
             "a": "We don't share your data with anyone else, and we don't run ads or ad-tracking on "
                  "this site. Uploaded files and generated deliverables are currently kept on our "
                  "servers indefinitely - we don't yet have automatic deletion after a fixed period, so "
                  "if your program handles sensitive material, ask us (or use the \"Your data\" section "
                  "in Settings) to delete a file or your account data at any time, and we will."},
            {"q": "Can we pay by bank transfer to fit a grant disbursement cycle?",
             "a": "Yes - bank transfer is a real payment option on every plan, though it's the one "
                  "method that isn't confirmed automatically (a real person confirms once the transfer "
                  "lands). It's also the standard billing method on our Enterprise plan for "
                  "organizations with an ongoing relationship."},
            {"q": "What if our recording is in Kikuyu, not Swahili?",
             "a": "Kikuyu goes through an extra manual transcription step, since no speech-recognition "
                  "model handles it yet - a real human transcriber types the source by ear. Translation "
                  "and voicing after that run the same AI-plus-human-review pipeline as everything "
                  "else; Kikuyu orders just cost a bit more per minute to cover that real labor."},
            {"q": "Is there a discount for a large volume of program content?",
             "a": f"Yes - Pro is {billing.PLANS['pro']['discount']*100:.0f}% off every per-minute rate "
                  f"for ${billing.PLANS['pro']['price_usd']}/month, Premium is "
                  f"{billing.PLANS['premium']['discount']*100:.0f}% off for "
                  f"${billing.PLANS['premium']['price_usd']}/month, and Enterprise is a negotiated "
                  f"volume rate with a dedicated account manager and bank-transfer billing. The first "
                  f"{billing.FREE_MINUTES_PER_MONTH:.0f} minutes every month are free on every plan."},
            {"q": "Do you require consent before cloning an interview subject's or community leader's voice?",
             "a": "Yes, always - if a dub needs to sound like the original speaker rather than a "
                  "standard voice, we require your explicit confirmation of rights and consent before "
                  "that ever runs. It's enforced server-side, not just a checkbox in a form."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "An occasional field clip or two a month, testing quality before "
                                     "committing budget.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular campaign or program content, monthly reporting.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month, email support."},
            {"plan": "Premium", "for": "A program that needs burned-in captions or a dubbed video file "
                                        "ready for community screenings.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables and priority-quality translation included."},
            {"plan": "Enterprise", "for": "A multi-country program with a grant cycle that needs "
                                           "bank-transfer billing.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "youtubers": {
        "title": "Translate or Dub Your YouTube Videos - Swahili to English and Back",
        "meta_description": "Paste a YouTube link and get it translated from Swahili to English (or "
                             "English to Swahili) - a transcript, subtitles, or a fully dubbed track "
                             "back, optional human review before delivery, transparent per-minute pricing.",
        "kicker": "For YouTubers and content creators",
        "h1": "Translate your YouTube videos - Swahili to English, or English to Swahili",
        "intro": "Paste a YouTube link and Kauli fetches the audio itself - no downloading, re-uploading, or "
                 "juggling files. Get a translated transcript, subtitles, or a fully dubbed track back, "
                 "reviewed by a real editor before it's marked ready.",
        "why_heading": "Built for how creators actually work",
        "points": [
            {"title": "Paste a link, not a file",
             "body": "Give us a YouTube URL and we fetch the audio directly - the upload step most tools "
                      "force on you just isn't there."},
            {"title": "Auto-import for a whole channel",
             "body": "Connect a channel or playlist and new public uploads show up as one-click pending "
                      "orders in your dashboard - you decide what actually gets processed and paid for, "
                      "nothing runs automatically."},
            {"title": "Human-reviewed when you choose it",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio before it's "
                      "marked ready - accuracy your audience will actually notice."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process. First few minutes free to try the quality yourself, no card "
                      "required."},
        ],
        "cta_heading": "Try it on your last upload",
        "cta_body": "Paste the link, or connect your channel for ongoing auto-import.",
        "related": ["video-dubbing", "captioning", "voice-over"],
        "tour": {"mode": "link"},
        "faq": [
            {"q": "Do I have to download my video first?",
             "a": "No - paste the YouTube URL and we fetch the audio directly. The download-then-"
                  "reupload step most tools force on you just isn't there."},
            {"q": "Can I connect my whole channel instead of pasting links one at a time?",
             "a": "Yes - connect a channel or playlist and new public uploads show up as one-click "
                  "pending orders in your dashboard. Nothing runs (or gets billed) automatically - you "
                  "decide what actually gets processed."},
            {"q": "Will this work better than YouTube's own built-in auto-dub?",
             "a": "YouTube's auto-dub is a genuinely useful, free starting point for calm, single-"
                  "speaker content. It reliably struggles with fast dialogue, overlapping speakers, and "
                  "Sheng-inflected phrasing - exactly where a real editor checking every line against "
                  "the source audio earns its cost, which is what every Kauli order gets regardless of "
                  "plan."},
            {"q": "Do I get a downloadable dubbed video file, or just audio?",
             "a": f"Burned-in captions or a fully dubbed video file are included on Premium, or "
                  f"available as a "
                  f"${billing.ADDONS['video_deliverables']['rate_per_min']:.2f}/minute add-on on any "
                  "plan - plain audio, transcript and subtitles are included on every plan."},
            {"q": "What if my video mixes Sheng and English mid-sentence?",
             "a": "That's exactly the kind of code-switching a generic AI translation tends to "
                  "mishandle - it's why, on a Human-reviewed order, a real editor checks every line against "
                  "the source audio, not just the ones that look risky at a glance."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "Testing quality on one upload before you commit to a workflow.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "A weekly upload schedule, wanting a discount on regular volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "A channel that wants burned captions or a dubbed video file "
                                        "ready to re-upload.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables and priority-quality translation included."},
            {"plan": "Enterprise", "for": "A larger channel or network processing high volume across "
                                           "multiple creators.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "media-broadcast": {
        "title": "Subtitles and Dubbing for Media and Broadcast, On a Real Deadline",
        "meta_description": "Broadcast-ready subtitles, translated transcripts and dubbed video with burned "
                             "captions - with optional human review, priced per minute, with a real tracked delivery "
                             "deadline on every order.",
        "kicker": "For media houses and broadcasters",
        "h1": "Broadcast-ready subtitles and dubs, on a deadline you can actually see",
        "intro": "Kauli pairs fast AI transcription, translation and voice synthesis with a real editor on "
                 "every order - broadcast-ready subtitles, translated transcripts, or a fully dubbed track "
                 "with burned captions or a dubbed video file, priced by the minute, reviewed before it airs.",
        "why_heading": "Built for real broadcast deadlines",
        "points": [
            {"title": "A real delivery estimate, tracked live",
             "body": "Every Human-reviewed order gets an internal and client-facing delivery deadline the moment it's "
                      "confirmed, based on its actual length and service level - visible on your dashboard, "
                      "not a vague \"a few days.\""},
            {"title": "Broadcast-ready deliverables",
             "body": "Burned-in captions or a fully dubbed video file, not just a raw transcript - included "
                      "on Premium and Enterprise plans, or as a per-order add-on."},
            {"title": "Human-reviewed when you choose it",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it line by line against the source audio "
                      "before it's marked ready - broadcast accuracy, not best-effort."},
            {"title": "Enterprise turnaround and billing",
             "body": "A faster dedicated turnaround tier and bank-transfer billing for organizations with an "
                      "established relationship - talk to us about volume."},
        ],
        "cta_heading": "Talk to us about your next broadcast deadline",
        "cta_body": "Book a call, or start with a real clip to see the quality and turnaround for yourself.",
        "related": ["video-dubbing", "captioning", "voice-over"],
        "tour": {"mode": "upload", "filename": "evening_bulletin_segment.mp4"},
        "faq": [
            {"q": "Can you actually meet a same-day broadcast deadline?",
             "a": "Every Human-reviewed order gets a real delivery estimate the moment it's confirmed, based on its "
                  "actual length and service level - not a vague promise. Enterprise adds a faster "
                  "dedicated turnaround tier for organizations with an ongoing relationship; talk to us "
                  "about your specific deadline."},
            {"q": "Do you provide burned-in captions ready to air, or just a raw transcript?",
             "a": f"Burned-in captions or a fully dubbed video file are included on Premium and "
                  f"Enterprise, or available on any plan as a "
                  f"${billing.ADDONS['video_deliverables']['rate_per_min']:.2f}/minute add-on - a plain "
                  "transcript or subtitle file is included on every plan."},
            {"q": "What if our footage mixes English and Swahili mid-segment?",
             "a": "That's exactly the kind of code-switching a generic AI translation tends to "
                  "mishandle - it's why, on a Human-reviewed order, a real editor checks every line against "
                  "the source audio, not just the ones that look risky at a glance."},
            {"q": "Can we get bank-transfer billing for an ongoing relationship?",
             "a": "Yes - it's the standard billing method on our Enterprise plan, alongside a "
                  "negotiated volume rate and a dedicated account manager."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "A single segment or pilot piece, testing quality before a launch.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular weekly programming, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Broadcast-ready deliverables as standard - burned captions, "
                                        "dubbed video, priority turnaround.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables and priority-quality translation included."},
            {"plan": "Enterprise", "for": "An ongoing broadcast relationship needing bank-transfer "
                                           "billing and a dedicated contact.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "e-learning": {
        "title": "E-Learning Localization - Swahili, Kikuyu and English",
        "meta_description": "Translate and dub course material between Swahili, Kikuyu and English with "
                             "consistent terminology across every lesson - with optional human review, priced per minute.",
        "kicker": "For e-learning and training teams",
        "h1": "Localize a course once, keep it consistent through every cohort",
        "intro": "Terminology that drifts from lesson to lesson confuses learners - Kauli's human editors work "
                 "from the same review standard on every Human-reviewed order, so a term translated one way in lesson one "
                 "stays that way in lesson twelve, not whatever a memory-less AI tool happens to pick each time.",
        "why_heading": "Built for course content, not one-off clips",
        "points": [
            {"title": "Human-reviewed when you choose it",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio line by line "
                      "before it's marked ready - the same standard on lesson one and lesson fifty."},
            {"title": "Subtitles, transcript, or a full dub",
             "body": "Pick what your course actually needs per module - translated subtitles for a video "
                      "lecture, a clean transcript for reading material, or a fully dubbed track."},
            {"title": "Real delivery estimates",
             "body": "Every Human-reviewed order gets a real turnaround estimate the moment it's confirmed, based on its "
                      "actual length and service level - useful when you're localizing a course against a "
                      "cohort start date."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process across however many modules you have - no bundled credits, "
                      "first few minutes free to try the quality yourself."},
        ],
        "cta_heading": "Try it on one lesson first",
        "cta_body": "Upload a real module or paste a YouTube link - your first minutes are free.",
        "related": ["video-translation", "video-transcription", "video-localization"],
        "tour": {"mode": "upload", "filename": "module_3_lesson.mp4"},
        "faq": [
            {"q": "How do you keep terminology consistent across many lessons?",
             "a": "On Human-reviewed orders the same review standard applies to lesson one and lesson fifty - "
                  "a real editor checking each order against the source audio, not a memory-less AI tool "
                  "picking a different translation each time a term comes up."},
            {"q": "Can you handle a whole course at once, or just one video?",
             "a": "Each module is its own order today, priced per minute - there's no separate "
                  "bulk-course upload feature yet. If you have many modules to localize before a "
                  "cohort start date, tell us and we'll help you plan the batch."},
            {"q": "Subtitles or a full dub for training content?",
             "a": "Subtitles are usually the right first investment - faster and cheaper, and a lot of "
                  "training content is watched in shared or noisy environments where reading along "
                  "already happens naturally. A full dub earns its extra cost for dense, technical, or "
                  "safety-critical material where a learner needs to be listening, not splitting "
                  "attention between reading and watching a demonstration."},
            {"q": "Is there a discount for ongoing course localization?",
             "a": f"Yes - Pro is {billing.PLANS['pro']['discount']*100:.0f}% off every rate for "
                  f"${billing.PLANS['pro']['price_usd']}/month, Premium is "
                  f"{billing.PLANS['premium']['discount']*100:.0f}% off for "
                  f"${billing.PLANS['premium']['price_usd']}/month."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "Testing quality on one lesson before committing to a course.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "A course with several modules released on a regular schedule.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Dense or safety-critical training material that needs a full "
                                        "dub, not just subtitles.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables and priority-quality translation included."},
            {"plan": "Enterprise", "for": "A training program localizing many courses across cohorts.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    # Service-vocabulary pages, not audience ones (the four above) - the
    # real gap these fill: someone searching "voice over" or "closed
    # captions" is describing the exact same real services (dubbing,
    # subtitles) in different words, and neither term appeared anywhere on
    # the existing pages (checked live before adding these). Same real
    # facts as everywhere else, no new claims - just addressed in the
    # vocabulary that visitor actually used to search.
    "voice-over": {
        "title": "Swahili and Kikuyu Voice Over Services - AI or a Real Human Actor",
        "meta_description": "Swahili, Kikuyu and English voice over - AI-synthesized by default, or a real "
                             "human voice actor as an add-on. Consent required before any voice is cloned, "
                             "optional human review before delivery, transparent per-minute pricing.",
        "kicker": "Voice over services",
        "h1": "Swahili and Kikuyu voice over, AI-fast or a real human actor",
        "intro": "Every dub starts as an AI-synthesized voice over, checked line by line against the "
                 "source audio by a real editor before it ships. Want a real human voice instead - a "
                 "specific tone, a named voice actor - that's a real add-on, not a different product.",
        "why_heading": "Built for real voice over work, not just text-to-speech",
        "points": [
            {"title": "AI voice over, human-reviewed when you choose it",
             "body": "A dubbed track is synthesized, and on a Human-reviewed order it's also checked against the source audio before it's "
                      "marked ready - not just auto-generated and shipped."},
            {"title": "A real human voice actor, as an add-on",
             "body": "Need a real person's voice, not a synthesized one? Add a real voice actor to any "
                      "dub - priced per audio-minute, on top of the base rate."},
            {"title": "Consent required before any voice is cloned",
             "body": "If a dub needs to sound like a specific person - the original speaker, a known "
                      "presenter - we require explicit confirmation of rights and consent before that "
                      "ever runs. Never assumed."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits. Your first few minutes are free to "
                      "test the voice quality yourself."},
        ],
        "cta_heading": "Hear it on your own audio first",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-dubbing", "captioning", "video-localization"],
        "tour": {"mode": "upload", "filename": "product_explainer_voiceover.mp4"},
        "faq": [
            {"q": "Can I get a specific tone, or a named voice actor?",
             "a": f"Add a real human voice actor to any dub for "
                  f"${billing.ADDONS['human_voice_over']['rate_per_min']:.2f}/audio-minute on top of "
                  "the base rate. The AI dub still gets made and delivered right away either way; our "
                  "team casts and delivers the human version separately once it's ready."},
            {"q": "How is this different from plain text-to-speech?",
             "a": "A generic text-to-speech tool reads whatever text it's given. Kauli's voice over is "
                  "checked against the actual source audio by a real editor before it ships - the "
                  "translation and the delivery both get verified, not just synthesized and shipped."},
            {"q": "Do you require consent before cloning a voice?",
             "a": "Yes, always - if a voice over needs to sound like a specific real person rather than "
                  "a standard voice, we require explicit confirmation of rights and consent before that "
                  "ever runs. Enforced server-side, not just a checkbox."},
            {"q": "Is a human voice actor available on every plan?",
             "a": "Yes - it's a per-order add-on on any plan, not restricted to a specific tier."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "Testing AI voice quality on one clip before committing.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular voice-over work, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Needing burned captions or a dubbed video file alongside the "
                                        "voice track, plus priority-quality translation.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables included."},
            {"plan": "Enterprise", "for": "Ongoing voice-over production needing dedicated support and "
                                           "bank-transfer billing.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "captioning": {
        "title": "Swahili and Kikuyu Subtitles and Captions, Including for YouTube",
        "meta_description": "Subtitles and closed captions in Swahili, Kikuyu and English, for YouTube "
                             "or any video - burned into the video or as a separate file, "
                             "human-reviewed line by line, transparent per-minute pricing.",
        "kicker": "Subtitles, captions and closed captions",
        "h1": "Subtitles and closed captions for YouTube or any video, in Swahili and Kikuyu",
        "intro": "An AI draft gets the captions fast; on a Human-reviewed order a real editor checks every line against the "
                 "source audio before it's marked ready - burned into the video, or as a separate "
                 "subtitle file, your choice.",
        "why_heading": "Built for captions people actually rely on",
        "points": [
            {"title": "Human-reviewed captions, when you choose them",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio line by "
                      "line before it's marked ready - accuracy matters when captions are how someone "
                      "actually follows the content."},
            {"title": "Burned-in or a separate file",
             "body": "Captions burned directly into the video, or a standalone subtitle file to plug into "
                      "your own player - included on Premium and Enterprise plans, or as a per-order "
                      "add-on."},
            {"title": "Swahili, Kikuyu and English",
             "body": "Kikuyu captions go through real human transcription (no automated speech-recognition "
                      "model supports it yet), not a lower-quality automated fallback."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits. Your first few minutes are free to "
                      "test the accuracy yourself."},
        ],
        "cta_heading": "See the accuracy on your own video",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-transcription", "voice-over", "video-localization"],
        "tour": {"mode": "upload", "filename": "webinar_recording.mp4"},
        "faq": [
            {"q": "Burned-in captions, or a separate subtitle file?",
             "a": "Your choice - captions burned directly into the video, or a standalone SRT/VTT file "
                  "to plug into your own player."},
            {"q": "Do captions work for Kikuyu content?",
             "a": "Yes - Kikuyu captions go through real human transcription, since no automated "
                  "speech-recognition model supports the language yet. Translation and captioning "
                  "after that run the same review pipeline as everything else."},
            {"q": "Are burned-in captions included, or an add-on?",
             "a": f"Included on Premium and Enterprise plans; on Free or Pro it's a "
                  f"${billing.ADDONS['video_deliverables']['rate_per_min']:.2f}/minute add-on on any "
                  "order - a plain subtitle file is included on every plan either way."},
            {"q": "How accurate are the captions really?",
             "a": "An AI draft gets a first pass fast; on a Human-reviewed order a real editor checks every line against the "
                  "source audio before it's marked ready - on every Human-reviewed order, every plan, not just the "
                  "ones that look risky."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "One video, testing accuracy before committing.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular captioning needs, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Wanting burned-in captions included as standard, not a "
                                        "per-order add-on.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables included."},
            {"plan": "Enterprise", "for": "High-volume captioning needing bank-transfer billing and "
                                           "dedicated support.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    # Core commercial-intent pages - "video translation/dubbing/captioning/
    # transcription services" are real, high-intent search terms distinct
    # from the audience pages above and from voice-over/captioning's
    # narrower vocabulary-synonym framing. Deliberately NOT built as
    # separate /subtitle-translation, /video-subtitling, or /audio-
    # transcription pages too, even though a pasted SEO doc suggested them -
    # those would be near-duplicate content competing with captioning/
    # video-transcription for the same real intent, which is a real
    # keyword-cannibalization/thin-content risk, not a win. Also
    # deliberately doesn't mention Somali, despite that doc repeatedly
    # targeting "Somali dubbing" / "English to Somali" - Kauli does not
    # support Somali (see SOURCE_LANGUAGES above: Swahili, English, Kikuyu
    # only). Advertising a language this app cannot actually deliver would
    # be a real problem the moment a Somali-speaking lead showed up, not
    # just an SEO inaccuracy.
    "video-translation": {
        "title": "Swahili to English Video Translation, Human-Reviewed",
        "meta_description": "Convert a video from Swahili to English, English to Swahili, or into "
                             "Kikuyu - a real editor checks every line against the source before "
                             "delivery. Transparent per-minute pricing, based in Kenya.",
        "kicker": "Swahili, Kikuyu and English video translation",
        "h1": "Convert a Swahili video to English - or English to Swahili, or Kikuyu - checked line by line",
        "intro": "An AI draft translates fast; on a Human-reviewed order a real editor checks every line against the "
                 "source audio before it's marked ready - a translated transcript, subtitles, or a "
                 "fully dubbed track, whichever your video actually needs. Based in Kenya, built for "
                 "Swahili, Kikuyu and English specifically, not a generic hundred-language tool.",
        "why_heading": "Built for real translation work, not a generic tool",
        "points": [
            {"title": "Human-reviewed, not just machine-translated",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio "
                      "line by line before it's marked ready - accuracy that matters when a "
                      "mistranslation changes what was actually said."},
            {"title": "Transcript, subtitles, or a full dub",
             "body": "Pick the actual deliverable your video needs - a translated transcript for "
                      "reading, subtitles for the video itself, or a fully dubbed track."},
            {"title": "Swahili and Kikuyu specifically, not an afterthought",
             "body": "Kikuyu goes through real human transcription and translation (no automated "
                      "speech-recognition model supports it yet), not a lower-quality automated "
                      "fallback bolted onto a tool built for other languages."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits. Your first few minutes are "
                      "free to test the accuracy yourself."},
        ],
        "cta_heading": "See the translation quality on your own video",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-transcription", "video-dubbing", "video-localization"],
        "tour": {"mode": "upload", "filename": "interview_clip.mp4"},
        "faq": [
            {"q": "How do I convert a Swahili video to English?",
             "a": "Upload the file or paste a YouTube link, pick English as the target language and "
                  "the deliverable you need - a translated transcript, subtitles, or a fully dubbed "
                  "track - and submit. An AI draft runs first, then a real editor checks it against "
                  "the source audio line by line before it's marked ready."},
            {"q": "Do you translate text documents, or only audio/video?",
             "a": "Audio and video only - Kauli transcribes the spoken audio first, then translates "
                  "that. There's no standalone document-translation feature."},
            {"q": "Can I get just a translated transcript, without subtitles or a dub?",
             "a": "Yes - pick the actual deliverable your project needs when you submit an order: a "
                  "translated transcript, subtitles, or a fully dubbed track."},
            {"q": "How is Kikuyu translation handled?",
             "a": "Kikuyu goes through real human transcription first (no automated speech-recognition "
                  "model supports it yet), then the same AI-assisted translation and human-review "
                  "pipeline as every other language."},
            {"q": "Is the translation literal, or does it read naturally?",
             "a": "A real editor checks it reads the way an actual speaker would say it, not the way a "
                  "translation engine assembled it - a literal word-for-word translation often misses "
                  "idioms entirely, which is exactly what the human pass catches."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "One clip, testing translation accuracy before committing.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular translation needs, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Content that also needs burned captions or a dubbed video file, "
                                        "plus priority-quality translation.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables included."},
            {"plan": "Enterprise", "for": "High-volume translation needing bank-transfer billing and "
                                           "dedicated support.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "audio-to-text": {
        "title": "Audio to Text Converter - Transcribe Audio in 70+ Languages",
        "meta_description": "Convert audio and video to text in minutes in 70+ languages, from "
                             f"${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per minute. Download as Word, "
                             "text or SRT. Pay as you go, with optional human review for Swahili, Kikuyu "
                             "and English.",
        "kicker": "Audio to text",
        "h1": "Convert audio to text in minutes - in 70+ languages",
        "intro": "Upload a recording or paste a YouTube link and get a timestamped transcript back in "
                 "minutes, ready to download as Word, plain text or SRT. Pay by the minute - no subscription.",
        "why_heading": "Built for getting text out of audio, fast",
        "points": [
            {"title": "70+ languages, one upload",
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
                      "and numbers before you publish. For Swahili, Kikuyu and English you can add human review."},
            {"title": "Simple per-minute pricing",
             "body": f"Instant AI transcription is ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per "
                      "audio-minute. Your first few minutes are free."},
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
                  "available for Swahili, Kikuyu and English orders."},
            {"q": "Can I get captions or subtitles too?",
             "a": "Yes - SRT files are included, and Instant AI can translate your transcript into another "
                  "language (machine translation, not reviewed by a person). Dubbing and human review are "
                  "available for Swahili, Kikuyu and English."},
        ],
    },
    "youtube-video-to-text": {
        "title": "YouTube Video to Text - Transcribe Any YouTube Link",
        "meta_description": "Paste a YouTube link and get the transcript back as Word, text or SRT - "
                             f"from ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per minute, in 70+ "
                             "languages. Pay as you go.",
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
            {"title": "70+ spoken languages",
             "body": "Choose the language spoken in the video. Instant AI transcribes it and can translate it; "
                      "for Swahili, Kikuyu and English you can also add dubbing and human review."},
            {"title": "Subtitle files ready for YouTube",
             "body": "Download SRT or VTT files and upload them to your video in YouTube Studio, one per "
                      "language, so viewers can pick their own."},
            {"title": "If a link can't be fetched, upload the file",
             "body": "YouTube sometimes blocks automated downloads for particular videos. If that happens "
                      "to yours, upload the video file instead and the rest works exactly the same."},
            {"title": "Pay per minute, no subscription",
             "body": f"Instant AI transcription is ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} "
                      "per audio-minute, with your first few minutes free."},
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
        "meta_description": "Transcribe podcast episodes to text in minutes, with timestamps and "
                             f"speaker labels, from ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per "
                             "minute. Download as Word, text or SRT. Pay as you go.",
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
             "body": f"At ${billing.PRICE_CARD['intl']['transcribe']['ai']:.2f} per audio-minute, a "
                      f"60-minute episode costs about ${billing.PRICE_CARD['intl']['transcribe']['ai'] * 60:.2f}. "
                      "Pay as you go, no subscription."},
            {"title": "Swahili and Kikuyu shows",
             "body": "Podcasts in Swahili, Kikuyu or English can add dubbing and human review - "
                      "a niche generic tools handle poorly."},
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
                             "minutes, in 70+ languages. Word, text and SRT downloads. Human review "
                             "available for Swahili, Kikuyu and English.",
        "kicker": "Interview transcription",
        "h1": "Interview transcripts with speaker labels and timestamps",
        "intro": "For researchers, journalists and teams who need to get from a recording to quotable, "
                 "searchable text quickly - in the language the interview was actually held in.",
        "why_heading": "Built for interviews, not just meetings",
        "points": [
            {"title": "Speaker labels and timestamps",
             "body": "Each line shows who spoke and when, so you can find and verify a quote against the "
                      "recording."},
            {"title": "Interviews in 70+ languages",
             "body": "Transcribe the interview in the language it was held in, and translate it with Instant AI. "
                      "For Swahili, Kikuyu and English, add human review."},
            {"title": "Clean read or verbatim",
             "body": "On human-reviewed Swahili, Kikuyu and English orders, choose a cleaned-up read or a "
                      "verbatim record that keeps every false start."},
            {"title": "Word and text exports",
             "body": "Download a .docx to annotate or a .txt to feed your analysis tools."},
            {"title": "Human review where it counts",
             "body": "Instant AI isn't reviewed by a person. For a published quote or a research record in "
                      "Swahili, Kikuyu or English, add human review so a real editor checks it against the audio."},
        ],
        "cta_heading": "Try it on a real interview",
        "cta_body": "Your first minutes are free.",
        "related": ["audio-to-text", "video-transcription", "captioning"],
        "faq": [
            {"q": "Is my recording kept private?",
             "a": "Files are processed to produce your transcript and are only visible to your account "
                  "and our staff. See our privacy policy for how files and our transcription provider are handled."},
            {"q": "Can you transcribe interviews in Swahili or Kikuyu?",
             "a": "Yes - Swahili through our AI plus optional human review, and Kikuyu through a real human "
                  "transcriber, since no speech-recognition model supports it yet."},
        ],
    },
    "video-transcription": {
        "title": "Swahili, Kikuyu and English Video & Audio Transcription Services",
        "meta_description": "Swahili, Kikuyu or English video and audio transcription, with "
                             "timestamped segments - AI-drafted, checked word-by-word by a real "
                             "editor against the source before delivery. Transparent per-minute "
                             "pricing, based in Kenya.",
        "kicker": "Swahili, Kikuyu and English transcription services",
        "h1": "Video and audio transcription, checked word-by-word before it ships",
        "intro": "An AI draft transcribes fast, but a real editor checks it against the source "
                 "audio before it's marked ready - a clean read or a verbatim transcript, your "
                 "choice, in Swahili, Kikuyu, or English.",
        "why_heading": "Built for real transcription work",
        "points": [
            {"title": "Human-reviewed transcripts, when you choose them",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio "
                      "word by word before it's marked ready - not just auto-generated and shipped."},
            {"title": "Timestamped segments you can jump to",
             "body": "Every line of your transcript carries its start and end time from the source audio - "
                      "useful for a searchable archive, a deposition record, or cueing up an exact moment "
                      "in a long recording. (Timing is per line, not per word.)"},
            {"title": "Kikuyu goes through real human transcription",
             "body": "No automated speech-recognition model supports Kikuyu yet - orders in Kikuyu "
                      "go straight to a real human transcriber, not a lower-quality automated "
                      "guess."},
            {"title": "Clean read or verbatim, your choice",
             "body": "A clean read strips filler words and false starts for readability; verbatim "
                      "keeps every false start and filler word for a legal or research record - "
                      "pick what your actual use case needs."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits. Your first few minutes are "
                      "free to test the accuracy yourself."},
        ],
        "cta_heading": "See the accuracy on your own audio",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-translation", "captioning", "video-localization"],
        "tour": {"mode": "upload", "filename": "deposition_recording.mp4"},
        "faq": [
            {"q": "Clean read or verbatim - which do I need?",
             "a": "Clean read is fully cleaned-up, readable text - the default for subtitles and most "
                  "business use. Verbatim keeps every false start, filler word and stutter exactly as "
                  "spoken, which is what a legal or research record typically needs instead. You "
                  "choose per order."},
            {"q": "Do you provide timestamps?",
             "a": "Yes - every line of the transcript carries its start and end time from the source audio. "
                  "Timing is per line (segment), not per word."},
            {"q": "How is Kikuyu transcription handled?",
             "a": "A real human transcriber types the source by ear, since no speech-recognition model "
                  "supports Kikuyu yet - it costs a bit more per minute to cover that real labor."},
            {"q": "What format is the transcript delivered in?",
             "a": "A plain transcript, or SRT/VTT subtitle files if you need captions too - whichever "
                  "your order actually asked for."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "One file, testing accuracy before committing.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular transcription needs, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Content that also needs subtitles or a dubbed track, plus "
                                        "priority-quality translation.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables included."},
            {"plan": "Enterprise", "for": "High-volume transcription needing bank-transfer billing and "
                                           "dedicated support.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "video-dubbing": {
        "title": "Swahili to English Video Dubbing Services (and Kikuyu)",
        "meta_description": "Dub a Swahili video into English, or English into Swahili or Kikuyu - "
                             "AI-synthesized by default, a real human voice actor as an add-on, "
                             "optional human review before delivery. Transparent per-minute pricing, based "
                             "in Kenya.",
        "kicker": "Swahili, Kikuyu and English video dubbing",
        "h1": "Dub a video between Swahili, Kikuyu and English, reviewed before it ships",
        "intro": "Kauli pairs fast AI voice synthesis with a real editor on every Human-reviewed order - a fully "
                 "dubbed track, checked against the source audio line by line before it's marked "
                 "ready. Want a real human voice instead of a synthesized one? That's a real "
                 "add-on, not a different product.",
        "why_heading": "Built for real dubbing work, not a one-click gimmick",
        "points": [
            {"title": "Human-reviewed dubs, when you choose them",
             "body": "An AI-synthesized voice track is fast; on a Human-reviewed order a real editor checks the "
                      "translation and the audio against the source before it's marked ready."},
            {"title": "Burned-in captions or a dubbed video file",
             "body": "Get the dubbed audio track alone, or a full video with burned-in captions "
                      "and the new dub - included on Premium and Enterprise plans, or as a "
                      "per-order add-on."},
            {"title": "Consent required before any voice is cloned",
             "body": "If a dub needs to sound like the original speaker, we require explicit "
                      "confirmation of rights and consent before that ever runs. Never assumed."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits. Your first few minutes are "
                      "free to test the quality yourself."},
        ],
        "cta_heading": "Hear it on your own video first",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["voice-over", "captioning", "video-localization"],
        "tour": {"mode": "upload", "filename": "training_video.mp4"},
        "faq": [
            {"q": "AI voice or a real human actor?",
             "a": f"AI-synthesized by default, checked against the source audio by a real editor "
                  f"before it ships. A real human voice actor is available as a "
                  f"${billing.ADDONS['human_voice_over']['rate_per_min']:.2f}/minute add-on on any "
                  "order, delivered separately once cast and recorded."},
            {"q": "How do you handle multiple speakers in one video?",
             "a": "Each speaker gets tagged and assigned their own consistent voice, so the same "
                  "character sounds like the same person for the whole video, instead of one generic "
                  "voice reading every line - a normal part of every dub, not a premium add-on."},
            {"q": "Do you require consent before cloning a voice?",
             "a": "Yes, always - if a dub needs to sound like the original speaker, we require "
                  "explicit confirmation of rights and consent before that ever runs. Enforced "
                  "server-side, not just a checkbox."},
            {"q": "Do dubbed lines actually stay in sync with the video?",
             "a": "Yes - each line is time-fitted to its original slot, with natural, pitch-preserving "
                  "audio stretching applied automatically where needed to keep it synced."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "One clip, testing dub quality before committing.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute (dubbing itself isn't covered by the free tier)."},
            {"plan": "Pro", "for": "Regular dubbing needs, wanting a discount on standing volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Wanting a dubbed video file with burned captions included as "
                                        "standard.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables and priority-quality translation included."},
            {"plan": "Enterprise", "for": "High-volume dubbing needing bank-transfer billing and "
                                           "dedicated support.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
    "video-localization": {
        "title": "Swahili, Kikuyu and English Video Localization Services",
        "meta_description": "Full video localization between Swahili, Kikuyu and English - "
                             "transcription, translation, subtitles, captions or a full dub, all "
                             "human-reviewed, from one team. Based in Kenya.",
        "kicker": "Video localization services",
        "h1": "One team for transcription, translation, subtitles, and dubbing",
        "intro": "Localizing a video usually means several real steps - transcribe it, translate "
                 "it, then subtitle or dub it. Kauli does all of it, from one order, with the same "
                 "real editor checking every step against the source audio, not four different "
                 "vendors to coordinate.",
        "why_heading": "Every step, one real team",
        "points": [
            {"title": "Transcription, translation, subtitles, or a full dub",
             "body": "Pick what your video actually needs, or combine them - a translated "
                      "transcript, burned-in subtitles, or a fully dubbed track with captions."},
            {"title": "Human-reviewed when you choose it",
             "body": "An AI draft is fast; on a Human-reviewed order a real editor checks it against the source audio "
                      "line by line before it's marked ready, at every step, not just the final "
                      "output."},
            {"title": "Swahili, Kikuyu and English, done properly",
             "body": "Kikuyu goes through real human transcription (no automated speech-"
                      "recognition model supports it yet) - not an afterthought bolted onto a tool "
                      "built for other languages."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process, across however many steps your video actually "
                      "needs - no bundled credits, first few minutes free to try."},
        ],
        "cta_heading": "See the full process on your own video",
        "cta_body": "Upload a real clip or paste a YouTube link - your first minutes are free.",
        "related": ["video-translation", "video-dubbing", "captioning"],
        "tour": {"mode": "upload", "filename": "documentary_segment.mp4"},
        "faq": [
            {"q": "Do I really get everything in one order, or do I order each step separately?",
             "a": "One order, one service level (\"full dub\") covers transcription, translation and "
                  "voicing together - the real subtitle files (SRT/VTT) and translated transcript come "
                  "with it too, not a separate purchase."},
            {"q": "Can I get just some of it - subtitles without a dub, for example?",
             "a": "Yes - pick the actual combination your video needs when you submit an order: "
                  "transcription only, transcription plus translation, or the full dub with voicing."},
            {"q": "How is Kikuyu handled across the whole pipeline?",
             "a": "The transcription step is real human work (no automated speech-recognition model "
                  "supports Kikuyu yet) - translation and voicing after that run the same AI-assisted, "
                  "human-reviewed pipeline as every other language."},
            {"q": "Do I get a dubbed video file with captions, or just the audio track?",
             "a": f"A dubbed video file with burned-in captions is included on Premium and Enterprise, "
                  f"or available on any plan as a "
                  f"${billing.ADDONS['video_deliverables']['rate_per_min']:.2f}/minute add-on - the "
                  "dubbed audio track alone is included on every plan."},
        ],
        "fit_guide": [
            {"plan": "Free", "for": "One clip, testing transcription quality before committing to the "
                                     "full pipeline.",
             "detail": f"{billing.FREE_MINUTES_PER_MONTH:.0f} free transcription minutes every month, "
                       "then pay per minute."},
            {"plan": "Pro", "for": "Regular full-localization needs, wanting a discount on standing "
                                    "volume.",
             "detail": f"{billing.PLANS['pro']['discount']*100:.0f}% off every rate, "
                       f"${billing.PLANS['pro']['price_usd']}/month."},
            {"plan": "Premium", "for": "Wanting the complete package - dubbed video, burned captions, "
                                        "priority-quality translation - included as standard.",
             "detail": f"{billing.PLANS['premium']['discount']*100:.0f}% off every rate, video "
                       "deliverables included."},
            {"plan": "Enterprise", "for": "A large-scale localization program needing bank-transfer "
                                           "billing and dedicated support.",
             "detail": "Negotiated volume rate, dedicated account manager, bank transfer billing."},
        ],
    },
}


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
    return templates.TemplateResponse(request, "solution_page.html",
        {**_marketing_context(home="/", market=_display_market(request)), "page": page, "related_pages": related_pages})


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
    return f"{request.url.scheme}://{request.url.netloc}"


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
    return templates.TemplateResponse(request, "landing_localized.html", {
        "user": None, "s": s, "p": p, "locale": code, "hreflang": hreflang, "text_dir": direction,
        "locale_options": _LOCALE_OPTIONS, "base_url": base,
        "hreflang_alts": _hreflang_alternates(base), "hreflang_default": f"{base}/",
        "faq": [(s["q1"], s["a1"]), (s["q2"], s["a2"]), (s["q3"], s["a3"]), (s["q4"], s["a4"])],
        "lang_pills": lang_pills, "lang_links": lang_links,
    })


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
             "body": f"Instant AI transcription is ${price:.2f} per audio-minute - no subscription. Your first "
                      "minutes are free."},
            {"title": "Honest about the limits",
             "body": f"Instant AI is machine transcription and is not reviewed by a person; accuracy depends on "
                      f"audio quality, accent and background noise, so check names and numbers before you "
                      f"publish. Instant AI translation is machine translation, also not reviewed by a person. "
                      "Dubbing and human review are currently for Swahili, Kikuyu and English."},
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
                  "by a person. Dubbing and human review are currently for Swahili, Kikuyu and English."},
            {"q": f"How accurate is {label} transcription?",
             "a": "It depends on audio quality, accents and background noise. Instant AI is machine "
                  "transcription and is not reviewed by a person, so we don't publish a headline accuracy "
                  "figure. Skim the transcript for names, numbers and specialist terms before you publish."},
            {"q": f"How much does {label} transcription cost?",
             "a": f"${price:.2f} per audio-minute for Instant AI transcription in the US and internationally "
                  "(lower in Kenya and East Africa), with no subscription."},
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
    return templates.TemplateResponse(request, "solution_page.html", {
        **_marketing_context(home="/", market=_display_market(request)),
        "page": _language_page_dict(code), "related_pages": related_pages,
    })


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
# Kauli is one person (Godfrey, Forge Media Services), not a company with a
# separate Customer Success team, so these are both genuinely from him, not
# a fictional CSM persona. Every message is queued (see db.py's
# onboarding_messages) AND, now that Brevo is configured, auto-sent - see
# _queue_and_send below. A staff view of the queue (and anything that
# needed a manual fallback) lives on /staff/leads.
FOUNDER_NAME = "Godfrey Njoroge"


def _queue_and_send(user, kind: str, subject: str, body: str, base_url: str = "",
                     cta_text: str | None = None, cta_url: str | None = None,
                     html_inner: str | None = None) -> None:
    """Writes the message to onboarding_messages (the honest, always-there
    record) and, if a real mailer is configured, immediately attempts to
    actually send it - same "create the real record first, then try to
    deliver it, fall back gracefully" pattern as _activate_payment's
    receipt email. body is plain text (Godfrey's real voice, unchanged) -
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
# so it keeps its own copy) - internal ops alerts go to Godfrey directly,
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
        f"I'm {FOUNDER_NAME.split()[0]} - I actually run Kauli day to day at Forge Media "
        "Services, so this is genuinely from me, not an automated \"team\".\n\n"
        f"Your account has {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes loaded already - "
        "here's the fastest way to see a real result:\n\n"
        "1. Open your dashboard.\n"
        "2. Upload a short video or audio clip, or paste a YouTube link.\n"
        "3. Pick your language pair (e.g. Swahili -> English).\n"
        "4. We'll notify you the moment your result is ready to download. Instant AI results arrive in "
        "minutes and are AI-generated; Human-reviewed results are checked by a real editor first.\n\n"
        "If anything's unclear, or you'd rather just talk it through, just reply to this email.\n\n"
        f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
    )
    cta_url = f"{base_url.rstrip('/')}/client" if base_url else "/client"
    html_inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">I\'m {FOUNDER_NAME.split()[0]} - I actually run Kauli day to day '
        f'at Forge Media Services, so this is genuinely from me, not an automated "team".</p>'
        f'<p style="margin:0 0 6px;">Your account has <strong>{billing.FREE_MINUTES_PER_MONTH:.0f} free '
        f'minutes</strong> loaded already - here\'s the fastest way to see a real result:</p>'
        f'<ol style="margin:0 0 14px; padding-left:20px;">'
        f'<li style="margin-bottom:4px;">Open your dashboard.</li>'
        f'<li style="margin-bottom:4px;">Upload a short video or audio clip, or paste a YouTube link.</li>'
        f'<li style="margin-bottom:4px;">Pick your language pair (e.g. Swahili &rarr; English).</li>'
        f'<li>We\'ll notify you the moment your result is ready to download. Instant AI results arrive in minutes and are AI-generated; Human-reviewed results are checked by a real editor first.</li>'
        f'</ol>'
        f'<p style="margin:0 0 14px;">If anything\'s unclear, or you\'d rather just talk it through, '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email.</p>'
        f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
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
        "I'll be keeping an eye on this one personally. If the turnaround or the quality isn't "
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
    message_id = db.queue_onboarding_message(
        user["id"], "first_payment", subject,
        body + f"\n\n{FOUNDER_NAME}\nForge Media Services")
    if mailer.email_configured():
        html_body = (
            f'Thanks for the vote of confidence - that\'s your first paid order with Kauli, and we '
            f'don\'t take that lightly at this stage.\n\n'
            f'I\'ll be keeping an eye on this one personally. If the turnaround or the quality isn\'t '
            f'what you expected, tell me directly.'
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
        inner += f'<p style="margin:20px 0 0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
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
         "hreflang_alts": _hreflang_alternates(_site_base(request)),
         "hreflang_default": f"{_site_base(request)}/"})


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", _marketing_context(home="/"))


@app.get("/developers", response_class=HTMLResponse)
def developers_page(request: Request):
    ctx = _marketing_context(home="/")
    ctx["base"] = str(request.base_url).rstrip("/")
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
    base = f"{request.url.scheme}://{request.url.netloc}"
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
    ]
    static_urls += [(f"{base}/solutions/{slug}", "0.8") for slug in SOLUTION_PAGES]
    static_urls += [(f"{base}/languages", "0.8")]
    static_urls += [(f"{base}/transcribe/{slug}", "0.6") for slug in AI_LANGUAGE_SLUGS]
    # The homepage and the translated landing pages are alternates of each
    # other - every one lists the whole set, itself included.
    alt_urls = {f"{base}/"} | {f"{base}/{code}" for code in site_locales.LOCALES}
    static_urls += [(f"{base}/{code}", "0.7") for code in site_locales.LOCALES]
    alt_links = "".join(
        f'<xhtml:link rel="alternate" hreflang="{hl}" href="{href}"/>' for hl, href in _hreflang_alternates(base))
    alt_links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{base}/"/>'
    entries = [f"  <url><loc>{u}</loc>{alt_links if u in alt_urls else ''}<priority>{p}</priority></url>"
               for u, p in static_urls]
    for p in db.list_blog_posts(published_only=True):
        loc = f"{base}/blog/{p['slug']}"
        lastmod = datetime.fromtimestamp(p["updated_at"]).strftime("%Y-%m-%d") if p["updated_at"] else None
        entries.append(f"  <url><loc>{loc}</loc>"
                        f"{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}<priority>0.5</priority></url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
           + "\n".join(entries) + "\n</urlset>")
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt(request: Request):
    """Real disallow list matching this app's actual private routes (not a
    generic template) - /client, /staff, /settings, /receipts, /avatar,
    /status all require login, so a crawler has no legitimate reason to be
    pointed at them. Explicitly allows the AI crawlers actually worth
    naming today (OpenAI, Anthropic, Perplexity, Google's AI-training
    signal) - real user-agent strings, not invented ones."""
    base = f"{request.url.scheme}://{request.url.netloc}"
    body = f"""User-agent: *
Allow: /
Disallow: /client
Disallow: /staff
Disallow: /settings
Disallow: /receipts/
Disallow: /avatar/
Disallow: /status
Disallow: /webhooks/

User-agent: GPTBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /

Sitemap: {base}/sitemap.xml
"""
    return Response(content=body, media_type="text/plain")


@app.get("/llms.txt")
def llms_txt(request: Request):
    """Emerging, not-yet-universal convention (see chat) - a plain-text map
    of the site for an LLM to read directly, same spirit as robots.txt/
    sitemap.xml but aimed at an AI reader rather than a crawler or a
    search engine. Every line here is a real, existing page and a real,
    already-published fact (pricing, languages, contact) - nothing
    invented for this file specifically."""
    base = f"{request.url.scheme}://{request.url.netloc}"
    rates = "\n".join(
        f"- {sl['name']}: Instant AI (not human-reviewed) ${billing.PRICE_CARD['intl'][key]['ai']:.2f}, "
        f"human-reviewed ${billing.PRICE_CARD['intl'][key]['human']:.2f} per audio-minute "
        f"(US/international; Kenya & East Africa: ${billing.PRICE_CARD['ea'][key]['ai']:.2f} / "
        f"${billing.PRICE_CARD['ea'][key]['human']:.2f})"
        for key, sl in billing.SERVICE_LEVELS.items())
    posts = db.list_blog_posts(published_only=True)
    post_lines = "\n".join(f"- [{p['title']}]({base}/blog/{p['slug']})" for p in posts[:20])
    # Derived from SOLUTION_PAGES itself, not hand-typed - a previous
    # version of this list only had 4 of the 10 real solution pages on it,
    # and would only have drifted further out of sync as more got added.
    solution_lines = "\n".join(f"- [{p['title']}]({base}/solutions/{slug})" for slug, p in SOLUTION_PAGES.items())
    body = f"""# Kauli

> Audio and video transcription in 70+ languages (Instant AI), plus
> translation, dubbing and subtitles between Swahili, Kikuyu and English,
> operated by Forge Media Services. Two ways to order: Instant AI (ready in
> minutes, AI-generated, not reviewed by a person) and Human-reviewed (a real
> editor checks every line against the source audio before delivery).

## Pricing
Per audio-minute, no bundled credits:
{rates}
First {billing.FREE_MINUTES_PER_MONTH:.0f} minutes free to try.

## Languages
Instant AI transcription: {len(AI_ONLY_LANGUAGES) + len(SOURCE_LANGUAGES)} languages (transcription only).
Full list of languages: {base}/languages (each language has its own page at {base}/transcribe/<language>).
Translated landing pages: {', '.join(f"{base}/{c} ({m[0]})" for c, m in site_locales.LOCALES.items())}.
Instant AI translation: between all of them except Kikuyu (machine translation, not reviewed by a person).
Dubbing and human review: {', '.join(SOURCE_LANGUAGES.values())}.

## Key pages
- [Homepage]({base}/) - overview, pricing, FAQ
{solution_lines}
- [Blog]({base}/blog)
- [Formatting standards]({base}/formatting-standards)
- [Terms]({base}/terms) / [Privacy]({base}/privacy)

## Blog posts
{post_lines}

## Contact
{CONTACT_EMAIL}
"""
    return Response(content=body, media_type="text/plain")


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
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and billing.premium_voice_margin_ok()


def _audio_cleanup_available() -> bool:
    """Offered only with a key and while the add-on price still clears the margin floor at today's cost."""
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and billing.audio_cleanup_margin_ok()


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
            _marketing_context(lead_error=f"Too many requests - try again in {retry_after // 60 + 1} minute(s)."),
            status_code=429)
    # Form(""), not Form(...) - a blank-valued field is dropped entirely by
    # Starlette's form parser (indistinguishable from a missing one), so
    # Form(...) here would 422 with a raw JSON error instead of the
    # friendly message below whenever someone submits with an empty Name.
    name = name.strip()
    email = email.strip().lower()
    if not name or "@" not in email:
        return templates.TemplateResponse(request, "marketing.html",
            _marketing_context(lead_error="Please enter your name and a valid email address."),
            status_code=400)
    if not volume_estimate.strip() or not org_type.strip():
        return templates.TemplateResponse(request, "marketing.html",
            _marketing_context(lead_error="Please select your estimated volume and organization type."),
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
            # Price card for this account: US/international vs East Africa,
            # from Cloudflare's visitor-country header (absent when hit
            # directly -> the international card). Fixed from here on; only
            # staff can change it (see /staff/clients/{id}/market).
            db.set_billing_market(user["id"], billing.market_for_country(request.headers.get("cf-ipcountry")))
    request.session["user_id"] = user["id"]
    # Clicking a deep link from an email (an order, a receipt, billing)
    # while logged out used to always land back on the generic dashboard
    # after signing in, losing where they actually meant to go - this
    # sends them back to the real destination instead, same pattern every
    # real app uses. _safe_next already rejected anything that isn't a
    # real in-app path, so "/" (the normal role-based landing page) is
    # the only fallback, never an open redirect.
    return RedirectResponse(_safe_next(next), status_code=303)


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
        display_notice = "You were signed out after a while inactive - sign back in to continue."
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
                            next: str = Form("")):
    if not access_token or not refresh_token:
        return templates.TemplateResponse(request, "oauth_callback.html", {
            "error": "That didn't come back from Google correctly - please try again.",
        }, status_code=400)
    session, error = supabase_auth.complete_oauth_session(access_token, refresh_token)
    if error or not session:
        return templates.TemplateResponse(request, "oauth_callback.html", {
            "error": error or "That sign-in didn't work - please try again.",
        }, status_code=400)
    return _complete_auth_session(request, session, next, login_kind="google_oauth")


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
    if not _plan_chosen(user["client_scope_id"]):
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
@app.get("/notifications/recent")
def notifications_recent(request: Request):
    """JSON, polled by the bell in base.html - a real, persistent notification
    the user hasn't dismissed yet, not a client-side guess. See db.notify_all_staff
    for what actually creates these."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = db.list_recent_notifications(user["id"], limit=10)
    return JSONResponse({
        "unread_count": db.count_unread_notifications(user["id"]),
        "items": [
            {"id": r["id"], "title": r["title"], "link": r["link"], "kind": r["kind"],
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
    if not _plan_chosen(order["client_id"]):
        return False, "Choose a plan to send results to Notion."
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
        None if _plan_chosen(order["client_id"]) else "Choose a plan in Kauli to unlock downloads.", actions)


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
    troubleshooting without having to ask Godfrey every time. Written for
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
    Godfrey has personally shared the artifact link with."""
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
def client_files(request: Request):
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
    plan_ok = _plan_chosen(user["client_scope_id"])
    for o in db.list_orders_for_client(user["client_scope_id"]):
        deliverables = []
        if o["status"] in ("ready_for_delivery", "delivered") and plan_ok:
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
        rows.append({"order": o, "deliverables": deliverables, "source_size": source_size})
    return templates.TemplateResponse(request, "client_files.html", {"user": user, "rows": rows})


@app.get("/client/messages", response_class=HTMLResponse)
def client_messages(request: Request):
    """A real cross-order inbox - every order with a conversation, newest
    activity first, so a client with several orders doesn't have to open
    each one to check for a reply. Not a second messaging system:
    clicking through goes to the real order page, where the full thread
    and reply form already live (client_send_message) - this is a
    read-only summary over that same data."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    unread = db.unread_order_ids(user["client_scope_id"], include_internal=False)
    conversations = db.list_conversations_for_client(user["client_scope_id"])
    return templates.TemplateResponse(request, "client_messages.html", {
        "user": user, "conversations": conversations, "unread": unread,
    })


@app.get("/client/home", response_class=HTMLResponse)
def client_home(request: Request):
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
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return templates.TemplateResponse(request, "client_home.html", {
        "user": user, "greeting": greeting,
        "stats": stats, "recent_orders": orders[:6],
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
    })


@app.get("/client", response_class=HTMLResponse)
def client_dashboard(request: Request, reorder_youtube_url: str | None = None, reorder_source_lang: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    orders = db.list_orders_for_client(user["client_scope_id"])
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
        "user": user, "orders": orders, "unread": unread, "anthropic_available": anthropic_available,
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "review_tiers": billing.REVIEW_TIERS,
        "price_card": billing.price_card_for(db.get_billing_market(user["client_scope_id"])),
        "default_review_tier": ("human" if db.get_billing_market(user["client_scope_id"]) == "legacy" else "ai"),
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "source_languages": SOURCE_LANGUAGES, "ai_only_languages": AI_ONLY_LANGUAGES, "lara_available": _lara_configured(),
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "form_values": reorder_form_values,
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["client_scope_id"]),
        "youtube_pending_imports": db.list_pending_imports(user["client_scope_id"]),
        "show_tour": not user["onboarding_tour_seen_at"],
        "premium_voice_available": _elevenlabs_configured(),
        "audio_cleanup_available": _audio_cleanup_available(),
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
        "orders": db.list_orders_for_client(user["client_scope_id"]),
        "unread": db.unread_order_ids(user["client_scope_id"], include_internal=False),
        "anthropic_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "review_tiers": billing.REVIEW_TIERS,
        "price_card": billing.price_card_for(db.get_billing_market(user["client_scope_id"])),
        "default_review_tier": ("human" if db.get_billing_market(user["client_scope_id"]) == "legacy" else "ai"),
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
        "audio_cleanup_available": _audio_cleanup_available(),
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
        "<h1>Thanks for the feedback</h1><p>The founder reads every one of these personally.</p></div>"
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


def _resolve_uploaded_audio(upload_key: str, order_upload_dir: Path):
    """Step 2: the file the client already PUT to R2 gets pulled down to
    local disk and run through the real validation pipeline - see
    _LocalFileAdapter. Always deletes the R2 staging object afterward
    (success or failure) - it's transient staging, not permanent
    storage; the real, permanent copy is the local file, same as any
    other order's audio_path. Raises upload_security.UploadRejected on
    any hard failure, exactly like the direct-upload path does."""
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
    target_lang: str = Form("en"),
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
    if target_lang not in LANGUAGE_NAMES or target_lang == "ki":
        return _client_dashboard_error(request, user, "Unknown target language.", form_values=form_values)
    # Market pricing: the price card is the TEAM OWNER's (fixed at signup,
    # staff-changeable) - never something the submitter picks per order.
    # review_tier is the one thing they do pick: 'ai' (instant, no human
    # review) or 'human' (the original reviewed flow). A missing/unknown
    # value means the old behaviour (human), so any older client of this
    # endpoint keeps working exactly as before.
    billing_market = db.get_billing_market(user["client_scope_id"])
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
    if source_lang in AI_ONLY_LANGUAGES:
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
        # never human-reviewed), and never Kikuyu without a human in the loop.
        if not (target_lang in AI_ONLY_LANGUAGES and service_level == "translate"
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
    if wants_premium_voice:
        if service_level != "dub" or target_lang not in ("en", "sw"):
            return _client_dashboard_error(
                request, user, "Premium voice is available on a full dub into English or Swahili.", form_values=form_values)
        if not _elevenlabs_configured():
            return _client_dashboard_error(request, user, "Premium voices aren't available right now.", form_values=form_values)
        if premium_voice_id not in {v["id"] for v in _premium_voices()}:
            return _client_dashboard_error(request, user, "Choose one of the listed premium voices.", form_values=form_values)
        addons.append("premium_voice")
    wants_audio_cleanup = bool(addon_audio_cleanup)
    if wants_audio_cleanup:
        if not _audio_cleanup_available():
            return _client_dashboard_error(request, user, "Audio clean-up isn't available right now.", form_values=form_values)
        addons.append("audio_cleanup")
    if source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # Not a checkbox the client toggles - this is a real cost every
        # service level incurs for this language (every SERVICE_LEVELS tier
        # needs the ASR stage), so it's applied automatically the same way
        # the service level's own rate is, not offered as an opt-in upsell.
        addons.append("manual_transcription")
    if instr_verbatim_level not in db.VERBATIM_LEVELS:
        instr_verbatim_level = "clean_read"
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
    if youtube_url:
        try:
            audio_path, original_filename, youtube_video_id = _download_youtube(youtube_url, order_upload_dir)
        except Exception as exc:
            return _client_dashboard_error(request, user, f"Couldn't fetch that YouTube link: {exc}",
                                            form_values=form_values)
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
            audit, original_filename = _resolve_uploaded_audio(upload_key.strip(), order_upload_dir)
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

    if wants_audio_cleanup and not _audio_cleanup_capacity_ok(minutes):
        return _client_dashboard_error(
            request, user, "Audio clean-up is at capacity right now. Continue without it, or try again later.",
            form_values=form_values)
    if wants_premium_voice and not _premium_capacity_ok(minutes):
        return _client_dashboard_error(
            request, user, "Premium voices are at capacity right now. Choose the standard voice, or try again later.",
            form_values=form_values)

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
        asr = ("transkriptor" if os.environ.get("TRANSKRIPTOR_API_KEY") or os.environ.get("TRANSKRIPTOR_API_KEY_BACKUP")
               else "faster-whisper")
    if not level["mt"]:
        mt = "stub"
    else:
        # See _pick_mt: Lara first, with the Kikuyu/Claude rule and the old
        # local/Azure guards (a Swahili-to-English-only local model must never
        # be fed another direction - confirmed live to produce nonsense)
        # kept as fallbacks for when Lara isn't connected.
        mt, mt_error = _pick_mt(source_lang, target_lang, plan_mt, review_tier)
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
    elif target_lang == "sw":
        if not os.environ.get("AZURE_SPEECH_KEY"):
            return _client_dashboard_error(request, user,
                "Swahili dubbing needs our voice provider connected first, which isn't done yet - "
                "contact us and we'll handle this one manually in the meantime.", form_values=form_values)
        tts = "azure"
    else:
        tts = "piper"

    # Free minutes only ever apply to a transcription-only order - see
    # billing.FREE_MINUTES_SERVICE_LEVEL. A translate/dub order pays the
    # full rate from its first minute; order_cost_usd simply isn't offered
    # any free allowance to spend for those.
    #
    # Reserved atomically here (not just read via free_minutes_remaining)
    # so two near-simultaneous submissions can't both read the same "X
    # remaining" snapshot and each get the full free allowance applied -
    # see db.reserve_free_minutes's docstring for the actual race this
    # closes. Whatever's granted here is real and already spent the moment
    # this call returns, whether or not the rest of this order ends up
    # needing payment.
    if service_level == billing.FREE_MINUTES_SERVICE_LEVEL and review_tier == "ai":
        # (The free trial only ever buys the INSTANT AI tier now - it's
        # "see the AI transcription quality for yourself", and it must not
        # cost us a human reviewer's time.)
        # No card check before ordering any more: free minutes can be used
        # straight away, and the card confirmation (billing.
        # TRIAL_VERIFICATION_FEE_USD, credited back) happens when the client
        # chooses the Free plan - which is what unlocks downloads.
        free_cap = billing.FREE_MINUTES_PER_MONTH + (subscription["bonus_minutes"] or 0.0 if subscription else 0.0)
        free_minutes_for_this_order = db.reserve_free_minutes(user["client_scope_id"], minutes, free_cap)
    else:
        free_minutes_for_this_order = 0.0
    # Rush = Godfrey manually jumping the review queue - meaningless for an
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
    # order_cost_usd silently drops any addon the plan already includes -
    # reflect that back so we never store/charge for one that didn't apply.
    applied_addons = [line["key"] for line in cost["addons"]]
    # Free-tier only, never a wallet/real-money $0 order (those already
    # paid for their credits in a top-up) - this is what gates downloads
    # below and in order_detail.html. A rush order is never a free preview -
    # the surcharge alone (see billing.order_cost_usd) already makes
    # total_usd > 0, but spelled out here too for clarity.
    is_free_preview = (cost["free_minutes_applied"] > 0 and cost["credits_applied"] <= 0
                        and cost["total_usd"] <= 0 and not is_rush)

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
                f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
            )
            inner = (
                f'<p style="margin:0 0 14px;">Hi {name},</p>'
                f'<p style="margin:0 0 14px;">You\'re down to about {remaining:.0f} prepaid credits - just '
                f'a heads up so an upcoming order doesn\'t get held up waiting on a top-up.</p>'
                f'<p style="margin:0 0 14px;">Questions? '
                f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">Reply to this email</a>.</p>'
                f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
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
    steps = []
    for i, (key, label) in enumerate(step_defs):
        if i < current_idx:
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
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "subs_ready": (outdir / f"subs_{order['target_lang']}.srt").exists(),
        "receipt": db.get_receipt_for_order(order_id),
        "error": error, "notice": notice,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "progress_steps": _order_progress_steps(order["status"], order["review_tier"]),
        "youtube_error": youtube_error, "youtube_connected": youtube_connected, "youtube_pushed": youtube_pushed,
        "youtube_connection": db.get_youtube_connection(user["id"]),
        "upsell": _order_upsell_prices(order),
        "paystack_configured": billing.paystack_configured(),
        "transcript_teaser": (_transcript_teaser(job, order["id"])
                              if job and order["status"] in db.TERMINAL_OK_STATUSES
                              and not _plan_chosen(user["client_scope_id"]) else None),
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
def client_order_ai_summary(request: Request, order_id: str):
    """Lazy-generated, cached forever after - see the ai_summary_text
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
    if not _plan_chosen(user["client_scope_id"]):
        return JSONResponse({"ok": False, "error": "Choose a plan to use this - the Free plan is enough."},
                             status_code=403)
    if order["ai_summary_text"]:
        return JSONResponse({"ok": True, "summary": order["ai_summary_text"]})
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
    result = order_ai_assist.generate_order_summary(text)
    if result["ok"]:
        db.set_order_ai_summary(order_id, result["summary"])
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
    if not _plan_chosen(user["client_scope_id"]):
        return JSONResponse({"ok": False, "error": "Choose a plan to use this - the Free plan is enough."},
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
            audit, new_original_filename = _resolve_uploaded_audio(upload_key.strip(), order_upload_dir)
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
                  error: str | None = None, choose_plan: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse(f"/login?next={quote(request.url.path)}")
    subscription = db.get_subscription(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    is_test_account = plan == "enterprise" and user["email"].strip().lower() in billing.test_client_emails()
    bonus_minutes = (subscription["bonus_minutes"] or 0.0) if subscription else 0.0
    payments = db.list_payments_for_user(user["client_scope_id"])
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
        "plan_is_chosen": _plan_chosen(user["client_scope_id"]), "choose_plan": choose_plan,
        "subscription": subscription, "is_test_account": is_test_account,
        "payments": payments, "receipts_by_payment": receipts_by_payment,
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
    return RedirectResponse(
        "/client/billing?notice=Auto-renewal+cancelled.+You+keep+your+plan+until+the+period+you+paid+for+ends.",
        status_code=303)


@app.post("/client/billing/choose-free")
def billing_choose_free(request: Request):
    """Choosing the Free plan. Needs the card confirmation (the credited $1)
    once per account; if that is already done it just records the choice."""
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if user["client_scope_id"] != user["id"]:
        return RedirectResponse("/client/billing?error=Only+the+account+owner+can+choose+the+plan.", status_code=303)
    if user["plan_chosen_at"]:
        return RedirectResponse("/client/billing", status_code=303)
    if user["trial_verified_at"]:
        db.set_plan_chosen(user["id"])
        return RedirectResponse("/client/billing?notice=You%27re+on+the+Free+plan.+Downloads+are+unlocked.", status_code=303)
    return RedirectResponse("/client/verify-trial", status_code=303)


@app.get("/client/verify-trial", response_class=HTMLResponse)
def client_verify_trial(request: Request, error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if user["trial_verified_at"]:
        return RedirectResponse("/client", status_code=303)
    return templates.TemplateResponse(request, "verify_trial.html", {
        "user": user, "error": error, "fee_usd": billing.TRIAL_VERIFICATION_FEE_USD,
        "paystack_configured": billing.paystack_configured(),
    })


@app.post("/client/verify-trial/pay")
def client_verify_trial_pay(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if user["trial_verified_at"]:
        return RedirectResponse("/client", status_code=303)
    # Paystack only - M-Pesa (MPESA_ENV) is still sandbox on the real
    # server, so a real client's real phone number would just fail
    # against it. See billing.TRIAL_VERIFICATION_FEE_USD's own comment.
    if not billing.paystack_configured():
        return RedirectResponse("/client/verify-trial?error=Payment+isn%27t+configured+yet+-+contact+us.",
                                 status_code=303)
    return _checkout(request, user, "paystack", "trial_verification", billing.TRIAL_VERIFICATION_FEE_USD,
                      None, "", "/client/billing", "/client/verify-trial", payment_kind="trial_verification")


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
        # Instant AI -> Human-reviewed: it now waits for a real editor like
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
        # The $1 becomes real wallet credit, not a fee - see
        # billing.TRIAL_VERIFICATION_FEE_USD's own comment. Same
        # add_wallet_credits call a real top-up uses, so it shows up and
        # spends exactly the same way on their next real order.
        db.add_wallet_credits(payment["user_id"], billing.usd_to_credits(payment["amount_usd"]))
        db.set_trial_verified(payment["user_id"])
        description = "Free trial verification - credited to your account"
    else:
        db.set_subscription_plan(payment["user_id"], payment["plan"], billing.PLAN_PERIOD_DAYS)
        description = f"{billing.PLANS[payment['plan']]['name']} plan subscription"
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

    if provider == "paystack":
        if not billing.paystack_configured():
            return RedirectResponse(f"{back_url}?error=Paystack+isn%27t+configured+yet.", status_code=303)
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, None, "USD", "paystack", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind, "ga_client_id": ga_client_id}))
        callback_url = str(request.base_url).rstrip("/") + f"/billing/callback/paystack?payment_id={payment_id}"
        # A plan purchase (no order) on a plan that has a Paystack plan code
        # configured becomes a RECURRING subscription; anything else (orders,
        # top-ups, plans without a code) stays a one-off charge.
        plan_code = billing.paystack_plan_code(plan) if (order_id is None and payment_kind == "order") else None
        charge_currency = billing.paystack_currency_for_market(db.get_billing_market(user["client_scope_id"]))
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
                      phone: str = Form("")):
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
    return _checkout(request, user, provider, plan, billing.PLANS[plan]["price_usd"],
                      None, phone, "/client/billing", "/client/billing")


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
        # Free-minute results download once a plan is chosen: nothing to unlock,
        # and no human-review upsell priced against a free order.
        return prices
    else:
        market = order["billing_market"] or "legacy"
        diff = (billing.rate_for(order["service_level"], market, "human") - cost["rate_per_min"])
        upgrade = round(diff * (order["duration_minutes"] or 0), 2)
        prices["upgrade"] = upgrade if upgrade > 0 else None
    return prices


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
        if user:
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
    plan = next((p for p in billing.PLANS if p != "free" and billing.paystack_plan_code(p) == plan_code), None)
    user = db.get_user_by_email(((data.get("customer") or {}).get("email") or "").strip().lower())
    provider_ref = str(data.get("id") or data.get("reference") or "")
    if not (plan and user and provider_ref):
        return
    payment_id = billing.new_reference()
    amount_usd = billing.PLANS[plan]["price_usd"]
    db.create_payment(payment_id, user["id"], plan, amount_usd, None, "USD", "paystack",
                       meta=json.dumps({"kind": "subscription_renewal"}))
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
    return templates.TemplateResponse(request, "staff_leads.html", {
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
        f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
    )
    nudge_cta_url = f"{base_url.rstrip('/')}/client"
    html_inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">Noticed you signed up for Kauli but haven\'t uploaded anything '
        f'yet - your {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are still sitting there unused.</p>'
        f'<p style="margin:0 0 14px;">No pressure at all, just wanted to check if anything\'s unclear '
        f'or in the way. Happy to walk you through it - '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email.</p>'
        f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
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
        "user": user, "clients": rows, "q": q or "", "plans": billing.PLANS,
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
        "orders": db.list_orders_for_client(client_id) if tab == "orders" else [],
        "team_members": db.list_team_members(client_id) if tab == "team" else [],
        "payments": db.list_payments_for_user(client_id) if tab == "billing" else [],
        "api_key_row": db.get_client_api_key(client_id) if tab == "settings" else None,
        "webhook_row": db.get_client_webhook(client_id) if tab == "settings" else None,
        "trial_payment": db.get_trial_verification_payment(client_id) if tab == "settings" else None,
        "markets": billing.MARKETS, "client_market": db.get_billing_market(client_id),
    })


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
    if not _plan_chosen(order["client_id"]):
        return "Choose a plan to download your files."
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
    if not _plan_chosen(order["client_id"]):
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
    if not _plan_chosen(order["client_id"]):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Choose a plan in Kauli to unlock downloads."}]})
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
        if not _plan_chosen(user["client_scope_id"]):
            # Nothing downloads until a plan is chosen - the Free plan (card
            # confirmation, credited back) is enough, and covers results
            # made with the free monthly minutes.
            return RedirectResponse("/client/billing?choose_plan=1", status_code=303)
        plan = billing.effective_plan(user, db.get_subscription(user["client_scope_id"]))
        has_video = billing.PLANS[plan]["video_deliverables"] or db.order_has_addon(order, "video_deliverables")
        if kind in ("burned", "dubbed") and not has_video:
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
    canonical_url = f"{request.url.scheme}://{request.url.netloc}/blog/{post['slug']}"
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
    canonical_url = f"{request.url.scheme}://{request.url.netloc}/blog/{post['slug']}"
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
        "user": user, "now_ts": now, "greeting": greeting,
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
    return templates.TemplateResponse(request, "staff_dashboard.html", {
        "user": user, "rows": rows, "now_ts": now,
        "stuck_threshold_seconds": tat.STUCK_STAGE_THRESHOLD_SECONDS,
        "service_level_labels": service_level_labels,
        "total_jobs": len(rows), "needs_attention_n": needs_attention_n,
        "overdue_n": overdue_n, "due_today_n": due_today_n, "in_progress_n": in_progress_n,
        "awaiting_review_n": awaiting_review_n, "exceptions_n": exceptions_n,
        "ready_n": ready_n, "delivered_n": delivered_n, "delivered_today_n": delivered_today_n,
    })


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
    return templates.TemplateResponse(request, "staff_ops.html", {
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
                    f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services")
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
                f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
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
        "user": user, "order": order, "job": job,
        "has_translation": order_level["mt"], "has_dub": order_level["tts"],
        "source_lang_name": LANGUAGE_NAMES.get(order["source_lang"], order["source_lang"]),
        "target_lang_name": SOURCE_LANGUAGES.get(order["target_lang"], order["target_lang"]),
        # The alt+p pluralize shortcut (editor.js:pluralizeWord) is a real
        # English grammar-rules algorithm - it only ever produces a
        # correct result on English text, whichever STEP that happens to
        # be for this order (source for an en->sw job, target for sw->en).
        # This is the other language, whichever side it's actually on.
        "non_english_lang_name": (
            SOURCE_LANGUAGES.get(order["target_lang"], order["target_lang"]) if order["source_lang"] == "en"
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
        seg.literal = seg.source_final_text
        seg.spoken = seg.source_final_text
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


@app.post("/staff/orders/{order_id}/segments/{segment_id}/retranslate")
def editor_retranslate(request: Request, order_id: str, segment_id: str):
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

    try:
        _retranslate_and_resync(order, job, seg)
    except Exception as exc:  # noqa: BLE001 - a real provider failure (bad/missing key,
        # network error) should come back as a real error the editor UI can show, not a
        # raw 500 with no message.
        traceback.print_exc()
        return JSONResponse({"error": f"{order['mt']} provider failed: {exc}"}, status_code=502)

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
def editor_retranscribe(request: Request, order_id: str, segment_id: str):
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

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = str(Path(tmpdir) / "clip.wav")
        if not extract_audio_window(order["audio_path"], seg.start_ms, seg.end_ms, clip_path, sample_rate=16000):
            return JSONResponse({"error": "Could not extract this segment's audio (ffmpeg unavailable, or the window is empty)."}, status_code=502)
        try:
            asr_provider = get_asr(order["asr"])
            clip_segments = asr_provider.transcribe(clip_path, language=order["source_lang"])
        except Exception as exc:  # noqa: BLE001 - a real provider failure should come back
            # as a real error the editor UI can show, not a raw 500 with no message.
            traceback.print_exc()
            return JSONResponse({"error": f"{order['asr']} provider failed: {exc}"}, status_code=502)

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
        if getattr(seg, "segment_type", "speech") == "gap":
            continue
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
