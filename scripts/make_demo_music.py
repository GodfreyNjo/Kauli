"""Original background music for the demo videos, synthesized here so there is nothing to license: a soft pad over a chord
progression, with a light plucked arpeggio on the brighter bed.

    python scripts/make_demo_music.py     # writes webapp/static/demo/bed-bright.mp3 and bed-calm.mp3

The two beds are mixed quietly under the voice by scripts/make_demo_videos.py.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import wave

import numpy as np

SR = 44100
OUT = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "static" / "demo"


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def tone(freq: float, secs: float, harmonics=(1.0, 0.4, 0.15), detune=0.0) -> np.ndarray:
    t = np.arange(int(SR * secs)) / SR
    out = np.zeros_like(t)
    for k, amp in enumerate(harmonics, start=1):
        out += amp * np.sin(2 * np.pi * freq * k * (1 + detune) * t)
    return out


def pad(chord, secs: float) -> np.ndarray:
    n = int(SR * secs)
    sig = np.zeros(n)
    for note in chord:
        f = hz(note)
        sig += tone(f, secs, detune=0.0) + tone(f, secs, detune=0.003) + tone(f, secs, detune=-0.003)
    env = np.minimum(1.0, np.minimum(np.arange(n) / (SR * 0.8), (n - np.arange(n)) / (SR * 0.8)))
    return sig * env / (len(chord) * 3)


def pluck(freq: float, secs: float = 0.9) -> np.ndarray:
    n = int(SR * secs)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t)) * np.exp(-t * 5.5)


def build(progression, bpm: int, bars_per_chord: int, loops: int, arpeggio: bool) -> np.ndarray:
    beat = 60.0 / bpm
    chord_secs = beat * 4 * bars_per_chord
    total = int(SR * chord_secs * len(progression) * loops) + SR * 2
    mix = np.zeros(total)
    pos = 0
    for _ in range(loops):
        for root, chord in progression:
            seg = pad(chord, chord_secs + 0.6)
            mix[pos:pos + len(seg)] += seg * 0.8
            bass = tone(hz(root - 12), chord_secs, harmonics=(1.0, 0.2))
            bass *= np.minimum(1.0, np.arange(len(bass)) / (SR * 0.3)) * np.minimum(1.0, (len(bass) - np.arange(len(bass))) / (SR * 0.5))
            mix[pos:pos + len(bass)] += bass * 0.22
            if arpeggio:
                steps = int(bars_per_chord * 8)
                for i in range(steps):
                    note = chord[i % len(chord)] + 12
                    p = pluck(hz(note)) * 0.16
                    start = pos + int(SR * beat * 0.5 * i)
                    mix[start:start + len(p)] += p[: max(0, len(mix) - start)]
            pos += int(SR * chord_secs)
    peak = np.max(np.abs(mix)) or 1.0
    return mix / peak * 0.9


def write_mp3(signal: np.ndarray, name: str) -> None:
    stereo = np.stack([signal, np.roll(signal, 90)], axis=1)           # a hair of width
    pcm = (stereo * 32767).astype("<i2")
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = pathlib.Path(tmp) / "bed.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav_path), "-b:a", "96k", str(OUT / name)], check=True)
    print(name, (OUT / name).stat().st_size // 1024, "KB")


def main() -> None:
    # bright: C - G - Am - F at a steady pace, with a plucked arpeggio
    bright = [(60, [60, 64, 67]), (55, [59, 62, 67]), (57, [57, 60, 64]), (53, [57, 60, 65])]
    write_mp3(build(bright, bpm=104, bars_per_chord=1, loops=5, arpeggio=True), "bed-bright.mp3")
    # calm: Am - F - C - G, slow pads only
    calm = [(57, [57, 60, 64]), (53, [57, 60, 65]), (60, [60, 64, 67]), (55, [59, 62, 67])]
    write_mp3(build(calm, bpm=70, bars_per_chord=1, loops=3, arpeggio=False), "bed-calm.mp3")


if __name__ == "__main__":
    main()
