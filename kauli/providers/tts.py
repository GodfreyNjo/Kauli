"""TTS providers.

Cost note that drives the whole product sequencing:
  sw -> en  : output is English. Piper is free, local, and good. Cost = 0.
  en -> sw  : Amazon Polly has NO Swahili voice. You need Azure
              (sw-KE-ZuriNeural / sw-KE-RafikiNeural) or Google Cloud TTS.
That's why we ship sw->en first.
"""
from __future__ import annotations

import os
import wave


class TTSProvider:
    name = "base"
    sample_rate = 22050

    def synthesize(self, text: str, out_path: str, voice_id: str | None = None,
                   rate: float = 1.0) -> int:
        """Write audio to out_path. Return rendered duration in ms."""
        raise NotImplementedError


class StubTTS(TTSProvider):
    """Writes silence of the estimated length. Lets you test timing, mixing and
    subtitle alignment with zero models installed."""
    name = "stub"

    def synthesize(self, text: str, out_path: str, voice_id=None, rate: float = 1.0) -> int:
        from ..timing import estimate_duration_ms
        dur_ms = int(estimate_duration_ms(text, "en") / max(rate, 0.1))
        n = int(self.sample_rate * dur_ms / 1000)
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(b"\x00\x00" * n)
        return dur_ms


class PiperTTS(TTSProvider):
    """Free, local, offline, CPU-only. Download a voice once:
       https://github.com/rhasspy/piper  (e.g. en_GB-alba-medium)
    Put the .onnx and .onnx.json in ./data/voices/ and pass the path as voice_id.
    """
    name = "piper"
    sample_rate = 22050

    def __init__(self, voice_path: str | None = None):
        self.voice_path = voice_path or os.getenv("KAULI_PIPER_VOICE")
        self._voice = None

    def _load(self, voice_id: str | None):
        path = voice_id or self.voice_path
        if not path:
            raise RuntimeError("No Piper voice configured. Set KAULI_PIPER_VOICE.")
        if self._voice is None:
            from piper.voice import PiperVoice
            self._voice = PiperVoice.load(path)
        return self._voice

    def synthesize(self, text: str, out_path: str, voice_id=None, rate: float = 1.0) -> int:
        # piper-tts >=1.7 returns an iterable of AudioChunk instead of writing
        # straight into a wave.Wave_write (that was the <1.7 API this was
        # originally written against).
        from piper.config import SynthesisConfig

        voice = self._load(voice_id)
        syn_config = SynthesisConfig(length_scale=1.0 / max(rate, 0.1))
        chunks = list(voice.synthesize(text, syn_config=syn_config))

        first = chunks[0] if chunks else None
        sr = first.sample_rate if first else self.sample_rate
        sw = first.sample_width if first else 2
        ch = first.sample_channels if first else 1

        with wave.open(out_path, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(sw)
            w.setframerate(sr)
            for c in chunks:
                w.writeframes(c.audio_int16_bytes)

        with wave.open(out_path, "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)


class AzureTTS(TTSProvider):
    """Needed for the en->sw direction. Voices: sw-KE-ZuriNeural (f),
    sw-KE-RafikiNeural (m). Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION.
    Verify current pricing and the free-tier allowance before you rely on it."""
    name = "azure"
    sample_rate = 24000

    def __init__(self, default_voice: str = "sw-KE-ZuriNeural"):
        self.default_voice = default_voice

    def synthesize(self, text: str, out_path: str, voice_id=None, rate: float = 1.0) -> int:
        import azure.cognitiveservices.speech as speechsdk
        cfg = speechsdk.SpeechConfig(subscription=os.environ["AZURE_SPEECH_KEY"],
                                     region=os.environ["AZURE_SPEECH_REGION"])
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm)
        voice = voice_id or self.default_voice
        pct = int(round((rate - 1.0) * 100))
        ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                f'xml:lang="sw-KE"><voice name="{voice}">'
                f'<prosody rate="{pct:+d}%">{text}</prosody></voice></speak>')
        out = speechsdk.audio.AudioOutputConfig(filename=out_path)
        synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=out)
        result = synth.speak_ssml_async(ssml).get()
        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            raise RuntimeError(f"Azure TTS failed: {result.reason}")
        with wave.open(out_path, "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)


class XTTSCloneTTS(TTSProvider):
    """Voice cloning via Coqui XTTS-v2 - clones the ACTUAL source speaker
    instead of using a fixed pretrained voice like Piper/Azure.

    !! CONSENT, EVERY TIME !! Only run this on audio where you have the
    speaker's consent to clone their voice. This is not a style note - a
    voice clone made without consent is a right-of-publicity problem at
    minimum and the exact mechanism behind voice-fraud deepfakes at worst.
    Confirm consent before every job that reaches this provider, not just
    once at prototype stage.

    Licensing: XTTS-v2 ships under Coqui's CPML (Coqui Public Model
    License), which carries commercial-use conditions. Read those terms
    yourself before this handles a real paying client's job.

    Much heavier than Piper: ~1.87GB model (one-time download), and with no
    GPU on this machine expect roughly 5-15x realtime synthesis - a 30s
    segment can take several minutes. Fine to queue overnight, not for
    same-session iteration.

    voice_id here means "path to a reference clip", NOT a named preset voice
    - it wants 6-20s of clean, single-speaker reference audio. pipeline.py
    auto-extracts one from the source audio (longest segment) when --voice
    isn't given explicitly.
    """
    name = "xtts"
    sample_rate = 24000
    MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

    def __init__(self, reference_audio: str | None = None, language: str = "en"):
        self.reference_audio = reference_audio or os.getenv("KAULI_XTTS_REFERENCE")
        self.language = language
        self._tts = None

    def _load(self):
        if self._tts is None:
            from TTS.api import TTS
            self._tts = TTS(self.MODEL_NAME, progress_bar=False, gpu=False)
        return self._tts

    def synthesize(self, text: str, out_path: str, voice_id=None, rate: float = 1.0) -> int:
        ref = voice_id or self.reference_audio
        if not ref:
            raise RuntimeError(
                "XTTS needs a reference clip of the consenting speaker to "
                "clone. Pass voice_id=<path to a 6-20s clean reference wav>, "
                "or run this through pipeline.py, which auto-extracts one "
                "from the source audio.")
        tts = self._load()
        tts.tts_to_file(
            text=text, speaker_wav=ref, language=self.language,
            file_path=out_path, speed=max(rate, 0.5),
        )
        with wave.open(out_path, "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)


_REGISTRY = {
    "stub": StubTTS, "piper": PiperTTS, "azure": AzureTTS, "xtts": XTTSCloneTTS,
}

# A small, realistic picker for jobs where actual voice cloning (see
# XTTSCloneTTS) is either not wanted or too slow to wait for - a
# reasonable gender/accent match beats the one fixed default voice every
# job used to get regardless of who's actually speaking. Keys are what
# gets stored on the order (orders.dub_voice = "piper:<key>"); paths are
# relative to the project root, matching how KAULI_PIPER_VOICE already
# points at data/voices/. Download with data/voices/ as the working
# directory:
#   curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/<en_XX>/<name>/medium/<file>.onnx
#   curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/<en_XX>/<name>/medium/<file>.onnx.json
PIPER_VOICES = {
    "alba":   {"label": "Alba - British English (female)", "path": "data/voices/en_GB-alba-medium.onnx"},
    "alan":   {"label": "Alan - British English (male)", "path": "data/voices/en_GB-alan-medium.onnx"},
    "amy":    {"label": "Amy - US English (female)", "path": "data/voices/en_US-amy-medium.onnx"},
    "ryan":   {"label": "Ryan - US English (male)", "path": "data/voices/en_US-ryan-medium.onnx"},
    # Piper's "high" quality tier, not "medium" like the rest above -
    # noticeably more natural prosody from the same free/open model
    # family, no licensing cost or new provider. Used for the marketing
    # site's demo clip (webapp/static/demo-en.wav) and available here for
    # real dub orders too, not just marketing - same file either way.
    "lessac": {"label": "Lessac - US English (male, alternate, high quality)",
               "path": "data/voices/en_US-lessac-high.onnx"},
}


def get_tts(name: str, **kwargs) -> TTSProvider:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown TTS provider '{name}'. Options: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
