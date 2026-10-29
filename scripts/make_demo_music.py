"""Background music for the demo videos, composed by ElevenLabs Music (instrumental, no vocals), so it sounds like a real track rather
than a synthesizer loop. Two beds, mixed quietly under the voice by scripts/make_demo_videos.py:

    bright  - an upbeat, clean product-video bed (the creator/marketing demo)
    calm    - a soft piano and pad bed (the course and podcast demos)

    ELEVENLABS_API_KEY=... python scripts/make_demo_music.py [--only bright|calm] [--force]

The key is read from the environment, or from the ELEVENLABS_API_KEY line of the project's .env. It is never printed. Each bed is a
one-off render of about a minute (it costs ElevenLabs credits once, not per visitor); the files are committed, so nothing here runs in
production. Each result is levelled with ffmpeg and saved as a small mp3 in webapp/static/demo/.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "webapp" / "static" / "demo"
API = "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128"
LENGTH_MS = 60_000

BEDS = {
    "bright": ("Bright, modern, uplifting instrumental for a product video. Clean plucked guitar and soft marimba over a light, steady beat "
               "with a warm round bass, positive and confident, about 105 BPM. No vocals. A simple, repeating feel that stays out of the way "
               "of a speaking voice, with no big drops or loud builds."),
    "calm": ("Calm, warm instrumental for an educational course or a podcast. Soft felt piano and airy ambient pads, slow and gentle, "
             "about 70 BPM. No vocals and no drums. Very unobtrusive under a speaking voice, steady from start to finish."),
}


def api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Set ELEVENLABS_API_KEY (environment or .env) first.")


def compose(prompt: str, key: str) -> bytes:
    body = json.dumps({"prompt": prompt, "music_length_ms": LENGTH_MS, "force_instrumental": True}).encode()
    req = urllib.request.Request(API, data=body, headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"ElevenLabs Music said {exc.code}: {detail}")


def finish(raw: bytes, target: pathlib.Path) -> None:
    """Level the track so both beds sit at the same loudness, fade the ends, and save a small mp3."""
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "raw.mp3"
        src.write_bytes(raw)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af",
                        "loudnorm=I=-18:TP=-2:LRA=7,afade=t=in:d=0.8,afade=t=out:st=%.1f:d=2.5" % (LENGTH_MS / 1000 - 2.5),
                        "-ac", "2", "-b:a", "96k", str(target)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(BEDS), default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    key = api_key()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, prompt in BEDS.items():
        if args.only and name != args.only:
            continue
        target = OUT / f"bed-{name}.mp3"
        if target.exists() and not args.force:
            print(f"bed-{name}.mp3 exists (use --force to compose it again)")
            continue
        print(f"composing {name} ...", flush=True)
        raw = compose(prompt, key)
        if len(raw) < 50_000:
            raise SystemExit(f"{name}: the answer was only {len(raw)} bytes, not a track")
        finish(raw, target)
        print(f"bed-{name}.mp3", target.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    sys.exit(main())
