"""ElevenLabs audio clean-up and speaker labels: the provider calls, the pipeline hooks, pricing and the
margin guard, ordering, and the refund when an add-on is not delivered. No network: httpx is replaced."""
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg not installed", allow_module_level=True)

from kauli import pipeline  # noqa: E402
from kauli.models import Segment  # noqa: E402
from kauli.providers import elevenlabs_audio as ea  # noqa: E402
from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)
from webapp import billing  # noqa: E402


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "in.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ar", "16000", "-ac", "1", str(path)], check=True)
    return path


class Recorder:
    def __init__(self, status=200, body=b"", payload=None):
        self.calls, self.status, self.body, self.payload = [], status, body, payload or {}

    def __call__(self, url, headers=None, files=None, data=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, headers=headers, files=files, data=data))
        return SimpleNamespace(status_code=self.status, content=self.body, json=lambda: self.payload)


# ------------------------------------------------------------------------ audio clean-up
def test_isolation_uploads_the_audio_and_returns_a_16k_mono_wav(monkeypatch, clip, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi_key")
    cleaned_source = clip.read_bytes()                                  # ElevenLabs hands back audio; any container works
    rec = Recorder(body=cleaned_source)
    out = tmp_path / "cleaned.wav"
    ea.isolate_audio(str(clip), str(out), post=rec)
    call = rec.calls[0]
    assert call.url == "https://api.elevenlabs.io/v1/audio-isolation" and call.headers == {"xi-api-key": "xi_key"}
    assert call.files["audio"][0] == "audio.mp3" and call.data == {"file_format": "other"}
    with wave.open(str(out), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)


@pytest.mark.parametrize("status,words", [(401, "API key"), (413, "too large"), (429, "too many")])
def test_isolation_errors_are_plain(monkeypatch, clip, tmp_path, status, words):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    with pytest.raises(RuntimeError, match=f"ElevenLabs {status}") as exc:
        ea.isolate_audio(str(clip), str(tmp_path / "o.wav"), post=Recorder(status=status))
    assert words in str(exc.value)


def test_no_key_never_calls_out(monkeypatch, clip, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    rec = Recorder()
    with pytest.raises(RuntimeError, match="not set"):
        ea.isolate_audio(str(clip), str(tmp_path / "o.wav"), post=rec)
    with pytest.raises(RuntimeError, match="not set"):
        ea.diarize_turns(str(clip), post=rec)
    assert rec.calls == []


# --------------------------------------------------------------------------- speaker labels
WORDS = [
    {"text": "Hello", "type": "word", "start": 0.0, "end": 0.5, "speaker_id": "speaker_0"},
    {"text": " ", "type": "spacing", "start": 0.5, "end": 0.6, "speaker_id": "speaker_0"},
    {"text": "there", "type": "word", "start": 0.6, "end": 1.0, "speaker_id": "speaker_0"},
    {"text": "(laughter)", "type": "audio_event", "start": 1.0, "end": 1.4, "speaker_id": None},
    {"text": "Hi", "type": "word", "start": 2.0, "end": 2.4, "speaker_id": "speaker_1"},
    {"text": "again", "type": "word", "start": 2.5, "end": 3.0, "speaker_id": "speaker_1"},
    {"text": "Back", "type": "word", "start": 3.1, "end": 3.5, "speaker_id": "speaker_0"},
]


def test_diarization_asks_scribe_for_speakers_without_forcing_a_language(monkeypatch, clip):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi_key")
    rec = Recorder(payload={"language_code": "sw", "text": "x", "words": WORDS})
    turns = ea.diarize_turns(str(clip), post=rec)
    call = rec.calls[0]
    assert call.url == "https://api.elevenlabs.io/v1/speech-to-text" and call.headers == {"xi-api-key": "xi_key"}
    assert call.data["model_id"] == "scribe_v2" and call.data["diarize"] == "true" and "language_code" not in call.data
    assert turns == [(0, 1000, "speaker_0"), (2000, 3000, "speaker_1"), (3100, 3500, "speaker_0")]
    monkeypatch.setenv("ELEVENLABS_STT_MODEL", "scribe_v3")
    ea.diarize_turns(str(clip), post=rec)
    assert rec.calls[-1].data["model_id"] == "scribe_v3"
    with pytest.raises(RuntimeError, match="ElevenLabs 422"):
        ea.diarize_turns(str(clip), post=Recorder(status=422))


def _seg(i, start, end, speaker=None, kind="speech"):
    return Segment(segment_id=f"s{i}", index=i, start_ms=start, end_ms=end, speaker_id=speaker, segment_type=kind)


def test_segments_get_the_speaker_who_talks_most_and_existing_labels_are_kept():
    turns = [(0, 1000, "speaker_0"), (2000, 3000, "speaker_1"), (3100, 3500, "speaker_0")]
    segs = [_seg(0, 0, 1500), _seg(1, 1800, 3200), _seg(2, 3200, 3600), _seg(3, 3600, 4000, kind="gap"),
            _seg(4, 500, 900, speaker="Editor's label")]
    found = ea.assign_speakers(segs, turns)
    assert found == 2
    assert [s.speaker_id for s in segs] == ["Speaker 1", "Speaker 2", "Speaker 1", None, "Editor's label"]


# --------------------------------------------------------------------------- pipeline hooks
class SpyASR:
    name = "spy"
    cost_per_minute_usd = 0.0

    def __init__(self):
        self.heard = None

    def transcribe(self, audio_path, language="sw"):
        self.heard = audio_path
        return [_seg(0, 0, 1500), _seg(1, 1600, 2900)]


def _run_pipeline(tmp_path, clip, monkeypatch, **kw):
    asr = SpyASR()
    monkeypatch.setattr(pipeline, "get_asr", lambda name, **k: asr)
    job = pipeline.run(str(clip), str(tmp_path / "out"), asr="spy", mt="stub", tts="stub", source_lang="en",
                       target_lang="en", skip_tts=True, verbose=False, **kw)
    return job, asr


def test_pipeline_cleans_the_audio_and_labels_speakers_when_asked(tmp_path, clip, monkeypatch):
    def fake_isolate(src, out):
        shutil.copy(src, out)

    monkeypatch.setattr(ea, "isolate_audio", fake_isolate)
    monkeypatch.setattr(ea, "diarize_turns", lambda src: [(0, 1500, "a"), (1600, 2900, "b")])
    job, asr = _run_pipeline(tmp_path, clip, monkeypatch, clean_audio=True, label_speakers=True)
    assert asr.heard.endswith("cleaned.wav") and job.providers["audio_cleanup"] == "elevenlabs"   # transcribed the clean copy
    assert job.providers["speaker_labels"] == "elevenlabs"
    assert [s.speaker_id for s in job.segments if s.segment_type == "speech"] == ["Speaker 1", "Speaker 2"]


def test_pipeline_does_nothing_extra_by_default(tmp_path, clip, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(ea, "isolate_audio", boom)
    monkeypatch.setattr(ea, "diarize_turns", boom)
    job, asr = _run_pipeline(tmp_path, clip, monkeypatch)
    assert asr.heard == str(clip) and "audio_cleanup" not in job.providers and "speaker_labels" not in job.providers


def test_an_elevenlabs_failure_never_loses_the_order(tmp_path, clip, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("ElevenLabs 429: too many requests")

    monkeypatch.setattr(ea, "isolate_audio", down)
    monkeypatch.setattr(ea, "diarize_turns", down)
    job, asr = _run_pipeline(tmp_path, clip, monkeypatch, clean_audio=True, label_speakers=True)
    assert asr.heard == str(clip)                                                  # fell back to the original audio
    assert "audio_cleanup" not in job.providers and "speaker_labels" not in job.providers
    assert any("Audio clean-up skipped" in w for w in job.warnings) and any("Speaker labels skipped" in w for w in job.warnings)
    assert len([s for s in job.segments if s.segment_type == "speech"]) == 2     # the transcript is intact


# ------------------------------------------------------------------------- pricing guards
def test_audio_cleanup_clears_the_margin_floor():
    cost, rate = billing.audio_cleanup_unit_cost_usd(), billing.ADDONS["audio_cleanup"]["rate_per_min"]
    assert cost == 0.12 and rate >= billing.audio_cleanup_min_rate() and billing.audio_cleanup_margin_ok()
    assert (rate * (1 - billing.PAYMENT_FEE_RATE) - cost) / rate >= billing.MIN_PREMIUM_MARGIN


def test_speaker_labels_cost_next_to_nothing(monkeypatch):
    assert billing.SPEAKER_LABELS_USD_PER_MIN < 0.006                                # under a cent a minute
    assert billing.SPEAKER_LABELS_USD_PER_MIN < 0.05 * billing.PRICE_CARD["intl"]["transcribe"]["ai"]   # under 5% of the cheapest rate


def test_audio_cleanup_switches_itself_off_if_it_would_lose_money(env, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert env.app._audio_cleanup_available()
    monkeypatch.setenv("ELEVENLABS_ISOLATOR_USD_PER_MIN", "0.30")                    # e.g. ElevenLabs raised its price
    assert not billing.audio_cleanup_margin_ok() and not env.app._audio_cleanup_available()
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.delenv("ELEVENLABS_ISOLATOR_USD_PER_MIN")
    assert not env.app._audio_cleanup_available()


# ----------------------------------------------------------------------------- ordering
@pytest.fixture
def studio(env, wav_bytes, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(env.app, "_audio_cleanup_capacity_ok", lambda minutes: True)
    env.wav = wav_bytes
    return env


def test_a_noisy_recording_can_be_cleaned_first_for_a_flat_per_minute_price(studio):
    plain = studio.order_from(studio.place(studio.wav))
    cleaned = studio.order_from(studio.place(studio.wav, addon_audio_cleanup="1"))
    assert studio.db.order_has_addon(cleaned, "audio_cleanup") and not studio.db.order_has_addon(plain, "audio_cleanup")
    line = [x for x in json.loads(cleaned["cost_breakdown_json"])["addons"] if x["key"] == "audio_cleanup"][0]
    assert line["rate_per_min"] == 0.30 and abs(line["cost_usd"] - 0.30 * cleaned["duration_minutes"]) < 0.005
    assert cleaned["cost_usd"] > plain["cost_usd"]                                    # paid for in full, never absorbed by free minutes


def test_clean_up_is_refused_without_a_key_or_a_safe_margin_or_spare_credits(studio, monkeypatch):
    monkeypatch.setattr(studio.app, "_audio_cleanup_capacity_ok", lambda minutes: False)
    r = studio.place(studio.wav, addon_audio_cleanup="1")
    assert "at capacity" in r.text and studio.db.list_orders_for_client("u1") == []
    monkeypatch.setattr(studio.app, "_audio_cleanup_capacity_ok", lambda minutes: True)
    monkeypatch.setenv("ELEVENLABS_ISOLATOR_USD_PER_MIN", "0.50")
    assert "available right now" in studio.place(studio.wav, addon_audio_cleanup="1").text
    monkeypatch.delenv("ELEVENLABS_ISOLATOR_USD_PER_MIN")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert "available right now" in studio.place(studio.wav, addon_audio_cleanup="1").text
    assert studio.db.list_orders_for_client("u1") == []


def test_the_wizard_offers_clean_up_only_when_it_can_be_sold(studio, monkeypatch):
    html = studio.client.get("/client").text
    assert 'id="audio-cleanup-checkbox"' in html and 'data-audio-cleanup-rate="0.3"' in html and "ElevenLabs" in html
    assert 'id="price-audio-cleanup-row"' in html
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert 'id="audio-cleanup-checkbox"' not in studio.client.get("/client").text


# ------------------------------------------------------------- the worker and the refunds
def _run(env, order, providers):
    worker = env.app.worker
    calls = []
    env.monkeypatch.setattr(worker, "kauli_run", lambda **kw: calls.append(kw))
    env.monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(
        cost_usd=0.01, flagged_count=0, providers=providers)))
    env.monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    worker._run_job(order["id"])
    return calls


def test_the_worker_passes_the_studio_options_to_the_pipeline(studio, monkeypatch):
    plain = studio.order_from(studio.place(studio.wav))
    assert _run(studio, plain, {})[0]["clean_audio"] is False and _run(studio, plain, {})[0]["label_speakers"] is False
    cleaned = studio.order_from(studio.place(studio.wav, addon_audio_cleanup="1", instr_speaker_ids="1"))
    call = _run(studio, cleaned, {"audio_cleanup": "elevenlabs"})[0]
    assert call["clean_audio"] is True and call["label_speakers"] is True
    monkeypatch.setenv("KAULI_SPEAKER_LABELS", "0")                                  # the kill switch
    assert _run(studio, cleaned, {"audio_cleanup": "elevenlabs"})[0]["label_speakers"] is False
    monkeypatch.delenv("KAULI_SPEAKER_LABELS")
    monkeypatch.delenv("ELEVENLABS_API_KEY")                                         # no key, no speaker labels
    assert _run(studio, cleaned, {})[0]["label_speakers"] is False


def test_a_skipped_clean_up_is_credited_back_once(studio):
    order = studio.order_from(studio.place(studio.wav, addon_audio_cleanup="1"))
    before = studio.db.wallet_credits_remaining("u1")
    _run(studio, order, {"audio_cleanup": "elevenlabs"})                              # delivered as paid for
    assert studio.db.wallet_credits_remaining("u1") == before
    _run(studio, order, {})                                                           # ElevenLabs failed, original audio used
    expected = billing.usd_to_credits(0.30 * order["duration_minutes"])
    assert abs(studio.db.wallet_credits_remaining("u1") - before - expected) < 0.01
    assert studio.db.get_order(order["id"])["audio_cleanup_refunded_at"]
    _run(studio, order, {})                                                           # a retry never refunds twice
    assert abs(studio.db.wallet_credits_remaining("u1") - before - expected) < 0.01


def test_the_two_add_ons_are_refunded_independently(studio, monkeypatch):
    monkeypatch.setattr(studio.app, "_premium_voices", lambda force=False: [{"id": "21m00Tcm4TlvDq8ikWAM", "name": "R", "description": ""}])
    monkeypatch.setattr(studio.app, "_premium_capacity_ok", lambda minutes: True)
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "s")
    order = studio.order_from(studio.place(studio.wav, source_lang="sw", target_lang="en", service_level="dub",
                                           review_tier="ai", addon_audio_cleanup="1", addon_premium_voice="1",
                                           premium_voice_id="21m00Tcm4TlvDq8ikWAM"))
    before = studio.db.wallet_credits_remaining("u1")
    _run(studio, order, {"tts": "elevenlabs"})                                        # voice fine, clean-up skipped
    minutes = order["duration_minutes"]
    assert abs(studio.db.wallet_credits_remaining("u1") - before - billing.usd_to_credits(0.30 * minutes)) < 0.01
    _run(studio, order, {"tts": "piper"})                                             # now the voice fell back too
    both = billing.usd_to_credits(0.30 * minutes) + billing.usd_to_credits(0.50 * minutes)
    assert abs(studio.db.wallet_credits_remaining("u1") - before - both) < 0.01
