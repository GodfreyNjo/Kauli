"""ElevenLabs audio services beyond voices, over their REST API (no SDK):

* isolate_audio  - Voice Isolator: strips background noise/music from a recording BEFORE it is transcribed,
                   which helps most on field interviews and phone or WhatsApp recordings.
* diarize_turns  - Scribe speech-to-text, used only for its speaker diarization: who spoke when. The words
                   themselves still come from Kauli's own transcription; this just labels the speakers.

Both need ELEVENLABS_API_KEY. Any failure raises RuntimeError with a plain reason, and the caller
(kauli.pipeline) carries on without the extra, so an order is never lost to an optional step."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

API = "https://api.elevenlabs.io"


def _key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    return key


def _explain(resp) -> str:
    meaning = {401: "the API key was rejected or the plan has no credits left", 413: "the file is too large",
               422: "the audio was not accepted", 429: "too many requests - slow down"}.get(resp.status_code, "request refused")
    return f"ElevenLabs {resp.status_code}: {meaning}"


def _ffmpeg(*args: str) -> None:
    done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.strip()[:200]}")


def _compress_for_upload(src: str, dst: str) -> None:
    """Mono 64 kbps mp3 (about 0.5 MB a minute): small enough to upload, plenty for speech. Strips video."""
    _ffmpeg("-i", src, "-vn", "-ac", "1", "-ar", "22050", "-b:a", "64k", dst)


def isolate_audio(src_path: str, out_wav_path: str, post=None) -> None:
    """Writes a noise-free 16 kHz mono WAV of `src_path` to `out_wav_path`."""
    import httpx
    post = post or httpx.post
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        upload = str(Path(tmp) / "upload.mp3")
        _compress_for_upload(src_path, upload)
        with open(upload, "rb") as fh:
            resp = post(f"{API}/v1/audio-isolation", headers={"xi-api-key": key},
                        files={"audio": ("audio.mp3", fh, "audio/mpeg")}, data={"file_format": "other"}, timeout=600.0)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(_explain(resp) if resp.status_code != 200 else "ElevenLabs returned no audio")
        cleaned = Path(tmp) / "cleaned.bin"                 # the container is not documented: let ffmpeg detect it
        cleaned.write_bytes(resp.content)
        _ffmpeg("-i", str(cleaned), "-ac", "1", "-ar", "16000", out_wav_path)


def build_turns(words: list[dict]) -> list[tuple[int, int, str]]:
    """Consecutive words by the same speaker merged into (start_ms, end_ms, speaker_id) turns."""
    turns: list[list] = []
    for w in words:
        if w.get("type") != "word" or w.get("speaker_id") is None or w.get("start") is None or w.get("end") is None:
            continue
        start, end, speaker = int(w["start"] * 1000), int(w["end"] * 1000), str(w["speaker_id"])
        if turns and turns[-1][2] == speaker and start - turns[-1][1] < 1500:
            turns[-1][1] = end
        else:
            turns.append([start, end, speaker])
    return [(a, b, s) for a, b, s in turns]


def _scribe_call(src_path: str, post=None, language_code: str | None = None) -> dict:
    """One Scribe speech-to-text request with speaker diarization and word timestamps."""
    import httpx
    post = post or httpx.post
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        upload = str(Path(tmp) / "upload.mp3")
        _compress_for_upload(src_path, upload)
        data = {"model_id": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"), "diarize": "true",
                "timestamps_granularity": "word", "tag_audio_events": "false"}
        if language_code:
            data["language_code"] = language_code
        with open(upload, "rb") as fh:
            resp = post(f"{API}/v1/speech-to-text", headers={"xi-api-key": key},
                        files={"file": ("audio.mp3", fh, "audio/mpeg")}, data=data, timeout=900.0)
    if resp.status_code != 200:
        raise RuntimeError(_explain(resp))
    return resp.json()


def diarize_turns(src_path: str, post=None) -> list[tuple[int, int, str]]:
    """Who spoke when, from Scribe. No language is sent, so any language works and the API detects it."""
    return build_turns(_scribe_call(src_path, post).get("words") or [])


def build_segments(words: list[dict], language: str) -> list:
    """Scribe's word stream cut into transcript segments: a new segment starts when the speaker changes, after a
    pause of 0.8 s or more, after 12 s, or at a sentence end once the segment is at least 2.5 s long. Speaker
    labels ("Speaker 1", ...) are only set when more than one speaker was heard, so a single-speaker recording
    is exactly as it was before."""
    import math
    from ..models import Segment, Word

    toks = [w for w in words if w.get("type") == "word" and w.get("start") is not None and w.get("end") is not None
            and str(w.get("text", "")).strip()]
    raw_ids: dict = {}
    for w in toks:
        if w.get("speaker_id") is not None:
            raw_ids.setdefault(w["speaker_id"], f"Speaker {len(raw_ids) + 1}")
    label_speakers = len(raw_ids) > 1

    groups: list[dict] = []
    for w in toks:
        start, end = int(w["start"] * 1000), int(w["end"] * 1000)
        speaker = raw_ids.get(w.get("speaker_id")) if label_speakers else None
        cur = groups[-1] if groups else None
        if cur and (speaker != cur["speaker"] or start - cur["end"] >= 800 or end - cur["start"] > 12000
                    or (cur["words"][-1]["text"].rstrip()[-1:] in ".?!" and cur["end"] - cur["start"] >= 2500)):
            cur = None
        if cur is None:
            cur = {"start": start, "end": end, "speaker": speaker, "words": []}
            groups.append(cur)
        cur["end"] = end
        cur["words"].append({"text": str(w["text"]).strip(), "start": start, "end": end,
                             "conf": math.exp(min(0.0, float(w.get("logprob") or 0.0)))})
    segments = []
    for i, g in enumerate(groups):
        confs = [x["conf"] for x in g["words"]]
        segments.append(Segment(
            segment_id=f"seg_{i + 1:04d}", index=i, start_ms=g["start"], end_ms=g["end"], speaker_id=g["speaker"],
            source_language=language, source_transcript=" ".join(x["text"] for x in g["words"]),
            source_confidence=round(sum(confs) / len(confs), 3),
            words=[Word(text=x["text"], start_ms=x["start"], end_ms=x["end"], confidence=round(x["conf"], 3))
                   for x in g["words"]]))
    return segments


def scribe_segments(src_path: str, language: str, post=None) -> list:
    """Studio-tier transcription: segments with word timings, confidence and speaker labels, from Scribe."""
    return build_segments(_scribe_call(src_path, post, language_code=language).get("words") or [], language)


def assign_speakers(segments, turns: list[tuple[int, int, str]]) -> int:
    """Sets `speaker_id` ("Speaker 1", "Speaker 2", ...) on every speech segment, by which speaker talks most
    during it. Numbering follows first appearance. Never overwrites a label someone already set. Returns the
    number of distinct speakers found."""
    order: dict[str, str] = {}
    for _, _, raw in turns:
        order.setdefault(raw, f"Speaker {len(order) + 1}")
    for seg in segments:
        if getattr(seg, "segment_type", "speech") != "speech" or getattr(seg, "speaker_id", None):
            continue
        share: dict[str, int] = {}
        for a, b, raw in turns:
            overlap = min(seg.end_ms, b) - max(seg.start_ms, a)
            if overlap > 0:
                share[raw] = share.get(raw, 0) + overlap
        if share:
            seg.speaker_id = order[max(share, key=share.get)]
    return len(order)
