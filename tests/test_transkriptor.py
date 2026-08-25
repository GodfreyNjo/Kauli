"""kauli/providers/asr.py:TranskriptorASR - unit tests for the parts that
don't need real network access (the live API contract itself, including
the millisecond-vs-second timing unit, was verified separately against a
real 8s clip - see that class's docstring). These tests are what stay
green in CI / on any machine without a real TRANSKRIPTOR_API_KEY."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch, MagicMock

from kauli.providers.asr import TranskriptorASR, FasterWhisperASR, _approximate_words
from kauli.models import Segment


def test_approximate_words_spans_the_full_range_proportionally():
    words = _approximate_words("hi there friend", start_ms=1000, end_ms=1900, confidence=0.9)
    assert [w.text for w in words] == ["hi", "there", "friend"]
    assert words[0].start_ms == 1000
    assert words[-1].end_ms == 1900  # tail drift absorbed exactly, not left short/long
    assert all(w.confidence == 0.9 for w in words)
    # each word's own slot is non-overlapping and in order
    for a, b in zip(words, words[1:]):
        assert a.end_ms == b.start_ms


def test_approximate_words_empty_text():
    assert _approximate_words("   ", 0, 1000, 0.9) == []


def test_parse_uses_milliseconds_and_skips_empty_or_zero_duration():
    provider = TranskriptorASR(api_key="fake")
    content = [
        {"text": "Habari.", "StartTime": 500, "EndTime": 1500, "Speaker": "SPK_1"},
        {"text": "  ", "StartTime": 2000, "EndTime": 3000, "Speaker": "SPK_1"},  # blank - skipped
        {"text": "Zero span.", "StartTime": 4000, "EndTime": 4000, "Speaker": "SPK_1"},  # skipped
        {"text": "Asante.", "StartTime": 5000, "EndTime": 6200, "Speaker": "SPK_2"},
    ]
    segs = provider._parse(content, "sw")
    assert [s.source_transcript for s in segs] == ["Habari.", "Asante."]
    assert segs[0].start_ms == 500 and segs[0].end_ms == 1500
    assert segs[1].speaker_id == "SPK_2"
    assert all(s.source_confidence == TranskriptorASR.ASSUMED_CONFIDENCE for s in segs)
    # per-word timing was backfilled even though the API gave none
    assert segs[0].words and segs[0].words[0].text == "Habari."


def test_falls_back_to_faster_whisper_on_any_failure():
    provider = TranskriptorASR(api_key="fake", poll_interval_s=0.01, timeout_s=1.0)
    fallback_segs = [Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000,
                              source_transcript="from whisper")]
    with patch("requests.post", side_effect=ConnectionError("no network")), \
         patch.object(FasterWhisperASR, "transcribe", return_value=fallback_segs) as fw:
        result = provider.transcribe("/dev/null", language="sw")
    assert result == fallback_segs
    fw.assert_called_once()
    assert provider.fallback_used is True
    assert "ConnectionError" in provider.fallback_reason


def test_no_api_key_falls_back_immediately_without_a_network_call():
    provider = TranskriptorASR(api_key=None)
    fallback_segs = [Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000)]
    with patch("requests.post") as post, \
         patch.object(FasterWhisperASR, "transcribe", return_value=fallback_segs):
        result = provider.transcribe("/dev/null", language="sw")
    post.assert_not_called()
    assert result == fallback_segs
    assert provider.fallback_used is True
