"""Groq and AssemblyAI as low-cost transcription providers, the chain that tries providers in turn, and who gets which:
Transkriptor for everyone who pays and for a new account's first short upload, the low-cost providers for the free minutes
after that, and each as the next stop behind the other."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from kauli.providers import asr as asr_mod, asr_cloud  # noqa: E402
from webapp import billing  # noqa: E402


def _resp(status=200, body=None, text=""):
    return SimpleNamespace(status_code=status, json=lambda: body, text=text)


@pytest.fixture
def audio(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x" * 100)
    return f


# ------------------------------------------------------------------------------------------------ Groq
GROQ_BODY = {
    "segments": [
        {"start": 0.0, "end": 2.0, "text": " Hello there.", "avg_logprob": -0.1, "no_speech_prob": 0.01},
        {"start": 2.0, "end": 4.0, "text": " Thanks for joining.", "avg_logprob": -0.2, "no_speech_prob": 0.02},
        {"start": 4.0, "end": 6.0, "text": " Thank you for watching.", "avg_logprob": -1.9, "no_speech_prob": 0.97},     # invented over silence
    ],
    "words": [{"word": "Hello", "start": 0.0, "end": 0.6}, {"word": "there.", "start": 0.7, "end": 1.5},
              {"word": "Thanks", "start": 2.1, "end": 2.6}, {"word": "for", "start": 2.7, "end": 2.9}, {"word": "joining.", "start": 3.0, "end": 3.8}],
}


def test_groq_segments_carry_words_confidence_and_drop_whisper_inventions():
    segs = asr_cloud.GroqWhisperASR._parse(GROQ_BODY, "en", offset_ms=0)
    assert [s.source_transcript for s in segs] == ["Hello there.", "Thanks for joining."]
    assert [w.text for w in segs[1].words] == ["Thanks", "for", "joining."] and segs[1].words[0].start_ms == 2100
    assert 0.8 < segs[0].source_confidence <= 1.0 and segs[0].segment_id == "seg_0001"


def test_groq_joins_chunks_of_a_long_file_with_the_right_offsets(monkeypatch, audio, tmp_path):
    parts = [(audio, 0), (audio, 1_200_000)]
    monkeypatch.setattr(asr_cloud, "prepare_chunks", lambda src, tmp, chunk_s: parts)
    sent = []
    monkeypatch.setattr(asr_cloud.httpx, "post", lambda url, **kw: (sent.append(kw), _resp(200, GROQ_BODY))[1])
    segs = asr_cloud.GroqWhisperASR(api_key="k").transcribe(str(audio), "en")
    assert len(segs) == 4 and segs[2].start_ms == 1_200_000 and segs[3].words[0].start_ms == 1_202_100
    assert [s.segment_id for s in segs] == ["seg_0001", "seg_0002", "seg_0003", "seg_0004"]
    assert sent[0]["data"]["model"] == "whisper-large-v3-turbo" and sent[0]["headers"]["Authorization"] == "Bearer k"


def test_groq_refuses_what_it_cannot_do_so_the_chain_can_move_on(audio):
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asr_cloud.GroqWhisperASR(api_key="").transcribe(str(audio), "en")
    with pytest.raises(RuntimeError, match="does not cover"):
        asr_cloud.GroqWhisperASR(api_key="k").transcribe(str(audio), "ke")                 # Kikuyu


def test_groq_http_errors_become_plain_failures(monkeypatch, audio):
    monkeypatch.setattr(asr_cloud, "prepare_chunks", lambda src, tmp, chunk_s: [(audio, 0)])
    monkeypatch.setattr(asr_cloud.httpx, "post", lambda url, **kw: _resp(429, text="rate limit"))
    with pytest.raises(RuntimeError, match="Groq 429"):
        asr_cloud.GroqWhisperASR(api_key="k").transcribe(str(audio), "en")


# ------------------------------------------------------------------------------------------------ AssemblyAI
AAI_BODY = {"status": "completed", "words": [
    {"text": "Hi", "start": 0, "end": 300, "confidence": 0.99, "speaker": "A"}, {"text": "there.", "start": 350, "end": 900, "confidence": 0.95, "speaker": "A"},
    {"text": "Hello", "start": 1500, "end": 2000, "confidence": 0.9, "speaker": "B"}, {"text": "back.", "start": 2050, "end": 2500, "confidence": 0.9, "speaker": "B"}]}


def test_assemblyai_words_become_segments_with_speaker_ids():
    segs = asr_cloud.AssemblyAIASR._parse(AAI_BODY, "en")
    assert [s.source_transcript for s in segs] == ["Hi there.", "Hello back."]
    assert [s.speaker_id for s in segs] == ["Speaker 1", "Speaker 2"] and segs[1].start_ms == 1500


def test_assemblyai_uploads_requests_and_polls(monkeypatch, audio):
    monkeypatch.setattr(asr_cloud, "prepare_chunks", lambda src, tmp, chunk_s: [(audio, 0)])
    calls = []

    def post(url, **kw):
        calls.append(url)
        return _resp(200, {"upload_url": "https://cdn/x"}) if url.endswith("/upload") else _resp(200, {"id": "t1"})

    states = iter([{"status": "processing"}, AAI_BODY])
    monkeypatch.setattr(asr_cloud.httpx, "post", post)
    monkeypatch.setattr(asr_cloud.httpx, "get", lambda url, **kw: _resp(200, next(states)))
    segs = asr_cloud.AssemblyAIASR(api_key="k", poll_interval_s=0).transcribe(str(audio), "en")
    assert len(segs) == 2 and calls[0].endswith("/upload") and calls[1].endswith("/transcript")


def test_assemblyai_errors_and_missing_key_fail_plainly(monkeypatch, audio):
    with pytest.raises(RuntimeError, match="ASSEMBLYAI_API_KEY"):
        asr_cloud.AssemblyAIASR(api_key="").transcribe(str(audio), "en")
    monkeypatch.setattr(asr_cloud, "prepare_chunks", lambda src, tmp, chunk_s: [(audio, 0)])
    monkeypatch.setattr(asr_cloud.httpx, "post", lambda url, **kw: _resp(401, text="bad key"))
    with pytest.raises(RuntimeError, match="AssemblyAI upload 401"):
        asr_cloud.AssemblyAIASR(api_key="k").transcribe(str(audio), "en")


# ------------------------------------------------------------------------------------------------ the chain
class _Fake:
    def __init__(self, name, result):
        self.name, self._result = name, result

    def transcribe(self, path, language="sw"):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _chain(monkeypatch, fakes, names):
    monkeypatch.setattr(asr_cloud, "get_asr", lambda name, **kw: fakes[name])
    return asr_cloud.ChainASR(names)


def test_the_chain_returns_the_first_provider_that_works_and_says_which(monkeypatch):
    chain = _chain(monkeypatch, {"a": _Fake("a", RuntimeError("down")), "b": _Fake("b", ["seg"]), "c": _Fake("c", ["no"])}, ["a", "b", "c"])
    assert chain.transcribe("x.mp3") == ["seg"] and chain.used_name == "b" and "a: RuntimeError: down" in chain.notes[0]


def test_the_chain_tries_the_next_provider_when_one_hears_nothing_but_accepts_silence_at_the_end(monkeypatch):
    chain = _chain(monkeypatch, {"a": _Fake("a", []), "b": _Fake("b", ["heard"])}, ["a", "b"])
    assert chain.transcribe("x.mp3") == ["heard"] and chain.used_name == "b"
    silent = _chain(monkeypatch, {"a": _Fake("a", []), "b": _Fake("b", [])}, ["a", "b"])
    assert silent.transcribe("x.mp3") == [] and silent.used_name == "b"


def test_a_chain_where_everything_fails_raises_with_every_reason(monkeypatch):
    chain = _chain(monkeypatch, {"a": _Fake("a", RuntimeError("x")), "b": _Fake("b", ValueError("y"))}, ["a", "b"])
    with pytest.raises(RuntimeError, match="a: RuntimeError: x; b: ValueError: y"):
        chain.transcribe("x.mp3")


def test_get_asr_understands_chains_and_the_new_providers(monkeypatch):
    chain = asr_mod.get_asr("chain:groq-whisper,assemblyai,faster-whisper")
    assert isinstance(chain, asr_cloud.ChainASR) and chain.names == ["groq-whisper", "assemblyai", "faster-whisper"]
    assert isinstance(asr_mod.get_asr("groq-whisper"), asr_cloud.GroqWhisperASR) and isinstance(asr_mod.get_asr("assemblyai"), asr_cloud.AssemblyAIASR)
    with pytest.raises(ValueError):
        asr_mod.get_asr("nope")


def test_transkriptor_can_raise_instead_of_falling_back_so_the_chain_can_go_on(monkeypatch):
    monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "k")
    monkeypatch.setattr(asr_mod.TranskriptorASR, "_transcribe_via_api", lambda self, p, l, k: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setattr(asr_mod.FasterWhisperASR, "transcribe", lambda self, p, language="sw": ["whisper"])
    assert asr_mod.TranskriptorASR().transcribe("a.wav") == ["whisper"]                      # unchanged on its own
    with pytest.raises(RuntimeError, match="Transkriptor ASR failed"):
        asr_mod.TranskriptorASR(local_fallback=False).transcribe("a.wav")


def test_the_free_allowance_costs_cents_on_the_low_cost_providers():
    for provider in (asr_cloud.GroqWhisperASR, asr_cloud.AssemblyAIASR):
        assert provider.cost_per_minute_usd * billing.FREE_MINUTES_PER_MONTH <= 0.20, provider.name
    assert billing.FREE_MINUTES_PER_MONTH == 30


# ------------------------------------------------------------------------------------------------ routing
def test_the_routing_rules(env):
    route = env.app._route_asr
    env.monkeypatch.setenv("KAULI_OWN_ASR", "off")                                                           # our own model has its own tests
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "t")
    assert route("transkriptor", source_lang="en", free_covered=True) == "transkriptor"                       # no low-cost key: unchanged
    env.monkeypatch.setenv("GROQ_API_KEY", "g")
    env.monkeypatch.setenv("ASSEMBLYAI_API_KEY", "a")
    assert route("transkriptor", source_lang="en", free_covered=True) == "chain:groq-whisper,assemblyai,transkriptor,faster-whisper"
    assert route("transkriptor", source_lang="en", free_covered=False) == "chain:transkriptor,groq-whisper,assemblyai,faster-whisper"
    assert route("transkriptor", source_lang="sw", free_covered=True) == "chain:transkriptor,groq-whisper,assemblyai,faster-whisper"   # Swahili keeps the quality provider
    assert route("manual", source_lang="ke", free_covered=True) == "manual" and route("elevenlabs-scribe", source_lang="en", free_covered=True) == "elevenlabs-scribe"
    env.monkeypatch.setenv("KAULI_CHEAP_ASR_ORDER", "assemblyai,groq-whisper")
    assert route("transkriptor", source_lang="en", free_covered=True).startswith("chain:assemblyai,groq-whisper,")
    env.monkeypatch.delenv("TRANSKRIPTOR_API_KEY")
    assert route("faster-whisper", source_lang="en", free_covered=True) == "chain:assemblyai,groq-whisper,faster-whisper"
    env.monkeypatch.setenv("KAULI_CHEAP_ASR", "off")
    assert route("faster-whisper", source_lang="en", free_covered=True) == "faster-whisper"


def test_instant_ai_goes_to_the_low_cost_providers_first_from_the_very_first_upload(env, wav_bytes):
    env.monkeypatch.setenv("KAULI_OWN_ASR", "off")                                                           # our own model has its own tests
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "t")
    env.monkeypatch.setenv("GROQ_API_KEY", "g")
    env.monkeypatch.setenv("ASSEMBLYAI_API_KEY", "a")
    first = env.order_from(env.place(wav_bytes, source_lang="en"))
    assert first["asr"] == "chain:groq-whisper,assemblyai,transkriptor,faster-whisper"           # no special first upload any more
    assert env.order_from(env.place(wav_bytes, source_lang="en"))["asr"] == first["asr"]
    swahili = env.order_from(env.place(wav_bytes, source_lang="sw"))
    assert swahili["asr"].startswith("chain:transkriptor,")                                       # the languages we are known for
    env.monkeypatch.setenv("KAULI_CHEAP_ASR", "off")
    assert env.order_from(env.place(wav_bytes, source_lang="en"))["asr"] == "transkriptor"


def test_human_reviewed_orders_keep_transkriptor_first_and_instant_ai_on_a_plan_goes_to_the_low_cost_providers(env, wav_bytes):
    env.monkeypatch.setenv("KAULI_OWN_ASR", "off")
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "t")
    env.monkeypatch.setenv("GROQ_API_KEY", "g")
    env.db.set_subscription_plan("u1", "pro", 30)
    assert env.order_from(env.place(wav_bytes, source_lang="en"))["asr"] == "chain:groq-whisper,transkriptor,faster-whisper"
    human = env.order_from(env.place(wav_bytes, source_lang="en", review_tier="human"))
    assert human["asr"].startswith("chain:transkriptor,")


def test_instant_ai_orders_start_with_our_own_model_where_it_is_accurate(env, wav_bytes):
    env.monkeypatch.delenv("KAULI_OWN_ASR", raising=False)
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "t")
    env.monkeypatch.setenv("GROQ_API_KEY", "g")
    english = env.order_from(env.place(wav_bytes, source_lang="en"))
    assert english["asr"] == "chain:own,groq-whisper,transkriptor"
    assert env.order_from(env.place(wav_bytes, source_lang="sw"))["asr"].startswith("chain:transkriptor,")
    assert env.order_from(env.place(wav_bytes, source_lang="en", review_tier="human"))["asr"].startswith("chain:transkriptor,")


def test_the_manifest_records_which_provider_really_ran():
    chain = asr_cloud.ChainASR(["a"])
    chain.used_name, chain.notes = "assemblyai", ["Transcribed by assemblyai after: groq-whisper: RuntimeError: down"]
    from kauli import pipeline
    src = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert 'getattr(asr_provider, "used_name", None)' in src and 'job.providers["asr"] = ran' in src
