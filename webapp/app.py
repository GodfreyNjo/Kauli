"""Local demo platform for Kauli - client upload -> processing -> staff
review -> delivery, as a clickable UI on top of the real pipeline.

Auth is real (Supabase email/password) but everything else here still
isn't production: no payments, not deployed, single-tenant SQLite. It
exists so you can experience/demo the end-to-end flow before the roadmap's
later phases get built for real. Run with:
    uvicorn webapp.app:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yt_dlp
from fastapi import Body, FastAPI, Request, UploadFile, Form, File
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from . import billing, db, supabase_auth, worker, upload_security, logging_setup, rate_limit, medium_publish, devto_publish, blog_ai_assist, youtube_poll, mailer, notifications, tat, r2_uploads  # noqa: E402
from kauli import timing  # noqa: E402
from kauli.models import Job, Word, split_off_speaker_tag  # noqa: E402
from kauli.mixer import build_timeline, write_wav_mono, extract_reference_clip, extract_audio_window, time_stretch  # noqa: E402
from kauli.pipeline import translate_segment, apply_stretch_fit, resolve_voice_for_segment, _insert_non_speech_segments, spell_out_text, render_gap_audio, strip_bracket_tags_for_tts  # noqa: E402
from kauli.providers import get_asr, get_mt, get_tts  # noqa: E402
from kauli.providers.tts import PIPER_VOICES  # noqa: E402
from kauli.subtitles import to_srt, to_vtt, _wrap_caption_text, _display_end_ms  # noqa: E402

WEBAPP_DIR = Path(__file__).parent
UPLOAD_DIR = WEBAPP_DIR / "data" / "uploads"
OUTPUT_DIR = WEBAPP_DIR / "data" / "output"
AVATAR_DIR = WEBAPP_DIR / "data" / "avatars"
RECEIPT_DIR = WEBAPP_DIR / "data" / "receipts"
ALLOWED_AVATAR_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
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
# "Sign in to confirm you're not a bot" - a real, live problem, confirmed
# 2026-08-23 against a real video a client hit this on. Already-current
# yt-dlp alone doesn't fix it (checked - no newer release existed). The
# real, officially-supported fix yt-dlp ships for this is requesting
# through the ANDROID app's API surface instead of the web player's -
# not a login bypass or a scraping trick, just a different first-party
# client YouTube itself still serves without this particular check.
# Verified working against the exact video that failed. Falls back to
# "web" if the android client doesn't have what's needed for a given
# video (occasionally offers fewer/lower-quality formats than web does
# when unblocked) - real cookies (--cookies-from-browser upstream calls
# this) are the other documented option, but that needs an actual signed-
# in browser session's cookies handed to this server, which is a real
# account-security tradeoff to make deliberately, not something to wire
# in silently.
YT_DLP_EXTRACTOR_ARGS = {"extractor_args": {"youtube": {"player_client": ["android", "web"]}}}


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
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com https://assets.calendly.com "
    "https://www.youtube.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://i.ytimg.com; "
    "connect-src 'self' https://cloudflareinsights.com https://static.cloudflareinsights.com "
    "https://calendly.com https://*.calendly.com https://*.r2.cloudflarestorage.com; "
    "frame-src https://calendly.com https://*.calendly.com https://www.youtube.com; "
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
    # actually served over real HTTPS; sending it over plain HTTP does
    # nothing (browsers ignore it), so this is inert right now and starts
    # working the moment a real domain+TLS cert exists, no code change
    # needed then.
    if request.url.scheme == "https":
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


def current_user(request: Request):
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
    source_name = SOURCE_LANGUAGES.get(order["source_lang"], order["source_lang"])
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

# No ASR model available to us - local Whisper or any vendor we checked -
# recognizes these languages at all. Orders in one of these get the
# "manual" provider instead of faster-whisper: real ffmpeg-detected speech
# segments with no transcript text, so a human transcriber types the source
# straight into the editor instead of the model guessing wrong. See
# kauli/providers/asr.py:ManualASR - every segment starts at 0.0 confidence,
# which already routes it into the normal staff review queue for free.
MANUAL_TRANSCRIPTION_LANGUAGES = {"ki"}


# ----------------------------------------------------------- marketing ----
CONTACT_PHONE = "0712 531 841"
CONTACT_PHONE_TEL = "+254712531841"  # tel: link needs the international form
CONTACT_PHONE_WHATSAPP = "254712531841"  # wa.me links want the number with no "+" or leading 0
CONTACT_EMAIL = "hello@kauli-forgemedia.com"
# base.html's account menu (every authenticated page) needs a real contact
# link without every single route threading it through by hand - same
# reasoning as the other templates.env.globals assignments near the top
# of this file (tat_status, free_minutes, etc).
templates.env.globals["contact_email"] = CONTACT_EMAIL
templates.env.globals["contact_whatsapp_url"] = f"https://wa.me/{CONTACT_PHONE_WHATSAPP}"
templates.env.globals["contact_phone_tel"] = CONTACT_PHONE_TEL
templates.env.globals["contact_phone_display"] = CONTACT_PHONE

# Real answers only - every figure here is read from billing.py, not typed
# in twice, so a rate change can never leave the FAQ quietly wrong. No
# fabricated turnaround SLA (nothing in the app enforces one), no
# fabricated client counts.
MARKETING_FAQ = [
    {"q": "How much does Swahili to English translation or dubbing cost?",
     "a": f"Transcription is ${billing.SERVICE_LEVELS['transcribe']['rate_per_min']:.2f} per audio-minute, "
          f"transcription plus translation is ${billing.SERVICE_LEVELS['translate']['rate_per_min']:.2f}, "
          f"and a full dub (transcription, translation and a synthesized voice track) is "
          f"${billing.SERVICE_LEVELS['dub']['rate_per_min']:.2f} per audio-minute. "
          f"The first {billing.FREE_MINUTES_PER_MONTH:.0f} minutes every month are free on every plan, "
          "and a Pro or Premium plan discounts every minute beyond that."},
    {"q": "What's the difference between verbatim and clean read?",
     "a": "Clean read is fully cleaned-up, grammatically readable text - the default, and what most "
          "subtitling and dubbing work uses. Verbatim keeps every false start, filler word and stutter "
          "exactly as spoken, which is what legal or research use typically needs instead. You choose "
          "per order when you submit it."},
    {"q": "How long does an order take?",
     "a": "It depends on length and what's already in the queue - a first AI draft is usually ready "
          "within minutes of upload, then a human editor reviews it line by line against the source "
          "audio before it's marked ready, which is the part that scales with length and complexity "
          "rather than being instant."},
    {"q": "Is my uploaded file safe?",
     "a": "Every upload is checked against its real file signature (not just trusted by its file name), "
          "scanned for malware, and validated as genuine audio or video before it's ever processed - "
          "nothing is taken on trust."},
    {"q": "Can you clone a speaker's voice for the dubbed track?",
     "a": "Yes, but only with your explicit confirmation that you hold the rights and consent to use "
          "that voice - that confirmation is required before any voice-cloning order runs, not an "
          "afterthought."},
    {"q": "Do I need to install anything?",
     "a": "No - Kauli is entirely web-based. Upload a file or paste a YouTube link from your browser, "
          "and download the finished transcript, subtitles or dubbed track the same way."},
    {"q": "What languages do you support?",
     "a": f"{', '.join(SOURCE_LANGUAGES.values())} today, translating into English or Swahili. "
          "Kikuyu goes through an extra manual transcription step since no speech-recognition model "
          "handles it yet - translation and voicing still run the same AI-plus-human-review pipeline "
          "as everything else, it just costs a bit more per minute to cover that step. We only add a "
          "language once we've verified the pipeline actually works on it, not on request alone - if "
          "you need one we don't list yet, tell us and we'll look into it."},
    {"q": "Can I get a real human voice actor instead of an AI-synthesized voice?",
     "a": f"Yes - add it to any full dub order for an extra "
          f"${billing.ADDONS['human_voice_over']['rate_per_min']:.2f} per audio-minute. The AI dub still "
          "gets made and delivered right away either way; our team casts and delivers the human "
          "version separately once it's ready, which takes longer than the automated dub."},
    {"q": "Can you dub a video with multiple speakers - a man, a woman, children?",
     "a": "Yes. Each speaker gets tagged (automatically when the transcription provider detects distinct "
          "speakers, or by a human editor listening to the audio) and then assigned their own consistent "
          "voice, so the same character keeps the same voice for the whole video instead of one generic "
          "voice reading every line. It's a real editorial step our team handles as part of every dub, not "
          "an extra you have to request."},
    {"q": "Do you create deepfakes, or make someone say something they didn't actually say?",
     "a": "No. Voice cloning at Kauli only ever translates what a speaker actually said in the source "
          "recording into another language, in their own voice - there's no feature to type new or "
          "different text and have it spoken in a cloned voice. We require confirmed rights and consent "
          "before cloning any speaker's voice, and we refuse or cancel any order intended to impersonate "
          "someone or fabricate statements they never made."},
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
        "title": "Kauli for NGOs - Swahili/English localization without agency rates",
        "meta_description": "Transcription, translation and dubbing between Swahili, Kikuyu and English for "
                             "field reports, training videos and campaigns - transparent per-minute pricing, "
                             "every order human-reviewed before delivery.",
        "kicker": "For NGOs and civil-society organizations",
        "h1": "Reach Swahili and English-speaking audiences, without an agency budget or a six-week wait",
        "intro": "Field reports, training videos and awareness campaigns need to speak to the people they're "
                 "actually about. Kauli transcribes, translates and dubs between Swahili, Kikuyu and English at "
                 "a transparent per-minute rate, with a real editor checking every line before it reaches you.",
        "why_heading": "Built for real field and campaign work",
        "points": [
            {"title": "Every order human-reviewed",
             "body": "An AI draft is fast, but a real editor checks it against the source audio line by line "
                      "before it's marked ready - it matters when the content is about real people and real "
                      "communities."},
            {"title": "Consent required before any voice is cloned",
             "body": "If a dub needs to sound like the original speaker - an interview subject, a community "
                      "leader - we require your explicit confirmation of rights and consent before that ever "
                      "runs. Never assumed."},
            {"title": "A real delivery estimate, not a guess",
             "body": "Every order gets a real turnaround estimate the moment it's confirmed, based on its "
                      "actual length and service level, visible on your dashboard - so you know if it'll be "
                      "ready before your event or publication date."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process - no bundled credits, no surprise tiers. Your first few minutes "
                      "are free to test the quality yourself, no card required."},
        ],
        "cta_heading": "See the quality for yourself, on your own material",
        "cta_body": "Upload a real clip or paste a YouTube link - no card required to try it.",
    },
    "youtubers": {
        "title": "Kauli for YouTubers - dub or subtitle your videos into English or Swahili",
        "meta_description": "Paste a YouTube link and get a translated transcript, subtitles, or a fully "
                             "dubbed track back - human-reviewed before delivery, transparent per-minute "
                             "pricing.",
        "kicker": "For YouTubers and content creators",
        "h1": "Reach your English-speaking audience, without manually managing every upload",
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
            {"title": "Every order human-reviewed",
             "body": "An AI draft is fast, but a real editor checks it against the source audio before it's "
                      "marked ready - accuracy your audience will actually notice."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process. First few minutes free to try the quality yourself, no card "
                      "required."},
        ],
        "cta_heading": "Try it on your last upload",
        "cta_body": "Paste the link, or connect your channel for ongoing auto-import.",
    },
    "media-broadcast": {
        "title": "Kauli for media and broadcast - subtitles and dubs on a real deadline",
        "meta_description": "Broadcast-ready subtitles, translated transcripts and dubbed video with burned "
                             "captions - human-reviewed, priced per minute, with a real tracked delivery "
                             "deadline on every order.",
        "kicker": "For media houses and broadcasters",
        "h1": "Broadcast-ready subtitles and dubs, on a deadline you can actually see",
        "intro": "Kauli pairs fast AI transcription, translation and voice synthesis with a real editor on "
                 "every order - broadcast-ready subtitles, translated transcripts, or a fully dubbed track "
                 "with burned captions or a dubbed video file, priced by the minute, reviewed before it airs.",
        "why_heading": "Built for real broadcast deadlines",
        "points": [
            {"title": "A real delivery estimate, tracked live",
             "body": "Every order gets an internal and client-facing delivery deadline the moment it's "
                      "confirmed, based on its actual length and service level - visible on your dashboard, "
                      "not a vague \"a few days.\""},
            {"title": "Broadcast-ready deliverables",
             "body": "Burned-in captions or a fully dubbed video file, not just a raw transcript - included "
                      "on Premium and Enterprise plans, or as a per-order add-on."},
            {"title": "Every order human-reviewed",
             "body": "An AI draft is fast, but a real editor checks it line by line against the source audio "
                      "before it's marked ready - broadcast accuracy, not best-effort."},
            {"title": "Enterprise turnaround and billing",
             "body": "A faster dedicated turnaround tier and bank-transfer billing for organizations with an "
                      "established relationship - talk to us about volume."},
        ],
        "cta_heading": "Talk to us about your next broadcast deadline",
        "cta_body": "Book a call, or start with a real clip to see the quality and turnaround for yourself.",
    },
    "e-learning": {
        "title": "Kauli for e-learning - localize a course once, reuse it every cohort",
        "meta_description": "Translate and dub course material between Swahili, Kikuyu and English with "
                             "consistent terminology across every lesson - human-reviewed, priced per minute.",
        "kicker": "For e-learning and training teams",
        "h1": "Localize a course once, keep it consistent through every cohort",
        "intro": "Terminology that drifts from lesson to lesson confuses learners - Kauli's human editors work "
                 "from the same review standard on every order, so a term translated one way in lesson one "
                 "stays that way in lesson twelve, not whatever a memory-less AI tool happens to pick each time.",
        "why_heading": "Built for course content, not one-off clips",
        "points": [
            {"title": "Every order human-reviewed",
             "body": "An AI draft is fast, but a real editor checks it against the source audio line by line "
                      "before it's marked ready - the same standard on lesson one and lesson fifty."},
            {"title": "Subtitles, transcript, or a full dub",
             "body": "Pick what your course actually needs per module - translated subtitles for a video "
                      "lecture, a clean transcript for reading material, or a fully dubbed track."},
            {"title": "Real delivery estimates",
             "body": "Every order gets a real turnaround estimate the moment it's confirmed, based on its "
                      "actual length and service level - useful when you're localizing a course against a "
                      "cohort start date."},
            {"title": "Transparent per-minute pricing",
             "body": "Pay for what you process across however many modules you have - no bundled credits, "
                      "first few minutes free to try the quality yourself."},
        ],
        "cta_heading": "Try it on one lesson first",
        "cta_body": "Upload a real module or paste a YouTube link - no card required to try it.",
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
    return templates.TemplateResponse(request, "solution_page.html",
        {**_marketing_context(home="/"), "page": page})


def _marketing_context(sent: bool = False, lead_error: str | None = None, home: str = "") -> dict:
    """Shared by both marketing.html render points below - keeps them from
    quietly drifting apart the way two copies of the same context dict
    always eventually do. home="" on the homepage itself (bare #anchor
    links), "/" on a /solutions/* page reusing _nav.html (which has no
    #how/#demo/etc. sections of its own to scroll to)."""
    return {
        "user": None, "home": home,
        "plans": billing.PLANS, "service_levels": billing.SERVICE_LEVELS,
        "addons": billing.ADDONS,
        "free_minutes": billing.FREE_MINUTES_PER_MONTH,
        "phone_display": CONTACT_PHONE, "phone_tel": CONTACT_PHONE_TEL,
        "email": CONTACT_EMAIL,
        "whatsapp_url": f"https://wa.me/{CONTACT_PHONE_WHATSAPP}",
        "calendly_url": os.environ.get("KAULI_CALENDLY_URL"),
        "faq": MARKETING_FAQ,
        "source_languages": SOURCE_LANGUAGES,
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "sent": sent,
        "lead_error": lead_error,
    }


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




def _queue_welcome_message(user, base_url: str = "") -> None:
    if db.has_onboarding_message(user["id"], "welcome"):
        return  # already queued once for this account - don't duplicate on a second login
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    subject = f"Welcome to Kauli, {name}!"
    body = (
        f"Hi {name},\n\n"
        f"I'm {FOUNDER_NAME.split()[0]} - I actually run Kauli day to day at Forge Media "
        "Services, so this is genuinely from me, not an automated \"team\".\n\n"
        f"Your account has {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes loaded already - "
        "upload a real clip whenever you're ready and see the AI-drafted, human-checked result "
        "for yourself before you spend anything.\n\n"
        "If anything's unclear, or you'd rather just talk it through, reply to this email or "
        f"message me directly on WhatsApp: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
        f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
    )
    cta_url = f"{base_url.rstrip('/')}/client" if base_url else "/client"
    html_inner = (
        f'<p style="margin:0 0 14px;">Hi {name},</p>'
        f'<p style="margin:0 0 14px;">I\'m {FOUNDER_NAME.split()[0]} - I actually run Kauli day to day '
        f'at Forge Media Services, so this is genuinely from me, not an automated "team".</p>'
        f'<p style="margin:0 0 14px;">Your account has {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes '
        f'loaded already - upload a real clip whenever you\'re ready and see the AI-drafted, '
        f'human-checked result for yourself before you spend anything.</p>'
        f'<p style="margin:0 0 14px;">If anything\'s unclear, or you\'d rather just talk it through, '
        f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> to this email '
        f'or message me directly on <a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a>.</p>'
        f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
    )
    _queue_and_send(user, "welcome", subject, body, base_url=base_url,
                     cta_text="Upload your first order for free", cta_url=cta_url, html_inner=html_inner)


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
        f"what you expected, tell me directly - reply here or WhatsApp me: "
        f"https://wa.me/{CONTACT_PHONE_WHATSAPP} - and we'll make it right.\n\n"
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
            + f'<p style="margin:0 0 14px;">Tell me directly - '
              f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> here or message me on '
              f'<a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a> '
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
        _marketing_context(sent=request.query_params.get("sent") == "1"))


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", _marketing_context(home="/"))


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _marketing_context(home="/"))


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
    })


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(request: Request, slug: str):
    post = db.get_blog_post_by_slug(slug, published_only=True)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
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
    one rather than a fabricated date that would just be a guess."""
    base = f"{request.url.scheme}://{request.url.netloc}"
    static_urls = [f"{base}/", f"{base}/terms", f"{base}/privacy", f"{base}/blog"]
    static_urls += [f"{base}/solutions/{slug}" for slug in SOLUTION_PAGES]
    entries = [f"  <url><loc>{u}</loc></url>" for u in static_urls]
    for p in db.list_blog_posts(published_only=True):
        loc = f"{base}/blog/{p['slug']}"
        lastmod = datetime.fromtimestamp(p["updated_at"]).strftime("%Y-%m-%d") if p["updated_at"] else None
        entries.append(f"  <url><loc>{loc}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
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
    rates = "\n".join(f"- {sl['name']}: ${sl['rate_per_min']:.2f} per audio-minute"
                       for sl in billing.SERVICE_LEVELS.values())
    posts = db.list_blog_posts(published_only=True)
    post_lines = "\n".join(f"- [{p['title']}]({base}/blog/{p['slug']})" for p in posts[:20])
    body = f"""# Kauli

> AI-drafted, human-verified transcription, translation and dubbing between
> Swahili, Kikuyu and English, operated by Forge Media Services. Every order
> is checked line-by-line by a real editor against the source audio before
> delivery; nothing ships on AI output alone.

## Pricing
Per audio-minute, no bundled credits:
{rates}
First {billing.FREE_MINUTES_PER_MONTH:.0f} minutes free to try, no card required.

## Languages
Source: {', '.join(SOURCE_LANGUAGES.values())}. Translates into English or Swahili.

## Key pages
- [Homepage]({base}/) - overview, pricing, FAQ
- [For NGOs]({base}/solutions/ngos)
- [For YouTubers & creators]({base}/solutions/youtubers)
- [For media & broadcast]({base}/solutions/media-broadcast)
- [For e-learning & training]({base}/solutions/e-learning)
- [Blog]({base}/blog)
- [Terms]({base}/terms) / [Privacy]({base}/privacy)

## Blog posts
{post_lines}

## Contact
{CONTACT_EMAIL} / WhatsApp: https://wa.me/{CONTACT_PHONE_WHATSAPP}
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
        return RedirectResponse("/?sent=1#book", status_code=303)
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
    db.create_lead(name, email, phone.strip() or None, company.strip() or None,
                    message.strip() or None, preferred_time.strip() or None,
                    volume_estimate=volume_estimate.strip() or None, org_type=org_type.strip() or None,
                    personal_email_flag=_is_personal_email_domain(email))
    # Redirect (not a direct render) so refreshing the confirmation page
    # never re-submits the form - standard POST/redirect/GET.
    return RedirectResponse("/?sent=1#book", status_code=303)


def _safe_next(path: str | None) -> str:
    """Only ever a relative in-app path - "/client/orders/xyz", never a
    protocol-relative or absolute URL (which could redirect somewhere
    outside Kauli after a real login). Falls back to "/" (the normal
    role-based landing page) for anything else, including empty/missing."""
    if path and path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, mode: str = "signin", notice: str | None = None, next: str | None = None):
    display_notice = None
    if notice == "account_closed":
        display_notice = "Your account has been closed."
    elif notice == "password_reset":
        display_notice = "Your password has been reset - sign in with your new password."
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "notice": display_notice, "mode": "signup" if mode == "signup" else "signin",
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
        redirect_to = str(request.base_url).rstrip("/") + "/reset-password"
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
    ok, error = supabase_auth.set_new_password(access_token, refresh_token, password)
    if not ok:
        return templates.TemplateResponse(request, "reset_password.html", {
            "error": error or "That reset link is invalid or has expired. Request a new one.",
        }, status_code=400)
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
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Too many attempts - try again in {retry_after}s.", "notice": None, "mode": "signin",
            "next": _safe_next(next),
        }, status_code=429)
    session, error = supabase_auth.sign_in(email, password)
    if error or not session:
        return templates.TemplateResponse(request, "login.html", {
            "error": error or "Wrong email or password.", "notice": None, "mode": "signin",
            "next": _safe_next(next),
        })
    role, is_admin = _resolve_role_and_admin(email)
    user, was_new = db.get_or_create_user(session.user.id, email, default_role=role)
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
        if role == "voice_actor":
            db.link_voice_actor_user(email, user["id"])  # consumed - same idea, see that function's comment
        if role == "client":  # the welcome message is written for a client, not a new staff account
            _queue_welcome_message(user, base_url=str(request.base_url))
    request.session["user_id"] = user["id"]
    # Clicking a deep link from an email (an order, a receipt, billing)
    # while logged out used to always land back on the generic dashboard
    # after signing in, losing where they actually meant to go - this
    # sends them back to the real destination instead, same pattern every
    # real app uses. _safe_next already rejected anything that isn't a
    # real in-app path, so "/" (the normal role-based landing page) is
    # the only fallback, never an open redirect.
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           marketing_consent: str = Form(""), next: str = Form("")):
    email = email.strip().lower()
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
    role, is_admin = _resolve_role_and_admin(email)
    user, was_new = db.get_or_create_user(
        session.user.id, email, default_role=role,
        marketing_consent=bool(marketing_consent), consent_ip=request.client.host if request.client else None,
    )
    db.accept_team_invite(user["id"], email)
    if was_new:
        if is_admin:
            db.set_user_admin(user["id"], True)
        if role == "staff" and db.is_invited_staff(email):
            db.remove_staff_invite(email)
        if role == "voice_actor":
            db.link_voice_actor_user(email, user["id"])
        if role == "client":
            _queue_welcome_message(user, base_url=str(request.base_url))
    request.session["user_id"] = user["id"]
    return RedirectResponse(_safe_next(next), status_code=303)


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
@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, team_notice: str | None = None, team_error: str | None = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
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
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": False, "error": None,
        "theme": "dark" if user["role"] == "staff" else "light",
        "is_team_owner": is_team_owner, "team_members": team_members, "team_owner_name": team_owner_name,
        "team_notice": team_notice, "team_error": team_error,
    })


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
        return RedirectResponse("/settings", status_code=303)
    email = email.strip().lower()
    if not email or "@" not in email:
        return RedirectResponse("/settings?team_error=Enter+a+real+email+address.", status_code=303)
    if email == user["email"].strip().lower():
        return RedirectResponse("/settings?team_error=That%27s+your+own+email.", status_code=303)
    existing = [m for m in db.list_team_members(user["id"]) if m["invited_email"] == email]
    if existing:
        return RedirectResponse("/settings?team_error=Already+invited.", status_code=303)
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
    return RedirectResponse("/settings?team_notice=Invite+sent.", status_code=303)


@app.post("/settings/team/remove")
def settings_team_remove(request: Request, member_id: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client" or user["client_scope_id"] != user["id"]:
        return RedirectResponse("/login")
    db.remove_team_member(user["id"], member_id)
    return RedirectResponse("/settings?team_notice=Removed.", status_code=303)


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
        "source_languages": SOURCE_LANGUAGES,
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
    theme = "dark" if user["role"] == "staff" else "light"
    if not display_name:
        return templates.TemplateResponse(request, "settings.html", {
            "user": user, "saved": False, "error": "Name can't be empty.", "theme": theme,
        })

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
            return templates.TemplateResponse(request, "settings.html", {
                "user": user, "saved": False, "error": str(exc), "theme": theme,
            })
        mime = upload_security.sniff_image_type(head)
        ext = ALLOWED_AVATAR_TYPES.get(mime)
        if not ext:
            tmp_dest.unlink(missing_ok=True)
            return templates.TemplateResponse(request, "settings.html", {
                "user": user, "saved": False,
                "error": "Photo must be a real PNG, JPEG, or WebP file.", "theme": theme,
            })
        # Clear any previous photo under a different extension first, so
        # switching from a .png to a .jpg doesn't leave the old one behind
        # for /avatar/{id} to find first.
        for old in AVATAR_DIR.glob(f"{user['id']}.*"):
            old.unlink(missing_ok=True)
        dest = AVATAR_DIR / f"{user['id']}{ext}"
        tmp_dest.rename(dest)
        avatar_path = str(dest)

    db.update_profile(user["id"], display_name, avatar_path)
    user = db.get_user(user["id"])  # re-fetch so the page reflects what was just saved
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": True, "error": None, "theme": theme,
    })


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
    user = db.get_user(user["id"])
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": True, "error": None,
        "theme": "dark" if user["role"] == "staff" else "light",
    })


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
    theme = "light"
    if confirm_text.strip().lower() != user["email"].strip().lower():
        return templates.TemplateResponse(request, "settings.html", {
            "user": user, "saved": False, "theme": theme,
            "error": "Type your email exactly to confirm closing your account.",
        })
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
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": False, "error": None, "theme": "light",
        "deletion_requested": True,
    })


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
    for o in db.list_orders_for_client(user["client_scope_id"]):
        deliverables = []
        if o["status"] in ("ready_for_delivery", "delivered") and not o["is_free_preview"]:
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
        "source_languages": SOURCE_LANGUAGES,
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
    reorder_form_values = {}
    if reorder_youtube_url:
        reorder_form_values = {"youtube_url": reorder_youtube_url, "source_lang": reorder_source_lang or "sw"}
    return templates.TemplateResponse(request, "client_dashboard.html", {
        "user": user, "orders": orders, "unread": unread, "anthropic_available": anthropic_available,
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "source_languages": SOURCE_LANGUAGES,
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "form_values": reorder_form_values,
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["client_scope_id"]),
        "youtube_pending_imports": db.list_pending_imports(user["client_scope_id"]),
    })


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
    subscription = db.get_subscription_current(user["client_scope_id"])
    plan = billing.effective_plan(user, subscription)
    return templates.TemplateResponse(request, "client_dashboard.html", {
        "user": user,
        "orders": db.list_orders_for_client(user["client_scope_id"]),
        "unread": db.unread_order_ids(user["client_scope_id"], include_internal=False),
        "anthropic_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_credits": db.wallet_credits_remaining(user["client_scope_id"]),
        "rush_surcharge_pct": billing.RUSH_SURCHARGE_PCT,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "source_languages": SOURCE_LANGUAGES,
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "error": error,
        "exception_context": exception_context,
        "form_values": form_values or {},
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["client_scope_id"]),
        "youtube_pending_imports": db.list_pending_imports(user["client_scope_id"]),
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
        "instr_speaker_ids": instr_speaker_ids, "instr_verbatim_level": instr_verbatim_level,
        "instr_transcribe_lyrics": instr_transcribe_lyrics, "instr_use_italics": instr_use_italics,
        "instr_existing_subs": instr_existing_subs, "instr_no_audio": instr_no_audio,
        "instr_wrong_language": instr_wrong_language, "instr_instrumental_only": instr_instrumental_only,
        "instr_notes": instr_notes, "folder_name": folder_name,
        "voice_clone_consent": voice_clone_consent, "rush": rush,
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
    if source_lang not in SOURCE_LANGUAGES:
        return _client_dashboard_error(request, user, "Unknown source language.", form_values=form_values)
    if target_lang not in ("en", "sw"):
        return _client_dashboard_error(request, user, "Unknown target language.", form_values=form_values)
    addons = ["video_deliverables"] if addon_video_deliverables else []
    # Only meaningful on a full dub - there's no voice to record on a
    # transcription/translation-only order. Enforced here, not just by
    # hiding the checkbox client-side - a submitted form field for the
    # wrong service level is simply ignored, not trusted.
    wants_human_voice_over = bool(addon_human_voice_over) and service_level == "dub"
    if wants_human_voice_over:
        addons.append("human_voice_over")
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
        asr = "transkriptor" if os.environ.get("TRANSKRIPTOR_API_KEY") else "faster-whisper"
    if not level["mt"]:
        mt = "stub"
    elif source_lang in MANUAL_TRANSCRIPTION_LANGUAGES:
        # Local MT (Helsinki-NLP) is Congo-Swahili-only - it would silently
        # mistranslate Kikuyu text into nonsense rather than failing loudly.
        # Claude at least has a real shot at it; refuse the order instead of
        # shipping a wrong translation if it isn't wired up yet.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _client_dashboard_error(request, user,
                "Kikuyu translation needs our AI translation provider connected first, "
                "which isn't done yet - contact us and we'll handle this one manually "
                "in the meantime.", form_values=form_values)
        mt = "claude"
    else:
        mt = plan_mt
        # Real bug this fixes, same class as the Kikuyu guard just above:
        # LocalMT (kauli/providers/mt.py) is Helsinki-NLP/opus-mt-swc-en -
        # a Swahili-TO-English-ONLY model that ignores source_lang/
        # target_lang entirely. It only ever produces a real answer for
        # sw->en; any other direction reaching "local" (most concretely,
        # an en->sw order on Free/Pro tier) fed English text through a
        # model built to output English, producing nonsense regardless of
        # input - confirmed live, not hypothetical. AzureTranslateMT is
        # genuinely bidirectional; refuse rather than ship garbage if it
        # isn't configured yet, same pattern as Kikuyu and the dub-voice
        # case below.
        if mt == "local" and not (source_lang == "sw" and target_lang == "en"):
            if os.environ.get("AZURE_TRANSLATOR_KEY"):
                mt = "azure-translate"
            else:
                return _client_dashboard_error(request, user,
                    "Translating that language pair needs our translation provider connected first, "
                    "which isn't done yet - contact us and we'll handle this one manually in the meantime.",
                    form_values=form_values)
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
    if service_level == billing.FREE_MINUTES_SERVICE_LEVEL:
        free_cap = billing.FREE_MINUTES_PER_MONTH + (subscription["bonus_minutes"] or 0.0 if subscription else 0.0)
        free_minutes_for_this_order = db.reserve_free_minutes(user["client_scope_id"], minutes, free_cap)
    else:
        free_minutes_for_this_order = 0.0
    is_rush = bool(rush)
    # Same double-spend concern as free minutes above, for prepaid credits:
    # reserve atomically rather than read-then-consume-later. The ceiling a
    # reservation could possibly need is a pure function of minutes/rate/
    # plan/discount, none of which touch the wallet - so it's safe to price
    # the order once with wallet_credits_available=0 just to learn that
    # ceiling, reserve up to it (see db.reserve_wallet_credits), then price
    # it again for real with whatever was actually granted.
    preliminary_cost = billing.order_cost_usd(minutes, service_level, plan, free_minutes_for_this_order,
                                               addons=addons, wallet_credits_available=0.0, rush=is_rush)
    credits_ceiling = billing.usd_to_credits(preliminary_cost["gross_usd"] - preliminary_cost["discount_usd"])
    reserved_credits = db.reserve_wallet_credits(user["client_scope_id"], credits_ceiling)
    cost = billing.order_cost_usd(minutes, service_level, plan, free_minutes_for_this_order,
                                   addons=addons, wallet_credits_available=reserved_credits, rush=is_rush)
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
    if youtube_video_id:
        # Closes the loop on the auto-import flow, if this happens to be
        # one of those videos - a real order now exists for it, so it's no
        # longer "pending".
        db.mark_pending_import_ordered(user["id"], youtube_video_id)
    db.set_order_billing(order_id, service_level, minutes, cost["total_usd"], addons=applied_addons,
                          cost_breakdown=cost, is_rush=is_rush)
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
                f"Questions? WhatsApp me: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
                f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
            )
            inner = (
                f'<p style="margin:0 0 14px;">Hi {name},</p>'
                f'<p style="margin:0 0 14px;">You\'re down to about {remaining:.0f} prepaid credits - just '
                f'a heads up so an upcoming order doesn\'t get held up waiting on a top-up.</p>'
                f'<p style="margin:0 0 14px;">Questions? Message me on '
                f'<a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a>.</p>'
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

    if cost["total_usd"] <= 0:
        # Fully covered by this month's free allowance - no payment step,
        # straight to processing, same as the old free experience. Usage
        # was already recorded above by db.reserve_free_minutes at the
        # moment it was granted - not repeated here.
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


def _order_progress_steps(status: str) -> list[dict]:
    """Real step states for order_detail.html's progress tracker - never
    a fixed animation, always derived from the actual order.status. Two
    real statuses don't fit a clean "moving forward" story and get their
    own marker instead of silently advancing: returned_to_client (needs
    the CLIENT's action, not further progress) and failed/dead_letter
    (something broke) both mark their step 'attention' instead of
    'current' or 'done'."""
    current_key = _STEP_FOR_STATUS.get(status, "submitted")
    current_idx = next((i for i, (k, _) in enumerate(_PROGRESS_STEPS) if k == current_key), 0)
    attention = status in ("returned_to_client", "failed", "dead_letter")
    steps = []
    for i, (key, label) in enumerate(_PROGRESS_STEPS):
        if i < current_idx:
            state = "done"
        elif i == current_idx:
            state = "attention" if attention else "current"
        else:
            state = "pending"
        steps.append({"key": key, "label": label, "state": state})
    return steps


@app.get("/client/orders/{order_id}", response_class=HTMLResponse)
def client_order_detail(request: Request, order_id: str, error: str | None = None, notice: str | None = None):
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
    return templates.TemplateResponse(request, "order_detail.html", {
        "user": user, "order": order, "job": job, "role": "client", "messages": messages,
        "has_translation": order_level["mt"], "has_dub": order_level["tts"],
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "receipt": db.get_receipt_for_order(order_id),
        "error": error, "notice": notice,
        "folders": db.list_folders_for_client(user["client_scope_id"]),
        "progress_steps": _order_progress_steps(order["status"]),
    })


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
                  error: str | None = None):
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
    })


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
    lines = [{"label": level["name"], "detail": detail, "amount_usd": cost["gross_usd"]}]
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
    payer = db.get_user(payment["user_id"])
    if payer and payer["onboarding_status"] != "activated":
        # First real money from this account, of any kind (order, plan,
        # wallet top-up) - the actual "this is a real customer now" moment.
        # See _queue_first_payment_message - auto-sent now that Brevo is
        # configured, same as the welcome message.
        _queue_first_payment_message(payer, base_url=base_url)
        db.set_onboarding_status(payer["id"], "activated")
    try:
        payment_kind = json.loads(payment["meta"]).get("kind", "order") if payment["meta"] else "order"
    except (json.JSONDecodeError, AttributeError):
        payment_kind = "order"
    line_items = None
    if payment["order_id"] and payment_kind == "difficulty_surcharge":
        # Already processed and delivered-ready - this only clears the
        # surcharge-approval gate, never re-queues the job.
        db.approve_difficulty_surcharge(payment["order_id"])
        description = f"Additional work on order {payment['order_id']}"
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
    return True


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

    if provider == "paystack":
        if not billing.paystack_configured():
            return RedirectResponse(f"{back_url}?error=Paystack+isn%27t+configured+yet.", status_code=303)
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, None, "USD", "paystack", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind}))
        callback_url = str(request.base_url).rstrip("/") + f"/billing/callback/paystack?payment_id={payment_id}"
        result = billing.paystack_initialize(user["email"], amount_usd, payment_id, callback_url)
        if "error" in result:
            db.fail_payment(payment_id)
            return RedirectResponse(f"{back_url}?error={result['error']}", status_code=303)
        # Stashed so a client who lands back on this page while this
        # payment is still "pending" (see order_pay_page) gets a real
        # "Resume payment" link straight back to the SAME Paystack page,
        # not just a countdown telling them to wait - Paystack's checkout
        # page for a given reference stays open well past our own 15-
        # minute pending window, so re-using it is safe and correct.
        db.update_payment_meta(payment_id, json.dumps({"kind": payment_kind, "authorization_url": result["authorization_url"]}))
        return RedirectResponse(result["authorization_url"], status_code=303)

    if provider == "mpesa":
        if not billing.mpesa_live():
            return RedirectResponse(f"{back_url}?error=M-Pesa+direct+is+coming+soon+-+not+live+yet.", status_code=303)
        if not phone.strip():
            return RedirectResponse(f"{back_url}?error=Enter+the+M-Pesa+phone+number.", status_code=303)
        amount_kes, rate_source = billing.usd_to_kes(amount_usd)
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, amount_kes, "KES", "mpesa",
                           meta=json.dumps({"phone": phone.strip(), "rate_source": rate_source, "kind": payment_kind}),
                           order_id=order_id)
        callback_url = str(request.base_url).rstrip("/") + "/webhooks/mpesa"
        result = billing.mpesa_stk_push(phone.strip(), amount_kes, payment_id, callback_url)
        if "error" in result:
            db.fail_payment(payment_id)
            return RedirectResponse(f"{back_url}?error={result['error']}", status_code=303)
        db.update_payment_meta(payment_id, json.dumps({
            "phone": phone.strip(), "rate_source": rate_source,
            "checkout_request_id": result["checkout_request_id"], "kind": payment_kind,
        }))
        return RedirectResponse(
            f"{back_url}?notice=Check+your+phone+({phone.strip()})+for+the+M-Pesa+PIN+prompt+-+"
            f"KES+{amount_kes:,.2f}.", status_code=303)

    if provider == "bank":
        db.create_payment(payment_id, user["client_scope_id"], plan, amount_usd, None, "USD", "bank", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind}))
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
        "phone_display": CONTACT_PHONE, "email": CONTACT_EMAIL,
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
    if event.get("event") == "charge.success":
        data = event["data"]
        payment_id = data["reference"]
        payment = db.get_payment(payment_id)
        if payment:
            _activate_payment(payment, data.get("id", payment_id), base_url=str(request.base_url))
    return JSONResponse({"ok": True})


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
    db.create_lead(name, email, phone=None, company=None,
                    message="Booked a call via Calendly." + ("\n" + "\n".join(notes_parts) if notes_parts else ""),
                    preferred_time=None, source="calendly")
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
                 error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "staff_leads.html", {
        "user": user, "leads": db.list_leads(status=status, source=source),
        "filter_status": status, "filter_source": source, "error": error,
        "pipeline": db.leads_pipeline_summary(),
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


@app.post("/staff/onboarding/{user_id}/nudge")
def staff_queue_nudge(request: Request, user_id: str):
    """Deliberately a staff-triggered action, not a cron job - see
    clients_needing_activation_nudge's docstring for why a human decides
    when a real client actually gets chased."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    client = db.get_user(user_id)
    if client and not db.has_onboarding_message(user_id, "inactivity_nudge"):
        name = (client["display_name"] or client["email"].split("@")[0]).strip()
        subject = "Still thinking it over?"
        body = (
            f"Hi {name},\n\n"
            f"Noticed you signed up for Kauli but haven't uploaded anything yet - your "
            f"{billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are still sitting there unused.\n\n"
            "No pressure at all, just wanted to check if anything's unclear or in the way. "
            f"Happy to walk you through it - reply here or WhatsApp me: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
            f"Talk soon,\n{FOUNDER_NAME}\nForge Media Services"
        )
        nudge_cta_url = f"{str(request.base_url).rstrip('/')}/client"
        html_inner = (
            f'<p style="margin:0 0 14px;">Hi {name},</p>'
            f'<p style="margin:0 0 14px;">Noticed you signed up for Kauli but haven\'t uploaded anything '
            f'yet - your {billing.FREE_MINUTES_PER_MONTH:.0f} free minutes are still sitting there unused.</p>'
            f'<p style="margin:0 0 14px;">No pressure at all, just wanted to check if anything\'s unclear '
            f'or in the way. Happy to walk you through it - '
            f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> here or message me on '
            f'<a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a>.</p>'
            f'<p style="margin:0;">Talk soon,<br>{FOUNDER_NAME}<br>(Forge Media Services)</p>'
        )
        _queue_and_send(client, "inactivity_nudge", subject, body, base_url=str(request.base_url),
                         cta_text="Upload your first order for free", cta_url=nudge_cta_url, html_inner=html_inner)
        db.set_onboarding_status(user_id, "nudged")
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
        if order["is_free_preview"]:
            # Free-tier minutes buy a preview (see order_detail.html), never
            # a download - that's true regardless of status or which file
            # kind is being asked for.
            return RedirectResponse(f"/client/orders/{order_id}", status_code=303)
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


@app.post("/staff/orders/{order_id}/render/{kind}")
def render_video_deliverable(request: Request, order_id: str, kind: str):
    """Video-based delivery formats: burned-in captions or a dubbed video.
    Only staff-triggered (not automatic) since it's a real ffmpeg render -
    seconds for a short clip, real time for a long one - and, for a
    YouTube-sourced order, the one place that ever fetches the actual
    video (see fetch_youtube_video's docstring)."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    if kind not in ("burned", "dubbed"):
        return HTMLResponse("Unknown render kind.", status_code=404)
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)

    outdir = Path(order["outdir"])
    try:
        if is_video_file(order["audio_path"]):
            video_path = Path(order["audio_path"])
        elif order["source_youtube_id"]:
            video_path = fetch_youtube_video(order["source_youtube_id"], outdir / "video_cache")
        else:
            return HTMLResponse(
                "This order has no video source (audio-only upload) - "
                "nothing to burn captions onto or dub.", status_code=400)

        if kind == "burned":
            srt_path = outdir / f"subs_{order['target_lang']}.srt"
            if not srt_path.exists():
                return HTMLResponse("Subtitles aren't ready yet.", status_code=400)
            render_burned_captions(video_path, srt_path, outdir / f"burned_captions_{order['target_lang']}.mp4")
        else:
            dub_path = outdir / f"dub_{order['target_lang']}.wav"
            if not dub_path.exists():
                return HTMLResponse("Dubbed audio isn't ready yet.", status_code=400)
            render_dubbed_video(video_path, dub_path, outdir / f"dubbed_video_{order['target_lang']}.mp4")
    except subprocess.CalledProcessError as exc:
        return HTMLResponse(f"ffmpeg render failed: {exc}", status_code=500)
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
        return RedirectResponse(f"/staff/admin?notice=Promoted+the+existing+account+for+{email}+to+staff.", status_code=303)
    db.add_staff_invite(email, invited_by=admin["id"])
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
    db.demote_from_staff(user_id)
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
    return templates.TemplateResponse(request, "staff_newsletter.html", {
        "user": user, "notice": notice,
        "posts": db.list_blog_posts(published_only=True),
        "history": db.list_newsletters(),
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

    recipients = db.list_marketing_opted_in_clients()
    sent_count = 0
    for client in recipients:
        unsub_url = f"{base_url}/unsubscribe/{client['id']}"
        html = mailer.wrap_email_html(
            inner, cta_text=newsletter_cta_text, cta_url=newsletter_cta_url, base_url=base_url,
            footer_note=f'You\'re getting this because you opted in to Kauli updates. '
                        f'<a href="{unsub_url}" style="color:{mailer.BRAND_MUTED};">Unsubscribe</a> anytime.',
        )
        ok, _ = mailer.send_email(client["email"], subject, html)
        if ok:
            sent_count += 1

    db.create_newsletter_record(subject, blog_post_id or None, feature_update.strip(),
                                 industry_trend_text.strip(), industry_trend_url.strip(),
                                 user["id"], sent_count)
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
                       description: str = Form(""), body_html: str = Form(...), category: str = Form("")):
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
    return RedirectResponse(f"/staff/blog/{post_id}/edit?saved=1", status_code=303)


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

    orders = db.list_all_orders()
    # Compact by design - "everything visible without scrolling" (a real
    # ask, not a nice-to-have) means this widget shows a short real slice
    # with a real link to the full, properly-paginated list at /staff/jobs,
    # not every order crammed into one tall table.
    active_jobs = orders[:5]
    review_queue = [o for o in orders if o["status"] == "awaiting_review"][:3]
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
        "active_jobs": active_jobs, "review_queue": review_queue, "edited_pct": edited_pct,
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
    orders = db.list_all_orders()
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
    return templates.TemplateResponse(request, "staff_ops.html", {
        "user": user, "days": days, "now_ts": time.time(),
        "status_counts": db.orders_by_status(),
        "stale_orders": db.stale_active_orders(threshold_hours=24.0),
        "turnaround": db.turnaround_stats(days=days),
        "failures": db.failure_rate(days=days),
        "minutes_processed": db.minutes_processed(days=days),
        "orders_created": db.orders_created_since(days=days),
        "revenue": db.revenue_summary(days=days),
        "margin": db.margin_summary(days=days),
        "new_leads": db.count_new_leads(),
        "ops_triage": db.list_orders_needing_ops_triage(),
        "ai_spend_today": ai_spend_today,
        "ai_spend_alert_threshold": ai_spend_alert_threshold,
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
        (order["duration_minutes"] or 0) * billing.SERVICE_LEVELS.get(
            order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])["rate_per_min"]
        * DIFFICULTY_SURCHARGE_DEFAULT_PCT, 2)

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
        usd = round((order["duration_minutes"] or 0) * billing.SERVICE_LEVELS.get(
            order["service_level"] or "dub", billing.SERVICE_LEVELS["dub"])["rate_per_min"] * pct, 2)
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
                    f"Or reply here, or WhatsApp me directly: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\n"
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
                f'<p style="margin:0 0 14px;">Or '
                f'<a href="mailto:{CONTACT_EMAIL}" style="color:{mailer.BRAND_ACCENT};">reply</a> here, or '
                f'message me directly on '
                f'<a href="https://wa.me/{CONTACT_PHONE_WHATSAPP}" style="color:{mailer.BRAND_ACCENT};">WhatsApp</a>.</p>'
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
    if next == "next_job":
        nxt = db.next_order_needing_review(order_id)
        if nxt:
            return RedirectResponse(f"/staff/orders/{nxt['id']}/editor", status_code=303)
        return RedirectResponse("/staff", status_code=303)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


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
        "source_lang_name": SOURCE_LANGUAGES.get(order["source_lang"], order["source_lang"]),
        "target_lang_name": SOURCE_LANGUAGES.get(order["target_lang"], order["target_lang"]),
        # The alt+p pluralize shortcut (editor.js:pluralizeWord) is a real
        # English grammar-rules algorithm - it only ever produces a
        # correct result on English text, whichever STEP that happens to
        # be for this order (source for an en->sw job, target for sw->en).
        # This is the other language, whichever side it's actually on.
        "non_english_lang_name": (
            SOURCE_LANGUAGES.get(order["target_lang"], order["target_lang"]) if order["source_lang"] == "en"
            else SOURCE_LANGUAGES.get(order["source_lang"], order["source_lang"])
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
