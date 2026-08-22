"""Run with: python -m pytest tests/ -v   (or just: python tests/test_pipeline.py)"""
import sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kauli import timing
from kauli.pipeline import run
from kauli.models import Job


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
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        for s in job.segments:
            assert abs(s.time_stretch_pct) <= timing.MAX_STRETCH_PCT


def test_manifest_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        job = run("/dev/null", d, asr="stub", mt="stub", tts="stub", verbose=False)
        reloaded = Job.load(os.path.join(d, "manifest.json"))
        assert len(reloaded.segments) == len(job.segments)
        assert reloaded.segments[0].final_text == job.segments[0].final_text


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
