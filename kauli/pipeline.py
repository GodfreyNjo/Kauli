"""End-to-end orchestration: audio in, dubbed audio + subtitles + manifest out.

Every stage writes back to the manifest so you can resume, inspect or hand a
half-finished job to the review editor. Don't add a database until this hurts.
"""
from __future__ import annotations

import os
import re
import subprocess
import uuid
import wave
from pathlib import Path

from . import timing
from .models import Job, Segment, split_off_speaker_tag
from .mixer import build_timeline, extract_audio_window, extract_reference_clip, time_stretch, write_wav_mono
from .providers import get_asr, get_mt, get_tts
from .speaker_gender import AZURE_VOICE_BY_GENDER, estimate_speaker_genders
from .subtitles import to_srt, to_vtt

# A full second or more of no detected speech at all gets its own real,
# taggable segment in Ereri - matches webapp/app.py's GAP_THRESHOLD_MS
# (the same idea, applied between segments here instead of between words
# within one). Kept as a separate constant rather than imported - webapp
# imports kauli, not the other way around.
GAP_THRESHOLD_MS = 1000


def _insert_non_speech_segments(segments: list[Segment], total_duration_ms: int) -> list[Segment]:
    """Real Segment objects for any stretch with no detected speech at all -
    before the first segment, between two segments, or after the last one.
    Each one has an empty words list, which means webapp/app.py's
    _find_gaps already renders its whole span as a single taggable
    gap-cell in Ereri - the same mechanism already used for a silent
    pause within a segment, just given something to attach to here.
    Without this, a stretch of music/laughter/sound-effect-only audio
    between two speech segments simply had no cell at all - not
    editable, not even visible as a gap - because no Segment existed for
    that time range in the first place."""
    ordered = sorted(segments, key=lambda s: s.start_ms)
    filled: list[Segment] = []
    cursor = 0
    for seg in ordered:
        if seg.start_ms - cursor >= GAP_THRESHOLD_MS:
            filled.append(Segment(
                segment_id=f"gap-{uuid.uuid4().hex[:10]}", index=0,
                start_ms=cursor, end_ms=seg.start_ms, segment_type="gap",
                source_confidence=1.0,  # genuinely no speech here, not a low-confidence ASR guess
            ))
        filled.append(seg)
        cursor = max(cursor, seg.end_ms)
    if total_duration_ms - cursor >= GAP_THRESHOLD_MS:
        filled.append(Segment(
            segment_id=f"gap-{uuid.uuid4().hex[:10]}", index=0,
            start_ms=cursor, end_ms=total_duration_ms, segment_type="gap",
            source_confidence=1.0,
        ))
    for i, seg in enumerate(filled):
        seg.index = i
    return filled

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
    """Real duration via ffprobe, not just a WAV header read - a client's
    real upload is mp3/mp4/m4a/whatever just as often as WAV, and this
    value becomes job.source_duration_ms, which build_timeline uses as
    the delivered dub's EXACT length (see kauli/mixer.py). Reading only
    WAV headers silently returned 0 for anything else, which fell back to
    the last segment's end_ms - almost always a bit SHORTER than the real
    file (there's normally a little trailing audio after the last
    detected segment), so the delivered dub quietly ran short of the
    client's actual upload. webapp/app.py's probe_duration_minutes (used
    for billing) already worked this exact way - this just brings the
    pipeline's own copy in line with it instead of leaving a second,
    weaker implementation to drift.
    Falls back to the plain WAV reader only if ffprobe itself isn't
    available or the file genuinely can't be read - never silently 0
    without at least trying the reliable path first."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return int(float(result.stdout.strip()) * 1000)
    except Exception:
        pass
    try:
        with wave.open(path, "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:
        return 0


def _borrowed_budget_ms(seg, all_segments) -> int:
    """seg's own [start,end) slot, extended into a directly-following
    silent gap if there is one - see timing.GAP_BORROW_MIN_KEEP_MS. Looks
    the segment up by id rather than trusting object identity/equality,
    since callers may be re-translating a segment loaded fresh from a
    saved manifest rather than the exact same object holding all_segments.
    Returns seg.duration_ms unchanged if all_segments isn't given, or no
    gap immediately follows."""
    if not all_segments:
        return seg.duration_ms
    for i, s in enumerate(all_segments):
        if s.segment_id == seg.segment_id:
            if i + 1 < len(all_segments):
                nxt = all_segments[i + 1]
                if nxt.segment_type == "gap" and nxt.start_ms == seg.end_ms:
                    borrow = max(0, nxt.duration_ms - timing.GAP_BORROW_MIN_KEEP_MS)
                    return seg.duration_ms + borrow
            break
    return seg.duration_ms


def resolve_voice_for_segment(job, seg, default_voice_id, tts_provider_name: str | None = None):
    """The actual multi-speaker guarantee: if this segment has a
    speaker_id AND that speaker has an assigned voice (job.speaker_voices -
    see Job's own comment on where speaker_id/speaker_voices come from),
    that voice wins over the order's single default voice. A segment with
    no speaker_id, or a speaker nobody's assigned a voice to yet, falls
    back to default_voice_id exactly like before this existed - zero
    behavior change for any order that isn't using per-speaker voices.

    tts_provider_name guards a real crash this caught in testing:
    job.speaker_voices values are always Piper voice-model paths (see
    webapp/app.py's editor_assign_speaker_voice, Piper-only on purpose).
    Handing one of those to a DIFFERENT provider - e.g. this segment gets
    re-rendered through the order's own xtts voice-clone route, which
    passes its own reference_speaker.wav as default_voice_id - makes XTTS
    try to load a .onnx model file as if it were reference speaker AUDIO,
    which crashes deep in torchcodec's decoder instead of failing
    cleanly. So the speaker override only ever applies when the caller is
    actually about to synthesize through Piper; anything else falls back
    to default_voice_id, same as a segment with no speaker assignment at
    all - the segment simply speaks in the order's own default voice
    until it's re-rendered through Piper again."""
    if (seg.speaker_id and job.speaker_voices.get(seg.speaker_id)
            and (tts_provider_name is None or tts_provider_name == "piper")):
        return job.speaker_voices[seg.speaker_id]
    # Same guarantee, for Azure's named voices instead of a Piper model
    # path - see Job.speaker_voice_names' own comment on why this can't
    # share the dict above (a Piper path and an Azure voice name mean
    # nothing to each other's provider).
    if (seg.speaker_id and job.speaker_voice_names.get(seg.speaker_id)
            and tts_provider_name == "azure"):
        return job.speaker_voice_names[seg.speaker_id]
    return default_voice_id


def render_gap_audio(seg, source_audio_path: str, segments_dir: Path, sample_rate: int) -> bool:
    """Fills a non-speech gap segment's audio_path with the REAL original
    audio for its exact time window, instead of the digital silence
    mixer.build_timeline used to leave there (a segment with no
    audio_path contributes nothing at all - see that function's own
    docstring). Music, applause, laughter, an instrument break: none of
    it is speech that needs replacing, so there's nothing to translate or
    synthesize - the original bed is simply carried straight through,
    the same way a professional dub leaves the M&E (music & effects)
    stem alone and only ever replaces the dialogue.

    Shared by kauli.pipeline.run's own TTS loop and webapp/app.py's
    _render_segment_audio (single-segment/editor-triggered resynthesis) -
    same reasoning apply_stretch_fit's docstring gives for being shared
    rather than two drifting copies.

    Returns True if it actually wrote real audio (ffmpeg available, a
    genuine >0ms window) - False leaves seg.audio_path unset, the exact
    same silent fallback this always had, so a machine without ffmpeg
    degrades exactly like before rather than crashing."""
    out_path = segments_dir / f"{seg.segment_id}.wav"
    if not extract_audio_window(source_audio_path, seg.start_ms, seg.end_ms, str(out_path), sample_rate):
        return False
    seg.audio_path = str(out_path)
    seg.rendered_duration_ms = seg.end_ms - seg.start_ms
    return True


def strip_bracket_tags_for_tts(text: str) -> str:
    """Removes inline [bracket] sound/caption tags - "[Applause]",
    "[MUZIKI YACHEZA]" - before text reaches the TTS engine. These are
    caption metadata (to_srt/to_vtt still show them, completely untouched
    - see _protect_untranslatable for the parallel guarantee on the
    translation side), never something a voice should actually read out
    loud - a TTS engine saying "music playing" out loud is exactly the
    "silent stitch" bug render_gap_audio (above) and this function
    together fix.

    A real, separate gap segment (see _insert_non_speech_segments) never
    reaches this at all - render_gap_audio handles those. This is for a
    bracket tag typed INLINE inside an otherwise-spoken segment's text,
    e.g. "Welcome back to the show. [Applause]"."""
    stripped = re.sub(r"\[[^\]]*\]", " ", text)
    return re.sub(r"\s+", " ", stripped).strip()


def apply_stretch_fit(seg, raw_path: Path, rendered_ms: int, segments_dir: Path,
                       warnings: list | None = None) -> tuple[Path, int]:
    """Fits one segment's freshly-synthesized audio to its budget_ms slot,
    mutating seg's timing/review fields in place and returning the
    (possibly stretched) path and final rendered duration to use. Shared
    by kauli.pipeline.run's own TTS loop and webapp/app.py's
    _render_segment_audio (single-segment/editor-triggered resynthesis) -
    these used to be two separately-drifting copies of the same logic.

    Two tiers, both using ffmpeg's pitch-preserving atempo filter (see
    mixer.time_stretch) - never a pitch-shifting resample:
      1. Up to timing.MAX_STRETCH_PCT: applied silently. This is the
         "sounds completely natural" range - no flag, nothing for a human
         to look at.
      2. Up to timing.EMERGENCY_STRETCH_PCT: applied as a last resort
         instead of leaving the segment unfittable, but flagged
         "emergency_stretch_applied" so an editor knows the pace was
         pushed and should give it a listen.
    Beyond that, the audio is left as rendered (no stretch applied) and
    flagged "stretch_cap_exceeded" - mixer.build_timeline's own
    non-overlap guarantee is what keeps this from ever audibly overlapping
    the next segment; the flag plus Ereri's 100%-edited shipping gate is
    what keeps a silently-clipped segment from ever reaching a client
    without a human having looked at it first."""
    need = timing.required_stretch_pct(rendered_ms, seg.budget_ms)
    raw, rendered = raw_path, rendered_ms
    if abs(need) <= 1.0:
        return raw, rendered
    if abs(need) <= timing.MAX_STRETCH_PCT or abs(need) <= timing.EMERGENCY_STRETCH_PCT:
        factor = rendered / seg.budget_ms
        fixed = segments_dir / f"{seg.segment_id}_fit.wav"
        if time_stretch(str(raw), str(fixed), factor):
            raw = fixed
            rendered = seg.budget_ms
            seg.time_stretch_pct = round(need, 2)
            if abs(need) > timing.MAX_STRETCH_PCT:
                seg.review_flag = True
                if "emergency_stretch_applied" not in seg.review_reasons:
                    seg.review_reasons.append("emergency_stretch_applied")
        elif warnings is not None:
            warnings.append("ffmpeg not found - segments not time-fitted")
    else:
        seg.review_flag = True
        if "stretch_cap_exceeded" not in seg.review_reasons:
            seg.review_reasons.append("stretch_cap_exceeded")
    return raw, rendered


def spell_out_text(text: str) -> str:
    """Forces a TTS engine to read text letter-by-letter instead of as a
    word - a real, standard workaround for engines with no SSML
    <say-as interpret-as="characters"> support. Neither Piper nor XTTS
    (the two providers actually wired into this pipeline's real orders -
    see providers/tts.py) understand SSML at all, so this is the honest
    lever actually available: spacing every character out reliably breaks
    a neural TTS model's word-level pronunciation and falls back to
    naming each character instead.

    Applies to the WHOLE string handed in - see Segment.spell_out's own
    docstring for why that means this only makes sense on a segment
    that's mostly just the name/acronym in question, not a full sentence
    with one troublesome word buried in it (this pipeline renders and
    times audio per-segment, not per-word)."""
    out = []
    for ch in text:
        if ch.isspace():
            out.append(". ")  # a firmer break where the original had a word boundary
        else:
            out.append(ch + " ")
    return "".join(out).strip()


def _protect_untranslatable(text: str) -> tuple[str, dict[str, str]]:
    """Swaps out everything that must survive translation completely
    unchanged - real paragraph breaks, a leading speaker tag on any
    paragraph, and bracket sound/caption tags like "[MUSIC]" - for plain,
    translation-safe placeholder tokens before the text ever reaches an
    MT provider. Restore with _restore_untranslatable afterward.

    The real problem this fixes: asking a translator to "translate" an
    already-English (or already-fine-as-is) bracket tag or a speaker's
    own name routinely mangled or reworded them - a caption tag and a
    proper noun aren't Swahili content to translate, they're labels that
    need to come out the other side byte-for-byte identical. A literal
    blank line between paragraphs had no such guarantee either - nothing
    stopped a translation model from reformatting or merging paragraphs,
    quietly losing structure a human had already corrected.

    Placeholders are plain, spaceless, all-caps alphanumeric tokens - the
    form both a real NMT engine (Lara) and a real LLM (Claude) are most
    likely to carry straight through untouched, the same trick real
    translation pipelines use to protect variables/tags from translation.
    LocalMT (a small, closed-vocabulary NMT with no instruction-following
    ability - see its own docstring) is the one real provider that can
    still subtly mangle a token like this in production - see
    _restore_untranslatable's fuzzy-match fallback for how that's
    recovered from rather than silently leaking garbage into final text.
    """
    placeholders: dict[str, str] = {}

    def _swap(real_value: str) -> str:
        token = f"QQTAG{len(placeholders)}QQ"
        placeholders[token] = real_value
        return token

    paragraphs = re.split(r"\n\n+", text)
    protected_paragraphs = []
    for para in paragraphs:
        tag, remainder = split_off_speaker_tag(para.strip())
        piece = (_swap(tag) + " ") if tag else ""
        piece += re.sub(r"\[[^\]]*\]", lambda m: _swap(m.group(0)), remainder)
        protected_paragraphs.append(piece)
    protected_text = (" " + _swap("\n\n") + " ").join(protected_paragraphs)
    return protected_text, placeholders


def _restore_untranslatable(text: str | None, placeholders: dict[str, str]) -> str:
    text = text or ""
    for token, real_value in placeholders.items():
        text = text.replace(token, real_value)
    if not placeholders or "QQ" not in text:
        return text
    # Real bug caught in production: LocalMT (MarianMT - a small, closed-
    # vocabulary NMT with no instruction-following ability, unlike
    # Lara/Claude - see LocalMT's own docstring) can subtly mangle an
    # opaque placeholder during its own subword tokenize/generate
    # round-trip. Observed live: "QQTAG0QQ" came back as "QQAG0QQ" - one
    # character dropped - so the exact .replace() above silently left that
    # garbage sitting in the final text instead of the real content it
    # stood for. Recovery falls back to matching on the token's own
    # embedded index digit (the part that DIDN'T get mangled here, not a
    # coincidence - it's a single short digit run, the least likely part
    # of the token for subword tokenization to touch) rather than
    # requiring an exact spelling match.
    remaining = [(tok, val) for tok, val in placeholders.items() if tok not in text]

    def _fuzzy_sub(m: re.Match) -> str:
        chunk_digits = re.search(r"\d+", m.group(0))
        if chunk_digits:
            for tok, val in list(remaining):
                tok_digits = re.search(r"\d+", tok)
                if tok_digits and tok_digits.group(0) == chunk_digits.group(0):
                    remaining.remove((tok, val))
                    return val
        return m.group(0)  # genuinely unmatched - leave it rather than guess wrong

    return re.sub(r"QQ\w*QQ", _fuzzy_sub, text)


def translate_segment(seg, mt_provider, source_lang: str, target_lang: str, cps: float,
                       all_segments: list | None = None) -> None:
    """Translate one segment and pick the best-fitting candidate, mutating
    it in place. Pulled out of run()'s main loop so the webapp editor's
    "re-translate from corrected Swahili" action can call the exact same
    logic (via seg.source_final_text, which picks up a staff correction to
    the source transcript) instead of a second copy of it drifting apart.

    all_segments (the job's full, ordered segment list) is optional only
    for backward compatibility - pass it whenever you have it, so a
    translation that's merely a little too long for its own slot gets a
    real chance to fit into a directly-following silent gap instead of
    being flagged or clipped unnecessarily (see _borrowed_budget_ms)."""
    seg.target_language = target_lang
    seg.budget_ms = _borrowed_budget_ms(seg, all_segments)
    target_chars = max(10, int(seg.budget_ms / 1000 * cps))

    # Protect paragraph breaks / speaker tags / bracket caption tags from
    # the translator itself before it ever sees them - see
    # _protect_untranslatable's own docstring for the real bug this
    # closes. Restored in every candidate the provider returns, not just
    # whichever one ends up chosen, so an edit that changes which
    # candidate fits best never re-exposes an un-restored placeholder.
    protected_text, placeholders = _protect_untranslatable(seg.source_final_text)
    r = mt_provider.translate(protected_text, target_chars, source_lang, target_lang)
    seg.literal = _restore_untranslatable(r.get("literal"), placeholders)
    seg.translation_confidence = float(r.get("confidence") or 0.0)
    seg.cultural_notes = r.get("notes")

    candidates = [
        {"text": _restore_untranslatable(r.get("spoken"), placeholders), "similarity": 0.97, "variant": "spoken"},
        {"text": _restore_untranslatable(r.get("shorter"), placeholders) if r.get("shorter") else None,
         "similarity": 0.90, "variant": "shorter"},
        {"text": _restore_untranslatable(r.get("longer"), placeholders) if r.get("longer") else None,
         "similarity": 0.95, "variant": "longer"},
        {"text": seg.literal, "similarity": 1.00, "variant": "literal"},
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
    resume: bool = False,
) -> Job:
    out = Path(outdir)
    (out / "segments").mkdir(parents=True, exist_ok=True)

    def log(msg):
        if verbose:
            print(msg, flush=True)

    # Real cost bug this fixes: webapp/worker.py's own retry-on-failure used
    # to always call this fresh from ASR, no matter which stage actually
    # failed - a real, PAID cloud ASR call (Transkriptor, at $/minute) got
    # re-billed for the full file on every single retry, even when the
    # transcript and translation from the FAILED attempt were already
    # sitting on disk, correct, in its manifest - only a later stage (TTS/
    # mix) had failed. resume=True (set by worker.py's retry path only,
    # never the original attempt) loads that existing manifest and reuses
    # whatever real ASR/MT work it already contains, per segment - a
    # segment already transcribed is never re-sent to a paid ASR provider
    # just because a DIFFERENT segment's TTS call happened to fail.
    resumed_segments = None
    resumed_cost_usd = 0.0
    manifest_path = out / "manifest.json"
    if resume and manifest_path.exists():
        try:
            candidate = Job.load(str(manifest_path))
            if candidate.segments:
                resumed_segments = candidate.segments
                resumed_cost_usd = candidate.cost_usd or 0.0
        except Exception:
            resumed_segments = None  # a corrupt/partial manifest - fall back to a real full run rather than crash

    job = Job(
        source_path=audio_path,
        source_language=source_lang,
        target_language=target_lang,
        providers={"asr": asr, "mt": mt, "tts": tts},
    )
    job.source_duration_ms = probe_duration_ms(audio_path)

    # ---------- 1. ASR ----------
    non_gap_resumed = [s for s in (resumed_segments or []) if s.segment_type != "gap"]
    if resumed_segments and non_gap_resumed and all(s.source_transcript for s in non_gap_resumed):
        log(f"[1/5] Resuming: reusing {len(resumed_segments)} already-transcribed segments (skipping {asr})")
        job.segments = resumed_segments
        job.status = "transcribing"
    else:
        log(f"[1/5] Transcribing with {asr} ...")
        job.status = "transcribing"
        asr_provider = get_asr(asr)
        job.segments = asr_provider.transcribe(audio_path, language=source_lang)
        # TranskriptorASR (and any future provider with a real fallback) sets
        # these on itself rather than raising - the manifest's own
        # providers["asr"] must reflect what actually ran, not what was asked
        # for, or an editor/client checking the job's real provenance would be
        # misled about which transcriber actually produced this text.
        if getattr(asr_provider, "fallback_used", False):
            job.providers["asr"] = "faster-whisper"
            reason = getattr(asr_provider, "fallback_reason", None)
            if reason:
                job.warnings.append(reason)
            log(f"      {reason or 'ASR fell back to faster-whisper'}")
        if not job.source_duration_ms and job.segments:
            job.source_duration_ms = job.segments[-1].end_ms
        job.segments = _insert_non_speech_segments(job.segments, job.source_duration_ms)
        n_gaps = sum(1 for s in job.segments if s.segment_type == "gap")
        log(f"      {len(job.segments)} segments ({n_gaps} non-speech), {job.source_duration_ms/1000:.1f}s")
    job.save(out / "manifest.json")

    # ---------- 2. Translate + fit ----------
    log(f"[2/5] Translating with {mt} ...")
    job.status = "translating"
    mt_provider = get_mt(mt)
    cps = timing.DEFAULT_CPS.get(target_lang, 14.0)

    for seg in job.segments:
        if seg.segment_type == "gap":
            continue  # nothing to translate - a human tags these manually in Ereri
        if seg.spoken:
            continue  # resumed with a real translation already - a retry never re-spends an MT call either
        translate_segment(seg, mt_provider, source_lang, target_lang, cps, all_segments=job.segments)
    # Same real "log it, don't hide it" rule the ASR fallback above
    # follows - get_mt() may have silently switched to a backup MT
    # provider mid-order (see ResilientMT); the manifest needs a real
    # record of that, not a providers["mt"] that quietly claims the
    # original provider ran when it didn't for some/all segments.
    if getattr(mt_provider, "fallback_used", False):
        job.providers["mt"] = mt_provider.fallback_name
        if mt_provider.fallback_reason:
            job.warnings.append(mt_provider.fallback_reason)
        log(f"      {mt_provider.fallback_reason or 'MT fell back to ' + mt_provider.fallback_name}")

    # Real dollar cost of this job's MT calls, when the provider tracks one
    # (ClaudeMT does; local/stub/AWS providers don't accrue a real per-call
    # cost the same way, so this is 0.0 for those - see ops_ai_spend_today
    # in webapp/db.py for what reads this back out). Added to, not replaced
    # by, whatever the RESUMED-from attempt already really spent - most of
    # a resumed job's segments were skipped above (already translated), so
    # this run's own mt_provider.total_cost_usd only reflects the FEW it
    # still had to redo; the resumed segments' real cost still happened and
    # must not silently disappear from the job's own cost record.
    job.cost_usd = resumed_cost_usd + getattr(mt_provider, "total_cost_usd", 0.0)

    log(f"      fit rate {job.fit_rate:.0%}, {job.flagged_count}/{len(job.segments)} flagged for review")
    job.save(out / "manifest.json")

    # ---------- 3. TTS ----------
    if skip_tts:
        log("[3/5] TTS skipped (--skip-tts)")
    else:
        log(f"[3/5] Synthesising with {tts} ...")
        job.status = "synthesizing"
        tts_provider = get_tts(tts, target_lang=target_lang)

        # Real multi-speaker default for Azure: a man, a woman, and several
        # other speakers on one source shouldn't all come out as the same
        # single voice. Guesses each detected speaker's gender from their
        # own audio's pitch (see kauli.speaker_gender - a genuine, if
        # coarse, signal, not a fabricated assignment) and maps it onto
        # Azure's two real Kiswahili voices. Only runs when nobody's
        # already set an assignment for this speaker (an editor's manual
        # override in Ereri always wins, this never clobbers one), and
        # only for speakers real diarization actually distinguished
        # (speaker_id set - faster-whisper alone never sets one, so a
        # single-speaker/no-diarization order is completely unaffected).
        if tts == "azure":
            speakers_needing_a_default = {
                s.speaker_id for s in job.segments
                if s.speaker_id and s.segment_type != "gap"
                and s.speaker_id not in job.speaker_voice_names
            }
            if speakers_needing_a_default:
                genders = estimate_speaker_genders(job, audio_path)
                for speaker_id in speakers_needing_a_default:
                    gender = genders.get(speaker_id, "unknown")
                    if gender in AZURE_VOICE_BY_GENDER:
                        job.speaker_voice_names[speaker_id] = AZURE_VOICE_BY_GENDER[gender]
                log(f"      auto-assigned voices for {len(speakers_needing_a_default)} speaker(s): "
                    f"{ {k: v for k, v in genders.items() if k in speakers_needing_a_default} }")

        # Voice cloning (--tts xtts): auto-extract a reference clip of the
        # SOURCE speaker from the source audio, unless one was given via
        # --voice. Only ever do this on audio you have consent to clone -
        # see the warning in providers/tts.py:XTTSCloneTTS. Single-speaker
        # assumption: takes the longest segment as the cleanest sample.
        # Multi-speaker sources need diarization (not built yet - roadmap).
        # Speech segments only - a long music/silence gap could easily be
        # the single longest segment in the file, which would extract a
        # "voice" reference clip containing no voice at all.
        speech_segments = [s for s in job.segments if s.segment_type != "gap"]
        if tts == "xtts" and not voice_id and speech_segments:
            ref_seg = max(speech_segments, key=lambda s: s.duration_ms)
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
            if seg.segment_type == "gap":
                # Real music/applause/laughter/SFX bed from the source,
                # not silence - see render_gap_audio's own docstring.
                if not render_gap_audio(seg, audio_path, out / "segments", tts_provider.sample_rate):
                    job.warnings.append(
                        f"ffmpeg not found - {seg.segment_id} (non-speech) rendered as silence")
                continue
            # A leading speaker tag ("RIGATHI GACHAGUA:") is caption
            # metadata, not a line to speak aloud - a real voice actor
            # reading a transcript doesn't say the character's name before
            # their own line either. Stripped only for what's SYNTHESIZED;
            # to_srt/to_vtt still caption the full text, tag included.
            _tag, text = split_off_speaker_tag(seg.final_text.strip())
            text = text.strip()
            # Same reasoning, for an inline [Applause]/[MUZIKI YACHEZA]
            # sound tag typed into an otherwise-spoken segment's text - the
            # voice reads the real words, never the bracket tag itself.
            text = strip_bracket_tags_for_tts(text)
            if not text:
                continue
            raw = out / "segments" / f"{seg.segment_id}.wav"
            # Multi-speaker consistency: a segment whose speaker has an
            # assigned voice (job.speaker_voices) uses THAT instead of the
            # order's single default - see resolve_voice_for_segment.
            seg_voice_id = resolve_voice_for_segment(job, seg, voice_id, tts)
            rendered = tts_provider.synthesize(text, str(raw), voice_id=seg_voice_id)
            raw, rendered = apply_stretch_fit(seg, raw, rendered, out / "segments", job.warnings)

            seg.audio_path = str(raw)
            seg.rendered_duration_ms = rendered
            seg.voice_id = seg_voice_id or tts_provider.name

        # Same "log it, don't hide it" rule as the ASR/MT fallbacks above -
        # get_tts() may have silently switched to Piper mid-order (see
        # ResilientTTS), and the manifest needs a real record of that.
        if getattr(tts_provider, "fallback_used", False):
            job.providers["tts"] = tts_provider.fallback_name
            if tts_provider.fallback_reason:
                job.warnings.append(tts_provider.fallback_reason)
            log(f"      {tts_provider.fallback_reason or 'TTS fell back to ' + tts_provider.fallback_name}")

        # ---------- 4. Mix ----------
        log("[4/5] Mixing timeline ...")
        job.status = "mixing"
        # Real bug this fixes: this used to construct a THROWAWAY second TTS
        # provider just to read .sample_rate, instead of the one that
        # actually just ran - harmless when it's a plain provider (same
        # sample rate either way), but wrong the moment tts_provider is a
        # ResilientTTS that fell back mid-order: Piper and Azure have
        # different real sample rates, and build_timeline needs the rate
        # the audio was ACTUALLY rendered at, not whatever a fresh instance
        # of the ORIGINAL provider would report.
        sr = tts_provider.sample_rate
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
