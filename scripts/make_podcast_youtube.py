"""The YouTube version of a podcast episode: a 1080p video, a thumbnail, channel art and captions.

    python scripts/make_podcast_youtube.py graphics    # thumbnail, channel banner and avatar, and the chapter frames
    python scripts/make_podcast_youtube.py captions    # captions.srt: timed by Kauli's own transcription model, worded from the script
    python scripts/make_podcast_youtube.py video       # the finished MP4 (frames + live waveform + progress bar + the audio)
    python scripts/make_podcast_youtube.py all

Reads marketing/podcast/kauli-guides-ep01.mp3 and the pieces in marketing/podcast/build (see make_podcast_episode.py), writes to
marketing/podcast/youtube/. Costs nothing: the pictures are drawn here, the timing is done on this machine, and nothing is uploaded.
"""
from __future__ import annotations

import difflib
import glob
import json
import math
import pathlib
import re
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOW = ROOT / "marketing" / "podcast"
BUILD = SHOW / "build"
OUT = SHOW / "youtube"
AUDIO = SHOW / "kauli-guides-ep01.mp3"
DIALOGUE = SHOW / "episode-01-dialogue-plain.txt"
LOGO = ROOT / "webapp" / "static" / "kauli-logo-nav-light.png"
COVER = ROOT / "webapp" / "static" / "podcast-cover.jpg"
FONTS = pathlib.Path("/mnt/c/Windows/Fonts")
BOLD, REG = FONTS / "segoeuib.ttf", FONTS / "segoeui.ttf"

W, H, FPS = 1920, 1080, 30
NAVY, BLUE, SKY, ORANGE, WHITE = (8, 54, 95), (10, 111, 184), (190, 216, 244), (244, 122, 10), (255, 255, 255)
GREEN, RED = (46, 190, 120), (240, 84, 84)

CHAPTERS = ["Cold open", "Welcome", "Why transcripts fail", "Fix it before you record", "Names, numbers, speakers",
            "Subtitles people can read", "Translation and dubbing", "AI or human, and cost", "The ten-point cheat sheet", "Wrap-up"]
TITLE_LINES = ["Why Your Transcript", "Is Wrong, and How", "to Fix It for Good"]


def font(path: pathlib.Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit(" ".join(cmd[:4]) + " failed:\n" + proc.stderr[-1200:])
    return proc.stdout


def duration(path: pathlib.Path) -> float:
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).strip())


# ------------------------------------------------------------------------------------------------------------------ chapter times
def chapter_starts() -> list[float]:
    """Where each chapter begins in the finished episode, from the lengths of the pieces it was mixed from (the same arithmetic as the mix)."""
    def part(i: int) -> float:
        files = sorted(glob.glob(str(BUILD / f"voice-{i:02d}-*.mp3")))
        return sum(duration(pathlib.Path(f)) for f in files) + 0.3 * (len(files) - 1)
    marks = [0.0]
    t = part(0) + 4                                  # the cold open, then the 4-second sting
    marks.append(t)                                  # welcome
    t += 9 + part(1) - 1.5
    for i in range(2, 8):                            # six parts, each after a 3-second stinger
        t += 3
        marks.append(t)
        t += part(i)
    t += 3
    marks.append(t)                                  # the cheat sheet
    t += part(8)
    marks.append(t)                                  # the wrap-up
    return marks


# ------------------------------------------------------------------------------------------------------------------ pictures
def gradient(w: int, h: int) -> Image.Image:
    y, x = np.mgrid[0:h, 0:w]
    t = ((x / w) * 0.55 + (y / h) * 0.75) / 1.3
    arr = np.zeros((h, w, 3), dtype=np.float32)
    for c in range(3):
        arr[..., c] = NAVY[c] + (BLUE[c] - NAVY[c]) * t
    return Image.fromarray(arr.astype("uint8"), "RGB").convert("RGBA")


def glow(img: Image.Image, cx: int, cy: int, r: int, color: tuple, alpha: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (alpha,))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r // 2)))


def logo(height: int) -> Image.Image:
    im = Image.open(LOGO).convert("RGBA")
    return im.resize((int(im.width * height / im.height), height), Image.LANCZOS)


def text(d: ImageDraw.ImageDraw, xy, s: str, f, fill, anchor="la", shadow=False) -> None:
    if shadow:
        d.text((xy[0] + 3, xy[1] + 4), s, font=f, fill=(0, 20, 45, 120), anchor=anchor)
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def frame(current: int) -> Image.Image:
    img = gradient(W, H)
    glow(img, 1500, 120, 420, SKY, 40)
    glow(img, 200, 980, 380, ORANGE, 38)
    img.alpha_composite(logo(74), (90, 64))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    text(d, (92, 214), "KAULI GUIDES  ·  EPISODE 1", font(BOLD, 32), ORANGE + (255,))
    for i, line in enumerate(TITLE_LINES):
        text(d, (90, 268 + i * 98), line, font(BOLD, 88), WHITE + (255,), shadow=True)
    # now playing
    text(d, (92, 620), "NOW PLAYING", font(BOLD, 26), SKY + (255,))
    text(d, (92, 660), CHAPTERS[current], font(BOLD, 54), WHITE + (255,))
    text(d, (1090, 690), f"{current + 1:02d} / {len(CHAPTERS)}", font(BOLD, 34), SKY + (255,), anchor="ra")
    # who is talking
    for k, (name, x) in enumerate((("AMARA", 92), ("DAN", 330))):
        d.rounded_rectangle((x, 930, x + 212, 986), 28, fill=(255, 255, 255, 34), outline=(255, 255, 255, 70), width=2)
        d.ellipse((x + 10, 940, x + 46, 976), fill=ORANGE + (255,) if k == 0 else SKY + (255,))
        text(d, (x + 28, 958), name[0], font(BOLD, 24), NAVY + (255,), anchor="mm")
        text(d, (x + 60, 958), name.title(), font(BOLD, 26), WHITE + (255,), anchor="lm")
    text(d, (580, 958), "Voiced with AI voices  ·  kauli-forgemedia.com", font(REG, 26), SKY + (255,), anchor="lm")
    # chapter list
    d.rounded_rectangle((1240, 64, 1840, 1012), 28, fill=(255, 255, 255, 26), outline=(255, 255, 255, 56), width=2)
    text(d, (1280, 108), "IN THIS EPISODE", font(BOLD, 26), ORANGE + (255,))
    for i, name in enumerate(CHAPTERS):
        y = 168 + i * 82
        on = i == current
        if on:
            d.rounded_rectangle((1262, y - 8, 1818, y + 64), 18, fill=(255, 255, 255, 46))
            d.rounded_rectangle((1262, y - 8, 1272, y + 64), 5, fill=ORANGE + (255,))
        a = 255 if on else 150
        d.ellipse((1296, y + 6, 1342, y + 52), fill=(ORANGE if on else WHITE) + ((255,) if on else (46,)))
        text(d, (1319, y + 29), str(i + 1), font(BOLD, 24), (WHITE if on else SKY) + (255,), anchor="mm")
        text(d, (1366, y + 29), name, font(BOLD if on else REG, 30), WHITE + (a,), anchor="lm")
    # progress track
    d.rectangle((0, H - 14, W, H), fill=(0, 20, 45, 150))
    img.alpha_composite(layer)
    return img.convert("RGB")


def thumbnail() -> Image.Image:
    w, h = 1280, 720
    img = gradient(w, h)
    glow(img, 1000, 140, 380, SKY, 46)
    glow(img, 140, 700, 320, ORANGE, 40)
    img.alpha_composite(logo(56), (50, 40))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def fit(s: str, max_w: int, top: int = 170) -> ImageFont.FreeTypeFont:
        size = top
        while size > 40 and d.textlength(s, font=font(BOLD, size)) > max_w:
            size -= 4
        return font(BOLD, size)

    left_w = 590                                             # the words stay left of the card
    f1, f2, f3 = fit("YOUR", left_w), fit("TRANSCRIPT", left_w), fit("IS WRONG", left_w)
    y = 140
    text(d, (50, y), "YOUR", f1, WHITE + (255,), shadow=True)
    y += int(f1.size * 1.12)
    text(d, (50, y), "TRANSCRIPT", f2, WHITE + (255,), shadow=True)
    y += int(f2.size * 1.12)
    text(d, (50, y), "IS ", f3, WHITE + (255,), shadow=True)
    text(d, (50 + d.textlength("IS ", font=f3), y), "WRONG", f3, ORANGE + (255,), shadow=True)
    d.rounded_rectangle((50, 612, 560, 676), 14, fill=ORANGE + (255,))
    text(d, (305, 644), "KAULI GUIDES  ·  EP 1", font(BOLD, 34), NAVY + (255,), anchor="mm")
    img.alpha_composite(layer)
    # the hook: a transcript card with the mistake and the fix
    card = Image.new("RGBA", (560, 400), (0, 0, 0, 0))
    c = ImageDraw.Draw(card, "RGBA")
    c.rounded_rectangle((0, 0, 559, 399), 30, fill=(255, 255, 255, 250))
    text(c, (36, 34), "TRANSCRIPT", font(BOLD, 24), (110, 130, 160, 255))
    text(c, (36, 82), "…budget for the new", font(REG, 40), (30, 50, 80, 255))
    text(c, (36, 130), "clinic in", font(REG, 40), (30, 50, 80, 255))
    wrong, right = font(BOLD, 64), font(BOLD, 64)
    text(c, (36, 196), "Nye Robbie", wrong, RED + (255,))
    ww = c.textlength("Nye Robbie", font=wrong)
    c.line((30, 232, 42 + ww, 232), fill=RED + (255,), width=7)
    c.rounded_rectangle((36, 292, 524, 366), 20, fill=(222, 247, 236, 255))
    text(c, (68, 329), "Nairobi", right, GREEN[:3] + (255,), anchor="lm")
    c.line((440, 330, 462, 352), fill=GREEN + (255,), width=10)
    c.line((462, 352, 500, 304), fill=GREEN + (255,), width=10)
    card = card.rotate(4, resample=Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", card.size, (0, 20, 45, 0))
    shadow.putalpha(card.split()[3].point(lambda a: int(a * 0.45)))
    pad = 40
    padded = Image.new("RGBA", (card.width + 2 * pad, card.height + 2 * pad), (0, 20, 45, 0))
    padded.paste(shadow, (pad, pad))
    img.alpha_composite(padded.filter(ImageFilter.GaussianBlur(16)), (690 - pad + 14, 130 - pad + 22))
    img.alpha_composite(card, (690, 130))
    return img.convert("RGB")


def banner() -> Image.Image:
    w, h = 2560, 1440
    img = gradient(w, h)
    glow(img, 2000, 300, 700, SKY, 42)
    glow(img, 300, 1250, 600, ORANGE, 38)
    lg = logo(150)
    img.alpha_composite(lg, ((w - lg.width) // 2, 470))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    text(d, (w // 2, 700), "KAULI GUIDES", font(BOLD, 120), WHITE + (255,), anchor="mm", shadow=True)
    text(d, (w // 2, 800), "Transcription, translation and dubbing, explained", font(REG, 54), SKY + (255,), anchor="mm")
    img.alpha_composite(layer)
    return img.convert("RGB")


def graphics() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    thumbnail().save(OUT / "thumbnail-ep01.jpg", quality=92, optimize=True)
    banner().save(OUT / "channel-banner.png")
    cover = Image.open(COVER).convert("RGB").resize((800, 800), Image.LANCZOS)
    cover.save(OUT / "channel-avatar.png")
    (OUT / "frames").mkdir(exist_ok=True)
    for i in range(len(CHAPTERS)):
        frame(i).save(OUT / "frames" / f"frame-{i:02d}.png")
    bar = Image.new("RGB", (W, 14), ORANGE)
    bar.save(OUT / "frames" / "progress.png")
    print("pictures written to", OUT)


# ------------------------------------------------------------------------------------------------------------------ captions
def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'"))


def script_tokens() -> list[tuple[str, str, int]]:
    """[(word as written, speaker, line number)] for every spoken word of the script."""
    out = []
    for n, line in enumerate(l for l in DIALOGUE.read_text(encoding="utf-8").splitlines() if l.strip()):
        speaker, _, body = line.partition(": ")
        out += [(w, speaker, n) for w in body.split()]
    return out


def asr_words() -> list[tuple[str, float, float]]:
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(AUDIO), language="en", word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((w.word, w.start, w.end))
    return words


def timed_script(heard: list[tuple[str, float, float]]) -> list[tuple[str, str, int, float, float]]:
    """Every word of the script with a start and end, taken from the words the model heard where they match and spread evenly where not."""
    tokens = script_tokens()
    a = [norm(t[0]) for t in tokens]
    b = [norm(h[0]) for h in heard]
    start = [None] * len(tokens)
    end = [None] * len(tokens)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            start[blk.a + k], end[blk.a + k] = heard[blk.b + k][1], heard[blk.b + k][2]
    i = 0
    while i < len(tokens):                                   # fill the gaps between matched words
        if start[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and start[j] is None:
            j += 1
        left = end[i - 1] if i > 0 else 0.0
        right = start[j] if j < len(tokens) else left + 0.35 * (j - i)
        step = max(right - left, 0.1 * (j - i)) / (j - i)
        for k in range(i, j):
            start[k], end[k] = left + step * (k - i), left + step * (k - i + 1)
        i = j
    return [(t[0], t[1], t[2], start[n], end[n]) for n, t in enumerate(tokens)]


def wrap(s: str, width: int = 42) -> str:
    words, lines, cur = s.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    if len(lines) > 2:                                       # keep to two lines: move the overflow into a longer second line
        lines = [lines[0], " ".join(lines[1:])]
    return "\n".join(lines)


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def cues(words: list[tuple[str, str, int, float, float]]) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    cur: list = []
    last_speaker = None

    def flush() -> None:
        nonlocal last_speaker
        if not cur:
            return
        body = " ".join(w[0] for w in cur)
        label = f"{cur[0][1]}: " if cur[0][1] != last_speaker else ""
        last_speaker = cur[0][1]
        out.append((cur[0][3], max(cur[-1][4], cur[0][3] + 0.8), wrap(label.title() + body if label else body)))
        cur.clear()

    for w in words:
        if cur and (w[2] != cur[-1][2]):                     # a new speaker line always starts a new cue
            flush()
        cur.append(w)
        text_len = len(" ".join(x[0] for x in cur))
        ends_sentence = bool(re.search(r"[.?!]$", w[0]))
        ends_clause = bool(re.search(r"[,;:]$", w[0]))
        if text_len >= 78 or (ends_sentence and text_len >= 30) or (ends_clause and text_len >= 52) or (w[4] - cur[0][3] > 6.0):
            flush()
    flush()
    # nothing overlaps, and long quiet stretches (the music) are marked
    fixed: list[tuple[float, float, str]] = []
    for k, (s, e, t) in enumerate(out):
        nxt = out[k + 1][0] if k + 1 < len(out) else e + 5
        e = min(e, nxt - 0.04)
        fixed.append((s, e, t))
    marked: list[tuple[float, float, str]] = []
    prev_end = 0.0
    for s, e, t in fixed:
        if s - prev_end > 3.5:
            marked.append((prev_end + 0.4, s - 0.3, "[music]"))
        marked.append((s, e, t))
        prev_end = e
    return marked


def captions() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    heard = asr_words()
    words = timed_script(heard)
    rows = cues(words)
    srt = "\n".join(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n" for i, (s, e, t) in enumerate(rows, 1))
    (OUT / "captions-ep01.en.srt").write_text(srt, encoding="utf-8")
    matched = sum(1 for _ in difflib.SequenceMatcher(None, [norm(w[0]) for w in script_tokens()], [norm(h[0]) for h in heard], autojunk=False)
                  .get_matching_blocks())
    print(f"{len(rows)} captions, {len(words)} script words, {len(heard)} words heard ({matched} matching stretches) -> {OUT / 'captions-ep01.en.srt'}")


# ------------------------------------------------------------------------------------------------------------------ video
def video() -> None:
    if not (OUT / "frames" / "frame-00.png").exists():
        graphics()
    total = duration(AUDIO)
    marks = chapter_starts()
    marks = [min(m, total - 1) for m in marks] + [total]
    lines = []
    for i in range(len(CHAPTERS)):
        lines.append(f"file '{(OUT / 'frames' / f'frame-{i:02d}.png').as_posix()}'")
        lines.append(f"duration {marks[i + 1] - marks[i]:.3f}")
    lines.append(f"file '{(OUT / 'frames' / f'frame-{len(CHAPTERS) - 1:02d}.png').as_posix()}'")
    listing = OUT / "frames" / "chapters.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    final = OUT / "kauli-guides-ep01-youtube.mp4"
    graph = (f"[0:v]fps={FPS},format=yuv420p[bg];"
             f"[1:a]showwaves=s=1040x170:mode=cline:rate={FPS}:colors=white|0xF47A0A:scale=sqrt,format=rgba[wv];"
             f"[bg][wv]overlay=92:742[v1];"
             f"[2:v]format=rgba[bar];[v1][bar]overlay=x='-w+w*t/{total:.3f}':y={H - 14}:shortest=1,format=yuv420p[v]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(AUDIO),
         "-loop", "1", "-framerate", str(FPS), "-i", str(OUT / "frames" / "progress.png"), "-filter_complex", graph,
         "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", "-metadata", "title=Why Your Transcript Is Wrong, and How to Fix It for Good",
         str(final)])
    (OUT / "chapters.json").write_text(json.dumps([{"start": round(m, 1), "title": t} for m, t in zip(marks, CHAPTERS)], indent=2), encoding="utf-8")
    print(f"Video: {final}  ({int(total // 60)}:{int(total % 60):02d}, {final.stat().st_size / 1_000_000:.0f} MB)")


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("graphics", "all"):
        graphics()
    if what in ("captions", "all"):
        captions()
    if what in ("video", "all"):
        video()


if __name__ == "__main__":
    main()
