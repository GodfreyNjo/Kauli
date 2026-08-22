"""End-to-end orchestration: audio in, dubbed audio + subtitles + manifest out.

Every stage writes back to the manifest so you can resume, inspect or hand a
half-finished job to the review editor. Don't add a database until this hurts.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

from . import timing
from .models import Job
from .mixer import build_timeline, extract_reference_clip, time_stretch, write_wav_mono
from .providers import get_asr, get_mt, get_tts
from .subtitles import to_srt, to_vtt

FLAG_CONF_ASR = 0.85     # below this, a human looks at it
FLAG_CONF_MT = 0.80

# Explicit-content safety net for AUDIO. There's no visual content to scan
# before transcription the way webapp/upload_security.py's real NSFW image
# classifier scans video frames - the only signal available this early is
# the words themselves, once ASR has produced them. This is a coarse,
# honest keyword heuristic, not a claim of reliable detection: real
# explicit content won't always use these exact words, and legitimate
# content (medical, educational, news reporting on assault) can trip a
# keyword match without being explicit at all. That's exactly why a match
# here FLAGS for a human editor's judgment call (same review queue as a
# low-confidence transcript) instead of silently blocking or auto-rejecting
# anything - a keyword list should never be the thing that makes a final
# call on its own.
EXPLICIT_CONTENT_KEYWORDS = {
    "porn", "pornographic", "pornography", "xxx video",
    "explicit sex", "hardcore sex", "child porn", "childporn", "cp video",
    "beheading video", "snuff film", "gore video",
}


def contains_explicit_content_keywords(text: str) -> bool:
    lowered = (text or "").lower()
    return any(kw in lowered for kw in EXPLICIT_CONTENT_KEYWORDS)


def probe_duration_ms(path: str) -> int:
    try:
        with wave.open(path, "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:
        return 0


def translate_segment(seg, mt_provider, source_lang: str, target_lang: str, cps: float) -> None:
    """Translate one segment and pick the best-fitting candidate, mutating
    it in place. Pulled out of run()'s main loop so the webapp editor's
    "re-translate from corrected Swahili" action can call the exact same
    logic (via seg.source_final_text, which picks up a staff correction to
    the source transcript) instead of a second copy of it drifting apart."""
    seg.target_language = target_lang
    seg.budget_ms = seg.duration_ms
    target_chars = max(10, int(seg.budget_ms / 1000 * cps))

    r = mt_provider.translate(seg.source_final_text, target_chars, source_lang, target_lang)
    seg.literal = r.get("literal") or ""
    seg.translation_confidence = float(r.get("confidence") or 0.0)
    seg.cultural_notes = r.get("notes")

    candidates = [
        {"text": r.get("spoken"), "similarity": 0.97, "variant": "spoken"},
        {"text": r.get("shorter"), "similarity": 0.90, "variant": "shorter"},
        {"text": r.get("longer"), "similarity": 0.95, "variant": "longer"},
        {"text": r.get("literal"), "similarity": 1.00, "variant": "literal"},
    ]
    chosen, scored = timing.choose_candidate(candidates, seg.budget_ms, target_lang, cps)

    seg.spoken = chosen.get("text", "")
    seg.selected = chosen.get("variant", "spoken")
    seg.est_duration_ms = chosen.get("est_duration_ms", 0)
    seg.fit_ratio = chosen.get("fit_ratio", 0.0)
    seg.fit_status = chosen.get("fit_status", "unknown")
    seg.alternates = [
        {k: c[k] for k in ("text", "variant", "est_duration_ms", "fit_ratio")}
        for c in scored if c.get("variant") != seg.selected
    ]
    # A re-translate should clear whatever edited_text was sitting on top of
    # the PREVIOUS translation - otherwise the human edit still "wins" via
    # final_text and the fresh translation silently never shows up.
    seg.edited_text = None

    # ---- review triage ----
    # `is not None` on purpose, not plain truthiness - a confidence of
    # exactly 0.0 is the WORST case (Whisper suppressed the segment's text
    # entirely - see providers/asr.py's no_speech_threshold note) and most
    # needs a human's attention, but `if seg.source_confidence and ...`
    # treats 0.0 as "no value" and short-circuits past the flag, silently
    # letting the single worst case skip review instead of catching it.
    reasons = []
    if seg.source_confidence is not None and seg.source_confidence < FLAG_CONF_ASR:
        reasons.append("low_asr_confidence")
    if seg.translation_confidence is not None and seg.translation_confidence < FLAG_CONF_MT:
        reasons.append("low_mt_confidence")
    # Distinct from low_asr_confidence on purpose - "nothing here at all"
    # (Whisper suppressed the whole segment) needs a human to transcribe
    # it from scratch, not just proofread a shaky guess, and deserves a
    # signal that says so plainly rather than looking like an ordinary
    # low-confidence dot on a segment that has actual words to check.
    if not (seg.source_transcript or "").strip() and seg.duration_ms > 1500:
        reasons.append("empty_transcript")
    if seg.fit_status in ("needs_shortening", "needs_lengthening"):
        reasons.append("duration_overflow")
    if seg.fit_status == "unfittable":
        reasons.append("unfittable")
    # Coarse keyword heuristic (see EXPLICIT_CONTENT_KEYWORDS above) - a
    # match here means a human editor makes the actual call before
    # anything ships, same as any other flagged segment. Checks the source
    # text a human may have corrected, not just the raw ASR guess.
    if contains_explicit_content_keywords(seg.source_final_text):
        reasons.append("explicit_content_flagged")
    for f in r.get("flags", []) or []:
        reasons.append(f"mt:{f}")
    seg.review_reasons = reasons
    seg.review_flag = bool(reasons)


def run(
    audio_path: str,
    outdir: str,
    asr: str = "faster-whisper",
    mt: str = "claude",
    tts: str = "piper",
    source_lang: str = "sw",
    target_lang: str = "en",
    voice_id: str | None = None,
    skip_tts: bool = False,
    verbose: bool = True,
) -> Job:
    out = Path(outdir)
    (out / "segments").mkdir(parents=True, exist_ok=True)

    def log(msg):
        if verbose:
            print(msg, flush=True)

    job = Job(
        source_path=audio_path,
        source_language=source_lang,
        target_language=target_lang,
        providers={"asr": asr, "mt": mt, "tts": tts},
    )
    job.source_duration_ms = probe_duration_ms(audio_path)

    # ---------- 1. ASR ----------
    log(f"[1/5] Transcribing with {asr} ...")
    job.status = "transcribing"
    job.segments = get_asr(asr).transcribe(audio_path, language=source_lang)
    if not job.source_duration_ms and job.segments:
        job.source_duration_ms = job.segments[-1].end_ms
    log(f"      {len(job.segments)} segments, {job.source_duration_ms/1000:.1f}s")
    job.save(out / "manifest.json")

    # ---------- 2. Translate + fit ----------
    log(f"[2/5] Translating with {mt} ...")
    job.status = "translating"
    mt_provider = get_mt(mt)
    cps = timing.DEFAULT_CPS.get(target_lang, 14.0)

    for seg in job.segments:
        translate_segment(seg, mt_provider, source_lang, target_lang, cps)

    # Real dollar cost of this job's MT calls, when the provider tracks one
    # (ClaudeMT does; local/stub/AWS providers don't accrue a real per-call
    # cost the same way, so this is 0.0 for those - see ops_ai_spend_today
    # in webapp/db.py for what reads this back out).
    job.cost_usd = getattr(mt_provider, "total_cost_usd", 0.0)

    log(f"      fit rate {job.fit_rate:.0%}, {job.flagged_count}/{len(job.segments)} flagged for review")
    job.save(out / "manifest.json")

    # ---------- 3. TTS ----------
    if skip_tts:
        log("[3/5] TTS skipped (--skip-tts)")
    else:
        log(f"[3/5] Synthesising with {tts} ...")
        job.status = "synthesizing"
        tts_provider = get_tts(tts)

        # Voice cloning (--tts xtts): auto-extract a reference clip of the
        # SOURCE speaker from the source audio, unless one was given via
        # --voice. Only ever do this on audio you have consent to clone -
        # see the warning in providers/tts.py:XTTSCloneTTS. Single-speaker
        # assumption: takes the longest segment as the cleanest sample.
        # Multi-speaker sources need diarization (not built yet - roadmap).
        if tts == "xtts" and not voice_id and job.segments:
            ref_seg = max(job.segments, key=lambda s: s.duration_ms)
            ref_end = min(ref_seg.end_ms, ref_seg.start_ms + 20_000)
            ref_path = out / "reference_speaker.wav"
            if extract_reference_clip(audio_path, ref_seg.start_ms, ref_end, str(ref_path)):
                voice_id = str(ref_path)
                dur_s = (ref_end - ref_seg.start_ms) / 1000
                log(f"      clone reference: {ref_seg.segment_id}, {dur_s:.1f}s")
                if dur_s < 6.0:
                    job.warnings.append(
                        f"voice-clone reference is only {dur_s:.1f}s - XTTS "
                        "wants 6-20s for a reliable clone")
            else:
                job.warnings.append("ffmpeg not found - could not extract voice-clone reference")

        for seg in job.segments:
            text = seg.final_text.strip()
            if not text:
                continue
            raw = out / "segments" / f"{seg.segment_id}.wav"
            rendered = tts_provider.synthesize(text, str(raw), voice_id=voice_id)

            # Only stretch if we must, and never beyond the cap.
            need = timing.required_stretch_pct(rendered, seg.budget_ms)
            if abs(need) > 1.0 and abs(need) <= timing.MAX_STRETCH_PCT:
                factor = rendered / seg.budget_ms
                fixed = out / "segments" / f"{seg.segment_id}_fit.wav"
                if time_stretch(str(raw), str(fixed), factor):
                    raw = fixed
                    rendered = seg.budget_ms
                    seg.time_stretch_pct = round(need, 2)
                else:
                    job.warnings.append("ffmpeg not found - segments not time-fitted")
            elif abs(need) > timing.MAX_STRETCH_PCT:
                seg.review_flag = True
                if "stretch_cap_exceeded" not in seg.review_reasons:
                    seg.review_reasons.append("stretch_cap_exceeded")

            seg.audio_path = str(raw)
            seg.rendered_duration_ms = rendered
            seg.voice_id = voice_id or tts_provider.name

        # ---------- 4. Mix ----------
        log("[4/5] Mixing timeline ...")
        job.status = "mixing"
        sr = get_tts(tts).sample_rate
        track = build_timeline(
            [(s.start_ms, s.audio_path) for s in job.segments],
            job.source_duration_ms or (job.segments[-1].end_ms if job.segments else 0),
            sample_rate=sr,
        )
        write_wav_mono(str(out / f"dub_{target_lang}.wav"), track, sr)

    # ---------- 5. Deliverables ----------
    log("[5/5] Writing subtitles and manifest ...")
    (out / f"subs_{target_lang}.srt").write_text(to_srt(job), encoding="utf-8")
    (out / f"subs_{target_lang}.vtt").write_text(to_vtt(job), encoding="utf-8")
    (out / f"transcript_{source_lang}.srt").write_text(to_srt(job, source=True), encoding="utf-8")

    job.status = "awaiting_review" if job.flagged_count else "completed"
    job.save(out / "manifest.json")
    log(f"\nDone -> {out.resolve()}")
    log(f"  status: {job.status} | fit rate: {job.fit_rate:.0%} | "
        f"flagged: {job.flagged_count}/{len(job.segments)}")
    return job
