"""Render the demo videos shown on /try: an illustrated speaker, the pre-rendered ElevenLabs voice, and the captions burned
into the picture. Nothing here calls ElevenLabs: it reads the MP3s that scripts/make_demo_audio.py already made.

For every scenario there is one video of the original (English audio, English captions) and, for every language, one of the
result: a dubbing scenario is the dubbed audio with captions in that language, the others are the English audio with that
language's subtitles burned in. The speakers are drawn, not filmed: no real person's face or voice is used or implied.

    python scripts/make_demo_videos.py            # all videos
    python scripts/make_demo_videos.py video es   # one scenario, one language ("en" for the original)

Needs ffmpeg and Pillow. Fonts come from the Windows font folder when run under WSL, or DejaVu elsewhere (Chinese needs a
CJK font: Microsoft YaHei on Windows, Noto Sans CJK on Linux).
"""
from __future__ import annotations

import array
import math
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp import demo_content  # noqa: E402

STATIC = ROOT / "webapp" / "static"
DEMO = STATIC / "demo"
W, H, FPS = 960, 540, 15
NAVY, BLUE, DEEP, SOFT, ORANGE = (8, 40, 72), (8, 120, 200), (8, 88, 168), (227, 241, 251), (244, 122, 10)

WIN = pathlib.Path("/mnt/c/Windows/Fonts")
DEJAVU = pathlib.Path("/usr/share/fonts/truetype/dejavu")
CJK_CANDIDATES = [WIN / "msyh.ttc", pathlib.Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]

# Which demos have a music bed (scripts/make_demo_music.py): the marketing video and the course, not the meeting.
MUSIC = demo_content.MUSIC_BEDS
MUSIC_VOLUME = 0.2

PEOPLE = {
    "meeting": {"skin": (226, 178, 146), "hair": (38, 28, 32), "shirt": (14, 116, 144), "long": True, "glasses": False, "prop": "meeting", "name": "Speaker 1"},
    "course": {"skin": (186, 136, 104), "hair": (28, 24, 24), "shirt": (8, 120, 200), "long": False, "glasses": True, "prop": "board", "name": "Instructor"},
    "video": {"skin": (244, 204, 174), "hair": (150, 70, 38), "shirt": (244, 122, 10), "long": True, "glasses": False, "prop": "backpack", "name": "Narrator"},
}


def font(lang: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    if lang == "zh":
        for p in CJK_CANDIDATES:
            if p.exists():
                return ImageFont.truetype(str(p), size)
    for p in ((WIN / ("arialbd.ttf" if bold else "arial.ttf")), (DEJAVU / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"))):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def duration(path: pathlib.Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def mouth_levels(path: pathlib.Path, seconds: float) -> list[float]:
    """How open the mouth is on each frame: the audio's loudness, smoothed, 0 to 1."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    per = 8000 // FPS
    levels = []
    for i in range(int(seconds * FPS) + 1):
        chunk = samples[i * per:(i + 1) * per]
        levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)) if chunk else 0.0)
    peak = max(levels) or 1.0
    levels = [min(1.0, (v / peak) * 1.5) for v in levels]
    out, prev = [], 0.0
    for v in levels:
        prev = prev * 0.45 + v * 0.55
        out.append(prev)
    return out


def sentences(text: str) -> list[str]:
    parts = re.findall(r"[^.!?。！？]+[.!?。！？]*", text)
    return [p.strip() for p in parts if p.strip()]


def wrap(text: str, fnt: ImageFont.FreeTypeFont, width: int, cjk: bool) -> list[str]:
    tokens = list(text) if cjk else text.split(" ")
    lines, cur = [], ""
    for tok in tokens:
        trial = cur + tok if cjk else (cur + " " + tok).strip()
        if fnt.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    return lines


def caption_pages(text: str, lang: str, total: float) -> list[tuple[float, float, list[str]]]:
    """(start, end, lines) for each caption on screen. Time is shared out by how much text each sentence has."""
    fnt = font(lang, 26, bold=True)
    cjk = lang == "zh"
    pages = []
    for s in sentences(text):
        lines = wrap(s, fnt, W - 180, cjk)
        for i in range(0, len(lines), 2):
            pages.append(lines[i:i + 2])
    weights = [sum(len(l) for l in p) for p in pages]
    lead, tail = 0.25, 0.2
    span = max(0.5, total - lead - tail)
    out, t = [], lead
    for p, w in zip(pages, weights):
        d = span * w / sum(weights)
        out.append((t, t + d, p))
        t += d
    return out


def make_background(spec: dict) -> Image.Image:
    img = Image.new("RGBA", (W, H), NAVY + (255,))
    px = img.load()
    for y in range(H):
        for x in range(W):
            t = min(1.0, (x / W) * 0.6 + (y / H) * 0.5)
            px[x, y] = tuple(int(NAVY[i] + (DEEP[i] - NAVY[i]) * t) for i in range(3)) + (255,)
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))             # drawn apart, so translucent fills blend
    d = ImageDraw.Draw(over)
    d.ellipse((560, -120, 1100, 380), fill=(124, 196, 240, 28))
    d.ellipse((-160, 260, 340, 700), fill=(8, 120, 200, 34))
    prop = spec["prop"]
    if prop == "board":                                 # a whiteboard with a ruled table drawn on it
        d.rounded_rectangle((590, 110, 900, 330), radius=14, fill=(245, 249, 253, 235))
        for i in range(5):
            d.line((614, 150 + i * 36, 876, 150 + i * 36), fill=(160, 190, 215, 255), width=3)
        d.line((745, 138, 745, 308), fill=(160, 190, 215, 255), width=3)
    elif prop == "backpack":                            # the product on a shelf
        d.rounded_rectangle((640, 150, 820, 330), radius=46, fill=ORANGE + (255,))
        d.rounded_rectangle((668, 250, 792, 322), radius=18, fill=(214, 98, 6, 255))
        d.arc((676, 112, 784, 200), 190, 350, fill=(255, 255, 255, 230), width=9)
    elif prop == "meeting":                             # a few other call participants as tiles
        for i, (x, y) in enumerate(((620, 120), (770, 120), (620, 250), (770, 250))):
            d.rounded_rectangle((x, y, x + 130, y + 110), radius=12, fill=(255, 255, 255, 30), outline=(255, 255, 255, 60))
            d.ellipse((x + 45, y + 22, x + 85, y + 62), fill=(255, 255, 255, 70))
            d.ellipse((x + 28, y + 66, x + 102, y + 140), fill=(255, 255, 255, 50))
    img.alpha_composite(over)
    logo = Image.open(STATIC / "kauli-logo-nav-light.png").convert("RGBA")
    logo = logo.resize((int(logo.width * 0.62), int(logo.height * 0.62)), Image.LANCZOS)
    img.alpha_composite(logo, (28, 22))
    return img


def draw_person(d: ImageDraw.ImageDraw, spec: dict, level: float, t: float) -> None:
    cx, cy = 330, 235 + int(math.sin(t * 2.2) * 3)
    skin, hair, shirt = spec["skin"], spec["hair"], spec["shirt"]
    if spec["long"]:
        d.ellipse((cx - 88, cy - 98, cx + 88, cy + 150), fill=hair)
    d.ellipse((cx - 150, cy + 105, cx + 150, cy + 420), fill=shirt)                # shoulders
    d.polygon([(cx - 30, cy + 108), (cx + 30, cy + 108), (cx, cy + 160)], fill=skin)
    d.rectangle((cx - 24, cy + 55, cx + 24, cy + 118), fill=skin)                  # neck
    d.ellipse((cx - 68, cy - 82, cx + 68, cy + 80), fill=skin)                     # head
    d.pieslice((cx - 74, cy - 100, cx + 74, cy + 36), 180, 360, fill=hair)         # hair
    blink = (t % 3.4) < 0.13
    for dx in (-27, 27):
        ey = cy - 6
        if blink:
            d.line((cx + dx - 9, ey, cx + dx + 9, ey), fill=(40, 30, 30), width=3)
        else:
            d.ellipse((cx + dx - 8, ey - 10, cx + dx + 8, ey + 10), fill=(250, 250, 250))
            d.ellipse((cx + dx - 4, ey - 6, cx + dx + 5, ey + 6), fill=(40, 28, 24))
        d.line((cx + dx - 12, ey - 22, cx + dx + 12, ey - 24), fill=hair, width=4)
    if spec["glasses"]:
        for dx in (-27, 27):
            d.ellipse((cx + dx - 19, cy - 26, cx + dx + 19, cy + 14), outline=(30, 40, 60), width=3)
        d.line((cx - 8, cy - 8, cx + 8, cy - 8), fill=(30, 40, 60), width=3)
    d.line((cx, cy + 4, cx - 5, cy + 26), fill=tuple(max(0, c - 40) for c in skin), width=3)
    open_h = 3 + int(level * 24)
    my = cy + 44
    if level < 0.08:
        d.arc((cx - 20, my - 12, cx + 20, my + 8), 20, 160, fill=(150, 60, 60), width=4)
    else:
        d.ellipse((cx - 17, my - open_h // 3, cx + 17, my + open_h), fill=(120, 36, 44))
        d.ellipse((cx - 11, my + open_h - 8, cx + 11, my + open_h), fill=(214, 110, 120))
    # a name tag under the speaker
    return


def render(key: str, lang: str, audio: pathlib.Path, text: str, caption_lang: str, badge: str, out: pathlib.Path) -> None:
    spec = PEOPLE[key]
    secs = duration(audio) + 0.4
    levels = mouth_levels(audio, secs)
    pages = caption_pages(text, caption_lang, secs)
    bg = make_background(spec)
    f_badge, f_tag, f_cap = font("en", 20, True), font("en", 22, True), font(caption_lang, 26, True)
    cache: dict[tuple, Image.Image] = {}

    def caption_layer(lines: list[str]) -> Image.Image:
        k = tuple(lines)
        if k not in cache:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            lh = 38
            box_h = lh * len(lines) + 26
            top = H - 40 - box_h
            d.rounded_rectangle((70, top, W - 70, top + box_h), radius=16, fill=(0, 0, 0, 175))
            for i, line in enumerate(lines):
                w = f_cap.getlength(line)
                d.text(((W - w) / 2, top + 13 + i * lh), line, font=f_cap, fill=(255, 255, 255, 255))
            cache[k] = layer
        return cache[k]

    tag_text = spec["name"]
    n = int(secs * FPS)
    work = pathlib.Path(tempfile.gettempdir()) / out.name          # encode off the (slow, picky) mounted drive, then move
    bed = MUSIC.get(key)
    bed_path = DEMO / f"bed-{bed}.mp3" if bed else None
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio)]
    if bed_path and bed_path.exists():                       # the voice on top, a quiet music bed underneath, faded out at the end
        fade_from = max(0.0, n / FPS - 1.2)
        cmd += ["-stream_loop", "-1", "-i", str(bed_path), "-filter_complex",
                f"[2:a]volume={MUSIC_VOLUME}[m];[1:a]apad[v];[v][m]amix=inputs=2:duration=longest:normalize=0,"
                f"afade=t=out:st={fade_from:.2f}:d=1.2[a]", "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-af", "apad"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "80k",
            "-t", f"{n / FPS:.3f}", "-movflags", "+faststart", str(work)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        t = i / FPS
        frame = bg.copy()
        d = ImageDraw.Draw(frame)
        draw_person(d, spec, levels[min(i, len(levels) - 1)], t)
        d.rounded_rectangle((220, 372 - 40, 440, 372 - 6), radius=17, fill=(255, 255, 255, 235))      # name tag
        d.text((330 - f_tag.getlength(tag_text) / 2, 372 - 38), tag_text, font=f_tag, fill=NAVY)
        bw = f_badge.getlength(badge) + 30
        d.rounded_rectangle((W - 28 - bw, 28, W - 28, 66), radius=19, fill=BLUE + (255,))
        d.text((W - 28 - bw + 15, 36), badge, font=f_badge, fill=(255, 255, 255))
        for start, end, lines in pages:
            if start <= t < end:
                frame.alpha_composite(caption_layer(lines))
                break
        ff.stdin.write(frame.convert("RGB").tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {out.name}")
    shutil.move(str(work), str(out))


def jobs(only_key: str | None, only_lang: str | None):
    for key, s in demo_content.SCENARIOS.items():
        if only_key and key != only_key:
            continue
        if not only_lang or only_lang == "en":
            yield key, "en", DEMO / demo_content.audio_name(key, None), s["en"], "en", "Original, English"
        for lang, text in s["tr"].items():
            if only_lang and lang != only_lang:
                continue
            name = demo_content.LANGUAGES[lang]["name"]
            if s["service"] == "dub":
                yield key, lang, DEMO / demo_content.audio_name(key, lang), text, lang, f"Dubbed in {name}"
            else:
                yield key, lang, DEMO / demo_content.audio_name(key, None), text, lang, f"{name} subtitles"


def main() -> None:
    only_key = sys.argv[1] if len(sys.argv) > 1 else None
    only_lang = sys.argv[2] if len(sys.argv) > 2 else None
    for key, lang, audio, text, cap_lang, badge in jobs(only_key, only_lang):
        out = DEMO / demo_content.video_name(key, lang)
        render(key, lang, audio, text, cap_lang, badge, out)
        print(f"{out.name}  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
