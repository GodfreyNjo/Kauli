"""Lay rendered segments onto a single timeline. Pure stdlib + numpy so it runs
on any laptop. ffmpeg is used only when a segment needs time-stretching."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave

import numpy as np


def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    return data, sr


def write_wav_mono(path: str, data: np.ndarray, sr: int) -> None:
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def time_stretch(in_path: str, out_path: str, factor: float) -> bool:
    """factor > 1.0 = play faster (shorter). Uses ffmpeg atempo, which
    preserves pitch. Returns False if ffmpeg is unavailable."""
    if not has_ffmpeg():
        return False
    factor = max(0.5, min(2.0, factor))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", in_path,
         "-filter:a", f"atempo={factor:.4f}", out_path],
        check=True, timeout=120,  # one segment's worth of audio, not a whole file - short cap is fine
    )
    return True


def extract_reference_clip(source_path: str, start_ms: int, end_ms: int,
                            out_path: str, sample_rate: int = 22050) -> bool:
    """Cut a clean single-speaker window out of the SOURCE audio, for use as
    a voice-clone reference (e.g. XTTS's speaker_wav). Only ever call this on
    audio you have the speaker's consent to clone - see providers/tts.py.
    Returns False if ffmpeg is unavailable."""
    if not has_ffmpeg() or end_ms <= start_ms:
        return False
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", source_path,
         "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}",
         "-ar", str(sample_rate), "-ac", "1",
         out_path],
        check=True, timeout=60,  # a 6-20s reference clip, short cap is fine
    )
    return True


def extract_audio_window(source_path: str, start_ms: int, end_ms: int,
                          out_path: str, sample_rate: int = 22050) -> bool:
    """Cuts an exact window of the SOURCE audio, resampled to sample_rate,
    mono - used to carry the real music/applause/laughter/instrument-break
    bed through a non-speech gap in the dub (see
    kauli.pipeline.render_gap_audio) instead of leaving digital silence
    there. Same ffmpeg operation extract_reference_clip already does for
    XTTS's cloning reference - kept as its own function so a call site
    reads for what it's actually doing, not "grabbing a voice sample".
    Returns False if ffmpeg is unavailable or the window is empty/invalid."""
    if not has_ffmpeg() or end_ms <= start_ms:
        return False
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", source_path,
         "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}",
         "-ar", str(sample_rate), "-ac", "1",
         out_path],
        check=True, timeout=180,  # a real music/applause gap can run much longer than a 6-20s voice reference
    )
    return True


def normalize_peak(data: np.ndarray, target_dbfs: float = -3.0) -> np.ndarray:
    peak = np.max(np.abs(data)) if data.size else 0.0
    if peak <= 0:
        return data
    target = 10 ** (target_dbfs / 20)
    return data * (target / peak)


def build_timeline(segments, total_ms: int, sample_rate: int = 22050) -> np.ndarray:
    """segments: iterable of (start_ms, wav_path). Placed in start_ms order,
    each one hard-clipped so it can never run into the start of the next
    one that actually has audio - two segments' audio literally cannot
    overlap in the output, by construction, regardless of any upstream
    translation-length or stretch-cap imperfection.

    This used to sum overlapping audio instead - a real bug, not a design
    choice worth keeping: whenever a segment's rendered audio ran long
    enough to reach the next segment's start (exactly what
    review_reasons like "stretch_cap_exceeded" already exist to flag),
    the result was two voices playing at once in the delivered file -
    the "voice talking over itself" a client actually heard. Silently
    truncating a flagged segment's tail is the right failure mode here:
    it's inaudible instead of broken, the flag is what tells a human
    editor (via Ereri, before the 100%-edited shipping gate lets the
    order through) that this specific segment needs a shorter
    translation, not a mixer that quietly plays two voices at once.

    A segment with no path (silent gaps - see
    kauli.pipeline._insert_non_speech_segments) never constrains this at
    all, which is also what lets kauli.pipeline.translate_segment let a
    segment's translation run into a directly-following silent gap on
    purpose (see timing.GAP_BORROW_MIN_KEEP_MS) without this function
    needing to know that borrowing happened - it just looks at where the
    next real audio actually starts.

    The output is exactly total_ms long - no padding. This used to add a
    full extra second on top, which meant every delivered dub track ran
    ~1s longer than the client's real uploaded file, a real sync problem
    for anyone muxing the dub against the original video."""
    total_samples = int(sample_rate * total_ms / 1000)
    track = np.zeros(total_samples, dtype=np.float32)

    real = sorted(((s, p) for s, p in segments if p), key=lambda t: t[0])

    for i, (start_ms, path) in enumerate(real):
        data, sr = read_wav_mono(path)
        if sr != sample_rate:
            # cheap linear resample; fine for speech, replace with soxr if it matters
            idx = np.linspace(0, len(data) - 1, int(len(data) * sample_rate / sr))
            data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
        start = int(sample_rate * start_ms / 1000)
        next_start_ms = real[i + 1][0] if i + 1 < len(real) else total_ms
        boundary = min(total_samples, int(sample_rate * next_start_ms / 1000))
        end = min(start + len(data), boundary)
        if end > start:
            track[start:end] += data[: end - start]

    return normalize_peak(track)
