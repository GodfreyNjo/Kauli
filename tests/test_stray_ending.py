"""A word the recognizer invents over the closing music ("Sam." stretched across ten seconds) is not left on the end of a transcript."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli.models import Job, Segment  # noqa: E402
from kauli.pipeline import _insert_non_speech_segments, drop_stray_final_words  # noqa: E402
from test_order_flow import env  # noqa: E402,F401


def _seg(i, a, b, text, kind="speech", spk=None, edited=None):
    return Segment(segment_id=f"s{i}", index=i, start_ms=a, end_ms=b, segment_type=kind, source_transcript=text, speaker_id=spk, source_edited_transcript=edited)


def _podcast(tail=("Sam.", 1374290, 1384110), **kw):
    return [_seg(0, 0, 4000, "Welcome to Kauli Guides.", spk="SPK_1"), _seg(1, 4500, 9000, "Always check your numbers.", spk="SPK_2"),
            _seg(2, 9000, 11000, "", kind="gap"), _seg(3, tail[1], tail[2], tail[0], spk="SPK_2", **kw)]


def test_one_word_stretched_over_ten_seconds_at_the_end_is_removed_and_the_stretch_becomes_non_speech():
    segs = _podcast()
    assert drop_stray_final_words(segs) == ["Sam."] and [s.source_transcript for s in segs if s.segment_type != "gap"] == ["Welcome to Kauli Guides.", "Always check your numbers."]
    filled = _insert_non_speech_segments(segs, 1384110)
    assert filled[-1].segment_type == "gap" and filled[-1].end_ms == 1384110 and not any("Sam" in (s.source_transcript or "") for s in filled)


def test_real_speech_a_human_edit_and_short_clips_are_left_alone():
    assert drop_stray_final_words(_podcast(tail=("Thank you for listening, see you next week.", 1374290, 1384110))) == []          # a real closing line
    assert drop_stray_final_words(_podcast(tail=("Bye.", 1374290, 1376290))) == []                                                    # a short word is normal
    typed = _podcast(edited="Sam, the host.")
    assert drop_stray_final_words(typed) == [] and typed[-1].source_edited_transcript == "Sam, the host."                       # someone wrote it: it stays
    labelled = _podcast(edited="DAN: Sam.")                                                                                      # only the speaker label added
    assert drop_stray_final_words(labelled) == ["Sam."]
    assert drop_stray_final_words([_seg(0, 0, 9000, "Hello.")]) == []                                                           # a clip of one word is the clip


def test_an_order_made_before_the_guard_is_repaired_when_it_is_opened_and_its_files_are_written_again(env):
    with tempfile.TemporaryDirectory() as d:
        job = Job(source_duration_ms=1384110, source_language="en", target_language="en", segments=_podcast())
        order = {"outdir": d, "target_lang": "en", "source_lang": "en"}
        assert env.app._drop_stray_ending(job, order) is True
        assert not any("Sam." in (s.source_transcript or "") for s in job.segments) and job.segments[-1].segment_type == "gap"
        assert any("stray word" in w and "Sam." in w for w in job.warnings)
        assert "Sam" not in (Path(d) / "transcript_en.srt").read_text() and (Path(d) / "subs_en.vtt").exists()
        assert env.app._drop_stray_ending(job, order) is False                                                                  # nothing left to remove
