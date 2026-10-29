"""Builds the Kauli promo video ("Type it? Never again.", about 75 seconds, 1920x1080) from the script in
marketing/kauli-promo-script.md.

    ELEVENLABS_API_KEY=... python scripts/make_promo_video.py audio   [--force] [--dry-run]   voiceover + sound effects
    python scripts/make_promo_video.py video                                                  music, overlays, captions, final mix

The voiceover is ElevenLabs v3 (the [tags] in the text are voice directions) and is rendered once per scene and cached in
marketing/promo/audio, so a re-run costs nothing; --force re-takes. Sound effects come from ElevenLabs Sound Effects. The music is
composed here (scripts/make_promo_music.py). The pictures are the human photos in marketing/promo/images (01-hook.png ... 07-cta.png,
prompts in marketing/promo/image-prompts.md). A scene with no photo yet gets a clearly marked placeholder card, and the file is then
named kauli-promo-animatic.mp4 instead of kauli-promo.mp4. The real call-center demo plays as proof inside two scenes.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_promo_music import compose  # noqa: E402

PROMO = ROOT / "marketing" / "promo"
IMAGES, AUDIO, BUILD = PROMO / "images", PROMO / "audio", PROMO / "build"
DEMO = ROOT / "webapp" / "static" / "demo"
LOGO = ROOT / "webapp" / "static" / "kauli-logo-nav-light.png"
W, H, FPS = 1920, 1080, 30
USE_SYNTH_MUSIC = False        # --synth-music forces the code-made bed even when an ElevenLabs one exists
BURN_CAPTIONS = False          # captions are added afterwards (with Kauli); pass --captions to burn them in
NAVY, BLUE, ORANGE = (8, 40, 72), (8, 120, 200), (244, 122, 10)
VOICES = {
    "creator": "cgSgspJ2msm6clMCkdW9",     # Jessica: bright, expressive, American; she is the person in the first two scenes
    "narrator": "iP95p4xoKVk53GoZ742B",    # Chris: charming, down-to-earth; he carries the product story
}
MODEL = "eleven_v3"
WIN = pathlib.Path("/mnt/c/Windows/Fonts")
DEJAVU = pathlib.Path("/usr/share/fonts/truetype/dejavu")

# Sound effects: name -> (ElevenLabs prompt, seconds)
SFX = {
    "clock": ("Wall clock ticking in a quiet room", 3.0),
    "typing": ("Fast keyboard typing that slows down and stops", 3.0),
    "record_stop": ("Soft vinyl record stopping then silence", 1.0),
    "click": ("Bright modern user interface click, clean and short", 0.6),
    "whoosh": ("Short airy upward whoosh", 1.2),
    "ding": ("Gentle warm notification ding", 1.2),
    "flip": ("Quick soft page flip whoosh", 0.6),
    "pop": ("Small cheerful pop", 0.5),
    "chime": ("Soft positive confirmation chime", 1.2),
    "sting": ("Cheerful bright success sting, short and uplifting", 1.8),
}

# One entry per scene. min = the least time it stays on screen. vo_at = when the voice starts inside the scene. sfx = (name, seconds
# into the scene [or "end" for the end of the voice, then an offset], volume).
SCENES = [
    dict(id="01-hook", voice="creator", min=6.5, vo_at=0.7,
         vo="[sighs] Two hours of recording. [short pause] And I have to type every single word.",
         cap="Two hours of recording. And I have to type every single word.",
         text="2 hours of recording.", sfx=[("clock", 0.0, 0.5), ("typing", 3.0, 0.45)],
         desc="A tired creator at a desk late at night, rubbing her eyes, a long audio waveform on the laptop"),
    dict(id="02-turn", voice="creator", min=7.0, vo_at=1.2,
         vo="[short pause] [curious] Wait... what if I just... uploaded it?",
         cap="Wait... what if I just... uploaded it?",
         text="What if you just... uploaded it?", sfx=[("record_stop", 0.0, 0.7), ("click", 3.3, 0.6), ("whoosh", 3.6, 0.6)],
         desc="The same creator looks up, a smile spreading, a lightbulb moment"),
    dict(id="03-meet", voice="narrator", min=15.0, vo_at=0.6,
         vo="[excited] Meet Kauli. [short pause] Drop in any audio or video, and get an accurate transcript in minutes. [warmly] Every speaker labeled. Every word timestamped.",
         cap="Meet Kauli. Drop in any audio or video, and get an accurate transcript in minutes. Every speaker labeled. Every word timestamped.",
         text="Transcripts in minutes.", inset=[("vid-call-en.mp4", 6.0), ("screens/transcript.png", 8.0)], sfx=[("ding", 6.6, 0.55), ("typing", 7.0, 0.2)],
         desc="The creator at a laptop with a bright screen, pointing at it, left side of frame, calm space on the right"),
    dict(id="04-translate", voice="narrator", min=14.0, vo_at=0.5,
         vo="Then translate it into more than seventy languages. [short pause] Subtitles. [short pause] Even a dubbed voice, in selected languages. [excited] Same video, brand new audience.",
         cap="Then translate it into more than seventy languages. Subtitles. Even a dubbed voice, in selected languages. Same video, brand new audience.",
         text="70+ languages. Subtitles. Dubbing.", inset=[("vid-call-es.mp4", 3.9), ("vid-call-sw.mp4", 3.9), ("vid-call-zh.mp4", 3.9)],
         sfx=[("flip", 1.2, 0.5), ("flip", 4.6, 0.5), ("flip", 8.2, 0.5), ("pop", 11.4, 0.6)],
         desc="Four people of different backgrounds smiling at a screen together, left side of frame, space on the right"),
    dict(id="05-trust", voice="narrator", min=12.0, vo_at=0.5,
         vo="[calmly] And when it has to be perfect, add a real human editor. [short pause] You get up to ninety-nine percent accuracy.",
         cap="And when it has to be perfect, add a real human editor. You get up to ninety-nine percent accuracy.",
         text="Up to 99% accurate with human review.", sfx=[("chime", 6.8, 0.6)],
         desc="A focused professional editor in headphones reviewing text against a waveform"),
    dict(id="06-tools", voice="narrator", min=11.6, vo_at=0.5,
         vo="[upbeat] Edit right in your browser. Make AI notes in one click. Share a link. Or send every finished transcript straight to your website or your tools.",
         cap="Edit right in your browser. Make AI notes in one click. Share a link. Or send every finished transcript straight to your website or your tools.",
         text="Edit. Notes. Share. Automate.", inset=[("screens/transcript.png", 2.7), ("screens/notes.png", 2.7), ("screens/download.png", 2.7), ("screens/integrations.png", 2.7)], sfx=[("click", 1.0, 0.5), ("click", 3.0, 0.5), ("click", 4.6, 0.5), ("click", 6.4, 0.5)],
         desc="A bright modern team office, two colleagues collaborating at a laptop, smiling"),
    dict(id="07-cta", voice="narrator", min=10.5, vo_at=0.6,
         vo="[confident] Your first thirty minutes, every month, are free. [short pause] [warmly] Start at kauli dash forge media dot com. [short pause] Kauli. Type it? Never again.",
         cap="Your first thirty minutes, every month, are free. Start at kauli-forgemedia.com. Kauli. Type it? Never again.",
         text="Your first 30 minutes every month are free.", cta=True, sfx=[("sting", ("end", -1.1), 0.7)],
         desc="The creator relaxed at the desk with a coffee, leaning back and smiling, work done"),
]


def run(cmd: list, **kw) -> None:
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def duration(path: pathlib.Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", text)).strip()


# ------------------------------------------------------------------------------------------------ audio
def render_audio(force: bool, dry: bool) -> None:
    key = os.environ.get("ELEVENLABS_API_KEY")
    AUDIO.mkdir(parents=True, exist_ok=True)
    todo_vo = [s for s in SCENES if force or not (AUDIO / f"vo-{s['id']}.mp3").exists()]
    todo_sfx = [n for n in SFX if force or not (AUDIO / f"sfx-{n}.mp3").exists()]
    chars = sum(len(s["vo"]) for s in todo_vo)
    secs = sum(SFX[n][1] for n in todo_sfx)
    print(f"voice: {len(todo_vo)} scenes, {chars} characters; sound effects: {len(todo_sfx)}, {secs:.1f} seconds")
    if dry or not (todo_vo or todo_sfx):
        return
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY is not set")
    for s in todo_vo:
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICES[s['voice']]}", params={"output_format": "mp3_44100_128"},
                       headers={"xi-api-key": key}, timeout=180,
                       json={"text": s["vo"], "model_id": MODEL, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}})
        if r.status_code != 200:
            raise SystemExit(f"ElevenLabs refused the voice for {s['id']} ({r.status_code}): {r.text[:300]}")
        (AUDIO / f"vo-{s['id']}.mp3").write_bytes(r.content)
        print("voice", s["id"], f"{duration(AUDIO / ('vo-' + s['id'] + '.mp3')):.1f}s")
    for name in todo_sfx:
        prompt, seconds = SFX[name]
        r = httpx.post("https://api.elevenlabs.io/v1/sound-generation", params={"output_format": "mp3_44100_128"},
                       headers={"xi-api-key": key}, timeout=120,
                       json={"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.5})
        if r.status_code != 200:
            print(f"sound effect {name} refused ({r.status_code}): {r.text[:200]}; skipping it")
            continue
        (AUDIO / f"sfx-{name}.mp3").write_bytes(r.content)
        print("sfx", name)


# ------------------------------------------------------------------------------------------------ pictures
def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for p in ((WIN / ("segoeuib.ttf" if bold else "segoeui.ttf")), (WIN / ("arialbd.ttf" if bold else "arial.ttf")),
              (DEJAVU / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"))):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur] if cur else lines


def base_image(scene: dict, idx: int) -> tuple[Image.Image, bool]:
    """The scene photo cropped to 16:9 at 1.2x (room to move), with a soft dark band at the bottom for the captions; or a placeholder."""
    path = next((IMAGES / f"{scene['id']}{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp") if (IMAGES / f"{scene['id']}{ext}").exists()), None)
    size = (int(W * 2), int(H * 2))
    if path:
        img = Image.open(path).convert("RGB")
        scale = max(size[0] / img.width, size[1] / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        left, top = (img.width - size[0]) // 2, (img.height - size[1]) // 2
        img = img.crop((left, top, left + size[0], top + size[1]))
    else:
        img = Image.new("RGB", size, NAVY)
        px = img.load()
        for y in range(size[1]):
            for x in range(0, size[0], 4):
                t = (x / size[0] * 0.5 + y / size[1] * 0.5)
                c = tuple(int(NAVY[i] + (BLUE[i] - NAVY[i]) * t) for i in range(3))
                for dx in range(4):
                    if x + dx < size[0]:
                        px[x + dx, y] = c
        d = ImageDraw.Draw(img)
        d.text((size[0] // 2, size[1] // 2 - 70), f"PHOTO {idx + 1} GOES HERE", font=font(84), fill=(255, 255, 255), anchor="mm")
        for i, line in enumerate(wrap(d, scene["desc"], font(46, False), 1500)):
            d.text((size[0] // 2, size[1] // 2 + 30 + i * 62), line, font=font(46, False), fill=(190, 220, 245), anchor="mm")
    band = Image.new("RGBA", size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    top = int(size[1] * 0.62)
    for y in range(top, size[1]):
        bd.line([(0, y), (size[0], y)], fill=(0, 0, 0, int(165 * (y - top) / (size[1] - top))))
    return Image.alpha_composite(img.convert("RGBA"), band).convert("RGB"), path is not None


def overlay_png(scene: dict, out: pathlib.Path) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # the logo on a navy tag, top right
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.resize((int(logo.width * 70 / logo.height), 70), Image.LANCZOS)
        d.rounded_rectangle((W - logo.width - 120, 40, W - 40, 130), radius=22, fill=NAVY + (235,))
        img.alpha_composite(logo, (W - logo.width - 80, 50))
    if scene.get("cta"):
        d.rounded_rectangle((330, 250, W - 330, 740), radius=44, fill=NAVY + (238,))
        f1 = font(84)
        for i, line in enumerate(wrap(d, scene["text"], f1, W - 760)):
            d.text((W // 2, 360 + i * 100), line, font=f1, fill=(255, 255, 255), anchor="mm")
        d.text((W // 2, 570), "kauli-forgemedia.com", font=font(72), fill=(124, 196, 240), anchor="mm")
        d.rounded_rectangle((W // 2 - 230, 628, W // 2 + 230, 716), radius=44, fill=ORANGE + (255,))
        d.text((W // 2, 673), "Start free  →", font=font(54), fill=(255, 255, 255), anchor="mm")
    else:
        fnt = font(76)
        lines = wrap(d, scene["text"], fnt, 1180)
        width = int(max(d.textlength(line, font=fnt) for line in lines)) + 90
        height = len(lines) * 96 + 50
        d.rounded_rectangle((70, 80, 70 + width, 80 + height), radius=34, fill=NAVY + (232,))
        d.rounded_rectangle((70, 80, 86, 80 + height), radius=8, fill=ORANGE + (255,))
        for i, line in enumerate(lines):
            d.text((115, 80 + 25 + i * 96), line, font=fnt, fill=(255, 255, 255))
    img.save(out)


# ------------------------------------------------------------------------------------------------ timeline and video
def timeline() -> list[dict]:
    t, out = 0.0, []
    for s in SCENES:
        vo = AUDIO / f"vo-{s['id']}.mp3"
        if not vo.exists():
            raise SystemExit(f"missing {vo.name}: run the audio step first")
        vo_len = duration(vo)
        dur = max(s["min"], s["vo_at"] + vo_len + (2.4 if s.get("cta") else 0.9))
        out.append({**s, "start": t, "dur": dur, "vo_len": vo_len, "vo_end": t + s["vo_at"] + vo_len})
        t += dur
    return out


def srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(scene: dict, path: pathlib.Path) -> None:
    sentences = [x.strip() for x in re.findall(r"[^.?!]+[.?!]+(?:\.\.)?|[^.?!]+$", scene["cap"]) if x.strip()]
    total = sum(len(x) for x in sentences) or 1
    t, rows = scene["vo_at"] + 0.05, []
    for i, sentence in enumerate(sentences, start=1):
        span = (scene["vo_len"] - 0.1) * len(sentence) / total
        rows.append(f"{i}\n{srt_time(t)} --> {srt_time(t + span - 0.05)}\n{sentence}\n")
        t += span
    path.write_text("\n".join(rows), encoding="utf-8")


def build_scene(i: int, s: dict, work: pathlib.Path) -> tuple[pathlib.Path, bool]:
    base, real = base_image(s, i)
    base_path, ov_path, srt_path, out = work / f"base{i}.png", work / f"ov{i}.png", work / f"cap{i}.srt", work / f"scene{i}.mp4"
    base.save(base_path)
    overlay_png(s, ov_path)
    write_srt(s, srt_path)
    d = s["dur"]
    zoom = "min(1+0.00035*on,1.14)" if i % 2 == 0 else "max(1.14-0.00035*on,1.0)"
    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", base_path,
              "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", ov_path]
    graph = [f"[0:v]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},setsar=1[bg]",
             f"[1:v]format=rgba,fade=t=in:st=0.45:d=0.4:alpha=1[ov]", "[bg][ov]overlay=0:0[v1]"]
    last = "v1"
    clips = s.get("inset") or []
    if clips:
        parts, labels = [], []
        for n, (name, secs) in enumerate(clips):
            src = PROMO / name if name.startswith("screens/") else DEMO / name
            if src.suffix.lower() in (".png", ".jpg", ".jpeg"):
                inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{secs:.3f}", "-i", src]
            else:
                inputs += ["-t", f"{secs:.3f}", "-i", src]
            parts.append(f"[{2 + n}:v]scale=832:468:force_original_aspect_ratio=decrease,pad=832:468:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,fps={FPS}[c{n}]")
            labels.append(f"[c{n}]")
        cat = ("".join(labels) + f"concat=n={len(clips)}:v=1:a=0[cc]") if len(clips) > 1 else "[c0]null[cc]"
        graph += [";".join(parts) + ";" + cat,
                  "[cc]tpad=stop_mode=clone:stop_duration=4,pad=848:484:8:8:color=white,format=rgba,fade=t=in:st=0.9:d=0.35:alpha=1[ins]",
                  "[v1][ins]overlay=W-w-90:300:shortest=1[v2]"]
        last = "v2"
    style = "Alignment=2,MarginV=16,MarginL=48,MarginR=48,FontSize=12,Outline=1.1,Shadow=0.4,BorderStyle=1,Bold=1"
    fade_in = "" if i == 0 else f",fade=t=in:st=0:d=0.25"
    caption_filter = f"subtitles={srt_path.name}:force_style='{style}'" if BURN_CAPTIONS else "null"
    graph.append(f"[{last}]{caption_filter}{fade_in},fade=t=out:st={d - 0.25:.3f}:d=0.25,scale=out_color_matrix=bt709:flags=lanczos,format=yuv420p[out]")
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-t", f"{d:.3f}",
         "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p", "-r", FPS, "-g", FPS * 2,
         "-bf", "2", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", out], cwd=work)
    return out, real


def write_full_srt(tl: list[dict], path: pathlib.Path) -> None:
    """All the captions with times from the start of the video, for uploading to YouTube as the caption track."""
    rows, n = [], 1
    for s in tl:
        sentences = [x.strip() for x in re.findall(r"[^.?!]+[.?!]+(?:\.\.)?|[^.?!]+$", s["cap"]) if x.strip()]
        total = sum(len(x) for x in sentences) or 1
        t = s["start"] + s["vo_at"] + 0.05
        for sentence in sentences:
            span = (s["vo_len"] - 0.1) * len(sentence) / total
            rows.append(f"{n}\n{srt_time(t)} --> {srt_time(t + span - 0.05)}\n{sentence}\n")
            n += 1
            t += span
    path.write_text("\n".join(rows), encoding="utf-8")


def make_thumbnail(path: pathlib.Path) -> None:
    """1280x720 JPEG under 2 MB: the first scene's picture with the three-word hook, large and high-contrast."""
    face = next((IMAGES / f"thumbnail-face{e}" for e in (".png", ".jpg", ".jpeg", ".webp") if (IMAGES / f"thumbnail-face{e}").exists()), None)
    if face:
        src = Image.open(face).convert("RGB")
        scale = max(1280 / src.width, 720 / src.height)
        src = src.resize((round(src.width * scale), round(src.height * scale)), Image.LANCZOS)
        left, top = (src.width - 1280) // 2, (src.height - 720) // 2
        img = src.crop((left, top, left + 1280, top + 720)).convert("RGBA")
    else:
        base, _ = base_image(SCENES[0], 0)
        img = base.resize((1280, 720), Image.LANCZOS).convert("RGBA")
    d = ImageDraw.Draw(img)
    big = font(168)
    for i, word in enumerate(("STOP", "TYPING.")):
        x, y = 60, 250 + i * 190
        w = d.textlength(word, font=big)
        d.rounded_rectangle((x - 24, y - 10, x + w + 24, y + 170), radius=26, fill=(ORANGE if i else NAVY) + (245,))
        d.text((x, y - 18), word, font=big, fill=(255, 255, 255))
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.resize((int(logo.width * 70 / logo.height), 70), Image.LANCZOS)
        d.rounded_rectangle((1280 - logo.width - 60, 24, 1256, 112), radius=20, fill=NAVY + (235,))
        img.alpha_composite(logo, (1280 - logo.width - 40, 34))
    img.convert("RGB").save(path, quality=92)


def build_video() -> pathlib.Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    tl = timeline()
    total = tl[-1]["start"] + tl[-1]["dur"]
    work = pathlib.Path(tempfile.mkdtemp(prefix="promo-", dir=BUILD))
    clips, real_count = [], 0
    for i, s in enumerate(tl):
        clip, real = build_scene(i, s, work)
        clips.append(clip)
        real_count += real
        print("scene", s["id"], f"{s['dur']:.1f}s", "photo" if real else "placeholder")
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    silent = work / "silent.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", silent])

    shutil.copyfile(silent, BUILD / "silent-latest.mp4")
    (BUILD / "silent-latest.json").write_text(json.dumps({"real": real_count, "scenes": len(tl)}), encoding="utf-8")
    final = mix_and_mux(tl, BUILD / "silent-latest.mp4", total, work, real_count == len(tl))
    write_full_srt(tl, PROMO / "kauli-promo.srt")
    make_thumbnail(PROMO / "kauli-promo-thumbnail.jpg")
    save_timeline(tl)
    print("wrote", final, f"{total:.1f}s", f"{real_count}/{len(tl)} scenes with photos")
    return final


def save_timeline(tl: list[dict]) -> None:
    (PROMO / "timeline.json").write_text(json.dumps([{k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()
                                                       if k in ("id", "start", "dur", "vo_end")} for s in tl], indent=1), encoding="utf-8")


MUSIC_BED = AUDIO / "music-bed.mp3"


def music_cues(tl: list[dict], total: float) -> dict:
    """Where the music must change: it follows the picture."""
    return {"enter": tl[1]["start"] + 3.3, "build": tl[2]["start"], "peak": tl[3]["start"], "dip": tl[4]["start"], "lift": tl[5]["start"],
            "end": tl[6]["vo_end"] - 0.9, "total": total}


def render_music(force: bool, dry: bool) -> None:
    """A real instrumental from ElevenLabs Music, written section by section so it builds and relaxes where the scenes do."""
    if MUSIC_BED.exists() and not force:
        print("music bed already exists (use --force to make a new one)")
        return
    tl = timeline()
    total = tl[-1]["start"] + tl[-1]["dur"]
    c = music_cues(tl, total)
    marks = [("Quiet room", 0.0, c["enter"], ["almost silent, soft room ambience, a few gentle sparse piano notes"], ["drums", "vocals"]),
             ("The idea lands", c["enter"], c["build"], ["bright upbeat intro, light plucked guitar, soft finger snaps, optimistic"], ["heavy drums", "vocals"]),
             ("Build", c["build"], c["peak"], ["modern upbeat pop, steady drums, bass, bright synth arpeggios, rising energy"], ["vocals", "dark"]),
             ("Peak", c["peak"], c["dip"], ["full confident energy, punchy handclaps, uplifting hook, big and joyful"], ["vocals", "aggressive"]),
             ("Calm trust", c["dip"], c["lift"], ["pulls back, warm piano and soft pads, reassuring, light percussion"], ["vocals", "heavy drums"]),
             ("Lift and finish", c["lift"], total, ["energy returns, anthemic and bright, builds to one clean final chord with a short ring-out"], ["vocals", "fade out early"])]
    plan = {"positive_global_styles": ["upbeat modern corporate pop", "108 BPM", "instrumental", "optimistic", "YouTube product promo", "clean polished mix"],
            "negative_global_styles": ["vocals", "lyrics", "distorted", "dark", "aggressive", "lo-fi"],
            "sections": [{"section_name": n, "positive_local_styles": pos, "negative_local_styles": neg,
                          "duration_ms": max(3000, int(round((e - st) * 1000))), "lines": []} for n, st, e, pos, neg in marks]}
    length = sum(x["duration_ms"] for x in plan["sections"])
    print(f"music: {length / 1000:.1f} seconds, about {length / 1000 / 60 * 900:.0f} credits")
    if dry:
        return
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY is not set")
    AUDIO.mkdir(parents=True, exist_ok=True)
    r = httpx.post("https://api.elevenlabs.io/v1/music", params={"output_format": "mp3_44100_192"}, headers={"xi-api-key": key}, timeout=600,
                   json={"composition_plan": plan, "model_id": "music_v1"})
    if r.status_code != 200:
        print(f"the sectioned request was refused ({r.status_code}): {r.text[:300]}; trying a plain description")
        prompt = ("Upbeat modern corporate-pop instrumental for a YouTube product promo, 108 BPM, optimistic and polished, no vocals. "
                  f"Starts almost silent with soft room ambience and a few sparse piano notes for {c['enter']:.0f} seconds, then a bright light intro, "
                  f"builds with drums and bass from {c['build']:.0f} seconds, reaches full joyful energy at {c['peak']:.0f} seconds, pulls back to warm piano "
                  f"and pads at {c['dip']:.0f} seconds, and returns with anthemic energy at {c['lift']:.0f} seconds, ending on one clean final chord.")
        r = httpx.post("https://api.elevenlabs.io/v1/music", params={"output_format": "mp3_44100_192"}, headers={"xi-api-key": key}, timeout=600,
                       json={"prompt": prompt, "music_length_ms": int(total * 1000), "model_id": "music_v1", "force_instrumental": True})
    if r.status_code != 200:
        raise SystemExit(f"ElevenLabs Music refused ({r.status_code}): {r.text[:400]}")
    MUSIC_BED.write_bytes(r.content)
    print("music bed", f"{duration(MUSIC_BED):.1f}s")


def mix_and_mux(tl: list[dict], silent: pathlib.Path, total: float, work: pathlib.Path, all_photos: bool) -> pathlib.Path:
    """Voice, effects and music mixed (the music ducks under the voice) and joined to the picture."""
    if MUSIC_BED.exists() and not USE_SYNTH_MUSIC:
        music, music_gain = MUSIC_BED, 0.7
        print("music: ElevenLabs Music")
    else:
        music, music_gain = compose(music_cues(tl, total), work / "bed.mp3"), 0.8
        print("music: synthesized bed")
    inputs, vo_labels = [], []
    n = 0
    for s in tl:                                            # the voice, each scene at its own start
        inputs += ["-i", AUDIO / f"vo-{s['id']}.mp3"]
        ms = int((s["start"] + s["vo_at"]) * 1000)
        vo_labels.append((n, ms))
        n += 1
    fx = []
    for s in tl:
        for name, at, gain in s["sfx"]:
            f = AUDIO / f"sfx-{name}.mp3"
            if not f.exists():
                continue
            when = s["vo_end"] + at[1] if isinstance(at, tuple) else s["start"] + at
            fx.append((f, int(max(0, when) * 1000), gain))
    graph = []
    for k, (idx, ms) in enumerate(vo_labels):
        graph.append(f"[{idx}:a]aresample=48000,adelay={ms}|{ms},volume=1.0[v{k}]")
    graph.append("".join(f"[v{k}]" for k in range(len(vo_labels))) + f"amix=inputs={len(vo_labels)}:duration=longest:dropout_transition=0,volume={len(vo_labels)},asplit=2[vo][vokey]")
    fx_inputs = []
    for k, (f, ms, gain) in enumerate(fx):
        fx_inputs += ["-i", f]
        graph.append(f"[{n + k}:a]aresample=48000,adelay={ms}|{ms},volume={gain}[x{k}]")
    n_fx = len(fx)
    if n_fx:
        graph.append("".join(f"[x{k}]" for k in range(n_fx)) + f"amix=inputs={n_fx}:duration=longest:dropout_transition=0,volume={n_fx}[sfx]")
    mi = n + n_fx
    graph.append(f"[{mi}:a]aresample=48000,volume={music_gain}[m]")
    graph.append("[m][vokey]sidechaincompress=threshold=0.02:ratio=7:attack=15:release=450[duck]")
    mix_in = "[duck][vo]" + ("[sfx]" if n_fx else "")
    graph.append(f"{mix_in}amix=inputs={2 + (1 if n_fx else 0)}:duration=longest:dropout_transition=0,volume={2 + (1 if n_fx else 0)},"
                 f"loudnorm=I=-14:TP=-1.0:LRA=11,atrim=0:{total:.3f}[aout]")
    mixed = work / "mix.m4a"
    run(["ffmpeg", "-v", "error", "-y", *inputs, *fx_inputs, "-i", music, "-filter_complex", ";".join(graph), "-map", "[aout]",
         "-c:a", "aac", "-b:a", "384k", "-ar", "48000", "-ac", "2", mixed])
    final = PROMO / ("kauli-promo.mp4" if all_photos else "kauli-promo-animatic.mp4")
    run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", mixed, "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", "-shortest", final])
    return final


def remix() -> pathlib.Path:
    """New music or voice on the picture already rendered: seconds, not minutes."""
    silent = BUILD / "silent-latest.mp4"
    meta = json.loads((BUILD / "silent-latest.json").read_text(encoding="utf-8"))
    tl = timeline()
    total = tl[-1]["start"] + tl[-1]["dur"]
    work = pathlib.Path(tempfile.mkdtemp(prefix="promo-", dir=BUILD))
    final = mix_and_mux(tl, silent, total, work, meta["real"] == meta["scenes"])
    print("wrote", final, f"{total:.1f}s (remix)")
    return final


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["audio", "music", "video", "remix"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--synth-music", action="store_true")
    ap.add_argument("--captions", action="store_true", help="burn the captions into the picture (off by default)")
    a = ap.parse_args()
    global BURN_CAPTIONS, USE_SYNTH_MUSIC
    BURN_CAPTIONS = a.captions
    USE_SYNTH_MUSIC = a.synth_music
    IMAGES.mkdir(parents=True, exist_ok=True)
    if a.step == "audio":
        render_audio(a.force, a.dry_run)
    elif a.step == "music":
        render_music(a.force, a.dry_run)
    elif a.step == "remix":
        remix()
    else:
        build_video()


if __name__ == "__main__":
    main()
