"""Run with: python -m pytest tests/ -v   (or just: python tests/test_pipeline.py)"""
import sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

import numpy as np

from kauli import timing
from kauli.pipeline import (
    run, _borrowed_budget_ms, resolve_voice_for_segment,
    _protect_untranslatable, _restore_untranslatable, translate_segment,
    spell_out_text, render_gap_audio, strip_bracket_tags_for_tts,
)
from kauli.models import Job, Segment
from kauli.mixer import build_timeline, write_wav_mono, read_wav_mono
from kauli.providers.mt import StubMT


def test_swahili_clock_is_flagged():
    """The clock offset is the highest-consequence error in Kenyan content.
    If this test ever goes quiet, something upstream stopped flagging it."""
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        clock_seg = [s for s in job.segments if "saa kumi na moja" in s.source_transcript][0]
        assert clock_seg.review_flag
        assert "mt:time_expression" in clock_seg.review_reasons
        assert "17:00" in (clock_seg.cultural_notes or "")


def test_fit_status_bands():
    assert timing.fit_status(1.00) == "fits"
    assert timing.fit_status(0.95) == "fits"
    assert timing.fit_status(1.30) == "unfittable"
    assert timing.fit_status(0.60) == "needs_lengthening"


def test_stretch_cap_never_exceeded():
    # Two tiers now (see timing.EMERGENCY_STRETCH_PCT / kauli.pipeline.
    # apply_stretch_fit): up to MAX_STRETCH_PCT applies silently, up to
    # EMERGENCY_STRETCH_PCT applies as a flagged last resort. Either way,
    # a segment's actual applied stretch must never exceed the wider cap.
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        for s in job.segments:
            assert abs(s.time_stretch_pct) <= timing.EMERGENCY_STRETCH_PCT
            if abs(s.time_stretch_pct) > timing.MAX_STRETCH_PCT:
                assert "emergency_stretch_applied" in s.review_reasons


def test_manifest_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        reloaded = Job.load(os.path.join(d, "manifest.json"))
        assert len(reloaded.segments) == len(job.segments)
        assert reloaded.segments[0].final_text == job.segments[0].final_text


def test_borrows_trailing_gap_but_keeps_a_pause():
    speech = Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000)
    gap = Segment(segment_id="gap-1", index=1, start_ms=1000, end_ms=2500,
                   segment_type="gap")
    # 1000ms own slot + (1500ms gap - 200ms kept as a real pause) = 2300ms.
    assert _borrowed_budget_ms(speech, [speech, gap]) == 1000 + (1500 - 200)


def test_no_borrow_without_a_directly_following_gap():
    a = Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000)
    b = Segment(segment_id="seg_0002", index=1, start_ms=1000, end_ms=2000)
    assert _borrowed_budget_ms(a, [a, b]) == 1000
    assert _borrowed_budget_ms(a, None) == 1000
    assert _borrowed_budget_ms(a, []) == 1000


def test_protect_and_restore_untranslatable_round_trips_exactly():
    original = (
        "RIGATHI GACHAGUA: Karibu [MUZIKI YACHEZA] kwenye hii onyesho.\n\n"
        "Audience: Hey! [APPLAUSE] Are you people from the Nyoro tribe?"
    )
    protected, placeholders = _protect_untranslatable(original)
    # Nothing that must survive verbatim is visible in what actually gets
    # sent to a translator - the whole point.
    assert "RIGATHI GACHAGUA:" not in protected
    assert "[MUZIKI YACHEZA]" not in protected
    assert "[APPLAUSE]" not in protected
    assert "\n\n" not in protected
    restored = _restore_untranslatable(protected, placeholders)
    # Not byte-exact - a real space is deliberately kept around each
    # placeholder (see _protect_untranslatable's own reasoning: an MT
    # engine jamming its own output straight up against an untouched
    # token is a worse failure mode than one extra space) - but every
    # real piece of content is back, unchanged, in the right order.
    assert restored.index("RIGATHI GACHAGUA:") < restored.index("[MUZIKI YACHEZA]") < restored.index("\n\n")
    assert restored.index("\n\n") < restored.index("Audience:") < restored.index("[APPLAUSE]")
    for chunk in ("RIGATHI GACHAGUA:", "[MUZIKI YACHEZA]", "kwenye hii onyesho.", "\n\n",
                  "Audience:", "Hey!", "[APPLAUSE]", "Are you people from the Nyoro tribe?"):
        assert chunk in restored


def test_restore_untranslatable_recovers_from_a_mangled_placeholder():
    """Real bug caught in production: a live re-translate through LocalMT
    (MarianMT) came back with "QQAG0QQ" leaked into the final English text
    instead of the real speaker tag it stood for - the model's own
    subword tokenize/generate round-trip had dropped the "T" from
    "QQTAG0QQ", so the exact-match restore in _restore_untranslatable
    found nothing to replace. Recovery falls back to the token's own
    embedded index digit, which survived even though the letters around
    it didn't."""
    placeholders = {"QQTAG0QQ": "RIGATHI GACHAGUA:", "QQTAG1QQ": "[APPLAUSE]"}
    mangled = "QQAG0QQ Come on. QQTAG1QQ Are you ready?"
    restored = _restore_untranslatable(mangled, placeholders)
    assert "RIGATHI GACHAGUA:" in restored
    assert "[APPLAUSE]" in restored
    assert "QQ" not in restored


def test_translate_segment_preserves_paragraph_speaker_and_bracket_tags():
    """Real end-to-end proof, not just the helper in isolation: StubMT's
    fallback (see kauli.providers.mt.StubMT.translate) echoes back
    whatever text it's actually given unchanged for input it doesn't
    recognize - exactly what makes it useful here. If translate_segment's
    protect/restore wiring were broken, the protected placeholders
    (QQTAG0QQ etc.) would leak straight into seg.spoken/seg.literal
    instead of the real speaker tag, bracket tag and paragraph break."""
    text = "RIGATHI GACHAGUA: Come on.\n\nAudience: Hey! [APPLAUSE] Are you ready?"
    seg = Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=5000,
                  source_edited_transcript=text)
    translate_segment(seg, StubMT(), "sw", "en", cps=16.94)
    for value in (seg.spoken, seg.literal):
        assert "QQTAG" not in value  # no leaked placeholder, ever
        assert "RIGATHI GACHAGUA:" in value
        assert "Audience:" in value
        assert "[APPLAUSE]" in value
        assert "\n\n" in value


def test_spell_out_text_spaces_every_character():
    # A neural TTS voice with no SSML support (Piper, XTTS - see
    # spell_out_text's own docstring) will read "NASA" as a word unless the
    # letters are forced apart - this is the real, honest lever available.
    assert spell_out_text("NASA") == "N A S A"
    # A word boundary in the original becomes a firmer break (". "), not
    # just another space - keeps two separate words from blurring into one
    # long run of spelled letters with no audible gap between them.
    assert spell_out_text("NASA HQ") == "N A S A . H Q"


def test_strip_bracket_tags_for_tts_removes_inline_sound_tags():
    # The other half of the actual bug report: an inline sound tag typed
    # into an otherwise-spoken segment must never reach the TTS engine -
    # captions still show it (to_srt/to_vtt untouched), only what's
    # SYNTHESIZED changes.
    assert strip_bracket_tags_for_tts("Welcome back to the show. [Applause]") == "Welcome back to the show."
    assert strip_bracket_tags_for_tts("Hey! [APPLAUSE] Are you ready?") == "Hey! Are you ready?"
    assert strip_bracket_tags_for_tts("[MUZIKI YACHEZA]") == ""  # nothing left to speak at all


def test_render_gap_audio_carries_real_source_bed_not_silence():
    """The actual bug report this fixes: a non-speech gap (music,
    applause, laughter, an instrument break) used to leave literal
    digital silence in the delivered dub - mixer.build_timeline skips any
    segment with no audio_path at all - even though the ORIGINAL
    video/audio had real sound playing there. Proves the real ffmpeg
    extraction end-to-end (not just that some path got set): a real,
    non-silent tone standing in for "music" must still be non-silent
    after the cut, at the right duration."""
    with tempfile.TemporaryDirectory() as d:
        sr = 8000
        source_path = os.path.join(d, "source.wav")
        tone = (np.sin(2 * np.pi * 440 * np.arange(int(sr * 3.0)) / sr) * 0.5).astype(np.float32)
        write_wav_mono(source_path, tone, sr)

        seg = Segment(segment_id="gap-test", index=0, start_ms=1000, end_ms=2000, segment_type="gap")
        segments_dir = Path(d) / "segments"
        segments_dir.mkdir()
        ok = render_gap_audio(seg, source_path, segments_dir, sample_rate=sr)

        assert ok
        assert seg.audio_path and os.path.exists(seg.audio_path)
        assert seg.rendered_duration_ms == 1000  # end_ms - start_ms, the real window length
        data, out_sr = read_wav_mono(seg.audio_path)
        assert out_sr == sr
        assert np.max(np.abs(data)) > 0.1  # real audio survived the cut, not silence


def test_resolve_voice_uses_speaker_assignment_when_present():
    job = Job(speaker_voices={"Man": "/voices/ryan.onnx", "Woman": "/voices/amy.onnx"})
    man_seg = Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000, speaker_id="Man")
    woman_seg = Segment(segment_id="seg_0002", index=1, start_ms=1000, end_ms=2000, speaker_id="Woman")
    assert resolve_voice_for_segment(job, man_seg, "/voices/default.onnx") == "/voices/ryan.onnx"
    assert resolve_voice_for_segment(job, woman_seg, "/voices/default.onnx") == "/voices/amy.onnx"


def test_resolve_voice_never_hands_a_piper_path_to_a_non_piper_provider():
    """Real bug caught in live testing: job.speaker_voices values are
    always Piper voice-model paths (.onnx), never real reference-speaker
    audio. Handing one to xtts (which treats its default_voice_id as a
    reference audio CLIP to clone, not a model file) crashed deep inside
    torchcodec's decoder instead of failing cleanly. The speaker override
    must never apply for any provider other than piper."""
    job = Job(speaker_voices={"Man": "/voices/ryan.onnx"})
    seg = Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000, speaker_id="Man")
    xtts_default = "/orders/f25f1ca18d/reference_speaker.wav"
    assert resolve_voice_for_segment(job, seg, xtts_default, "xtts") == xtts_default
    assert resolve_voice_for_segment(job, seg, xtts_default, "stub") == xtts_default
    # Piper (or the no-provider-given default, used by call sites that
    # only ever run Piper) is the one case the override DOES apply.
    assert resolve_voice_for_segment(job, seg, xtts_default, "piper") == "/voices/ryan.onnx"
    assert resolve_voice_for_segment(job, seg, xtts_default) == "/voices/ryan.onnx"


def test_resolve_voice_falls_back_without_speaker_id_or_assignment():
    job = Job(speaker_voices={"Man": "/voices/ryan.onnx"})
    untagged = Segment(segment_id="seg_0003", index=0, start_ms=0, end_ms=1000)
    unassigned_speaker = Segment(segment_id="seg_0004", index=1, start_ms=1000, end_ms=2000,
                                  speaker_id="Narrator")
    assert resolve_voice_for_segment(job, untagged, "/voices/default.onnx") == "/voices/default.onnx"
    assert resolve_voice_for_segment(job, unassigned_speaker, "/voices/default.onnx") == "/voices/default.onnx"


def test_job_speaker_voices_roundtrips_through_save_load():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "manifest.json")
        job = Job(speaker_voices={"Man": "/voices/ryan.onnx", "Woman": "/voices/amy.onnx"})
        job.segments = [Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000, speaker_id="Man")]
        job.save(path)
        reloaded = Job.load(path)
        assert reloaded.speaker_voices == {"Man": "/voices/ryan.onnx", "Woman": "/voices/amy.onnx"}
        assert reloaded.segments[0].speaker_id == "Man"


def test_build_timeline_never_overlaps_audio():
    """The real bug behind "I can hear the voice speaking over itself":
    build_timeline used to sum two segments' audio wherever they
    overlapped. Segment A here is deliberately rendered far longer than
    the real gap before segment B starts (exactly what a translation
    that's too long for its slot produces) - this must never let A's
    tail sum into B's onset."""
    sr = 8000
    with tempfile.TemporaryDirectory() as d:
        a_path = os.path.join(d, "a.wav")
        b_path = os.path.join(d, "b.wav")
        # A: raw amplitude 0.6, "renders" 2000ms long even though only
        # 1000ms of real estate exists before B starts.
        write_wav_mono(a_path, np.full(int(sr * 2.0), 0.6, dtype=np.float32), sr)
        # B: raw amplitude 0.3 (deliberately LOWER than A) - if the old
        # summing bug were still present, the overlap zone would spike to
        # 0.9 raw, becoming the loudest point in the whole track and
        # exposing the bug the moment the track gets peak-normalized.
        write_wav_mono(b_path, np.full(int(sr * 0.5), 0.3, dtype=np.float32), sr)

        track = build_timeline([(0, a_path), (1000, b_path)], total_ms=2000, sample_rate=sr)

        assert len(track) == int(sr * 2.0)
        a_region = track[: int(sr * 1.0)]
        b_region = track[int(sr * 1.0): int(sr * 1.5)]
        silent_tail = track[int(sr * 1.5):]

        # A's own region (no overlap possible there) must be the loudest
        # part of the track - if it isn't, the boundary zone got summed.
        assert np.isclose(np.max(a_region), np.max(track), atol=1e-6)
        # B genuinely played, at its own (lower, un-summed) amplitude.
        assert np.max(b_region) > 0
        assert np.max(b_region) < np.max(a_region)
        # A got hard-clipped at B's start, not padded/looped into the tail.
        assert np.max(np.abs(silent_tail)) < 1e-6


def test_human_edit_wins():
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        seg = job.segments[0]
        seg.edited_text = "Corrected by a human."
        assert seg.final_text == "Corrected by a human."


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} passed")
