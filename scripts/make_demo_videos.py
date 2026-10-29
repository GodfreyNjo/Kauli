"""Render the demo videos shown on /try: people on screen who speak with the pre-rendered ElevenLabs voices, with the captions
burned into the picture. Nothing here calls ElevenLabs: it reads the MP3s and timing.json that scripts/make_demo_audio.py made.

Each scenario has a cast (webapp/demo_content.py), one person on screen for every voice and the same gender as the voice: a
man's voice is a man on screen, a woman's a woman. Whoever is speaking has their mouth move with the audio and their name tag lit;
in a two-person scene the other listens. In the marketing video the narrator holds up the product while he talks about it.

For every scenario there is a video of the original (English audio, English captions) and, for every language, one of the result:
a dubbing scenario is the dubbed audio with captions in that language, the others are the English audio with that language's
subtitles burned in. The people are drawn, not filmed: no real person's face or voice is used or implied.

    python scripts/make_demo_videos.py            # all videos
    python scripts/make_demo_videos.py video es   # one scenario, one language ("en" for the original)

Needs ffmpeg and Pillow. Fonts come from the Windows font folder when run under WSL, or DejaVu elsewhere (Chinese needs a
CJK font: Microsoft YaHei on Windows, Noto Sans CJK on Linux).
"""
from __future__ import annotations

import array
import json
import math
import pathlib
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
LABEL_COLOUR = (124, 196, 240, 255)

WIN = pathlib.Path("/mnt/c/Windows/Fonts")
DEJAVU = pathlib.Path("/usr/share/fonts/truetype/dejavu")
CJK_CANDIDATES = [WIN / "msyh.ttc", pathlib.Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]

# Which demos have a music bed under the voice (scripts/make_demo_music.py).
MUSIC = demo_content.MUSIC_BEDS
MUSIC_VOLUME = 0.25

# How each person looks. The gender matches the voice in demo_content.SCENARIOS[...]["cast"] (a test checks that).
LOOKS = {
    ("meeting", 0): dict(skin=(226, 178, 146), hair=(38, 28, 32), style="long", shirt=(14, 116, 144)),
    ("meeting", 1): dict(skin=(205, 156, 124), hair=(58, 40, 30), style="short", beard=True, shirt=(40, 62, 120)),
    ("call", 0): dict(skin=(196, 146, 114), hair=(34, 26, 28), style="bob", headphones=True, shirt=(20, 128, 120)),
    ("call", 1): dict(skin=(152, 106, 80), hair=(24, 20, 20), style="short", shirt=(168, 72, 52)),
    ("interview", 0): dict(skin=(232, 190, 160), hair=(120, 108, 100), style="short", glasses=True, shirt=(92, 104, 120)),
    ("interview", 1): dict(skin=(168, 118, 90), hair=(30, 24, 24), style="bob", shirt=(150, 44, 76)),
    ("podcast", 0): dict(skin=(240, 200, 170), hair=(70, 46, 32), style="short", headphones=True, shirt=(48, 132, 96)),
    ("podcast", 1): dict(skin=(230, 182, 152), hair=(214, 170, 92), style="long", headphones=True, shirt=(118, 80, 170)),
    ("course", 0): dict(skin=(186, 136, 104), hair=(28, 24, 24), style="bun", glasses=True, shirt=(8, 120, 200)),
    ("news", 0): dict(skin=(236, 196, 166), hair=(62, 56, 62), style="short", tie=True, shirt=(28, 40, 66)),
    ("field", 0): dict(skin=(150, 102, 76), hair=(26, 20, 20), style="bun", vest=True, shirt=(214, 168, 70)),
    ("video", 0): dict(skin=(244, 204, 174), hair=(46, 36, 30), style="short", shirt=(34, 54, 108)),
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
    """How open a mouth is on each frame: the audio's loudness, smoothed, 0 to 1."""
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


def caption_pages(key: str, text: str, lang: str, marks: list, total: float) -> list[tuple[float, float, list[str], str]]:
    """(start, end, lines, speaker label) for each caption on screen. Each turn's captions fill that turn's time in the audio, and the
    caption that opens a turn names the speaker when the scene has more than one."""
    cast = demo_content.SCENARIOS[key]["cast"]
    fnt = font(lang, 26, bold=True)
    cjk = lang == "zh"
    turns = demo_content.turns(key, text)
    pages = []
    for (who, body), mark in zip(turns, marks):
        label = demo_content.label_in(cast[who]["name"], lang) if len(cast) > 1 else ""
        t0, t1 = float(mark[1]) + 0.05, float(mark[2]) + 0.1
        prefix = f"{label}: " if label else ""
        sentences = [s.strip() for s in __import__("re").findall(r"[^.!?。！？]+[.!?。！？]*", body) if s.strip()]
        groups = []
        for n, sent in enumerate(sentences):
            lines = wrap((prefix if n == 0 else "") + sent, fnt, W - 180, cjk)
            for i in range(0, len(lines), 2):
                groups.append((lines[i:i + 2], label if (n == 0 and i == 0) else ""))
        weights = [sum(len(l) for l in g[0]) for g in groups]
        span, t = max(0.5, t1 - t0), t0
        for (lines, lab), w in zip(groups, weights):
            d = span * w / sum(weights)
            pages.append((t, t + d, lines, lab))
            t += d
    return pages


# ----------------------------------------------------------------------------- scenery
def backdrop(key: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), NAVY + (255,))
    px = img.load()
    for y in range(H):
        for x in range(W):
            t = min(1.0, (x / W) * 0.6 + (y / H) * 0.5)
            px[x, y] = tuple(int(NAVY[i] + (DEEP[i] - NAVY[i]) * t) for i in range(3)) + (255,)
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))                # drawn apart, so translucent fills blend
    d = ImageDraw.Draw(over)
    d.ellipse((560, -120, 1100, 380), fill=(124, 196, 240, 28))
    d.ellipse((-160, 260, 340, 700), fill=(8, 120, 200, 34))
    if key == "course":                                           # a whiteboard with a ruled table
        d.rounded_rectangle((590, 110, 900, 330), radius=14, fill=(245, 249, 253, 235))
        for i in range(5):
            d.line((614, 150 + i * 36, 876, 150 + i * 36), fill=(160, 190, 215, 255), width=3)
        d.line((745, 138, 745, 308), fill=(160, 190, 215, 255), width=3)
    elif key == "meeting":                                        # other people on the call, as tiles
        for x, y in ((395, 70), (525, 70), (655, 70), (785, 70)):
            d.rounded_rectangle((x, y, x + 110, y + 90), radius=10, fill=(255, 255, 255, 26), outline=(255, 255, 255, 60))
            d.ellipse((x + 38, y + 14, x + 72, y + 48), fill=(255, 255, 255, 70))
            d.ellipse((x + 22, y + 52, x + 88, y + 118), fill=(255, 255, 255, 50))
    elif key == "interview":                                      # a warm room: a lamp and a plant
        d.ellipse((425, 60, 535, 170), fill=(255, 214, 140, 70))
        d.rectangle((476, 150, 484, 330), fill=(255, 255, 255, 60))
        d.rounded_rectangle((430, 340, 530, 366), radius=8, fill=(255, 255, 255, 40))
        for ang in range(-50, 51, 25):
            d.ellipse((860 + ang, 230 - abs(ang) // 2, 900 + ang, 330), fill=(60, 160, 110, 150))
        d.rounded_rectangle((850, 330, 910, 400), radius=8, fill=(190, 120, 80, 190))
    elif key == "podcast":                                        # acoustic panels and an ON AIR light
        for i in range(5):
            d.rounded_rectangle((60 + i * 180, 90, 60 + i * 180 + 150, 300), radius=10, fill=(255, 255, 255, 20), outline=(255, 255, 255, 38))
        d.rounded_rectangle((420, 28, 540, 66), radius=19, fill=(214, 40, 40, 255))
        f_air = font("en", 18, True)
        d.text((480 - f_air.getlength("ON AIR") / 2, 36), "ON AIR", font=f_air, fill=(255, 255, 255, 255))
    elif key == "news":                                           # a city skyline, a bridge, and the bulletin desk
        for i, (x, h) in enumerate(((500, 210), (560, 280), (620, 190), (690, 320), (760, 240), (830, 300), (890, 200))):
            d.rectangle((x, 400 - h, x + 52, 400), fill=(255, 255, 255, 30 + 8 * (i % 3)))
        d.arc((480, 330, 940, 470), 200, 340, fill=(244, 122, 10, 220), width=7)
        d.rectangle((0, 392, W, 410), fill=(244, 122, 10, 230))
    elif key == "field":                                          # hills, huts, a tree and the sun
        d.ellipse((780, 60, 860, 140), fill=(255, 222, 130, 230))
        d.ellipse((380, 300, 1000, 640), fill=(60, 140, 90, 120))
        d.ellipse((520, 340, 1100, 700), fill=(40, 110, 80, 140))
        for x in (560, 700, 830):
            d.rectangle((x, 300, x + 70, 360), fill=(200, 150, 100, 220))
            d.polygon([(x - 10, 300), (x + 35, 255), (x + 80, 300)], fill=(150, 100, 70, 240))
        d.rectangle((905, 250, 915, 345), fill=(110, 80, 60, 255))
        d.ellipse((880, 200, 940, 270), fill=(46, 140, 80, 240))
    elif key == "video":                                          # a soft spotlight on a studio floor
        d.ellipse((120, 60, 700, 520), fill=(255, 255, 255, 22))
        d.ellipse((90, 440, 740, 540), fill=(0, 0, 0, 50))
    img.alpha_composite(over)
    logo = Image.open(STATIC / "kauli-logo-nav-light.png").convert("RGBA")
    logo = logo.resize((int(logo.width * 0.62), int(logo.height * 0.62)), Image.LANCZOS)
    img.alpha_composite(logo, (28, 22))
    return img


def bag_image() -> Image.Image:
    """The Aurora backpack, drawn once, with its top handle near the top edge so a hand can hold it."""
    im = Image.new("RGBA", (190, 250), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.arc((55, 6, 135, 90), 190, 350, fill=(250, 250, 250, 255), width=9)                       # handle
    d.rounded_rectangle((20, 50, 170, 240), radius=46, fill=ORANGE + (255,))                  # body
    d.rounded_rectangle((20, 50, 170, 240), radius=46, outline=(190, 84, 4, 255), width=4)
    d.rounded_rectangle((42, 150, 148, 226), radius=18, fill=(214, 98, 6, 255))                # front pocket
    d.line((42, 168, 148, 168), fill=(250, 250, 250, 220), width=4)                          # zip
    d.rounded_rectangle((8, 110, 28, 210), radius=8, fill=(34, 54, 108, 255))                  # side strap
    d.rounded_rectangle((162, 110, 182, 210), radius=8, fill=(34, 54, 108, 255))
    d.ellipse((72, 88, 118, 134), outline=(255, 255, 255, 230), width=5)                       # a badge
    d.line((86, 112, 106, 100), fill=(255, 255, 255, 230), width=4)
    d.rounded_rectangle((34, 66, 70, 80), radius=6, fill=(255, 190, 120, 160))                 # a highlight
    return im


# ----------------------------------------------------------------------------- people
def draw_person(d: ImageDraw.ImageDraw, look: dict, gender: str, cx: int, cy: int, k: float, level: float, t: float) -> None:
    S = lambda v: int(round(v * k))                                                    # noqa: E731
    skin, hair, shirt = look["skin"], look["hair"], look["shirt"]
    style = look.get("style", "short")
    wide = 175 if gender == "m" else 150
    if style == "long":
        d.ellipse((cx - S(88), cy - S(98), cx + S(88), cy + S(150)), fill=hair)
    elif style == "bob":
        d.ellipse((cx - S(86), cy - S(96), cx + S(86), cy + S(52)), fill=hair)
    elif style == "bun":
        d.ellipse((cx - S(28), cy - S(122), cx + S(28), cy - S(70)), fill=hair)
    d.ellipse((cx - S(wide), cy + S(105), cx + S(wide), cy + S(420)), fill=shirt)               # shoulders
    if look.get("vest"):
        d.polygon([(cx - S(wide) + S(14), cy + S(150)), (cx - S(24), cy + S(112)), (cx - S(24), cy + S(420)), (cx - S(wide) + S(40), cy + S(420))], fill=(70, 100, 70))
        d.polygon([(cx + S(wide) - S(14), cy + S(150)), (cx + S(24), cy + S(112)), (cx + S(24), cy + S(420)), (cx + S(wide) - S(40), cy + S(420))], fill=(70, 100, 70))
    d.polygon([(cx - S(30), cy + S(108)), (cx + S(30), cy + S(108)), (cx, cy + S(160))], fill=skin if not look.get("tie") else (245, 245, 245))
    if look.get("tie"):
        d.polygon([(cx - S(9), cy + S(112)), (cx + S(9), cy + S(112)), (cx + S(14), cy + S(200)), (cx, cy + S(222)), (cx - S(14), cy + S(200))], fill=(196, 40, 52))
    d.rectangle((cx - S(24), cy + S(55), cx + S(24), cy + S(118)), fill=skin)                  # neck
    head_w = 70 if gender == "m" else 64
    d.ellipse((cx - S(head_w), cy - S(82), cx + S(head_w), cy + S(80)), fill=skin)             # head
    if style in ("long", "bob"):
        d.pieslice((cx - S(74), cy - S(100), cx + S(74), cy + S(36)), 180, 360, fill=hair)
        d.rectangle((cx - S(74), cy - S(36), cx - S(60), cy + S(20)), fill=hair)
        d.rectangle((cx + S(60), cy - S(36), cx + S(74), cy + S(20)), fill=hair)
    elif style == "bun":
        d.pieslice((cx - S(72), cy - S(98), cx + S(72), cy + S(30)), 180, 360, fill=hair)
    else:
        d.pieslice((cx - S(76), cy - S(104), cx + S(76), cy + S(16)), 180, 360, fill=hair)
        d.rectangle((cx - S(76), cy - S(50), cx - S(66), cy - S(8)), fill=hair)
        d.rectangle((cx + S(66), cy - S(50), cx + S(76), cy - S(8)), fill=hair)
    if look.get("beard"):
        stubble = tuple((a + b) // 2 for a, b in zip(hair, skin))
        d.chord((cx - S(66), cy - S(6), cx + S(66), cy + S(84)), 0, 180, fill=stubble)
    blink = (t % 3.4) < 0.13
    for dx in (-27, 27):
        ex, ey = cx + S(dx), cy - S(6)
        if blink:
            d.line((ex - S(9), ey, ex + S(9), ey), fill=(40, 30, 30), width=max(2, S(3)))
        else:
            d.ellipse((ex - S(8), ey - S(10), ex + S(8), ey + S(10)), fill=(250, 250, 250))
            d.ellipse((ex - S(4), ey - S(6), ex + S(5), ey + S(6)), fill=(40, 28, 24))
        brow = max(2, S(5 if gender == "m" else 3))
        d.line((ex - S(12), ey - S(22), ex + S(12), ey - S(24)), fill=hair, width=brow)
        if gender == "f" and not blink:
            d.line((ex - S(10), ey - S(11), ex - S(6), ey - S(15)), fill=(40, 30, 30), width=2)
            d.line((ex + S(10), ey - S(11), ex + S(6), ey - S(15)), fill=(40, 30, 30), width=2)
    if look.get("glasses"):
        for dx in (-27, 27):
            d.ellipse((cx + S(dx) - S(19), cy - S(26), cx + S(dx) + S(19), cy + S(14)), outline=(30, 40, 60), width=max(2, S(3)))
        d.line((cx - S(8), cy - S(8), cx + S(8), cy - S(8)), fill=(30, 40, 60), width=max(2, S(3)))
    if look.get("headphones"):
        d.arc((cx - S(84), cy - S(110), cx + S(84), cy + S(40)), 200, 340, fill=(36, 38, 48), width=max(3, S(9)))
        for sx in (-1, 1):
            d.rounded_rectangle((cx + sx * S(78) - S(13), cy - S(22), cx + sx * S(78) + S(13), cy + S(34)), radius=S(10), fill=(36, 38, 48))
    d.line((cx, cy + S(4), cx - S(5), cy + S(26)), fill=tuple(max(0, c - 40) for c in skin), width=max(2, S(3)))
    my = cy + S(46)
    lip = (190, 84, 96) if gender == "f" else (150, 60, 60)
    if level < 0.08:
        d.arc((cx - S(20), my - S(12), cx + S(20), my + S(8)), 20, 160, fill=lip, width=max(2, S(4)))
    else:
        open_h = S(3 + level * 24)
        d.ellipse((cx - S(17), my - open_h // 3, cx + S(17), my + open_h), fill=(120, 36, 44))
        d.ellipse((cx - S(11), my + open_h - S(8), cx + S(11), my + open_h), fill=(214, 110, 120))
    if look.get("earrings"):
        pass


def name_tag(d: ImageDraw.ImageDraw, label: str, cx: int, y: int, active: bool, fnt: ImageFont.FreeTypeFont) -> None:
    w = fnt.getlength(label) + 34
    d.rounded_rectangle((cx - w / 2, y, cx + w / 2, y + 34), radius=17, fill=(BLUE + (255,)) if active else (255, 255, 255, 235))
    d.text((cx - fnt.getlength(label) / 2, y + 5), label, font=fnt, fill=(255, 255, 255) if active else NAVY)


def show_bag(frame: Image.Image, d: ImageDraw.ImageDraw, look: dict, cx: int, cy: int, bag: Image.Image, t: float) -> None:
    """The narrator's arm lifts the product towards the viewer and turns it, over and over while he talks."""
    lift = abs(math.sin(t * 1.5))
    hx, hy = cx + 215 + int(10 * math.sin(t * 2.1)), cy + 118 - int(150 * lift)                   # hand
    sx, sy = cx + 120, cy + 150                                                                 # shoulder
    d.line((sx, sy, hx, hy), fill=look["shirt"], width=46)
    d.ellipse((sx - 23, sy - 23, sx + 23, sy + 23), fill=look["shirt"])
    d.ellipse((hx - 23, hy - 23, hx + 23, hy + 23), fill=look["shirt"])
    scale = 1.05 + 0.1 * lift
    img = bag.resize((int(bag.width * scale), int(bag.height * scale)), Image.BICUBIC).rotate(9 * math.sin(t * 1.5), resample=Image.BICUBIC, expand=True)
    frame.alpha_composite(img, (hx - img.width // 2, hy - int(34 * scale) - (img.height - int(bag.height * scale)) // 2))
    d2 = ImageDraw.Draw(frame)
    d2.ellipse((hx - 20, hy - 14, hx + 20, hy + 22), fill=look["skin"])                          # the hand holding the handle
    if lift > 0.8:                                                                              # a sparkle when it is held up high
        for dx, dy in ((-96, -70), (88, -92), (110, 20)):
            x, y = hx + dx, hy + dy - 60
            d2.line((x - 11, y, x + 11, y), fill=(255, 255, 255, 235), width=3)
            d2.line((x, y - 11, x, y + 11), fill=(255, 255, 255, 235), width=3)


def layout(key: str, n: int) -> list[tuple[int, int, float]]:
    """(centre x, centre y, scale) for each person."""
    if n == 2:
        return [(250, 240, 0.9), (690, 240, 0.9)]
    if key == "video":
        return [(330, 235, 1.0)]
    return [(330, 235, 1.0)]


def badge_text(key: str, lang: str) -> str:
    s = demo_content.SCENARIOS[key]
    if lang == "en":
        return "Original, English"
    name = demo_content.LANGUAGES[lang]["name"]
    return f"Dubbed in {name}" if s["service"] == "dub" else f"{name} subtitles"


def render(key: str, lang: str, audio: pathlib.Path, text: str, caption_lang: str, out: pathlib.Path, timing: dict) -> None:
    scenario = demo_content.SCENARIOS[key]
    cast = scenario["cast"]
    marks = timing.get(audio.name) or [[0, 0.0, duration(audio)]]
    secs = duration(audio) + 0.4
    levels = mouth_levels(audio, secs)
    pages = caption_pages(key, text, caption_lang, marks, secs)
    bg = backdrop(key)
    bag = bag_image() if key == "video" else None
    f_badge, f_tag, f_cap = font("en", 20, True), font(caption_lang, 20, True), font(caption_lang, 26, True)
    spots = layout(key, len(cast))
    cache: dict[tuple, Image.Image] = {}
    badge = badge_text(key, lang)

    def caption_layer(lines: list[str], label: str) -> Image.Image:
        k = (tuple(lines), label)
        if k not in cache:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            lh = 38
            box_h = lh * len(lines) + 26
            top = H - 40 - box_h
            d.rounded_rectangle((70, top, W - 70, top + box_h), radius=16, fill=(0, 0, 0, 175))
            for i, line in enumerate(lines):
                w = f_cap.getlength(line)
                x = (W - w) / 2
                if i == 0 and label and line.startswith(label + ": "):
                    head = label + ": "
                    d.text((x, top + 13), head, font=f_cap, fill=LABEL_COLOUR)
                    d.text((x + f_cap.getlength(head), top + 13), line[len(head):], font=f_cap, fill=(255, 255, 255, 255))
                else:
                    d.text((x, top + 13 + i * lh), line, font=f_cap, fill=(255, 255, 255, 255))
            cache[k] = layer
        return cache[k]

    n = int(secs * FPS)
    work = pathlib.Path(tempfile.gettempdir()) / out.name          # encode off the (slow, picky) mounted drive, then move
    bed = MUSIC.get(key)
    bed_path = DEMO / f"bed-{bed}.mp3" if bed else None
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio)]
    if bed_path and bed_path.exists():                       # the voices on top, a quiet music bed underneath, faded out at the end
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
        active = next((int(m[0]) for m in marks if float(m[1]) - 0.02 <= t < float(m[2]) + 0.12), None)
        frame = bg.copy()
        d = ImageDraw.Draw(frame)
        for idx, (cx, cy, k) in enumerate(spots):
            person = cast[idx]
            look = LOOKS[(key, idx)]
            speaking = active == idx
            level = levels[min(i, len(levels) - 1)] if speaking else 0.0
            draw_person(d, look, person["gender"], cx, cy + int(math.sin(t * 2.2 + idx) * 3), k, level, t + idx * 0.9)
            if bag is not None:
                show_bag(frame, d, look, cx, cy, bag, t)
                d = ImageDraw.Draw(frame)
            if len(cast) > 1 or key != "course":
                name_tag(d, demo_content.label_in(person["name"], caption_lang), cx, int(cy + 118 * k), speaking, f_tag)
        bw = f_badge.getlength(badge) + 30
        d.rounded_rectangle((W - 28 - bw, 28, W - 28, 66), radius=19, fill=BLUE + (255,))
        d.text((W - 28 - bw + 15, 36), badge, font=f_badge, fill=(255, 255, 255))
        for start, end, lines, label in pages:
            if start <= t < end:
                frame.alpha_composite(caption_layer(lines, label))
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
            yield key, "en", DEMO / demo_content.audio_name(key, None), s["en"], "en"
        for lang, text in s["tr"].items():
            if only_lang and lang != only_lang:
                continue
            if s["service"] == "dub":
                yield key, lang, DEMO / demo_content.audio_name(key, lang), text, lang
            else:
                yield key, lang, DEMO / demo_content.audio_name(key, None), text, lang


def main() -> None:
    only_key = sys.argv[1] if len(sys.argv) > 1 else None
    only_lang = sys.argv[2] if len(sys.argv) > 2 else None
    timing = json.loads((DEMO / "timing.json").read_text(encoding="utf-8"))
    for key, lang, audio, text, cap_lang in jobs(only_key, only_lang):
        out = DEMO / demo_content.video_name(key, lang)
        render(key, lang, audio, text, cap_lang, out, timing)
        print(f"{out.name}  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
