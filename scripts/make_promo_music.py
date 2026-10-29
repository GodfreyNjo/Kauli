"""Original music for the Kauli promo video, synthesized here so there is nothing to license.

An upbeat pop-style bed at 108 BPM in C major (C - G - Am - F): silence, a riser, a light first section, a build with drums and bass,
a full peak, a dip under the trust line, a lift, and one clean final chord. The section times come from the video's scene timeline
(scripts/make_promo_video.py), so the music always changes where the picture does.

    from make_promo_music import compose
    compose({"enter": 6.4, "build": 13, "peak": 28, "dip": 42, "lift": 54, "end": 72, "total": 76}, "bed.mp3")
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import wave

import numpy as np

SR = 44100
BPM = 108
BEAT = 60.0 / BPM
BAR = BEAT * 4
# (bass, upper triad) as MIDI notes for each bar of the loop
CHORDS = [(36, (60, 64, 67)), (43, (59, 62, 67)), (45, (57, 60, 64)), (41, (57, 60, 65))]


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _rng():
    return np.random.default_rng(7)


def _add(buf: np.ndarray, sig: np.ndarray, t: float, gain: float = 1.0) -> None:
    i = int(t * SR)
    if i >= len(buf) or i < 0:
        return
    n = min(len(sig), len(buf) - i)
    buf[i:i + n] += sig[:n] * gain


def pluck(freq: float, dur: float = 0.55) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    s = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2 * t) + 0.12 * np.sin(2 * np.pi * freq * 3 * t)
    return s * np.exp(-t * 6.5) * np.minimum(1, t * 400)


def kick() -> np.ndarray:
    t = np.arange(int(SR * 0.32)) / SR
    f = 42 + 85 * np.exp(-t * 32)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9)


def snap(rng) -> np.ndarray:
    t = np.arange(int(SR * 0.14)) / SR
    n = rng.standard_normal(len(t))
    n = n - np.convolve(n, np.ones(6) / 6, mode="same")           # a rough high-pass
    return n * np.exp(-t * 38) * 0.9


def hat(rng, length: float = 0.05) -> np.ndarray:
    t = np.arange(int(SR * length)) / SR
    n = rng.standard_normal(len(t))
    n = np.diff(n, prepend=0)
    return n * np.exp(-t * 110) * 0.6


def bass(freq: float, dur: float = 0.5) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    return (np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2 * t)) * np.exp(-t * 3.2) * np.minimum(1, t * 120)


def pad(notes, secs: float) -> np.ndarray:
    t = np.arange(int(SR * secs)) / SR
    sig = np.zeros_like(t)
    for m in notes:
        f = hz(m)
        for d in (0.0, 0.003, -0.003):
            sig += np.sin(2 * np.pi * f * (1 + d) * t) + 0.3 * np.sin(2 * np.pi * f * 2 * (1 + d) * t)
    env = np.minimum(1, np.minimum(t / 0.35, (secs - t) / 0.45))
    return sig * np.clip(env, 0, 1) / (len(notes) * 3)


def riser(secs: float, rng) -> np.ndarray:
    t = np.arange(int(SR * secs)) / SR
    f = 250 + 2600 * (t / secs) ** 2
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
    noise = np.diff(rng.standard_normal(len(t)), prepend=0)
    return (0.55 * tone + 0.45 * noise) * (t / secs) ** 1.6 * 0.7


def level_at(t: float, cues: dict) -> int:
    """0 silent, 1 light, 2 build, 3 full."""
    if t < cues["enter"]:
        return 0
    if t < cues["build"]:
        return 1
    if t < cues["peak"]:
        return 2
    if t < cues["dip"]:
        return 3
    if t < cues["lift"]:
        return 1
    if t < cues["lift"] + 4:
        return 2
    return 3 if t < cues["end"] else 0


def compose(cues: dict, out_path: str | pathlib.Path) -> pathlib.Path:
    """cues: enter, build, peak, dip, lift, end (the last chord), total (seconds). Writes an mp3."""
    rng = _rng()
    total = cues["total"]
    buf = np.zeros(int((total + 1) * SR))
    # a whisper of room tone before the music enters
    _add(buf, rng.standard_normal(int(cues["enter"] * SR)) * 0.002, 0.0)
    bar_i, t0 = 0, cues["enter"]
    while t0 < cues["end"]:
        lvl = level_at(t0, cues)
        bass_note, triad = CHORDS[bar_i % 4]
        if lvl >= 1:
            _add(buf, pad((bass_note + 12,) + triad, BAR + 0.2), t0, 0.55 if lvl == 1 else 0.4)
        for step in range(8):                                   # eighth notes
            t = t0 + step * BEAT / 2
            up = triad[(step if step < 4 else 7 - step) % 3]
            if lvl == 1 and step % 2 == 0:
                _add(buf, pluck(hz(up), 0.6), t, 0.22)
            elif lvl == 2:
                _add(buf, pluck(hz(up + (12 if step % 4 == 3 else 0)), 0.5), t, 0.2)
            elif lvl == 3:
                _add(buf, pluck(hz(up + 12), 0.45), t, 0.2)
            if lvl >= 2:
                _add(buf, hat(rng), t, 0.16 if lvl == 2 else 0.24)
            if lvl >= 2 and step in (0, 3):
                _add(buf, bass(hz(bass_note), 0.55), t, 0.55)
            if lvl == 3 and step == 6:
                _add(buf, bass(hz(bass_note + 7), 0.4), t, 0.4)
        for beat in range(4):
            t = t0 + beat * BEAT
            if lvl >= 2 and (beat in (0, 2) or lvl == 3):
                _add(buf, kick(), t, 0.7)
            if lvl >= 2 and beat in (1, 3):
                _add(buf, snap(rng), t, 0.32)
        bar_i += 1
        t0 += BAR
    for end_time, secs in ((cues["enter"], 1.8), (cues["peak"], 1.3), (cues["lift"], 1.6)):
        _add(buf, riser(secs, rng), max(0.0, end_time - secs), 0.3)
    # the last chord: everything lands together, then rings out
    end = cues["end"]
    for m in (36, 48, 52, 55, 59, 64, 67, 72):
        _add(buf, pluck(hz(m), 2.6), end, 0.2)
    _add(buf, pad((48, 55, 60, 64, 67, 71), 3.2), end, 0.5)
    _add(buf, kick(), end, 0.8)
    _add(buf, bass(hz(36), 1.6), end, 0.6)
    buf = buf[: int((total) * SR)]
    fade = int(0.6 * SR)
    buf[-fade:] *= np.linspace(1, 0, fade)
    peak = np.max(np.abs(buf)) or 1.0
    pcm = (buf / peak * 0.85 * 32767).astype(np.int16)
    out_path = pathlib.Path(out_path)
    with tempfile.TemporaryDirectory() as tmp:
        wav = pathlib.Path(tmp) / "bed.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)], check=True)
    return out_path


if __name__ == "__main__":
    print(compose({"enter": 6.4, "build": 13.4, "peak": 28.4, "dip": 42.4, "lift": 54.4, "end": 72.0, "total": 76.0}, "promo-bed-test.mp3"))
