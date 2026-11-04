"""Nothing is transcribed twice. When a customer orders something new for a file we already transcribed for them (a translation, a dub, a
human check), the new order starts from the transcript they already received, including every correction they made, and goes straight to
the new work.

How an earlier order is found, in order of preference:
  1. the customer pressed "Do more with this file" on a finished order (create_order's reuse_order_id),
  2. the same file bytes were sent before by the same account (a SHA-256 of the file), or the same YouTube video, in the same language.
Only orders that finished (ready or delivered) and have real transcript text are used, and only the same account's own orders.

The new order gets a manifest seeded from the old one: timings, speakers (each line's speaker and the job's speaker voices, names and
subtitle choices) and the delivered source text are kept exactly as the editor left them, with the formatting they did (speaker names typed
into lines, paragraph breaks, italics, sound tags), so no work is done twice. Everything downstream (translation, audio, edits to the old
translation) starts clean. worker._run_job then runs the pipeline in resume mode, which skips speech recognition because every segment
already has its text.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from kauli.models import Job

from . import db

DONE = ("ready_for_delivery", "delivered")


def file_sha256(path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load(order):
    try:
        manifest = Path(order["outdir"]) / "manifest.json"
        return Job.load(str(manifest)) if manifest.exists() else None
    except Exception:  # noqa: BLE001 - an unreadable manifest simply means "nothing to reuse"
        return None


def _has_text(job) -> bool:
    speech = [s for s in (job.segments if job else []) if getattr(s, "segment_type", "speech") != "gap"]
    return bool(speech) and all((s.source_final_text or "").strip() for s in speech)


def usable(order, client_id: str, source_lang: str) -> bool:
    """A finished order of this account, in this language, whose transcript is complete."""
    if not order or order["client_id"] != client_id or order["status"] not in DONE or (order["source_lang"] or "") != source_lang:
        return False
    return _has_text(_load(order))


def pick(client_id: str, source_lang: str, explicit=None, sha256: str | None = None, youtube_id: str | None = None):
    """The earlier order whose transcript the new order should start from, or None."""
    if explicit is not None and usable(explicit, client_id, source_lang):
        return explicit
    conn = db.get_conn()
    try:
        rows = []
        if sha256:
            rows += conn.execute(
                "SELECT * FROM orders WHERE client_id = ? AND source_sha256 = ? AND status IN ('ready_for_delivery', 'delivered') ORDER BY created_at DESC",
                (client_id, sha256)).fetchall()
        if youtube_id:
            rows += conn.execute(
                "SELECT * FROM orders WHERE client_id = ? AND source_youtube_id = ? AND status IN ('ready_for_delivery', 'delivered') ORDER BY created_at DESC",
                (client_id, youtube_id)).fetchall()
    finally:
        conn.close()
    for row in rows:
        if usable(row, client_id, source_lang):
            return row
    return None


def _clean(seg):
    """A copy of a delivered segment that keeps what was heard (and any correction) and forgets everything downstream."""
    s = copy.deepcopy(seg)
    if getattr(s, "segment_type", "speech") == "gap":
        return s
    s.source_transcript = seg.source_final_text            # the delivered text: the customer's corrections win
    # The human's corrected text stays AS the correction. The editor rebuilds a corrected line from that text (its speaker name, paragraph
    # breaks, italics and sound tags), where a line with no correction is rebuilt from the recogniser's original words, which have none of it.
    s.source_edited_transcript = seg.source_edited_transcript
    s.literal, s.spoken, s.alternates, s.selected = "", "", [], "spoken"
    s.translation_confidence, s.cultural_notes = 0.0, None
    s.est_duration_ms, s.fit_ratio, s.fit_status = 0, 0.0, "unknown"
    s.voice_id, s.audio_path, s.rendered_duration_ms, s.time_stretch_pct, s.manual_pace_pct, s.spell_out = None, None, None, 0.0, 0.0, False
    s.review_flag, s.review_reasons, s.edited_text, s.approved, s.translation_stale, s.notes = False, [], None, False, False, None
    s.para_start_target = bool(getattr(s, "para_start_source", False))             # a paragraph the editor started stays a paragraph in the translation
    return s


def seed_manifest(order) -> bool:
    """Write the new order's manifest from the earlier order it reuses (order["reuse_of"]). True when it was seeded."""
    earlier_id = order["reuse_of"] if "reuse_of" in order.keys() else None
    if not earlier_id:
        return False
    earlier = db.get_order(earlier_id)
    if not earlier or earlier["client_id"] != order["client_id"]:
        return False
    job = _load(earlier)
    if not _has_text(job):
        return False
    seeded = Job(source_path=order["audio_path"] or "", source_language=order["source_lang"], target_language=order["target_lang"],
                 segments=[_clean(s) for s in job.segments], providers={"asr": "reused", "mt": order["mt"] or "stub", "tts": order["tts"] or "stub"},
                 source_duration_ms=job.source_duration_ms,
                 # who is who, carried as the editor set it: a speaker's voice, the voice name for Azure, and which speakers are in the subtitles
                 speaker_voices=dict(job.speaker_voices), speaker_voice_names=dict(job.speaker_voice_names),
                 speaker_subtitle_included=dict(job.speaker_subtitle_included),
                 # a transcript an editor already worked on keeps the paragraphs they chose; one that was never opened gets "a new speaker, a new paragraph"
                 speaker_paragraphs_applied=bool(job.speaker_paragraphs_applied))
    out = Path(order["outdir"])
    out.mkdir(parents=True, exist_ok=True)
    seeded.save(str(out / "manifest.json"))
    return True
