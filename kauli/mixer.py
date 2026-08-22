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


def normalize_peak(data: np.ndarray, target_dbfs: float = -3.0) -> np.ndarray:
    peak = np.max(np.abs(data)) if data.size else 0.0
    if peak <= 0:
        return data
    target = 10 ** (target_dbfs / 20)
    return data * (target / peak)


def build_timeline(segments, total_ms: int, sample_rate: int = 22050) -> np.ndarray:
    """segments: iterable of (start_ms, wav_path). Later segments never clobber
    earlier ones - overlaps are summed, which surfaces timing bugs audibly."""
    total_samples = int(sample_rate * total_ms / 1000) + sample_rate  # 1s tail
    track = np.zeros(total_samples, dtype=np.float32)

    for start_ms, path in segments:
        if not path:
            continue
        data, sr = read_wav_mono(path)
        if sr != sample_rate:
            # cheap linear resample; fine for speech, replace with soxr if it matters
            idx = np.linspace(0, len(data) - 1, int(len(data) * sample_rate / sr))
            data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
        start = int(sample_rate * start_ms / 1000)
        end = min(start + len(data), total_samples)
        if end > start:
            track[start:end] += data[: end - start]

    return normalize_peak(track)
