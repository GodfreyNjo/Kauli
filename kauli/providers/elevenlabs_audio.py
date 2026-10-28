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


def diarize_turns(src_path: str, post=None) -> list[tuple[int, int, str]]:
    """Who spoke when, from Scribe. No language is sent, so any language works and the API detects it."""
    import httpx
    post = post or httpx.post
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        upload = str(Path(tmp) / "upload.mp3")
        _compress_for_upload(src_path, upload)
        with open(upload, "rb") as fh:
            resp = post(f"{API}/v1/speech-to-text", headers={"xi-api-key": key},
                        files={"file": ("audio.mp3", fh, "audio/mpeg")},
                        data={"model_id": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"), "diarize": "true",
                              "timestamps_granularity": "word", "tag_audio_events": "false"}, timeout=900.0)
    if resp.status_code != 200:
        raise RuntimeError(_explain(resp))
    return build_turns(resp.json().get("words") or [])


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
