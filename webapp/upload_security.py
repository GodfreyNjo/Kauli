"""File upload security: everything a raw client upload has to pass before
Kauli treats it as legitimate media.

Context: the enterprise blueprint this was built against (pre-signed S3
URLs, Lambda-triggered AV scanning, per-job Kubernetes/Docker sandboxing,
IAM least-privilege) assumes real cloud infrastructure - this app is a
single-process local prototype with no AWS/GCP account, no S3, no
Kubernetes. Rather than wire in non-functional references to
infrastructure that doesn't exist here (which would look secure without
doing anything), this implements the equivalent LOCAL, FUNCTIONAL defenses:
filename/path safety, extension + magic-byte + ffprobe validation, a real
server-side size cap, a real ClamAV scan (installed locally - see
README/ops notes), and an audit trail. Defense in depth still applies
without a cloud provider's building blocks.

What this deliberately does NOT attempt: per-job container sandboxing
(would need real container orchestration to do properly - a fragile
home-grown approximation is worse than being honest that it isn't there
yet) and cloud IAM segregation (no cloud account to scope). Both are real
next steps once this actually deploys to real infrastructure, not
something to fake locally.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB - generous for audio/video, still a real, enforced cap
CHUNK_SIZE = 1024 * 1024  # stream in 1MB chunks - never buffer a whole upload in memory

ALLOWED_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".flac", ".ogg", ".webm", ".aac", ".wma", ".avi",
}
ALLOWED_STYLE_GUIDE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".png", ".jpg", ".jpeg",
}

# Magic bytes (file signatures) for the containers we actually accept -
# never trust a file's extension alone, it's just a string the uploader
# chose. This is the "is this .mp3 actually a decoy for something else"
# check (the doc's "Malware Guard" / anti-polyglot item).
_SIMPLE_SIGNATURES = [
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"),  # MP3 frame sync, no ID3 tag
    (b"RIFF", "riff"),          # WAV (also AVI - both are RIFF containers, ffprobe disambiguates later)
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"\x1a\x45\xdf\xa3", "ebml"),  # Matroska / WebM
    (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", "asf"),  # WMA/WMV (ASF container)
]
_ISO_BMFF_OFFSET = 4
_ISO_BMFF_MAGIC = b"ftyp"  # mp4/m4a/mov all share this - the brand tag sits at byte 4, not byte 0


class UploadRejected(Exception):
    """Message is written to be shown to the client as-is - keep it free
    of internal paths/details."""


def sniff_container(head: bytes) -> str | None:
    for sig, name in _SIMPLE_SIGNATURES:
        if head.startswith(sig):
            return name
    if len(head) >= 8 and head[_ISO_BMFF_OFFSET:_ISO_BMFF_OFFSET + 4] == _ISO_BMFF_MAGIC:
        return "iso-bmff"
    return None


def safe_stored_filename(original_filename: str, allowed_extensions: set[str]) -> tuple[str, str]:
    """Never build a filesystem path from a client-supplied filename
    directly - `../../etc/passwd`-style path traversal and null-byte
    tricks are real attacks against exactly this pattern. Returns a fresh
    UUID-based disk filename; the original name is for display only,
    stored separately in the DB. Raises UploadRejected if the extension
    isn't on the allowlist."""
    ext = Path(original_filename or "").suffix.lower()
    if ext not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise UploadRejected(f"That file type isn't accepted. Allowed: {allowed}")
    return f"{uuid.uuid4().hex}{ext}", ext


def stream_save_with_limits(upload_file, dest_path: Path, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[int, str, bytes]:
    """Streams an UploadFile to disk in chunks, hashing as it goes,
    enforcing a hard server-side size cap (the client-side `accept`/size
    check is just UX - trivially bypassed with a raw HTTP request).
    Returns (total_bytes, sha256_hex, first_chunk_for_magic_sniffing).
    Removes the partial file and raises UploadRejected if the cap is hit."""
    hasher = hashlib.sha256()
    total = 0
    first_chunk = b""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = upload_file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                if not first_chunk:
                    first_chunk = chunk
                total += len(chunk)
                if total > max_bytes:
                    raise UploadRejected(
                        f"File is larger than the {max_bytes // (1024 * 1024 * 1024)}GB limit.")
                hasher.update(chunk)
                out.write(chunk)
    except UploadRejected:
        dest_path.unlink(missing_ok=True)
        raise
    return total, hasher.hexdigest(), first_chunk


def probe_media(path: str, timeout_s: int = 30) -> dict | None:
    """Runs ffprobe (already a project dependency, no new install) and
    returns its parsed JSON, or None if the file isn't something ffprobe
    can actually make sense of - catches corrupt containers, decoys, and
    the "claims to be audio but has no audio track" case. The timeout
    guards against a file deliberately crafted to make ffprobe hang."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=timeout_s,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def has_valid_av_stream(probe: dict | None) -> bool:
    if not probe:
        return False
    return any(s.get("codec_type") in ("audio", "video") for s in probe.get("streams", []))


def clamav_available() -> bool:
    return shutil.which("clamdscan") is not None or shutil.which("clamscan") is not None


def scan_for_malware(path: str, timeout_s: int = 120) -> tuple[bool, str]:
    """Returns (clean, detail). Prefers clamdscan (talks to the always-on
    clamd daemon - fast, no per-call signature-DB load) and falls back to
    clamscan (loads ~28k signatures from disk every invocation - correct
    but slow, 10-30s+) only if the daemon isn't reachable.

    Parses stdout text ("... FOUND" vs "... OK") rather than trusting the
    process exit code - verified empirically against this ClamAV build
    that clamdscan's exit code does NOT reliably distinguish clean from
    infected (returned 0 in both cases in testing), so exit code alone
    would be a false sense of security here.

    If ClamAV isn't installed at all, returns (True, "not scanned - ...")
    rather than blocking every upload outright - see the caller in
    app.py for why that's a deliberate fail-open, not a silent gap."""
    scanner = shutil.which("clamdscan") or shutil.which("clamscan")
    if not scanner:
        return True, "not scanned - ClamAV not installed"
    try:
        result = subprocess.run(
            [scanner, "--no-summary", path],
            capture_output=True, timeout=timeout_s, text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if "FOUND" in output:
            detail = output.strip().splitlines()[-1] if output.strip() else "malware signature matched"
            return False, detail
        if "OK" in output or result.returncode == 0:
            return True, "clean"
        return True, f"scan inconclusive (exit {result.returncode}) - not blocked, see server logs"
    except subprocess.SubprocessError:
        return True, "scan timed out - not blocked, see server logs"
    except OSError:
        return True, "scan failed to run - not blocked, see server logs"


def sanitize_media_copy(src_path: str, dest_path: str, timeout_s: int = 300) -> bool:
    """Best-effort: re-mux through ffmpeg into a clean copy with all
    container-level metadata stripped and only the audio/video streams
    kept (subtitle/data streams and ID3/chapter metadata dropped
    entirely) - exactly the "hide code in ID3 tags / subtitle tracks"
    attack surface the blueprint calls out. Stream-copy only (-c copy),
    no re-encode, so it's fast and lossless when it works.

    Deliberately non-fatal: some legitimate files won't cleanly stream-
    copy across every codec/container combination this app accepts, and
    a security nicety should never be the reason a real client's real
    file gets rejected. By the time this runs the file has ALREADY passed
    extension + magic-byte + ffprobe + ClamAV checks, so falling back to
    the original on failure is a reasonable, bounded risk - not "skip all
    validation." Returns False (caller keeps the original) on any
    failure."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
             "-map", "0:v?", "-map", "0:a?",
             "-map_metadata", "-1", "-map_chapters", "-1",
             "-c", "copy", dest_path],
            check=True, timeout=timeout_s, capture_output=True,
        )
        return Path(dest_path).exists() and Path(dest_path).stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        Path(dest_path).unlink(missing_ok=True)
        return False


# ------------------------------------------------------ content safety ----
# Real, local, free - same "download a model once, run it offline forever"
# pattern as faster-whisper/Piper/LocalMT, not a fake reference to a cloud
# moderation API this app doesn't have credentials for. Falconsai/
# nsfw_image_detection is a real, published HuggingFace model (ViT-based,
# binary normal/nsfw classifier) - confirmed working locally before this
# was wired in, not assumed to work.
#
# Scope, honestly: this scans VIDEO frames for visual nudity/explicit
# imagery. It does not and cannot guarantee zero explicit content gets
# through - an image classifier has real false negatives (only a handful
# of frames sampled, not every one) AND real false positives. Confirmed
# empirically before shipping: a real legitimate client upload (surgical
# training footage) scored 0.999 "nsfw" - skin tone, blood and close-up
# body content is a well-known false-positive class for these models, and
# it's directly in the path of Kauli's own target clients (NGOs, medical/
# health e-learning). That's why a flag here HOLDS an order for a human's
# call (see app.py/worker.py) instead of auto-rejecting the upload - a
# classifier alone should never be the thing that turns away a real
# client. Audio-only uploads have no visual content to scan - see
# kauli/pipeline.py's transcript-based keyword flag for that surface
# instead, which runs after transcription since there's no way to know
# what an audio file contains before that.
NSFW_MODEL_NAME = "Falconsai/nsfw_image_detection"
NSFW_SCORE_THRESHOLD = 0.7   # the model's own confidence in the "nsfw" label
NSFW_SAMPLE_FRAMES = 5       # evenly spaced through the video - cost vs. coverage tradeoff

_nsfw_classifier = None
_nsfw_classifier_load_failed = False


def _get_nsfw_classifier():
    """Lazy singleton, loaded once per process - same pattern as
    FasterWhisperASR._load(). Fails open (returns None) rather than
    crashing every video upload if the model can't load (no network on
    first run, disk full, etc.) - logged so it's visible, not silent."""
    global _nsfw_classifier, _nsfw_classifier_load_failed
    if _nsfw_classifier is not None:
        return _nsfw_classifier
    if _nsfw_classifier_load_failed:
        return None
    try:
        from transformers import pipeline
        _nsfw_classifier = pipeline("image-classification", model=NSFW_MODEL_NAME)
        return _nsfw_classifier
    except Exception as exc:  # noqa: BLE001 - any load failure should fail open, not crash uploads
        print(f"WARNING: NSFW image classifier failed to load ({exc}) - video content-safety "
              "scanning is DISABLED until this is fixed. Uploads are not blocked on this.", flush=True)
        _nsfw_classifier_load_failed = True
        return None


def extract_sample_frames(video_path: str, n: int = NSFW_SAMPLE_FRAMES, timeout_s: int = 60) -> list[str]:
    """Pulls n evenly-spaced frames as temp JPEGs using ffmpeg (already a
    project dependency). Returns paths to extracted frames - caller is
    responsible for cleaning them up. Best-effort: a file that ffmpeg
    can't seek into cleanly just yields fewer (or zero) frames rather than
    raising, since this runs after ffprobe has already confirmed the file
    has a real stream."""
    if not shutil.which("ffmpeg"):
        return []
    probe = probe_media(video_path)
    duration = 0.0
    try:
        duration = float((probe or {}).get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        pass
    if duration <= 0:
        return []
    frames = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="kauli_nsfw_"))
    for i in range(n):
        t = duration * (i + 1) / (n + 1)
        out_path = tmp_dir / f"frame_{i:02d}.jpg"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video_path,
                 "-frames:v", "1", "-q:v", "4", str(out_path)],
                capture_output=True, timeout=timeout_s,
            )
            if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                frames.append(str(out_path))
        except (subprocess.SubprocessError, OSError):
            continue
    return frames


def scan_video_for_explicit_content(video_path: str) -> tuple[bool, float, str]:
    """Returns (flagged, max_nsfw_score, detail). Samples frames, runs each
    through the real local classifier, flags on the single worst frame -
    matches how a human reviewer would react to a video (one bad frame is
    enough), not an average across the whole clip which would dilute a
    real hit. If the classifier isn't available, fails open the same way
    scan_for_malware does - see _get_nsfw_classifier's docstring."""
    classifier = _get_nsfw_classifier()
    if classifier is None:
        return False, 0.0, "not scanned - content-safety classifier not loaded"

    frame_paths = extract_sample_frames(video_path)
    if not frame_paths:
        return False, 0.0, "not scanned - couldn't extract sample frames"

    max_score = 0.0
    try:
        for frame_path in frame_paths:
            try:
                results = classifier(frame_path)
            except Exception:  # noqa: BLE001 - one bad frame shouldn't abort the whole scan
                continue
            for r in results:
                if r.get("label") == "nsfw":
                    max_score = max(max_score, r.get("score", 0.0))
    finally:
        for frame_path in frame_paths:
            Path(frame_path).unlink(missing_ok=True)
        # frame_paths all share one tmp dir (see extract_sample_frames) - clean it up too
        if frame_paths:
            shutil.rmtree(Path(frame_paths[0]).parent, ignore_errors=True)

    flagged = max_score >= NSFW_SCORE_THRESHOLD
    detail = f"max nsfw score {max_score:.3f} across {len(frame_paths)} sampled frames"
    return flagged, max_score, detail


MAX_AVATAR_BYTES = 8 * 1024 * 1024  # 8MB - plenty for a profile photo

_IMAGE_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "riff"),  # WebP is RIFF+"WEBP" at offset 8, checked below
]


def sniff_image_type(head: bytes) -> str | None:
    """Real content-type from magic bytes, for when a client-supplied
    Content-Type header can't be trusted (it's just a string the browser
    sends, easily spoofed with a raw request) - see the avatar upload
    route, which used to trust it outright."""
    for sig, mime in _IMAGE_SIGNATURES:
        if head.startswith(sig):
            if mime == "riff":
                return "image/webp" if head[8:12] == b"WEBP" else None
            return mime
    return None


MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # 50MB - generous for a style guide / reference doc


def validate_generic_upload(upload_file, dest_path: Path, original_filename: str,
                             allowed_extensions: set[str], max_bytes: int = MAX_DOCUMENT_BYTES) -> dict:
    """Lighter path for non-media uploads (style guides, reference docs) -
    no ffprobe/magic-byte media check (doesn't apply), but the same
    filename safety, size cap, hashing, and ClamAV scan as a real media
    upload. PDFs and Word docs are a classic malware vector (embedded
    macros, crafted PDF JS) - skipping AV scanning here just because it
    isn't audio/video would be a real gap."""
    audit = {"original_filename": original_filename, "rejected": False, "reject_reason": None}
    try:
        stored_name, ext = safe_stored_filename(original_filename, allowed_extensions)
        final_path = dest_path.parent / stored_name
        audit["stored_filename"] = stored_name
        audit["extension"] = ext

        size, sha256, _head = stream_save_with_limits(upload_file, final_path, max_bytes)
        audit.update(size_bytes=size, sha256=sha256)

        clean, detail = scan_for_malware(str(final_path))
        audit["clamav_clean"] = clean
        audit["clamav_detail"] = detail
        if not clean:
            final_path.unlink(missing_ok=True)
            raise UploadRejected(
                "This file was flagged by malware scanning and can't be accepted. "
                "If you believe this is a mistake, contact us.")

        audit["final_path"] = str(final_path)
        return audit
    except UploadRejected as exc:
        audit["rejected"] = True
        audit["reject_reason"] = str(exc)
        raise


def validate_media_upload(upload_file, dest_path: Path, original_filename: str) -> dict:
    """The full local pipeline for one media upload, in order - cheapest/
    fastest checks first so an obviously-bad upload fails before paying
    for ffprobe or a virus scan:
      1. extension allowlist + safe on-disk filename
      2. streamed save with a hard size cap + SHA-256 hash
      3. magic-byte sniff vs the claimed extension
      4. ffprobe: is this actually a well-formed file with a real A/V stream
      5. ClamAV scan
      6. content-safety scan (video files only - see scan_video_for_explicit_content)
      7. best-effort metadata-stripping remux

    Raises UploadRejected (safe to show the client) on any hard failure,
    always cleaning up whatever partial file it created first. Returns an
    audit dict on success - see db.log_upload_audit."""
    audit = {"original_filename": original_filename, "rejected": False, "reject_reason": None}
    try:
        stored_name, ext = safe_stored_filename(original_filename, ALLOWED_MEDIA_EXTENSIONS)
        final_path = dest_path.parent / stored_name
        audit["stored_filename"] = stored_name
        audit["extension"] = ext

        size, sha256, head = stream_save_with_limits(upload_file, final_path)
        audit.update(size_bytes=size, sha256=sha256)

        container = sniff_container(head)
        audit["magic_sniff"] = container
        # RIFF covers both WAV and AVI, ASF covers both WMA and WMV - close
        # enough of a match to the allowed extension set that a stricter
        # per-extension mapping would just create false-positive rejections;
        # ffprobe below is the real, authoritative check.
        if container is None:
            final_path.unlink(missing_ok=True)
            raise UploadRejected(
                "That file's contents don't look like a real audio/video file, "
                "even though its name suggests one - upload rejected.")

        probe = probe_media(str(final_path))
        audit["ffprobe_ok"] = probe is not None
        if not has_valid_av_stream(probe):
            final_path.unlink(missing_ok=True)
            raise UploadRejected(
                "Couldn't find an actual audio or video track in that file - "
                "it may be corrupt, empty, or not really a media file.")

        clean, detail = scan_for_malware(str(final_path))
        audit["clamav_clean"] = clean
        audit["clamav_detail"] = detail
        if not clean:
            final_path.unlink(missing_ok=True)
            raise UploadRejected(
                "This file was flagged by malware scanning and can't be accepted. "
                "If you believe this is a mistake, contact us.")

        # Video files only - audio-only uploads have no visual content to
        # scan (see kauli/pipeline.py's transcript-based check for that
        # surface instead, which runs after transcription).
        #
        # Deliberately NOT an auto-reject, unlike malware above - real
        # testing against a real client upload (legitimate surgical
        # training footage) scored 0.999 "nsfw" on this classifier. Skin
        # tone, blood and close-up body content is a well-documented false-
        # positive class for these models, and Kauli's own target audience
        # (NGOs, e-learning/medical training) is exactly who'd hit it. A
        # flag here holds the order for a human's judgment call before
        # delivery (see app.py's create_order / worker.py's final-status
        # check) instead of silently rejecting a legitimate client outright.
        has_video_stream = any(s.get("codec_type") == "video" for s in (probe or {}).get("streams", []))
        if has_video_stream:
            nsfw_flagged, nsfw_score, nsfw_detail = scan_video_for_explicit_content(str(final_path))
            audit["content_safety_flagged"] = nsfw_flagged
            audit["content_safety_detail"] = nsfw_detail

        sanitized_path = final_path.with_name(f"{final_path.stem}_clean{final_path.suffix}")
        if sanitize_media_copy(str(final_path), str(sanitized_path)):
            final_path.unlink(missing_ok=True)
            sanitized_path.rename(final_path)
            audit["metadata_stripped"] = True
        else:
            sanitized_path.unlink(missing_ok=True)
            audit["metadata_stripped"] = False

        audit["final_path"] = str(final_path)
        return audit
    except UploadRejected as exc:
        audit["rejected"] = True
        audit["reject_reason"] = str(exc)
        raise
