"""ASR providers. Swappable on purpose: start free and local, move to a paid
API only when you can prove it's better on YOUR audio, not on a vendor benchmark."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
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

    def __init__(self, model_size: str | None = None, compute_type: str = "int8",
                 device: str = "cpu"):
        self.model_size = model_size or os.environ.get("KAULI_WHISPER_MODEL") or "small"
        self.compute_type = compute_type
        self.device = device
        self._model = None

    @staticmethod
    def model_root() -> str:
        """Where downloaded models are kept: the persistent data folder, so a redeploy does not download them all over again."""
        return os.environ.get("KAULI_ASR_MODEL_DIR") or str(Path(__file__).resolve().parents[2] / "webapp" / "data" / "models" / "whisper")

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: keeps stub tests dep-free
            self._model = WhisperModel(self.model_size, device=self.device,
                                       compute_type=self.compute_type, download_root=self.model_root())
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


class LowQualityTranscript(Exception):
    """Our own model heard the recording but is not confident enough in what it wrote. The draft is kept, so that if no other provider
    can do better it is still delivered (and reviewed) rather than lost."""
    def __init__(self, segments, score: float):
        super().__init__(f"low confidence ({score:.2f})")
        self.segments, self.score = segments, score


class OwnASR(FasterWhisperASR):
    """Kauli's own transcription: an open speech model (Whisper, run through faster-whisper) on our own server. No per-minute cost, no
    quota, and the audio never leaves the server. It checks its own work: if its average word confidence is below the bar
    (KAULI_OWN_ASR_MIN_CONF, default 0.70) it raises LowQualityTranscript, and the chain hands the recording to an outside provider.
    The model is chosen with KAULI_OWN_ASR_MODEL (default "small"; a larger one is more accurate and slower)."""
    name = "own"

    def __init__(self, **kw):
        super().__init__(model_size=os.environ.get("KAULI_OWN_ASR_MODEL") or "small", **kw)

    @staticmethod
    def confidence(segments) -> float | None:
        """Average confidence over every recognised word (None when there are no words)."""
        probs = [w.confidence for s in segments for w in (s.words or [])]
        return sum(probs) / len(probs) if probs else None

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        segments = super().transcribe(audio_path, language=language)
        score = self.confidence(segments)
        if score is not None and score < float(os.environ.get("KAULI_OWN_ASR_MIN_CONF", "0.70")):
            raise LowQualityTranscript(segments, score)
        return segments


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


def _approximate_words(text: str, start_ms: int, end_ms: int, confidence: float) -> list[Word]:
    """Splits text into Word objects with per-word timing proportioned by
    character count across [start_ms, end_ms) - not a real alignment, just
    an even spread, the same technique webapp/app.py's cell-builders
    already use for hand-edited/translated text that has no real per-word
    timing either. Used for any ASR provider (like Transkriptor) whose API
    only returns segment-level timing, not word-level."""
    words = text.split()
    if not words:
        return []
    total_chars = sum(len(w) for w in words) or 1
    duration = max(1, end_ms - start_ms)
    out = []
    t = start_ms
    for w in words:
        dur = int(duration * (len(w) / total_chars))
        out.append(Word(text=w, start_ms=t, end_ms=t + dur, confidence=confidence))
        t += dur
    out[-1].end_ms = end_ms  # absorb rounding drift at the tail
    return out


# Languages Instant AI transcription accepts beyond Kauli's three core ones
# (Swahili, English, Kikuyu). key -> (label, Transkriptor language code).
# Codes are from Transkriptor's own supported-languages table
# (developer.transkriptor.com/docs/supported-languages.md, checked
# 2026-10-28), one default variant per language; the key is an ISO-639 code
# that local faster-whisper also understands, so the automatic fallback in
# TranskriptorASR still works for every entry. Transcription only - there is
# no human reviewer, translator or voice for these, which is why the order
# form restricts them to the Instant AI tier.
TRANSKRIPTOR_LANGUAGES = {
    "af": ("Afrikaans", "af-ZA"), "am": ("Amharic", "am-ET"), "ar": ("Arabic", "ar-SA"),
    "az": ("Azerbaijani", "az-AZ"), "bg": ("Bulgarian", "bg-BG"), "bn": ("Bengali", "bn-IN"),
    "bs": ("Bosnian", "bs-BA"), "ca": ("Catalan", "ca-ES"), "cs": ("Czech", "cs-CZ"),
    "cy": ("Welsh", "cy-GB"), "da": ("Danish", "da-DK"), "de": ("German", "de-DE"),
    "el": ("Greek", "el-GR"), "es": ("Spanish", "es-US"), "et": ("Estonian", "et-EE"),
    "eu": ("Basque", "eu-ES"), "fa": ("Persian", "fa-IR"), "fi": ("Finnish", "fi-FI"),
    "tl": ("Filipino", "fil-PH"), "fr": ("French", "fr-FR"), "ga": ("Irish", "ga-IE"),
    "gl": ("Galician", "gl-ES"), "gu": ("Gujarati", "gu-IN"), "he": ("Hebrew", "he-IL"),
    "hi": ("Hindi", "hi-IN"), "hr": ("Croatian", "hr-HR"), "hu": ("Hungarian", "hu-HU"),
    "hy": ("Armenian", "hy-AM"), "id": ("Indonesian", "id-ID"), "is": ("Icelandic", "is-IS"),
    "it": ("Italian", "it-IT"), "ja": ("Japanese", "ja-JP"), "jv": ("Javanese", "jv-ID"),
    "ka": ("Georgian", "ka-GE"), "kk": ("Kazakh", "kk-KZ"), "km": ("Khmer", "km-KH"),
    "kn": ("Kannada", "kn-IN"), "ko": ("Korean", "ko-KR"), "lo": ("Lao", "lo-LA"),
    "lt": ("Lithuanian", "lt-LT"), "lv": ("Latvian", "lv-LV"), "mk": ("Macedonian", "mk-MK"),
    "ml": ("Malayalam", "ml-IN"), "mn": ("Mongolian", "mn-MN"), "mr": ("Marathi", "mr-IN"),
    "ms": ("Malay", "ms-MY"), "mt": ("Maltese", "mt-MT"), "my": ("Burmese", "my-MM"),
    "ne": ("Nepali", "ne-NP"), "nl": ("Dutch", "nl-NL"), "no": ("Norwegian", "nb-NO"),
    "pa": ("Punjabi", "pa-IN"), "pl": ("Polish", "pl-PL"), "ps": ("Pashto", "ps-AF"),
    "pt": ("Portuguese", "pt-BR"), "ro": ("Romanian", "ro-RO"), "ru": ("Russian", "ru-RU"),
    "si": ("Sinhala", "si-LK"), "sk": ("Slovak", "sk-SK"), "sl": ("Slovenian", "sl-SI"),
    "so": ("Somali", "so-SO"), "sq": ("Albanian", "sq-AL"), "sr": ("Serbian", "sr-RS"),
    "sv": ("Swedish", "sv-SE"), "ta": ("Tamil", "ta-IN"), "te": ("Telugu", "te-IN"),
    "th": ("Thai", "th-TH"), "tr": ("Turkish", "tr-TR"), "uk": ("Ukrainian", "uk-UA"),
    "ur": ("Urdu", "ur-IN"), "uz": ("Uzbek", "uz-UZ"), "vi": ("Vietnamese", "vi-VN"),
    "zh": ("Chinese (Mandarin)", "zh-CN"), "yue": ("Chinese (Cantonese)", "yue-CN"),
}


class TranskriptorASR(ASRProvider):
    """Real transcription via Transkriptor's API (developer.transkriptor.com) -
    the primary ASR for real orders, since it's a paid, purpose-built
    transcription service and (per manual side-by-side listening) does
    noticeably better on real Kenyan Swahili than local Whisper. Falls
    back to local FasterWhisperASR automatically on ANY failure - a bad
    API key, a network error, an HTTP error status, a job that comes back
    Failed, or a poll that times out - rather than raising and failing
    the whole order. That's the actual point of this class: "use
    Transkriptor first, fall back to what we already had" as ONE resolved
    behavior a caller selects with a single asr= value, not two separate
    configs a caller has to orchestrate. See self.fallback_used /
    self.fallback_reason, which kauli.pipeline.run reads back out after
    calling transcribe() to log a real job.warnings entry when this
    happens - the manifest should never silently claim "transkriptor" ran
    when it actually didn't.

    API contract (confirmed 2026-08-23 against the real API, including one
    live end-to-end test - see below for what that caught), a 3-step
    upload-then-poll flow:
      1. POST {BASE}/transcription/local_file/get_upload_url
         {"file_name": "..."} -> {"upload_url", "public_url"}
      2. PUT the raw file bytes to upload_url.
      3. POST {BASE}/transcription/local_file/initiate_transcription
         {"url": public_url, "language": "sw-KE", "service": "Standard"}
         -> 202 {"order_id": "..."}
      4. Poll GET {BASE}/files/{order_id}/content (response wrapped in a
         top-level "body" key) until body["status"] == "Completed", then
         body["content"] is a list of
         {"text", "StartTime", "EndTime", "VoiceStart", "VoiceEnd", "Speaker"}.

    Two things their docs alone did NOT make clear, caught only by
    actually running a real 8s clip of real Kenyan Swahili through it
    end-to-end before wiring this in:
      - StartTime/EndTime are MILLISECONDS, not seconds - their docs never
        state the unit, and every other timing field in this codebase is
        milliseconds too, so getting this wrong would have been an easy,
        silent, systemic timing bug rather than an obvious crash.
      - There is no word-level timing or confidence score anywhere in the
        response, only segment-level - see _approximate_words and
        ASSUMED_CONFIDENCE below for how that's handled honestly rather
        than fabricated.
    That one real test transcribed real speech correctly as far as
    timing/mechanics go, but came back sparse on the actual Swahili words
    (a noisy, crowd-heavy political-rally clip, not a clean sample) - the
    integration is verified correct, real-world Swahili accuracy on
    cleaner audio is not yet proven at volume. Treat its output with the
    same review-flag scrutiny as any other transcript until that's borne
    out on real orders.
    """
    name = "transkriptor"
    cost_per_minute_usd = 0.0  # metered on Transkriptor's own dashboard, not tracked locally
    BASE = "https://api.tor.app/developer"
    LANG_CODES = {**{k: code for k, (_label, code) in TRANSKRIPTOR_LANGUAGES.items()},
                  "sw": "sw-KE", "en": "en-US"}
    # Transkriptor's API returns no confidence score at all - this is a
    # fixed, disclosed placeholder (not a measured value) so
    # source_confidence still has SOME value for the review-flag pipeline
    # to compare against. It deliberately sits just above
    # kauli.pipeline.FLAG_CONF_ASR (0.85), meaning the low_asr_confidence
    # flag will rarely fire on a Transkriptor transcript - that's a real,
    # disclosed limitation, not a claim that Transkriptor is that reliable.
    ASSUMED_CONFIDENCE = 0.90

    def __init__(self, api_key: str | None = None, backup_api_key: str | None = None,
                 poll_interval_s: float = 5.0, timeout_s: float = 1800.0, local_fallback: bool = True):
        # local_fallback=False makes a failure raise instead of dropping to local Whisper, so a ChainASR can try the
        # next cloud provider first (see asr_cloud.ChainASR).
        self.local_fallback = local_fallback
        self.api_key = api_key or os.environ.get("TRANSKRIPTOR_API_KEY")
        # A second Transkriptor account's key, tried before ever giving up on
        # real Transkriptor quality and dropping to local Whisper - covers a
        # bad/rate-limited/exhausted primary key without silently degrading
        # every order behind it. Distinct from fallback_used/fallback_reason
        # below on purpose: using the backup key still means real Transkriptor
        # output ran (job.providers["asr"] stays "transkriptor" in
        # pipeline.py), it's just worth a quieter warning that the primary
        # key needs attention - not the same as the real quality hit of
        # falling back to local Whisper.
        self.backup_api_key = backup_api_key or os.environ.get("TRANSKRIPTOR_API_KEY_BACKUP")
        # A third key (another account, or a fresh key on the same one): tried only after the first two have failed.
        self.backup_api_key_2 = os.environ.get("TRANSKRIPTOR_API_KEY_BACKUP_2")
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.fallback_used = False
        self.fallback_reason: str | None = None
        self.backup_key_used = False
        self.backup_key_note: str | None = None

    def _headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json", "Accept": "application/json"}

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        # Real Transkriptor first, on each configured key in turn; local faster-whisper only when every key has failed.
        keys = [(name, k) for name, k in (("primary", self.api_key), ("backup", self.backup_api_key),
                                          ("second backup", self.backup_api_key_2)) if k]
        failures: list[tuple[str, Exception]] = []
        for i, (name, key) in enumerate(keys):
            try:
                segs = self._transcribe_via_api(audio_path, language, key)
            except Exception as exc:  # noqa: BLE001 - any failure here means "try the next key, or fall back"
                failures.append((name, exc))
                continue
            if i > 0:
                self.backup_key_used = True
                first = failures[0][1] if failures else None
                self.backup_key_note = (
                    f"{failures[0][0].capitalize()} Transkriptor key failed ({first.__class__.__name__}: {first}) - "
                    f"used the {name} key instead. Worth checking on that key/account."
                ) if failures else f"Used the {name} Transkriptor key (no earlier key configured)."
            return segs
        self.fallback_used = True
        if failures:
            detail = "; ".join(f"{n}: {e.__class__.__name__}: {e}" for n, e in failures)
            self.fallback_reason = (f"Transkriptor ASR failed on {'both keys' if len(failures) == 2 else 'every key' if len(failures) > 2 else 'its key'} "
                                    f"({detail}) - fell back to local faster-whisper.")
        else:
            self.fallback_reason = "Transkriptor ASR failed (no API key configured) - fell back to local faster-whisper."
        if not self.local_fallback:
            self.fallback_used = False
            raise RuntimeError(self.fallback_reason)
        return FasterWhisperASR().transcribe(audio_path, language=language)

    def _transcribe_via_api(self, audio_path: str, language: str, api_key: str) -> list[Segment]:
        import time
        import requests

        if not api_key:
            raise RuntimeError("no Transkriptor API key given")
        lang_code = self.LANG_CODES.get(language, language)
        file_name = os.path.basename(audio_path)
        headers = self._headers(api_key)

        r = requests.post(f"{self.BASE}/transcription/local_file/get_upload_url",
                           headers=headers, json={"file_name": file_name}, timeout=30)
        r.raise_for_status()
        d = r.json()
        upload_url, public_url = d["upload_url"], d["public_url"]

        with open(audio_path, "rb") as f:
            up = requests.put(upload_url, data=f, timeout=600)
        up.raise_for_status()

        r = requests.post(f"{self.BASE}/transcription/local_file/initiate_transcription",
                           headers=headers,
                           json={"url": public_url, "language": lang_code, "service": "Standard"},
                           timeout=30)
        r.raise_for_status()
        order_id = r.json()["order_id"]

        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            time.sleep(self.poll_interval_s)
            r = requests.get(f"{self.BASE}/files/{order_id}/content",
                              headers=headers, timeout=30)
            r.raise_for_status()
            payload = r.json()
            body = payload.get("body", payload)
            status = body.get("status")
            if status == "Completed":
                return self._parse(body.get("content") or [], language)
            if status in ("Failed", "Error"):
                raise RuntimeError(f"Transkriptor job {order_id} failed: {body}")
        raise TimeoutError(f"Transkriptor job {order_id} did not complete within {self.timeout_s}s")

    def _parse(self, content: list[dict], language: str) -> list[Segment]:
        out = []
        for i, seg in enumerate(content):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            start_ms = int(seg.get("StartTime") or 0)
            end_ms = int(seg.get("EndTime") or 0)
            if end_ms <= start_ms:
                continue
            out.append(Segment(
                segment_id=f"seg_{i+1:04d}", index=i, start_ms=start_ms, end_ms=end_ms,
                speaker_id=seg.get("Speaker") or None, source_language=language,
                source_transcript=text, source_confidence=self.ASSUMED_CONFIDENCE,
                words=_approximate_words(text, start_ms, end_ms, self.ASSUMED_CONFIDENCE),
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
    "transkriptor": TranskriptorASR,
    "elevenlabs-scribe": None,
    "own": None,
}


class ElevenLabsScribeASR(ASRProvider):
    """Studio-tier speech recognition: ElevenLabs Scribe v2 (English: excellent accuracy, Swahili: high accuracy)
    with speaker diarization and word timestamps. If anything goes wrong it falls back to local faster-whisper
    and says so, so an order is never lost - and the web app credits back the Studio recognition charge."""
    name = "elevenlabs-scribe"
    cost_per_minute_usd = 0.0037

    def __init__(self):
        self.fallback_used = False
        self.fallback_reason: str | None = None

    def transcribe(self, audio_path: str, language: str = "sw") -> list[Segment]:
        from . import elevenlabs_audio
        try:
            segments = elevenlabs_audio.scribe_segments(audio_path, language)
            if not segments:
                raise RuntimeError("no speech was returned")
            return segments
        except Exception as exc:  # noqa: BLE001 - any failure means "fall back", not "fail the order"
            self.fallback_used = True
            self.fallback_reason = f"elevenlabs-scribe failed ({exc}) - used faster-whisper instead"
            return FasterWhisperASR().transcribe(audio_path, language=language)


_REGISTRY["elevenlabs-scribe"] = ElevenLabsScribeASR
_REGISTRY["own"] = OwnASR


def get_asr(name: str, **kwargs) -> ASRProvider:
    """A provider by name. "chain:a,b,c" tries a, then b, then c (see asr_cloud.ChainASR); "groq-whisper" and "assemblyai"
    are the low-cost cloud providers."""
    if name.startswith("chain:"):
        from .asr_cloud import ChainASR
        return ChainASR(name[len("chain:"):].split(","))
    if name in ("groq-whisper", "assemblyai"):
        from .asr_cloud import AssemblyAIASR, GroqWhisperASR
        return {"groq-whisper": GroqWhisperASR, "assemblyai": AssemblyAIASR}[name](**kwargs)
    if name not in _REGISTRY:
        raise ValueError(f"Unknown ASR provider '{name}'. Options: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
