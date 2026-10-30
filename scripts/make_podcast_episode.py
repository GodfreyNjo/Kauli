"""Turn a podcast script (marketing/podcast/episode-01-script.md) into a finished episode, all on ElevenLabs:

    voices   ElevenLabs Text to Dialogue (Eleven v3): AMARA and DAN each get their own voice, one request per chunk of a section
    music    ElevenLabs Music: the theme, a quiet bed that sits under the voices, and the outro (instrumental)
    effects  ElevenLabs Sound Effects: room tone, key click, page turn, ding, whoosh, chime
    mix      ffmpeg: bed under the voices, stingers between the parts, levelled to -16 LUFS, MP3 with its tags

    python scripts/make_podcast_episode.py --dry-run          # how many characters and credits it will use, nothing is spent
    python scripts/make_podcast_episode.py --probe            # a two-line test of the voices (about 80 characters)
    python scripts/make_podcast_episode.py                    # the whole episode
    python scripts/make_podcast_episode.py --mock             # placeholder audio instead of ElevenLabs, to check the mixing for free

Everything it makes is kept in marketing/podcast/build/, and a step that already has its file is skipped, so a run that stops halfway (out
of credits, a dropped connection) carries on where it stopped. The key is read from the environment or the project's .env and is never
printed.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOW = ROOT / "marketing" / "podcast"
SCRIPT = SHOW / "episode-01-script.md"
BUILD = SHOW / "build"
FINAL = SHOW / "kauli-guides-ep01.mp3"
API = "https://api.elevenlabs.io"

VOICES = {"AMARA": "cgSgspJ2msm6clMCkdW9",      # Jessica - playful, bright, warm
          "DAN": "iP95p4xoKVk53GoZ742B"}         # Chris - charming, down-to-earth
MODEL = "eleven_v3"
CHUNK_CHARS = 2400                               # one request per chunk; a section is a few chunks
PARTS = ["COLD OPEN", "INTRO", "PART 1", "PART 2", "PART 3", "PART 4", "PART 5", "PART 6", "THE CHEAT SHEET", "OUTRO"]

MUSIC = {
    "theme": ("Warm, upbeat, modern podcast theme tune. Light percussion, plucked strings and soft marimba, positive and curious, about 100 BPM, "
              "a clear melody with a clean ending chord. No vocals.", 30_000),
    "bed": ("A quiet, steady instrumental bed to sit under two people talking: soft felt piano, airy pads and a very gentle pulse, about 80 BPM, "
            "no melody that competes with speech, no drops, no vocals, even from start to finish.", 60_000),
    "outro": ("A short, warm outro for a podcast: the same plucked strings and soft marimba, building gently for a few bars and resolving "
              "to a final chord. About 100 BPM. No vocals.", 20_000),
}
EFFECTS = {
    "roomtone": ("quiet room tone, very faint ambience of a small quiet room", 4.0),
    "keyclick": ("a single soft laptop key click", 1.0),
    "pageturn": ("a soft page turn", 1.5),
    "ding": ("a short light notification ding", 1.0),
    "whoosh": ("a soft gentle whoosh transition", 1.5),
    "chime": ("a soft pleasant single chime", 1.5),
}


def api_key() -> str:
    """The podcast has its own ElevenLabs account (ELEVENLABS_PODCAST_API_KEY), so its voices never use up the credits behind customers' Premium
    voices. Falls back to the site's key only when no podcast key is set."""
    for name in ("ELEVENLABS_PODCAST_API_KEY", "ELEVENLABS_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    env = ROOT / ".env"
    if env.exists():
        lines = env.read_text(encoding="utf-8", errors="ignore").splitlines()
        for name in ("ELEVENLABS_PODCAST_API_KEY", "ELEVENLABS_API_KEY"):
            for line in lines:
                if line.startswith(name + "="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    raise SystemExit("Set ELEVENLABS_PODCAST_API_KEY (environment or .env) first.")


def call(path: str, body: dict | None = None, key: str = "", timeout: int = 600) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers={"xi-api-key": key, "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"ElevenLabs said {exc.code} for {path.split('?')[0]}: {detail}")


def quota(key: str) -> tuple[int, int]:
    sub = json.loads(call("/v1/user/subscription", key=key))
    return int(sub["character_count"]), int(sub["character_limit"])


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit("ffmpeg failed: " + " ".join(cmd[:6]) + " ...\n" + proc.stderr[-800:])


def ff(*args: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args])


# ------------------------------------------------------------------------------------------------------------------ the script
def sections() -> dict[str, list[tuple[str, str]]]:
    """{part: [(speaker, text), ...]} from the script's `## ` headings; only AMARA:/DAN: lines are spoken."""
    out: dict[str, list[tuple[str, str]]] = {}
    current = None
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip().upper()
            current = next((p for p in PARTS if title.startswith(p)), None)
            if current:
                out[current] = []
        elif current:
            m = re.match(r"^(AMARA|DAN): (.+)$", line)
            if m:
                out[current].append((m.group(1), m.group(2).strip()))
    missing = [p for p in PARTS if not out.get(p)]
    if missing:
        raise SystemExit("These parts have no spoken lines in the script: " + ", ".join(missing))
    return out


def chunks(lines: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    groups, cur, size = [], [], 0
    for spk, text in lines:
        if cur and size + len(text) > CHUNK_CHARS:
            groups.append(cur)
            cur, size = [], 0
        cur.append((spk, text))
        size += len(text)
    if cur:
        groups.append(cur)
    return groups


# ------------------------------------------------------------------------------------------------------------------ making audio
def tone(path: pathlib.Path, seconds: float, freq: int = 220) -> None:
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-ac", "2", "-ar", "44100", str(path))


def dialogue(path: pathlib.Path, lines: list[tuple[str, str]], key: str, mock: bool) -> None:
    if path.exists():
        return
    if mock:
        tone(path, max(2.0, sum(len(t) for _, t in lines) / 15.0), 180 + 40 * (hash(lines[0][0]) % 3))
        return
    body = {"inputs": [{"text": text, "voice_id": VOICES[spk]} for spk, text in lines], "model_id": MODEL}
    path.write_bytes(call("/v1/text-to-dialogue?output_format=mp3_44100_128", body, key))


def music(name: str, key: str, mock: bool) -> pathlib.Path:
    path = BUILD / f"music-{name}.mp3"
    if path.exists():
        return path
    prompt, ms = MUSIC[name]
    if mock:
        tone(path, ms / 1000, {"theme": 330, "bed": 110, "outro": 392}[name])
    else:
        path.write_bytes(call("/v1/music?output_format=mp3_44100_128", {"prompt": prompt, "music_length_ms": ms, "force_instrumental": True}, key, 300))
    return path


def effect(name: str, key: str, mock: bool) -> pathlib.Path:
    path = BUILD / f"sfx-{name}.mp3"
    if path.exists():
        return path
    text, seconds = EFFECTS[name]
    if mock:
        tone(path, seconds, 880)
    else:
        path.write_bytes(call("/v1/sound-generation?output_format=mp3_44100_128", {"text": text, "duration_seconds": seconds}, key, 120))
    return path


# ------------------------------------------------------------------------------------------------------------------ the mix
def join(parts: list[pathlib.Path], out: pathlib.Path, gap: float = 0.0) -> None:
    listing = BUILD / (out.stem + ".txt")
    silence = BUILD / "gap.wav"
    if gap and not silence.exists():
        ff("-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(gap), str(silence))
    rows = []
    for i, p in enumerate(parts):
        rows.append(f"file '{p.as_posix()}'")
        if gap and i < len(parts) - 1:
            rows.append(f"file '{silence.as_posix()}'")
    listing.write_text("\n".join(rows) + "\n", encoding="utf-8")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-ar", "44100", "-ac", "2", str(out))


def under(voice: pathlib.Path, bed: pathlib.Path, out: pathlib.Path, level_db: int = -21) -> None:
    """The voices with the bed quietly underneath, the bed fading in and out around them."""
    ff("-i", str(voice), "-stream_loop", "-1", "-i", str(bed), "-filter_complex",
       f"[1:a]volume={level_db}dB,afade=t=in:d=1.5[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[m]", "-map", "[m]", "-ar", "44100", "-ac", "2", str(out))


def slice_audio(src: pathlib.Path, start: float, seconds: float, out: pathlib.Path, fade: float = 0.4, gain_db: int = 0) -> None:
    ff("-ss", str(start), "-t", str(seconds), "-i", str(src), "-af",
       f"afade=t=in:d={fade},afade=t=out:st={max(seconds - fade, 0)}:d={fade},volume={gain_db}dB", "-ar", "44100", "-ac", "2", str(out))


def overlay(base: pathlib.Path, fx: pathlib.Path, at: float, out: pathlib.Path, gain_db: int = -10) -> None:
    ff("-i", str(base), "-i", str(fx), "-filter_complex",
       f"[1:a]volume={gain_db}dB,adelay={int(at * 1000)}|{int(at * 1000)}[f];[0:a][f]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[m]",
       "-map", "[m]", "-ar", "44100", "-ac", "2", str(out))


def build(key: str, mock: bool) -> None:
    secs = sections()
    voice: dict[str, pathlib.Path] = {}
    for part in PARTS:
        pieces = []
        for i, grp in enumerate(chunks(secs[part])):
            p = BUILD / f"voice-{PARTS.index(part):02d}-{i}.mp3"
            dialogue(p, grp, key, mock)
            pieces.append(p)
        out = BUILD / f"part-{PARTS.index(part):02d}.wav"
        if not out.exists():
            join(pieces, out, gap=0.3)
        voice[part] = out
    theme, bed, outro = music("theme", key, mock), music("bed", key, mock), music("outro", key, mock)
    fx = {n: effect(n, key, mock) for n in EFFECTS}

    sting = BUILD / "sting.wav"
    slice_audio(theme, 0, 4, sting, 0.1, -3)
    stinger = BUILD / "stinger.wav"
    slice_audio(theme, 4, 3, stinger, 0.5, -8)

    cold = BUILD / "mix-cold.wav"
    cold_voice = BUILD / "mix-cold-voice.wav"
    under(voice["COLD OPEN"], fx["roomtone"], cold_voice, -26)
    overlay(cold_voice, fx["keyclick"], 0.8, cold, -14)

    intro_voice = BUILD / "mix-intro-voice.wav"
    under(voice["INTRO"], bed, intro_voice)
    theme_open = BUILD / "theme-open.wav"
    slice_audio(theme, 0, 9, theme_open, 0.05, 0)
    intro = BUILD / "mix-intro.wav"
    ff("-i", str(theme_open), "-i", str(intro_voice), "-filter_complex", "[0:a][1:a]acrossfade=d=1.5:c1=tri:c2=tri[m]", "-map", "[m]", str(intro))

    finals: list[pathlib.Path] = [cold, sting, intro]
    cues = {"PART 1": ("pageturn", 0.3, None), "PART 2": (None, 0, ("ding", -2.0)), "PART 4": (None, 0, ("whoosh", -2.5))}
    for part in ("PART 1", "PART 2", "PART 3", "PART 4", "PART 5", "PART 6"):
        mixed = BUILD / f"mix-{PARTS.index(part):02d}.wav"
        cue = cues.get(part)
        if cue and cue[0]:
            overlay(voice[part], fx[cue[0]], cue[1], mixed, -12)
        elif cue and cue[2]:
            length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(voice[part])],
                                          capture_output=True, text=True).stdout.strip() or 0)
            overlay(voice[part], fx[cue[2][0]], max(length + cue[2][1], 0), mixed, -12)
        else:
            mixed = voice[part]
        finals += [stinger, mixed]
    cheat = BUILD / "mix-cheat.wav"
    under(voice["THE CHEAT SHEET"], bed, cheat, -20)
    cheat_end = BUILD / "mix-cheat-end.wav"
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(cheat)],
                                  capture_output=True, text=True).stdout.strip() or 0)
    overlay(cheat, fx["chime"], max(length - 1.0, 0), cheat_end, -10)
    outro_voice = BUILD / "mix-outro-voice.wav"
    under(voice["OUTRO"], bed, outro_voice, -19)
    outro_tail = BUILD / "outro-tail.wav"
    slice_audio(outro, 0, 14, outro_tail, 0.1, 0)
    outro_mix = BUILD / "mix-outro.wav"
    ff("-i", str(outro_voice), "-i", str(outro_tail), "-filter_complex", "[0:a][1:a]acrossfade=d=2:c1=tri:c2=tri[m]",
       "-map", "[m]", str(outro_mix))
    finals += [stinger, cheat_end, outro_mix]

    full = BUILD / "episode-full.wav"
    join(finals, full, gap=0.0)
    FINAL.parent.mkdir(parents=True, exist_ok=True)
    ff("-i", str(full), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2", "-b:a", "128k",
       "-metadata", "title=Why Your Transcript Is Wrong, and How to Fix It for Good", "-metadata", "artist=Kauli", "-metadata", "album=Kauli Guides",
       "-metadata", "track=1", "-metadata", "comment=Voiced with AI voices", str(FINAL))
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(FINAL)],
                                   capture_output=True, text=True).stdout.strip() or 0)
    print(f"Done: {FINAL}  ({int(seconds // 60)}:{int(seconds % 60):02d}, {FINAL.stat().st_size / 1_000_000:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    secs = sections()
    voiced = sum(len(t) for lines in secs.values() for _, t in lines)
    requests = sum(len(chunks(v)) for v in secs.values())
    key = "" if args.mock else api_key()
    if args.dry_run or args.probe:
        used, limit = (0, 0) if args.mock else quota(key)
        print(f"Spoken text: {voiced:,} characters in {requests} dialogue requests (voices use one credit per character).")
        print(f"Music: {sum(ms for _, ms in MUSIC.values()) // 1000} seconds. Sound effects: {sum(s for _, s in EFFECTS.values()):.0f} seconds.")
        if limit:
            print(f"Credits this period: {used:,} used of {limit:,}, {limit - used:,} left. The voices alone need {voiced:,}"
                  + ("." if limit - used >= voiced else f", which is {voiced - (limit - used):,} more than is left."))
    if args.dry_run:
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    if args.probe:
        p = BUILD / "probe.mp3"
        dialogue(p, [("AMARA", "Okay, so, I'm going to read you a line from a transcript."), ("DAN", "[laughs] Always.")], key, args.mock)
        print("Probe written:", p, f"{p.stat().st_size} bytes")
        return
    if not args.mock:
        used, limit = quota(key)
        if limit - used < voiced:
            raise SystemExit(f"Not enough ElevenLabs credits: {limit - used:,} left and the voices need {voiced:,} (music and effects need more). "
                             "Nothing was spent. Add credits or wait for the monthly reset, then run this again.")
    build(key, args.mock)


if __name__ == "__main__":
    sys.exit(main())
