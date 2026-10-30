"""Our own transcription model: runs first where it is reliable, checks its own confidence, and hands over to an outside provider when it is not."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli.models import Segment, Word  # noqa: E402
from kauli.providers import asr as asr_mod  # noqa: E402
from kauli.providers import asr_cloud  # noqa: E402
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _segments(prob: float):
    words = [Word(text=w, start_ms=i * 300, end_ms=i * 300 + 250, confidence=prob) for i, w in enumerate("hello there my friend".split())]
    return [Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1200, source_language="en", source_transcript="hello there my friend",
                    source_confidence=prob, words=words)]


def _own(monkeypatch, prob):
    monkeypatch.setattr(asr_mod.FasterWhisperASR, "transcribe", lambda self, path, language="sw": _segments(prob))
    return asr_mod.get_asr("own")


def test_it_returns_a_transcript_it_is_confident_in(monkeypatch):
    segs = _own(monkeypatch, 0.93).transcribe("x.wav", language="en")
    assert segs[0].source_transcript == "hello there my friend"


def test_it_refuses_to_pass_off_a_shaky_transcript_and_keeps_the_draft(monkeypatch):
    own = _own(monkeypatch, 0.41)
    with pytest.raises(asr_mod.LowQualityTranscript) as e:
        own.transcribe("x.wav", language="en")
    assert e.value.segments[0].source_transcript == "hello there my friend" and abs(e.value.score - 0.41) < 0.001
    monkeypatch.setenv("KAULI_OWN_ASR_MIN_CONF", "0.30")                                         # the bar is a setting
    assert _own(monkeypatch, 0.41).transcribe("x.wav", language="en")


def test_the_chain_hands_a_shaky_recording_to_the_next_provider(monkeypatch):
    _own(monkeypatch, 0.41)

    class Outside(asr_mod.ASRProvider):
        name = "outside"

        def transcribe(self, audio_path, language="sw"):
            return [Segment(segment_id="seg_0001", index=0, start_ms=0, end_ms=1000, source_language=language, source_transcript="the paid provider's text",
                            source_confidence=0.95)]
    monkeypatch.setitem(asr_mod._REGISTRY, "outside", Outside)
    chain = asr_cloud.ChainASR(["own", "outside"])
    segs = chain.transcribe("x.wav", language="en")
    assert segs[0].source_transcript == "the paid provider's text" and chain.used_name == "outside"
    assert any("own" in n and "low confidence" in n for n in chain.notes)


def test_a_confident_own_transcript_never_reaches_the_paid_provider(monkeypatch):
    _own(monkeypatch, 0.95)
    called = []

    class Outside(asr_mod.ASRProvider):
        name = "outside"

        def transcribe(self, audio_path, language="sw"):
            called.append(1)
            return []
    monkeypatch.setitem(asr_mod._REGISTRY, "outside", Outside)
    chain = asr_cloud.ChainASR(["own", "outside"])
    assert chain.transcribe("x.wav", language="en") and chain.used_name == "own" and called == []


def test_when_every_other_provider_fails_our_own_draft_is_delivered_not_lost(monkeypatch):
    _own(monkeypatch, 0.41)

    class Broken(asr_mod.ASRProvider):
        name = "broken"

        def transcribe(self, audio_path, language="sw"):
            raise RuntimeError("quota exceeded")
    monkeypatch.setitem(asr_mod._REGISTRY, "broken", Broken)
    chain = asr_cloud.ChainASR(["own", "broken"])
    segs = chain.transcribe("x.wav", language="en")
    assert segs[0].source_transcript == "hello there my friend" and chain.used_name == "own"
    assert any("no other provider could take it" in n for n in chain.notes)


def test_models_are_kept_on_the_persistent_volume(monkeypatch, tmp_path):
    monkeypatch.delenv("KAULI_ASR_MODEL_DIR", raising=False)
    assert asr_mod.FasterWhisperASR.model_root().replace("\\", "/").endswith("webapp/data/models/whisper")
    monkeypatch.setenv("KAULI_ASR_MODEL_DIR", str(tmp_path))
    assert asr_mod.FasterWhisperASR.model_root() == str(tmp_path)
    monkeypatch.setenv("KAULI_OWN_ASR_MODEL", "medium")
    assert asr_mod.get_asr("own").model_size == "medium"


# ------------------------------------------------------------------------------------------------ routing
def test_our_own_model_leads_instant_ai_where_it_was_measured_accurate(env, monkeypatch):
    route = env.app._route_asr
    monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "k")
    monkeypatch.delenv("KAULI_CHEAP_ASR", raising=False)
    monkeypatch.delenv("KAULI_OWN_ASR", raising=False)
    monkeypatch.delenv("KAULI_OWN_ASR_LANGUAGES", raising=False)
    monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: ["groq-whisper"])
    # on by default for Instant AI and free minutes (free_covered): our own model, then the low-cost provider, then Transkriptor
    assert route("transkriptor", source_lang="en", free_covered=True) == "chain:own,groq-whisper,transkriptor"
    # human-reviewed and other paid orders keep Transkriptor first
    assert route("transkriptor", source_lang="en", free_covered=False) == "chain:transkriptor,groq-whisper,faster-whisper"
    # languages it was not measured accurate in, and Swahili, stay with the providers built for them
    assert "own" not in route("transkriptor", source_lang="sw", free_covered=True)
    assert "own" not in route("transkriptor", source_lang="ru", free_covered=True)
    # a long file goes to the fast cloud providers: our server would take too long
    assert "own" in route("transkriptor", source_lang="en", free_covered=True, minutes=10)
    assert "own" not in route("transkriptor", source_lang="en", free_covered=True, minutes=45)
    monkeypatch.setenv("KAULI_OWN_ASR_LANGUAGES", "en,sw")
    assert route("transkriptor", source_lang="sw", free_covered=True).startswith("chain:own,")
    monkeypatch.delenv("KAULI_OWN_ASR_LANGUAGES")
    monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: [])
    assert route("faster-whisper", source_lang="en", free_covered=True) == "chain:own"
    monkeypatch.setenv("KAULI_OWN_ASR", "off")
    assert "own" not in route("transkriptor", source_lang="en", free_covered=True)


def test_the_failover_view_lists_it(env, wav_bytes, monkeypatch):
    from webapp import provider_health
    order = env.order_from(env.place(wav_bytes, source_lang="en", service_level="transcribe", review_tier="ai"))
    opts = {a["name"]: a for a in provider_health.alternatives(order, "asr")}
    assert opts["own"]["label"] == "Kauli's own transcription model" and opts["own"]["fits"] and opts["own"]["connected"]
    assert "own" in provider_health.AUTO_ORDER["asr"]
