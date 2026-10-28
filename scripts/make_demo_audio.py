"""Render the audio for the /try demo with ElevenLabs and save it under webapp/static/demo/.

Run once (and again whenever webapp/demo_content.py changes):

    ELEVENLABS_API_KEY=... python scripts/make_demo_audio.py [--force]

It spends ElevenLabs characters (about 900 for the full set), so existing files are skipped unless --force is given.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp import demo_content  # noqa: E402

OUT = ROOT / "webapp" / "static" / "demo"


def render(text: str, voice: str, lang: str | None, key: str) -> bytes:
    # Swahili needs the v3 model with an explicit language; the others use the multilingual model.
    body: dict = {"text": text, "model_id": "eleven_v3" if lang == "sw" else "eleven_multilingual_v2"}
    if lang == "sw":
        body["language_code"] = "sw"
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", params={"output_format": "mp3_44100_64"},
                   headers={"xi-api-key": key}, json=body, timeout=120)
    if r.status_code != 200:
        raise SystemExit(f"ElevenLabs refused ({r.status_code}): {r.text[:300]}")
    return r.content


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise SystemExit("Set ELEVENLABS_API_KEY first.")
    OUT.mkdir(parents=True, exist_ok=True)
    chars = 0
    for name, voice, lang, text in demo_content.all_clips():
        target = OUT / name
        if target.exists() and not args.force:
            print("skip", name)
            continue
        target.write_bytes(render(text, voice, lang, key))
        chars += len(text)
        print("wrote", name, f"({len(text)} chars, {target.stat().st_size // 1024} KB)")
    print("characters used:", chars)


if __name__ == "__main__":
    main()
