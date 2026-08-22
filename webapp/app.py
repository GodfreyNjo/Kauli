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
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import yt_dlp
from fastapi import Body, FastAPI, Request, UploadFile, Form, File
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from . import billing, db, supabase_auth, worker, upload_security, logging_setup, rate_limit, medium_publish, devto_publish, blog_ai_assist, youtube_poll, mailer, notifications, tat  # noqa: E402
from kauli import timing  # noqa: E402
from kauli.models import Job  # noqa: E402
from kauli.mixer import build_timeline, write_wav_mono, extract_reference_clip  # noqa: E402
from kauli.pipeline import translate_segment  # noqa: E402
from kauli.providers import get_mt, get_tts  # noqa: E402
from kauli.providers.tts import PIPER_VOICES  # noqa: E402
from kauli.subtitles import to_srt, to_vtt  # noqa: E402

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
# (base.html's beacon script + its RUM endpoint), and Calendly's widget
# (marketing.html - script tag + the iframe/API calls it makes on its own).
# Paystack needs no entry here at all: checkout is a server-side redirect
# to Paystack's own hosted page (billing.py's authorization_url), never an
# embedded script or iframe on this site.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com https://assets.calendly.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' https://cloudflareinsights.com https://static.cloudflareinsights.com "
    "https://calendly.com https://*.calendly.com; "
    "frame-src https://calendly.com https://*.calendly.com; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
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
    recomputed from this on every login."""
    is_env_staff = supabase_auth.role_for_email(email) == "staff"
    if is_env_staff:
        return "staff", True
    if db.is_invited_staff(email):
        return "staff", False
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
    steps = []
    if level["asr"]:
        steps.append({"key": "source", "label": "Swahili source", "manual": True,
                      "done": bool(done.get("source"))})
    if level["mt"]:
        steps.append({"key": "target", "label": "English translation", "manual": True,
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
        words = seg.source_edited_transcript.split()
        total_chars = sum(len(w) for w in words) or 1
        duration = max(1, seg.end_ms - seg.start_ms)
        cells = []
        t = seg.start_ms
        for w in words:
            dur = int(duration * (len(w) / total_chars))
            cells.append({"type": "word", "text": w, "start_ms": t, "end_ms": t + dur, "confidence": 1.0, "approx": True})
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
    target_words = seg.final_text.split()
    total_chars = sum(len(w) for w in target_words) or 1
    duration = max(1, seg.end_ms - seg.start_ms)
    cells = []
    t = seg.start_ms
    for w in target_words:
        dur = int(duration * (len(w) / total_chars))
        cells.append({"type": "word", "text": w, "start_ms": t, "end_ms": t + dur})
        t += dur
    if cells:
        cells[-1]["end_ms"] = seg.end_ms  # absorb rounding drift at the tail

    cells += [{"type": "gap", "start_ms": g0, "end_ms": g1} for g0, g1 in _find_gaps(seg)]
    cells.sort(key=lambda c: c["start_ms"])
    return cells


def _resynthesize_full_dub(order, job: Job, tts_name: str, voice_id: str | None) -> None:
    """Re-render every segment's audio with a specific provider/voice and
    rebuild the mixed dub track - what a voice change needs that a single
    corrected segment (see _apply_segment_edit) doesn't: every segment has
    to move to the new voice together, or the dub would switch voices
    mid-file. Text itself is untouched (whatever's already in
    seg.final_text, edits included) - only who's saying it changes."""
    tts_provider = get_tts(tts_name)
    for seg in job.segments:
        raw = Path(order["outdir"]) / "segments" / f"{seg.segment_id}.wav"
        rendered = tts_provider.synthesize(seg.final_text.strip(), str(raw), voice_id=voice_id)
        seg.audio_path = str(raw)
        seg.rendered_duration_ms = rendered
        seg.time_stretch_pct = 0.0

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
        if not job.segments:
            raise RuntimeError("No segments to clone a reference from.")
        ref_seg = max(job.segments, key=lambda s: s.end_ms - s.start_ms)
        ref_end = min(ref_seg.end_ms, ref_seg.start_ms + 20_000)  # XTTS wants 6-20s
        if not extract_reference_clip(order["audio_path"], ref_seg.start_ms, ref_end, str(ref_path)):
            raise RuntimeError("Couldn't extract a reference clip (ffmpeg missing?).")
        _resynthesize_full_dub(order, job, "xtts", str(ref_path))
        db.set_dub_voice(order_id, "xtts", job_status=None)
    except Exception as exc:  # noqa: BLE001 - surface it to the editor UI
        traceback.print_exc()
        db.set_dub_voice_job_status(order_id, f"failed:{exc}")


def _apply_segment_edit(order, job: Job, seg, text: str, resynthesize: bool) -> None:
    """Shared by the plain-form review screen and the JS editor's save calls -
    one place that knows how to correct a segment, optionally re-render its
    audio, and rebuild the deliverables that depend on it."""
    seg.edited_text = text.strip() or None
    seg.approved = True
    seg.translation_stale = False  # a human just hand-edited this English text themselves -
    # whether or not they clicked Re-translate, they've now dealt with it directly

    if resynthesize and order["tts"] != "stub":
        # A per-segment fix should render in whatever voice the rest of the
        # dub is currently using (see the dub-voice picker), not silently
        # fall back to the provider's own default and give this one
        # segment a different voice than everything around it.
        dub_voice = order["dub_voice"]
        voice_id = None
        tts_name = order["tts"]
        if dub_voice == "xtts":
            ref_path = Path(order["outdir"]) / "reference_speaker.wav"
            if ref_path.exists():
                tts_name, voice_id = "xtts", str(ref_path)
        elif dub_voice and dub_voice.startswith("piper:") and dub_voice[6:] in PIPER_VOICES:
            tts_name, voice_id = "piper", str(PROJECT_ROOT / PIPER_VOICES[dub_voice[6:]]["path"])

        tts_provider = get_tts(tts_name)
        raw = Path(order["outdir"]) / "segments" / f"{seg.segment_id}.wav"
        rendered = tts_provider.synthesize(seg.final_text.strip(), str(raw), voice_id=voice_id)
        seg.audio_path = str(raw)
        seg.rendered_duration_ms = rendered
        seg.time_stretch_pct = 0.0

        sr = tts_provider.sample_rate
        track = build_timeline(
            [(s.start_ms, s.audio_path) for s in job.segments],
            job.source_duration_ms or (job.segments[-1].end_ms if job.segments else 0),
            sample_rate=sr,
        )
        write_wav_mono(str(Path(order["outdir"]) / f"dub_{order['target_lang']}.wav"), track, sr)

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
CONTACT_EMAIL = "kahunyurogodfrey@gmail.com"

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
        return RedirectResponse("/staff" if user["role"] == "staff" else "/client")
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


def _queue_and_send(user, kind: str, subject: str, body: str) -> None:
    """Writes the message to onboarding_messages (the honest, always-there
    record) and, if a real mailer is configured, immediately attempts to
    actually send it - same "create the real record first, then try to
    deliver it, fall back gracefully" pattern as _activate_payment's
    receipt email. body is plain text; a minimal HTML version is derived
    from it (paragraph breaks only, no template/branding beyond that -
    these are meant to read like a real person's email, not a marketing
    template)."""
    message_id = db.queue_onboarding_message(user["id"], kind, subject, body)
    if mailer.email_configured():
        html = "".join(f"<p>{part}</p>" for part in body.split("\n\n") if part.strip())
        ok, detail = mailer.send_email(user["email"], subject, html, body)
        db.set_onboarding_message_email_result(message_id, ok, detail)




def _queue_welcome_message(user) -> None:
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
    _queue_and_send(user, "welcome", subject, body)


def _queue_first_payment_message(user) -> None:
    if db.has_onboarding_message(user["id"], "first_payment"):
        return
    name = (user["display_name"] or user["email"].split("@")[0]).strip()
    subject = "Thanks for trusting Kauli with a real order"
    body = (
        f"Hi {name},\n\n"
        "Thanks for the vote of confidence - that's your first paid order with Kauli, and I don't "
        "take that lightly this early on.\n\n"
        "I'll be keeping an eye on this one personally. If the turnaround or the quality isn't "
        f"what you expected, tell me directly - reply here or WhatsApp me: "
        f"https://wa.me/{CONTACT_PHONE_WHATSAPP} - and I'll make it right.\n\n"
        f"Thanks again,\n{FOUNDER_NAME}\nForge Media Services"
    )
    _queue_and_send(user, "first_payment", subject, body)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Logged-in visitors go straight to their workspace, same as before.
    # Logged-out visitors now land on the public marketing page instead of
    # being bounced straight to /login - this is the "get clients from"
    # front door, not the app itself.
    user = current_user(request)
    if user:
        return RedirectResponse("/staff" if user["role"] == "staff" else "/client")
    return templates.TemplateResponse(request, "marketing.html",
        _marketing_context(sent=request.query_params.get("sent") == "1"))


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", _marketing_context())


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _marketing_context())


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
        **_marketing_context(),
        "posts": db.list_blog_posts(published_only=True),
    })


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(request: Request, slug: str):
    post = db.get_blog_post_by_slug(slug, published_only=True)
    if not post:
        return HTMLResponse("Post not found.", status_code=404)
    author = db.get_user(post["author_id"]) if post["author_id"] else None
    return templates.TemplateResponse(request, "blog_post.html", {
        **_marketing_context(),
        "post": post,
        "author": author,
        "author_name": author["display_name"] if author else "Kauli",
        "published_at_iso": datetime.fromtimestamp(post["published_at"]).isoformat() if post["published_at"] else None,
    })


@app.get("/sitemap.xml")
def sitemap(request: Request):
    base = f"{request.url.scheme}://{request.url.netloc}"
    urls = [f"{base}/", f"{base}/terms", f"{base}/privacy", f"{base}/blog"]
    urls += [f"{base}/solutions/{slug}" for slug in SOLUTION_PAGES]
    urls += [f"{base}/blog/{p['slug']}" for p in db.list_blog_posts(published_only=True)]
    body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>'
    return Response(content=xml, media_type="application/xml")


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


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, mode: str = "signin", notice: str | None = None):
    display_notice = None
    if notice == "account_closed":
        display_notice = "Your account has been closed."
    elif notice == "password_reset":
        display_notice = "Your password has been reset - sign in with your new password."
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "notice": display_notice, "mode": "signup" if mode == "signup" else "signin",
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
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    # Brute-force guard: keyed on the email being attempted, not just IP -
    # protects one account from being hammered from many IPs, and many
    # accounts from being hammered from one. Doesn't touch the client-side
    # password policy, this is purely about attempt rate.
    allowed, retry_after = rate_limit.check(f"login:{email}", limit=8, window_s=60)
    if not allowed:
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Too many attempts - try again in {retry_after}s.", "notice": None, "mode": "signin",
        }, status_code=429)
    session, error = supabase_auth.sign_in(email, password)
    if error or not session:
        return templates.TemplateResponse(request, "login.html", {
            "error": error or "Wrong email or password.", "notice": None, "mode": "signin",
        })
    role, is_admin = _resolve_role_and_admin(email)
    user, was_new = db.get_or_create_user(session.user.id, email, default_role=role)
    if was_new:
        if is_admin:
            db.set_user_admin(user["id"], True)
        if role == "staff" and db.is_invited_staff(email):
            db.remove_staff_invite(email)  # consumed - the invite did its job
        if role == "client":  # the welcome message is written for a client, not a new staff account
            _queue_welcome_message(user)
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           marketing_consent: str = Form("")):
    email = email.strip().lower()
    policy_errors = supabase_auth.password_policy_errors(password, email=email)
    if policy_errors:
        # Rejected before ever calling Supabase - no point spending an API
        # call on a password that fails our own rules regardless of what
        # Supabase's own (looser) minimum would have allowed.
        return templates.TemplateResponse(request, "login.html", {
            "error": "Password needs " + ", ".join(policy_errors) + ".",
            "notice": None, "mode": "signup",
        })
    session, error = supabase_auth.sign_up(email, password)
    if error:
        return templates.TemplateResponse(request, "login.html", {
            "error": error, "notice": None, "mode": "signup",
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
    if was_new:
        if is_admin:
            db.set_user_admin(user["id"], True)
        if role == "staff" and db.is_invited_staff(email):
            db.remove_staff_invite(email)
        if role == "client":
            _queue_welcome_message(user)
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ------------------------------------------------------------- settings ----
@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "saved": False, "error": None,
        "theme": "dark" if user["role"] == "staff" else "light",
    })


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
@app.get("/client", response_class=HTMLResponse)
def client_dashboard(request: Request, reorder_youtube_url: str | None = None, reorder_source_lang: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    orders = db.list_orders_for_client(user["id"])
    unread = db.unread_order_ids(user["id"], include_internal=False)
    anthropic_available = bool(os.environ.get("ANTHROPIC_API_KEY"))
    subscription = db.get_subscription_current(user["id"])
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
        "wallet_minutes": db.wallet_minutes_remaining(user["id"]),
        "folders": db.list_folders_for_client(user["id"]),
        "source_languages": SOURCE_LANGUAGES,
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "form_values": reorder_form_values,
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["id"]),
        "youtube_pending_imports": db.list_pending_imports(user["id"]),
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
    subscription = db.get_subscription_current(user["id"])
    plan = billing.effective_plan(user, subscription)
    return templates.TemplateResponse(request, "client_dashboard.html", {
        "user": user,
        "orders": db.list_orders_for_client(user["id"]),
        "unread": db.unread_order_ids(user["id"], include_internal=False),
        "anthropic_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "plan": plan, "plans": billing.PLANS, "addons": billing.ADDONS,
        "service_levels": billing.SERVICE_LEVELS,
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_minutes": db.wallet_minutes_remaining(user["id"]),
        "folders": db.list_folders_for_client(user["id"]),
        "source_languages": SOURCE_LANGUAGES,
        "manual_transcription_languages": MANUAL_TRANSCRIPTION_LANGUAGES,
        "error": error,
        "exception_context": exception_context,
        "form_values": form_values or {},
        "youtube_polling_configured": youtube_poll.youtube_polling_configured(),
        "youtube_watches": db.list_youtube_watches(client_id=user["id"]),
        "youtube_pending_imports": db.list_pending_imports(user["id"]),
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
    watches = {w["id"]: w for w in db.list_youtube_watches(client_id=user["id"])}
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


@app.post("/client/orders")
def create_order(
    request: Request,
    audio: UploadFile | None = File(None),
    youtube_url: str = Form(""),
    source_lang: str = Form("sw"),
    target_lang: str = Form("en"),
    service_level: str = Form("dub"),
    addon_video_deliverables: str = Form(""),
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
        "instr_speaker_ids": instr_speaker_ids, "instr_verbatim_level": instr_verbatim_level,
        "instr_transcribe_lyrics": instr_transcribe_lyrics, "instr_use_italics": instr_use_italics,
        "instr_existing_subs": instr_existing_subs, "instr_no_audio": instr_no_audio,
        "instr_wrong_language": instr_wrong_language, "instr_instrumental_only": instr_instrumental_only,
        "instr_notes": instr_notes, "folder_name": folder_name,
        "voice_clone_consent": voice_clone_consent,
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
        existing = db.get_order_by_idempotency_key(user["id"], idempotency_key.strip())
        if existing:
            return RedirectResponse(f"/client/orders/{existing['id']}", status_code=303)
    if service_level not in billing.SERVICE_LEVELS:
        return _client_dashboard_error(request, user, "Unknown service level.", form_values=form_values)
    if source_lang not in SOURCE_LANGUAGES:
        return _client_dashboard_error(request, user, "Unknown source language.", form_values=form_values)
    if target_lang not in ("en", "sw"):
        return _client_dashboard_error(request, user, "Unknown target language.", form_values=form_values)
    addons = ["video_deliverables"] if addon_video_deliverables else []
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

    subscription = db.get_subscription_current(user["id"])
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
        asr = "faster-whisper"
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
    tts = "piper" if level["tts"] else "stub"

    # Free minutes only ever apply to a transcription-only order - see
    # billing.FREE_MINUTES_SERVICE_LEVEL. A translate/dub order pays the
    # full rate from its first minute; order_cost_usd simply isn't offered
    # any free allowance to spend for those.
    free_minutes_for_this_order = (
        billing.free_minutes_remaining(subscription) if service_level == billing.FREE_MINUTES_SERVICE_LEVEL else 0.0
    )
    cost = billing.order_cost_usd(minutes, service_level, plan, free_minutes_for_this_order,
                                   addons=addons, wallet_minutes_available=db.wallet_minutes_remaining(user["id"]))
    # order_cost_usd silently drops any addon the plan already includes -
    # reflect that back so we never store/charge for one that didn't apply.
    applied_addons = [line["key"] for line in cost["addons"]]
    # Free-tier only, never a wallet/real-money $0 order (those already
    # paid for their minutes in a top-up) - this is what gates downloads
    # below and in order_detail.html.
    is_free_preview = cost["free_minutes_applied"] > 0 and cost["wallet_minutes_applied"] <= 0 and cost["total_usd"] <= 0

    db.create_order(
        order_id=order_id,
        client_id=user["id"], original_filename=original_filename,
        audio_path=str(audio_path), source_lang=source_lang, target_lang=target_lang,
        tier=plan, asr=asr, mt=mt, tts=tts, outdir=str(outdir),
        source_youtube_id=youtube_video_id,
        idempotency_key=idempotency_key.strip() or None,
        folder_name=folder_name.strip() or None,
    )
    if youtube_video_id:
        # Closes the loop on the auto-import flow, if this happens to be
        # one of those videos - a real order now exists for it, so it's no
        # longer "pending".
        db.mark_pending_import_ordered(user["id"], youtube_video_id)
    db.set_order_billing(order_id, service_level, minutes, cost["total_usd"], addons=applied_addons,
                          cost_breakdown=cost)
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
    if cost["wallet_minutes_applied"] > 0:
        # Deducted at submission, not payment confirmation, on purpose -
        # simpler than threading the applied amount through to
        # _activate_payment for the (rare) case where a not-fully-free
        # order sits in pending_payment. Same "reserved the moment you
        # submit" behavior most prepaid-wallet systems use.
        db.consume_wallet_minutes(user["id"], cost["wallet_minutes_applied"])
        if mailer.email_configured() and db.wallet_low_alert_needed(user["id"]):
            remaining = db.wallet_minutes_remaining(user["id"])
            name = (user["display_name"] or user["email"].split("@")[0]).strip()
            body = (
                f"Hi {name},\n\n"
                f"You're down to about {remaining:.1f} prepaid minutes - just a heads up so an upcoming "
                f"order doesn't get held up waiting on a top-up.\n\n"
                f"Top up here: {str(request.base_url).rstrip('/')}/client/billing\n\n"
                f"Questions? WhatsApp me: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\nForge Media Services"
            )
            html = "".join(f"<p>{part}</p>" for part in body.split("\n\n") if part.strip())
            mailer.send_email(user["email"], "Running low on prepaid Kauli minutes", html, body)
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
        # straight to processing, same as the old free experience.
        db.add_usage_minutes(user["id"], cost["free_minutes_applied"])
        deadlines = tat.compute_deadlines(plan, service_level, minutes)
        db.set_order_deadlines(order_id, deadlines["start_at"], deadlines["internal_deadline_at"],
                                deadlines["deadline_at"])
        worker.submit_job(order_id)
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)

    # Real cost beyond the free allowance - hold the order for payment.
    # worker.submit_job() is only ever called once a payment webhook
    # confirms it (see billing_checkout / the webhook handlers below).
    db.update_order_status(order_id, "pending_payment")
    return RedirectResponse(f"/client/orders/{order_id}/pay", status_code=303)


@app.get("/client/orders/{order_id}", response_class=HTMLResponse)
def client_order_detail(request: Request, order_id: str):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["id"]:
        return HTMLResponse("Order not found.", status_code=404)

    job = _load_job(order)
    # include_internal=False here is load-bearing, not decorative - this is
    # the one line standing between a client and staff-only notes.
    messages = db.list_messages(order_id, include_internal=False)
    db.mark_read(user["id"], order_id)

    outdir = Path(order["outdir"])
    return templates.TemplateResponse(request, "order_detail.html", {
        "user": user, "order": order, "job": job, "role": "client", "messages": messages,
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "receipt": db.get_receipt_for_order(order_id),
    })


@app.post("/client/orders/{order_id}/messages")
def client_send_message(request: Request, order_id: str, body: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if body.strip():
        # visibility is hardcoded here, never read from the request - a
        # client has no form field that could set it to 'internal'.
        db.create_message(order_id, user["id"], "client", body)
        db.mark_read(user["id"], order_id)
    return RedirectResponse(f"/client/orders/{order_id}", status_code=303)


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
    if not order or order["client_id"] != user["id"]:
        return HTMLResponse("Order not found.", status_code=404)
    db.set_voice_clone_consent(order_id, request.client.host if request.client else None)
    return RedirectResponse(f"/client/orders/{order_id}", status_code=303)


# -------------------------------------------------------------- billing ----
@app.get("/client/billing", response_class=HTMLResponse)
def billing_page(request: Request, upgrade_for: str | None = None, notice: str | None = None,
                  error: str | None = None):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    subscription = db.get_subscription(user["id"])
    plan = billing.effective_plan(user, subscription)
    is_test_account = plan == "enterprise" and user["email"].strip().lower() in billing.test_client_emails()
    bonus_minutes = (subscription["bonus_minutes"] or 0.0) if subscription else 0.0
    payments = db.list_payments_for_user(user["id"])
    receipts_by_payment = {
        r["payment_id"]: r for r in db.list_receipts_for_client(user["id"])
    }
    return templates.TemplateResponse(request, "billing.html", {
        "user": user, "plans": billing.PLANS, "current_plan": plan,
        "subscription": subscription, "is_test_account": is_test_account,
        "payments": payments, "receipts_by_payment": receipts_by_payment,
        "upgrade_for": upgrade_for, "notice": notice, "error": error,
        "paystack_configured": billing.paystack_configured(),
        "mpesa_configured": billing.mpesa_configured(),
        "free_minutes_base": billing.FREE_MINUTES_PER_MONTH,
        "bonus_minutes": bonus_minutes,
        "free_minutes_total": billing.FREE_MINUTES_PER_MONTH + bonus_minutes,
        "free_minutes_remaining": billing.free_minutes_remaining(subscription),
        "wallet_minutes": db.wallet_minutes_remaining(user["id"]),
        "wallet_packages": billing.WALLET_PACKAGES,
        "service_levels": billing.SERVICE_LEVELS,
    })


@app.post("/client/billing/wallet")
def buy_wallet_minutes(request: Request, package: str = Form(""), provider: str = Form(...),
                        phone: str = Form(""), custom_minutes: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    if custom_minutes.strip():
        try:
            minutes = float(custom_minutes.strip())
        except ValueError:
            return RedirectResponse("/client/billing?error=Enter+a+real+number+of+minutes.", status_code=303)
        if not (billing.WALLET_CUSTOM_MIN_MINUTES <= minutes <= billing.WALLET_CUSTOM_MAX_MINUTES):
            return RedirectResponse(
                f"/client/billing?error=Enter+between+{billing.WALLET_CUSTOM_MIN_MINUTES}+and+"
                f"{billing.WALLET_CUSTOM_MAX_MINUTES}+minutes.", status_code=303)
        pkg = billing.wallet_custom_price(minutes)
    elif package in billing.WALLET_PACKAGES:
        pkg = billing.WALLET_PACKAGES[package]
    else:
        return RedirectResponse("/client/billing?error=Unknown+minute+package.", status_code=303)
    return _checkout(request, user, provider, "wallet", pkg["price_usd"],
                      None, phone, "/client/billing", "/client/billing",
                      payment_kind=f"wallet_topup:{pkg['minutes']}")


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
        detail = (f"{billable:.1f} of {minutes:.1f} min billed (the rest covered by free/prepaid minutes) "
                   f"× ${rate:.2f}/min")
    else:
        detail = f"{minutes:.1f} min × ${rate:.2f}/min"
    lines = [{"label": level["name"], "detail": detail, "amount_usd": cost["gross_usd"]}]
    if cost.get("discount_pct"):
        lines.append({"label": f"Plan discount ({cost['discount_pct'] * 100:.0f}%)",
                       "detail": None, "amount_usd": -cost["discount_usd"]})
    for addon in cost.get("addons", []):
        lines.append({"label": addon["name"], "amount_usd": addon["cost_usd"],
                       "detail": f"{minutes:.1f} min × ${addon['rate_per_min']:.2f}/min"})
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
        # wallet top-up) - the actual "this is a real customer now" moment,
        # same idea as the doc's "first_payment" trigger. See
        # _queue_first_payment_message; queuing, not sending - same
        # no-real-ESP-yet situation as the welcome message.
        _queue_first_payment_message(payer)
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
            db.add_usage_minutes(payment["user_id"], order["duration_minutes"] or 0)
            db.update_order_status(payment["order_id"], "queued")
            deadlines = tat.compute_deadlines(order["tier"], order["service_level"],
                                               order["duration_minutes"] or 0)
            db.set_order_deadlines(payment["order_id"], deadlines["start_at"],
                                    deadlines["internal_deadline_at"], deadlines["deadline_at"])
            order = db.get_order(payment["order_id"])  # re-fetch with the deadline fields now set
            worker.submit_job(payment["order_id"])
        level = billing.SERVICE_LEVELS.get(order["service_level"]) if order else None
        description = (f"{level['name']} - {order['original_filename']}" if order and level
                        else f"Order {payment['order_id']} - {payment['plan']} plan")
        line_items = _order_receipt_line_items(order) if order else None
    elif payment_kind.startswith("wallet_topup:"):
        minutes = float(payment_kind.split(":", 1)[1])
        db.add_wallet_minutes(payment["user_id"], minutes)
        description = f"Prepaid minutes top-up - {minutes:.0f} minutes"
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
        html = (
            f"<p>Hi {payer['display_name']},</p>"
            f"<p>Your Kauli payment has been received.</p>"
            f"<p><strong>{description}</strong><br>Total: ${payment['amount_usd']:.2f}</p>"
            + (f'<p><a href="{receipt_url}">View your receipt →</a></p>' if receipt_url else "")
            + "<p>Forge Media Services</p>"
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
        db.create_payment(payment_id, user["id"], plan, amount_usd, None, "USD", "paystack", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind}))
        callback_url = str(request.base_url).rstrip("/") + f"/billing/callback/paystack?payment_id={payment_id}"
        result = billing.paystack_initialize(user["email"], amount_usd, payment_id, callback_url)
        if "error" in result:
            db.fail_payment(payment_id)
            return RedirectResponse(f"{back_url}?error={result['error']}", status_code=303)
        return RedirectResponse(result["authorization_url"], status_code=303)

    if provider == "mpesa":
        if not billing.mpesa_configured():
            return RedirectResponse(f"{back_url}?error=M-Pesa+isn%27t+configured+yet.", status_code=303)
        if not phone.strip():
            return RedirectResponse(f"{back_url}?error=Enter+the+M-Pesa+phone+number.", status_code=303)
        amount_kes, rate_source = billing.usd_to_kes(amount_usd)
        db.create_payment(payment_id, user["id"], plan, amount_usd, amount_kes, "KES", "mpesa",
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
        db.create_payment(payment_id, user["id"], plan, amount_usd, None, "USD", "bank", order_id=order_id,
                           meta=json.dumps({"kind": payment_kind}))
        return RedirectResponse(f"{back_url}?notice=Bank+transfer+request+received+-+"
                                 f"see+instructions+below%2C+reference+{payment_id}.", status_code=303)

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
    if not order or order["client_id"] != user["id"]:
        return HTMLResponse("Order not found.", status_code=404)
    return templates.TemplateResponse(request, "order_pay.html", {
        "user": user, "order": order, "notice": notice, "error": error,
        "paystack_configured": billing.paystack_configured(),
        "mpesa_configured": billing.mpesa_configured(),
        "has_video_addon": db.order_has_addon(order, "video_deliverables"),
    })


@app.post("/client/orders/{order_id}/pay")
def order_pay_checkout(request: Request, order_id: str, provider: str = Form(...), phone: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["id"]:
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
    if not order or order["client_id"] != user["id"]:
        return HTMLResponse("Order not found.", status_code=404)
    return templates.TemplateResponse(request, "order_surcharge_pay.html", {
        "user": user, "order": order, "notice": notice, "error": error,
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
    if not order or order["client_id"] != user["id"]:
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
        return RedirectResponse("/login")
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        return HTMLResponse("Receipt not found.", status_code=404)
    if user["role"] != "staff" and receipt["client_id"] != user["id"]:
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
            f"{FOUNDER_NAME}\nForge Media Services"
        )
        _queue_and_send(client, "inactivity_nudge", subject, body)
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
def staff_lead_detail(request: Request, lead_id: str):
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
    if user["role"] == "client" and order["client_id"] != user["id"]:
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
    if user["role"] == "client" and order["client_id"] != user["id"]:
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
        plan = billing.effective_plan(user, db.get_subscription(user["id"]))
        has_video = billing.PLANS[plan]["video_deliverables"] or db.order_has_addon(order, "video_deliverables")
        if kind in ("burned", "dubbed") and not has_video:
            return RedirectResponse("/client/billing?upgrade_for=video", status_code=303)

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
@app.get("/staff", response_class=HTMLResponse)
def staff_dashboard(request: Request):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    orders = db.list_all_orders()
    unread = db.unread_order_ids(user["id"], include_internal=True)
    return templates.TemplateResponse(request, "staff_dashboard.html", {
        "user": user, "orders": orders, "unread": unread,
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
def staff_review(request: Request, order_id: str):
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

    return templates.TemplateResponse(request, "staff_review.html", {
        "user": user, "order": order, "job": job,
        "client_messages": client_messages, "internal_messages": internal_messages,
        "has_video_source": has_video_source,
        "burned_ready": (outdir / f"burned_captions_{order['target_lang']}.mp4").exists(),
        "dubbed_ready": (outdir / f"dubbed_video_{order['target_lang']}.mp4").exists(),
        "return_reasons": db.RETURN_REASONS,
        "workflow_steps": workflow_steps_for_order(order),
        "difficulty_rate": difficulty_rate,
        "difficulty_threshold": DIFFICULTY_SURCHARGE_THRESHOLD,
        "suggested_surcharge_pct": DIFFICULTY_SURCHARGE_DEFAULT_PCT,
        "suggested_surcharge_usd": suggested_surcharge_usd,
        "extra_charge_reasons": db.EXTRA_CHARGE_REASONS,
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
                        body: str = Form(...), visibility: str = Form(...)):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    if body.strip() and visibility in ("client", "internal"):
        db.create_message(order_id, user["id"], visibility, body)
        db.mark_read(user["id"], order_id)
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
            body = (f"Hi {(client['display_name'] or client['email'].split('@')[0]).strip()},\n\n"
                    f"Your order ({order['original_filename']}) was returned without completing - "
                    f"we need something from you before we can continue.{reason_text}\n\n"
                    f"See the details and reply here: {link}\n\n"
                    f"Or WhatsApp me directly: https://wa.me/{CONTACT_PHONE_WHATSAPP}\n\nForge Media Services")
            html = "".join(f"<p>{part}</p>" for part in body.split("\n\n") if part.strip())
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
    dead-lettering again after one more try."""
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("Order not found.", status_code=404)
    db.reset_retry_count(order_id)
    db.update_order_status(order_id, "queued")
    worker.submit_job(order_id)
    return RedirectResponse(f"/staff/orders/{order_id}", status_code=303)


@app.post("/staff/orders/{order_id}/approve")
def staff_approve(request: Request, order_id: str, next: str = Form("")):
    user = current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
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
def staff_editor(request: Request, order_id: str):
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
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
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
            "source_cells": _build_source_cells(s),
            "target_cells": _build_target_cells(s),
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
    return templates.TemplateResponse(request, "editor.html", {
        "user": user, "order": order, "job": job,
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


@app.post("/staff/orders/{order_id}/segments/{segment_id}/source")
def editor_save_source(request: Request, order_id: str, segment_id: str, body: dict = Body(...)):
    """Step 1 of the editor's workflow: correct the Swahili ASR transcript
    itself. Kept separate from the translation-edit endpoint on purpose -
    this doesn't touch the English text or re-render anything by itself."""
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
    if changed:
        # The English currently shown was translated from whatever the
        # source said BEFORE this edit - flag it stale rather than silently
        # leaving a translation of text that no longer exists in step 1
        # looking exactly as "done" as one that's actually current. See
        # editor_retranslate/editor_save_target for where this clears.
        seg.translation_stale = True
    # Regenerate the actual delivered transcript file too, not just the
    # manifest - _apply_segment_edit already does this for the target-
    # language subtitle files on every save; this endpoint didn't, so a
    # saved Swahili correction never reached transcript_{lang}.srt until
    # someone happened to trigger a full re-render some other way.
    (Path(order["outdir"]) / f"transcript_{order['source_lang']}.srt").write_text(
        to_srt(job, source=True), encoding="utf-8")
    job.save(str(Path(order["outdir"]) / "manifest.json"))
    return JSONResponse({"ok": True, "source_final_text": seg.source_final_text,
                          "translation_stale": seg.translation_stale})


@app.post("/staff/orders/{order_id}/segments/{segment_id}/retranslate")
def editor_retranslate(request: Request, order_id: str, segment_id: str):
    """Step 1 -> step 2 of the editor's workflow: re-run MT on the (likely
    just-corrected) Swahili source, using the exact same translate_segment()
    the main pipeline uses - not a second, drifting copy of that logic."""
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

    mt_provider = get_mt(order["mt"])
    cps = timing.DEFAULT_CPS.get(order["target_lang"], 14.0)
    translate_segment(seg, mt_provider, order["source_lang"], order["target_lang"], cps)
    seg.translation_stale = False  # back in sync - this translation IS of the current source

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
    })


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


def _deadline_watch_loop() -> None:
    while True:
        time.sleep(DEADLINE_WATCH_INTERVAL_S)
        try:
            _deadline_watch_once()
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not kill the loop forever
            api_log.warning("deadline watch sweep failed", extra={"error": str(exc)})


threading.Thread(target=_deadline_watch_loop, daemon=True).start()
