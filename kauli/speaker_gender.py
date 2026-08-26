"""A real, if coarse, default for "who should get which dub voice" -
autocorrelation pitch (F0) estimation over a speaker's own audio, classified
male/female by a standard crossover threshold, then mapped onto Azure's two
actual Kiswahili neural voices. No new heavy dependency (librosa etc.) - this
is plain numpy + the stdlib wave module, on purpose, so it doesn't add
another package to get right on the production server mid-order.

This is a DEFAULT, not a diarization/casting product: four real speakers
still only ever get two possible voices out of this (Azure ships exactly
two Kiswahili voices today - see kauli/providers/tts.py), and pitch alone
gets the occasional speaker wrong (a low female voice, a high male one).
That's exactly why webapp/app.py's editor_assign_speaker_voice_azure exists
right next to this - the automatic guess is a starting point an editor
overrides by ear in Ereri, not the final word.
"""
from __future__ import annotations

import statistics
import wave
from pathlib import Path

import numpy as np

from .mixer import extract_audio_window

# Standard speech F0 crossover - a typical adult male fundamental sits
# roughly 85-180Hz, adult female roughly 165-255Hz. 165Hz is the usual
# textbook split point; there's real overlap either side of it, which is
# exactly why this is a default and not a guarantee (see module docstring).
_MALE_FEMALE_CROSSOVER_HZ = 165.0
_MIN_VOICED_HZ = 70.0   # below this is almost certainly not a real voiced pitch (rumble/noise)
_MAX_VOICED_HZ = 400.0  # above this is almost certainly a harmonic, not the fundamental

# Azure's actual two Kiswahili neural voices (kauli/providers/tts.py) - the
# only two real options this can map a detected gender onto today.
AZURE_VOICE_BY_GENDER = {"male": "sw-KE-RafikiNeural", "female": "sw-KE-ZuriNeural"}


def _frame_f0_hz(frame: np.ndarray, sample_rate: int) -> float | None:
    """One frame's fundamental frequency via autocorrelation, or None if the
    frame doesn't look voiced (autocorrelation peak too weak relative to
    the frame's own energy - silence, noise, or an unvoiced consonant)."""
    frame = frame - frame.mean()
    energy = float(np.dot(frame, frame))
    if energy < 1e-6:
        return None
    corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
    corr /= energy
    min_lag = int(sample_rate / _MAX_VOICED_HZ)
    max_lag = min(int(sample_rate / _MIN_VOICED_HZ), len(corr) - 1)
    if max_lag <= min_lag:
        return None
    window = corr[min_lag:max_lag]
    peak_idx = int(np.argmax(window))
    peak_val = window[peak_idx]
    if peak_val < 0.3:  # weak periodicity - not confidently voiced
        return None
    lag = min_lag + peak_idx
    if lag <= 0:
        return None
    return sample_rate / lag


def estimate_f0_hz(audio_path: str, frame_ms: float = 40.0, hop_ms: float = 20.0) -> float | None:
    """Median fundamental frequency across the voiced frames of a (short,
    single-speaker) mono WAV clip - None if nothing voiced enough was
    found (silence, pure noise, or a clip too short to frame at all)."""
    with wave.open(audio_path, "rb") as w:
        sample_rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
        sample_width = w.getsampwidth()
    if sample_width != 2:  # extract_audio_window always writes 16-bit PCM - a real mismatch, not silent data loss
        raise ValueError(f"expected 16-bit PCM, got sample_width={sample_width}")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return None

    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    hop_len = max(1, int(sample_rate * hop_ms / 1000))
    f0s: list[float] = []
    for start in range(0, max(1, samples.size - frame_len), hop_len):
        f0 = _frame_f0_hz(samples[start:start + frame_len], sample_rate)
        if f0 is not None:
            f0s.append(f0)
    return statistics.median(f0s) if f0s else None


def classify_gender(f0_hz: float | None) -> str:
    """'male' / 'female' / 'unknown' (no reliable pitch read) from a median
    F0 - see the module docstring for the real accuracy caveat."""
    if f0_hz is None:
        return "unknown"
    return "male" if f0_hz < _MALE_FEMALE_CROSSOVER_HZ else "female"


def estimate_speaker_genders(job, source_audio_path: str, sample_clips_per_speaker: int = 3) -> dict[str, str]:
    """One gender guess per distinct speaker_id in the job, from real audio -
    not a fabricated default. For each speaker, samples their longest
    speech segments (more audio -> a more stable pitch read than a
    half-second utterance) up to sample_clips_per_speaker, estimates F0 on
    each, and classifies from the median across all of them. A speaker
    with no usable voiced audio anywhere (very short/quiet clips only)
    comes back 'unknown' rather than a guessed default - see
    AZURE_VOICE_BY_GENDER's caller for how 'unknown' is handled (falls
    back to the order's single default voice, exactly as if this speaker
    had never been assigned one at all)."""
    by_speaker: dict[str, list] = {}
    for seg in job.segments:
        if seg.segment_type == "gap" or not seg.speaker_id:
            continue
        by_speaker.setdefault(seg.speaker_id, []).append(seg)

    results: dict[str, str] = {}
    for speaker_id, segs in by_speaker.items():
        segs = sorted(segs, key=lambda s: s.end_ms - s.start_ms, reverse=True)[:sample_clips_per_speaker]
        f0s: list[float] = []
        for seg in segs:
            with _temp_clip() as clip_path:
                if not extract_audio_window(source_audio_path, seg.start_ms, seg.end_ms, clip_path, sample_rate=16000):
                    continue
                f0 = estimate_f0_hz(clip_path)
                if f0 is not None:
                    f0s.append(f0)
        results[speaker_id] = classify_gender(statistics.median(f0s) if f0s else None)
    return results


class _temp_clip:
    """Tiny scoped temp-file helper - a real file path extract_audio_window
    can write to and ffmpeg can read back, cleaned up whether or not the
    extraction actually succeeded."""

    def __enter__(self) -> str:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav")
        import os
        os.close(fd)
        self._path = path
        return path

    def __exit__(self, *exc) -> None:
        Path(self._path).unlink(missing_ok=True)
