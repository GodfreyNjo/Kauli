"""ASR providers. Swappable on purpose: start free and local, move to a paid
API only when you can prove it's better on YOUR audio, not on a vendor benchmark."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from ..models import Segment, Word


class ASRProvider:
    name = "base"
    cost_per_minute_usd = 0.0

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        raise NotImplementedError


class StubASR(ASRProvider):
    """No models, no network. Lets you test the whole pipeline offline."""
    name = "stub"

    FIXTURE = [
        (340, 3100, "Habari yako, naitwa Wanjiru kutoka Duka Bora.", 0.94),
        (3400, 8950, "Nimepiga simu kuhusu ile order uliweka Jumatatu, imefika kwa store yetu ya Kilimani.", 0.89),
        (9200, 13800, "Kama uko sawa, unaweza kuja kuipick leo kabla ya saa kumi na moja jioni.", 0.81),
    ]

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        segs = []
        for i, (start, end, text, conf) in enumerate(self.FIXTURE):
            segs.append(Segment(
                segment_id=f"seg_{i+1:04d}", index=i, start_ms=start, end_ms=end,
                speaker_id="spk_1", source_language=language,
                source_transcript=text, source_confidence=conf,
            ))
        return segs


class FasterWhisperASR(ASRProvider):
    """Local, free, runs on CPU. This is your default until proven otherwise.

    model_size: tiny/base/small/medium/large-v3. On a CPU-only laptop, `small`
    is the sweet spot (~1-2x realtime). `medium` is noticeably better on Swahili
    but slow without a GPU — run it overnight for bulk jobs.
    """
    name = "faster-whisper"

    def __init__(self, model_size: str = "small", compute_type: str = "int8",
                 device: str = "cpu"):
        self.model_size = model_size
        self.compute_type = compute_type
        self.device = device
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: keeps stub tests dep-free
            self._model = WhisperModel(self.model_size, device=self.device,
                                       compute_type=self.compute_type)
        return self._model

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        model = self._load()
        segments, _info = model.transcribe(
            audio_path,
            language=language,
            word_timestamps=True,
            vad_filter=True,                       # kills the silence-hallucination problem
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,      # stops runaway repetition loops
            beam_size=5,
            # Whisper's own silence-fallback: a chunk gets dropped entirely
            # (zero words, not just low confidence) when no_speech_prob is
            # above no_speech_threshold AND avg_logprob is below
            # log_prob_threshold. Defaults (0.6 / -1.0) are tuned for clean
            # audio - on a noisy recording (crowd, applause, cross-talk,
            # distant mic) real speech routinely dips under -1.0 and reads
            # as more "no-speech-like" than it should, so a whole segment
            # of genuine speech silently vanishes instead of coming back
            # low-confidence. That's strictly worse for this product: a
            # low-confidence word gets caught by the existing flag/review
            # pipeline in seconds; a vanished segment means an editor
            # re-transcribing several sentences by hand from scratch - see
            # the "manually transcribed this chunk" workflow complaint this
            # was tuned in response to. Biased deliberately toward "return
            # something, let review catch it" over "stay silent" - same
            # model, same compute, no processing-time cost either way.
            no_speech_threshold=0.8,       # require more certainty before calling it silence
            log_prob_threshold=-2.0,       # tolerate a noisier decode instead of discarding it
        )
        out = []
        for i, s in enumerate(segments):
            words = [Word(text=w.word.strip(), start_ms=int(w.start * 1000),
                          end_ms=int(w.end * 1000), confidence=round(w.probability, 3))
                     for w in (s.words or [])]
            out.append(Segment(
                segment_id=f"seg_{i+1:04d}", index=i,
                start_ms=int(s.start * 1000), end_ms=int(s.end * 1000),
                source_language=language,
                source_transcript=s.text.strip(),
                source_confidence=round(min(1.0, max(0.0, 1.0 + s.avg_logprob)), 3),
                words=words,
            ))
        return out


def _ffprobe_duration_ms(audio_path: str) -> int:
    if not shutil.which("ffprobe"):
        return 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return int(round(float(out.stdout.strip()) * 1000))
    except Exception:
        return 0


class ManualASR(ASRProvider):
    """No transcription model at all. For a source language Whisper (and every
    other ASR vendor we checked - see the Kikuyu feasibility conversation)
    doesn't know, the honest move isn't to fake a transcript, it's to skip
    the model entirely and hand a human transcriber correctly-timed, sensibly-
    sized chunks to type into the editor themselves.

    Splits audio into speech-active segments with ffmpeg's silencedetect -
    real, useful segmentation (so a transcriber isn't handed one unworkable
    multi-minute blob), just with zero transcript text. An empty
    source_transcript on a segment longer than 1.5s already trips the
    `empty_transcript` review flag in pipeline.py's translate_segment (the
    same flag Whisper's own silence-suppression fallback produces on noisy
    audio) - so the existing "type in a transcript for this flagged segment"
    editor workflow handles it with no new UI needed.
    """
    name = "manual"

    def __init__(self, noise_db: float = -30.0, min_silence_s: float = 0.6,
                 max_segment_s: float = 12.0):
        self.noise_db = noise_db
        self.min_silence_s = min_silence_s
        self.max_segment_s = max_segment_s

    def _speech_intervals_ms(self, audio_path: str, total_ms: int) -> list[tuple[int, int]]:
        """Runs ffmpeg's silencedetect filter and inverts the silence windows
        it reports into speech windows. Falls back to one segment spanning
        the whole file if ffmpeg is missing or nothing gets detected."""
        if not shutil.which("ffmpeg") or total_ms <= 0:
            return [(0, total_ms)] if total_ms > 0 else []
        try:
            proc = subprocess.run(
                ["ffmpeg", "-i", audio_path, "-af",
                 f"silencedetect=noise={self.noise_db}dB:d={self.min_silence_s}",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=300,
            )
        except Exception:
            return [(0, total_ms)]
        starts = [float(m) for m in re.findall(r"silence_start:\s*(-?\d+\.?\d*)", proc.stderr)]
        ends = [float(m) for m in re.findall(r"silence_end:\s*(-?\d+\.?\d*)", proc.stderr)]
        silences = list(zip(starts, ends[:len(starts)]))
        if not silences:
            return [(0, total_ms)]
        speech = []
        cursor = 0
        for s_start, s_end in silences:
            s_start_ms, s_end_ms = int(s_start * 1000), int(s_end * 1000)
            if s_start_ms > cursor:
                speech.append((cursor, s_start_ms))
            cursor = max(cursor, s_end_ms)
        if cursor < total_ms:
            speech.append((cursor, total_ms))
        return speech or [(0, total_ms)]

    def _split_long(self, intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        cap_ms = int(self.max_segment_s * 1000)
        out = []
        for start, end in intervals:
            span = end - start
            if span <= cap_ms:
                out.append((start, end))
                continue
            n_chunks = -(-span // cap_ms)  # ceil
            chunk = span // n_chunks
            pos = start
            for _ in range(n_chunks - 1):
                out.append((pos, pos + chunk))
                pos += chunk
            out.append((pos, end))
        return out

    def transcribe(self, audio_path: str, language: str = "ki") -> list[Segment]:
        total_ms = _ffprobe_duration_ms(audio_path)
        intervals = self._split_long(self._speech_intervals_ms(audio_path, total_ms))
        out = []
        for i, (start, end) in enumerate(intervals):
            if end <= start:
                continue
            out.append(Segment(
                segment_id=f"seg_{i+1:04d}", index=i, start_ms=start, end_ms=end,
                source_language=language, source_transcript="", source_confidence=0.0,
            ))
        return out


class AwsTranscribeASR(ASRProvider):
    """$0.024/min. Only worth it if it beats local Whisper on your own audio —
    test before you commit. Needs an S3 bucket; batch jobs are async."""
    name = "aws-transcribe"
    cost_per_minute_usd = 0.024

    def __init__(self, bucket: str | None = None, region: str = "eu-west-1"):
        self.bucket = bucket or os.getenv("KAULI_S3_BUCKET")
        self.region = region

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        raise NotImplementedError(
            "Wire this up only after local Whisper proves insufficient. "
            "Needs: boto3, an S3 bucket, start_transcription_job with "
            "LanguageCode='sw-KE', then poll and parse the result JSON."
        )


_REGISTRY = {
    "stub": StubASR,
    "faster-whisper": FasterWhisperASR,
    "manual": ManualASR,
    "aws-transcribe": AwsTranscribeASR,
}


def get_asr(name: str, **kwargs) -> ASRProvider:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown ASR provider '{name}'. Options: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
