"""Low-cost cloud transcription: Groq (hosted Whisper) and AssemblyAI, plus the chain that tries providers in order.

Transkriptor stays Kauli's quality provider for paying customers (see TranskriptorASR). These two exist so the free allowance
costs almost nothing: Groq's Whisper large-v3-turbo is about $0.04 an hour and AssemblyAI about $0.15 an hour, so even a month
of free minutes is a few cents. The web app decides which orders go where (webapp/app.py, _route_asr); this module only knows
how to talk to each service and how to fall through from one to the next.

Both providers raise on any failure rather than returning something half-right, and ChainASR moves on to the next provider, so
an order is never lost and never silently transcribed by a worse service than the manifest says (job.providers["asr"] is set to
the provider that really ran).
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from ..models import Segment, Word
from .asr import ASRProvider, FasterWhisperASR, LowQualityTranscript, _ffprobe_duration_ms, get_asr

# What Whisper recognises. A language outside this list is refused up front so the chain moves on instead of guessing.
WHISPER_LANGUAGES = set(
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu ha haw he hi hr ht hu hy id is it ja jw "
    "ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr su sv "
    "sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue".split())


def _ffmpeg(*args: str) -> None:
    done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.strip()[:200]}")


def prepare_chunks(src: str, tmp: Path, chunk_s: int) -> list[tuple[Path, int]]:
    """The audio as small mono speech-quality mp3 files of at most `chunk_s` seconds, each with the millisecond offset at
    which it starts in the original. Video is stripped. (Groq refuses a request over 25 MB, and 20 minutes of this is 5 MB.)"""
    whole = tmp / "whole.mp3"
    _ffmpeg("-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(whole))
    total_ms = _ffprobe_duration_ms(str(whole))
    if total_ms <= chunk_s * 1000 * 1.05 or not total_ms:
        return [(whole, 0)]
    _ffmpeg("-i", str(whole), "-f", "segment", "-segment_time", str(chunk_s), "-c", "copy", str(tmp / "part%03d.mp3"))
    parts, offset = [], 0
    for path in sorted(tmp.glob("part*.mp3")):
        parts.append((path, offset))
        offset += _ffprobe_duration_ms(str(path))                # real lengths, so no drift builds up across a long file
    return parts


def _confidence(avg_logprob: float | None) -> float:
    return round(min(1.0, max(0.0, math.exp(min(0.0, float(avg_logprob or 0.0))))), 3)


class GroqWhisperASR(ASRProvider):
    """Whisper large-v3-turbo on Groq. Very cheap and fast, strong in widely spoken languages, weaker in small ones."""
    name = "groq-whisper"
    cost_per_minute_usd = 0.04 / 60
    API = "https://api.groq.com/openai/v1/audio/transcriptions"
    CHUNK_S = 1200

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")
        if language not in WHISPER_LANGUAGES:
            raise RuntimeError(f"Groq Whisper does not cover '{language}'")
        segments: list[Segment] = []
        with tempfile.TemporaryDirectory() as t:
            for path, offset in prepare_chunks(audio_path, Path(t), self.CHUNK_S):
                with open(path, "rb") as fh:
                    resp = httpx.post(
                        self.API, headers={"Authorization": f"Bearer {self.api_key}"},
                        files={"file": ("audio.mp3", fh, "audio/mpeg")},
                        data={"model": self.model, "response_format": "verbose_json", "language": language, "temperature": "0",
                              "timestamp_granularities[]": ["word", "segment"]},
                        timeout=600.0)
                if resp.status_code != 200:
                    raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:160]}")
                segments += self._parse(resp.json(), language, offset, start_index=len(segments))
        return segments

    @staticmethod
    def _parse(body: dict, language: str, offset_ms: int, start_index: int = 0) -> list[Segment]:
        words = body.get("words") or []
        out = []
        for raw in body.get("segments") or []:
            text = (raw.get("text") or "").strip()
            if not text:
                continue
            if float(raw.get("no_speech_prob") or 0.0) > 0.9 and float(raw.get("avg_logprob") or 0.0) < -1.0:
                continue                                   # Whisper inventing words over silence
            start, end = int(raw["start"] * 1000) + offset_ms, int(raw["end"] * 1000) + offset_ms
            conf = _confidence(raw.get("avg_logprob"))
            seg_words = [Word(text=str(w.get("word", "")).strip(), start_ms=int(w["start"] * 1000) + offset_ms,
                              end_ms=int(w["end"] * 1000) + offset_ms, confidence=conf)
                         for w in words if w.get("start") is not None and raw["start"] - 0.05 <= w["start"] < raw["end"] + 0.05
                         and str(w.get("word", "")).strip()]
            i = start_index + len(out)
            out.append(Segment(segment_id=f"seg_{i + 1:04d}", index=i, start_ms=start, end_ms=max(end, start + 1),
                               source_language=language, source_transcript=text, source_confidence=conf, words=seg_words))
        return out


class AssemblyAIASR(ASRProvider):
    """AssemblyAI's Universal model: cheap, with word timestamps, confidence and speaker labels."""
    name = "assemblyai"
    cost_per_minute_usd = 0.15 / 60
    BASE = "https://api.assemblyai.com/v2"

    def __init__(self, api_key: str | None = None, poll_interval_s: float = 3.0, timeout_s: float = 1800.0):
        self.api_key = api_key or os.environ.get("ASSEMBLYAI_API_KEY")
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        import time
        if not self.api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY is not set")
        headers = {"authorization": self.api_key}
        with tempfile.TemporaryDirectory() as t:
            (path, _), = prepare_chunks(audio_path, Path(t), chunk_s=10 ** 6)       # one small file, never split
            with open(path, "rb") as fh:
                up = httpx.post(f"{self.BASE}/upload", headers=headers, content=fh.read(), timeout=600.0)
        if up.status_code != 200:
            raise RuntimeError(f"AssemblyAI upload {up.status_code}: {up.text[:160]}")
        payload = {"audio_url": up.json()["upload_url"], "language_code": language, "speaker_labels": True,
                   "punctuate": True, "format_text": True}
        if os.environ.get("ASSEMBLYAI_SPEECH_MODEL"):
            payload["speech_model"] = os.environ["ASSEMBLYAI_SPEECH_MODEL"]
        made = httpx.post(f"{self.BASE}/transcript", headers=headers, json=payload, timeout=60.0)
        if made.status_code != 200:
            raise RuntimeError(f"AssemblyAI {made.status_code}: {made.text[:160]}")
        job_id = made.json()["id"]
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            r = httpx.get(f"{self.BASE}/transcript/{job_id}", headers=headers, timeout=60.0)
            if r.status_code != 200:
                raise RuntimeError(f"AssemblyAI poll {r.status_code}: {r.text[:160]}")
            body = r.json()
            if body.get("status") == "completed":
                return self._parse(body, language)
            if body.get("status") == "error":
                raise RuntimeError(f"AssemblyAI could not transcribe it: {body.get('error')}")
            time.sleep(self.poll_interval_s)
        raise TimeoutError(f"AssemblyAI job {job_id} did not finish within {self.timeout_s}s")

    @staticmethod
    def _parse(body: dict, language: str) -> list[Segment]:
        from .elevenlabs_audio import build_segments
        words = [{"type": "word", "text": w.get("text", ""), "start": w["start"] / 1000.0, "end": w["end"] / 1000.0,
                  "speaker_id": w.get("speaker"), "logprob": math.log(max(float(w.get("confidence") or 0.0), 1e-3))}
                 for w in body.get("words") or [] if w.get("start") is not None and w.get("end") is not None]
        return build_segments(words, language)


class ChainASR(ASRProvider):
    """Tries each provider in turn and returns the first real transcript. `used_name` says which one produced it, and `notes`
    records each one that failed first, so the manifest and the order's warnings tell the truth."""
    name = "chain"

    def __init__(self, names: list[str]):
        self.names = [n for n in names if n]
        self.used_name: str | None = None
        self.notes: list[str] = []
        self.fallback_used = False            # kept False: the chain's own notes say what happened (see pipeline)
        self.fallback_reason: str | None = None

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        failures: list[str] = []
        empty_ok: str | None = None
        draft = None                                       # our own model's low-confidence transcript, kept in case nothing else works
        for i, name in enumerate(self.names):
            last = i == len(self.names) - 1
            try:
                provider = get_asr(name, local_fallback=False) if name == "transkriptor" and not last else get_asr(name)
                segments = provider.transcribe(audio_path, language=language)
            except LowQualityTranscript as exc:
                draft = draft or (exc.segments, name)
                failures.append(f"{name}: {exc}")
                if not last:
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - any failure means "try the next one"
                failures.append(f"{name}: {exc.__class__.__name__}: {exc}")
                continue
            if not segments and not last:
                empty_ok = empty_ok or provider.name           # silence is legitimate, but another provider may hear more
                failures.append(f"{name}: no speech returned")
                continue
            self.used_name = provider.name
            if failures:
                self.notes.append(f"Transcribed by {provider.name} after: " + "; ".join(failures))
            return segments
        if draft:
            self.used_name = draft[1]
            self.notes.append("Transcribed by our own model; it was not fully confident and no other provider could take it: " + "; ".join(failures))
            return draft[0]                                # low-confidence segments are flagged for review downstream
        if empty_ok:
            self.used_name = empty_ok
            return []
        raise RuntimeError("every transcription provider failed: " + "; ".join(failures))


def cheap_providers_configured() -> list[str]:
    """The low-cost providers that have a key, in the order they are tried (KAULI_CHEAP_ASR_ORDER, default Groq then AssemblyAI)."""
    keys = {"groq-whisper": "GROQ_API_KEY", "assemblyai": "ASSEMBLYAI_API_KEY"}
    order = [n.strip() for n in os.environ.get("KAULI_CHEAP_ASR_ORDER", "groq-whisper,assemblyai").split(",")]
    return [n for n in order if n in keys and os.environ.get(keys[n])]


__all__ = ["GroqWhisperASR", "AssemblyAIASR", "ChainASR", "FasterWhisperASR", "cheap_providers_configured", "prepare_chunks"]
