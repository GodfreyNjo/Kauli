"""Render the audio for the /try demo with ElevenLabs and save it under webapp/static/demo/.

Run once (and again whenever webapp/demo_content.py changes):

    ELEVENLABS_API_KEY=... python scripts/make_demo_audio.py [--force] [--only meeting,interview] [--dry-run]

It spends ElevenLabs characters, so existing files are skipped unless --force is given, and --dry-run says how many a run would
spend without spending any. Every person in a scenario's cast has their own voice; a turn is rendered on its own and the turns
are joined with a short pause, and the start and end of every turn is saved in timing.json so the videos and the transcript know
who is speaking when. Speaker labels ("Speaker 1:") are never spoken.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp import demo_content  # noqa: E402

OUT = ROOT / "webapp" / "static" / "demo"
TIMING = OUT / "timing.json"
PAUSE_S = 0.45


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


def seconds(path: pathlib.Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def join(parts: list[pathlib.Path], target: pathlib.Path, tmp: pathlib.Path) -> None:
    sil = tmp / "pause.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(PAUSE_S),
                    "-c:a", "libmp3lame", "-b:a", "64k", str(sil)], check=True)
    lines = []
    for i, part in enumerate(parts):
        if i:
            lines.append(f"file '{sil}'")
        lines.append(f"file '{part}'")
    listing = tmp / "list.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame",
                    "-b:a", "64k", "-ar", "44100", "-ac", "1", str(target)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated scenarios")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only = {k for k in args.only.split(",") if k}
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    OUT.mkdir(parents=True, exist_ok=True)
    timing = json.loads(TIMING.read_text(encoding="utf-8")) if TIMING.exists() else {}
    todo = []
    for name, scenario, lang, text in demo_content.all_clips():
        if only and scenario not in only:
            continue
        target = OUT / name
        if target.exists() and not args.force:
            if name not in timing:                                   # an older single-voice clip: one turn, the whole clip
                timing[name] = [[0, 0.0, round(seconds(target), 2)]]
            continue
        todo.append((name, scenario, lang, text))
    chars = sum(len(body) for _, scenario, _, text in todo for _, body in demo_content.turns(scenario, text))
    print(f"{len(todo)} clips to render, {chars} characters")
    if args.dry_run:
        return
    if not key:
        raise SystemExit("Set ELEVENLABS_API_KEY first.")
    for name, scenario, lang, text in todo:
        cast = demo_content.SCENARIOS[scenario]["cast"]
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t)
            parts, marks, cursor = [], [], 0.0
            for i, (who, body) in enumerate(demo_content.turns(scenario, text)):
                part = tmp / f"t{i}.mp3"
                part.write_bytes(render(body, cast[who]["voice"], lang, key))
                d = seconds(part)
                if i:
                    cursor += PAUSE_S
                marks.append([who, round(cursor, 2), round(cursor + d, 2)])
                cursor += d
                parts.append(part)
            target = OUT / name
            if len(parts) == 1:
                target.write_bytes(parts[0].read_bytes())
            else:
                join(parts, target, tmp)
        timing[name] = marks
        TIMING.write_text(json.dumps(timing, indent=1), encoding="utf-8")
        print("wrote", name, marks)
    TIMING.write_text(json.dumps(timing, indent=1), encoding="utf-8")
    print("characters used:", chars)


if __name__ == "__main__":
    main()
