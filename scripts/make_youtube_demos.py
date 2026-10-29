"""Makes the YouTube versions of the interactive demos: for each use case, the English original upscaled to 1080p with a short title card
and an end card, plus a 1280x720 thumbnail and the title and description to paste in. Output: marketing/youtube-demos/.

    python scripts/make_youtube_demos.py            # all eight
    python scripts/make_youtube_demos.py call news  # just these

The pictures and voices are the demo clips in webapp/static/demo (see scripts/make_demo_audio.py and make_demo_videos.py).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webapp import demo_content  # noqa: E402

DEMO = ROOT / "webapp" / "static" / "demo"
OUT = ROOT / "marketing" / "youtube-demos"
LOGO = ROOT / "webapp" / "static" / "kauli-logo-nav-light.png"
W, H, FPS = 1920, 1080, 30
NAVY, BLUE, ORANGE = (8, 40, 72), (8, 120, 200), (244, 122, 10)
WIN = pathlib.Path("/mnt/c/Windows/Fonts")
DEJAVU = pathlib.Path("/usr/share/fonts/truetype/dejavu")
SITE = "https://kauli-forgemedia.com"

# scenario -> (file stem, YouTube title, the page it points to, description of what is shown, tags)
PACK = {
    "meeting": ("01-meetings", "Meeting Transcription with Speaker Labels and Timestamps | Kauli demo", "/solutions/audio-to-text",
                "Two people in a meeting. Kauli writes down every word, labels who is speaking, and timestamps each line.",
                "meeting transcription, speaker labels, timestamps, audio to text, meeting notes"),
    "call": ("02-call-centers", "Call Center Transcription: Agent and Customer Separated | Kauli demo", "/solutions/call-centers",
             "A support call. Kauli separates the agent from the customer, so a reviewer can read the call, search it, and send the result to their tools.",
             "call center transcription, call quality assurance, speaker separation, support calls, call transcripts"),
    "interview": ("03-interviews", "Interview Transcription: Who Said What, Ready to Quote | Kauli demo", "/solutions/interview-transcription",
                  "An interview. Kauli labels each speaker and timestamps every answer, ready to quote.",
                  "interview transcription, journalism, research interviews, speaker labels, transcribe interview"),
    "podcast": ("04-podcasts", "Podcast Transcripts and Translation for Every Episode | Kauli demo", "/solutions/podcast-transcription",
                "A podcast conversation. Kauli turns it into a transcript and translates it, so more people can read or follow the episode.",
                "podcast transcription, podcast transcripts, translate podcast, show notes, audio to text"),
    "course": ("05-courses", "Course Subtitles in Any Language: E-learning Demo | Kauli", "/solutions/e-learning",
               "A lesson. Kauli adds subtitles your learners can read in their own language.",
               "course subtitles, e-learning translation, lesson captions, online course localization, video translation"),
    "news": ("06-news-broadcast", "Broadcast Subtitles in Any Language, Ready for Air | Kauli demo", "/solutions/media-broadcast",
             "A news bulletin. Kauli produces subtitles in other languages, fast, with human review available for up to 99% accuracy.",
             "broadcast subtitles, news captions, media translation, closed captions, fast turnaround subtitles"),
    "field": ("07-ngo-field-work", "Translate NGO Field Recordings for Your Reports | Kauli demo", "/solutions/ngos",
              "A field recording. Kauli transcribes it and translates it, so it can go straight into a report.",
              "NGO transcription, field recordings, translate interviews, nonprofit translation, report transcription"),
    "video": ("08-video-dubbing", "AI Video Dubbing: The Same Video in a New Language | Kauli demo", "/solutions/video-dubbing",
              "A product video. Kauli can speak the same video in another language, in selected languages.",
              "AI dubbing, video dubbing, video localization, translate video, voice over"),
}


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for p in ((WIN / ("segoeuib.ttf" if bold else "segoeui.ttf")), (WIN / ("arialbd.ttf" if bold else "arial.ttf")),
              (DEJAVU / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"))):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def wrap(d: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if d.textlength(trial, font=fnt) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur] if cur else lines


def background(w: int = W, h: int = H) -> Image.Image:
    img = Image.new("RGB", (w, h), NAVY)
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x / w) * 0.55 + (y / h) * 0.45
            px[x, y] = tuple(int(NAVY[i] + (BLUE[i] - NAVY[i]) * t * 0.9) for i in range(3))
    return img


def logo_on(img: Image.Image, x: int, y: int, height: int = 84) -> None:
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.resize((int(logo.width * height / logo.height), height), Image.LANCZOS)
        img.paste(logo, (x, y), logo)


def intro_card(label: str, headline: str, path: pathlib.Path) -> None:
    img = background()
    d = ImageDraw.Draw(img)
    logo_on(img, 110, 90)
    d.text((110, 380), "KAULI DEMO", font=font(46), fill=ORANGE)
    d.text((110, 450), label, font=font(120), fill=(255, 255, 255))
    for i, line in enumerate(wrap(d, headline, font(58, False), 1500)):
        d.text((110, 620 + i * 80), line, font=font(58, False), fill=(190, 220, 245))
    img.save(path)


def end_card(path: pathlib.Path) -> None:
    img = background()
    d = ImageDraw.Draw(img)
    logo_on(img, W // 2 - 190, 150, 110)
    d.text((W // 2, 420), "Try it on your own file", font=font(110), fill=(255, 255, 255), anchor="mm")
    d.text((W // 2, 560), "Your first 30 minutes every month are free", font=font(60, False), fill=(190, 220, 245), anchor="mm")
    d.text((W // 2, 690), "kauli-forgemedia.com", font=font(84), fill=(124, 196, 240), anchor="mm")
    d.rounded_rectangle((W // 2 - 260, 780, W // 2 + 260, 890), radius=55, fill=ORANGE)
    d.text((W // 2, 835), "Start free  →", font=font(60), fill=(255, 255, 255), anchor="mm")
    img.save(path)


def thumbnail(scenario: str, label: str, path: pathlib.Path) -> None:
    poster = DEMO / f"vid-{scenario}-en.jpg"
    img = (Image.open(poster).convert("RGB").resize((1280, 720), Image.LANCZOS) if poster.exists() else background(1280, 720)).convert("RGBA")
    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    for y in range(300, 720):
        bd.line([(0, y), (1280, y)], fill=(8, 40, 72, int(235 * (y - 300) / 420)))
    img = Image.alpha_composite(img, band)
    d = ImageDraw.Draw(img)
    big = font(92)
    lines = wrap(d, label, big, 1120)
    y = 720 - 60 - len(lines) * 104
    d.text((60, y - 66), "KAULI DEMO", font=font(40), fill=ORANGE)
    for i, line in enumerate(lines):
        d.text((60, y + i * 104), line, font=big, fill=(255, 255, 255))
    logo_on(img, 1280 - 270, 30, 64)
    img.convert("RGB").save(path, quality=92)


def encode_still(png: pathlib.Path, seconds: float, out: pathlib.Path) -> None:
    run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-t", str(seconds), "-i", png,
         "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", f"scale={W}:{H},fade=t=in:st=0:d=0.4,fade=t=out:st={seconds - 0.4}:d=0.4,format=yuv420p", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", out])


def encode_main(src: pathlib.Path, out: pathlib.Path) -> None:
    run(["ffmpeg", "-v", "error", "-y", "-i", src,
         "-vf", f"scale={W}:{H}:flags=lanczos:out_color_matrix=bt709,unsharp=5:5:0.5:5:5:0.0,fps={FPS},format=yuv420p",
         "-af", "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out])


def run(cmd: list) -> None:
    subprocess.run([str(c) for c in cmd], check=True)


def description(scenario: str) -> str:
    stem, title, page, what, tags = PACK[scenario]
    s = demo_content.SCENARIOS[scenario]
    return (f"{what}\n\n"
            f"{s['headline']}.\n\n"
            f"Try Kauli free, 30 minutes every month: {SITE}{page}?utm_source=youtube&utm_medium=video&utm_campaign=demo&utm_content={scenario}\n"
            f"Hear it in other languages: {SITE}/try?use={scenario}&utm_source=youtube&utm_medium=video&utm_campaign=demo&utm_content={scenario}\n\n"
            "Kauli turns audio and video into accurate transcripts in minutes, translates them into 70+ languages as subtitles or dubbed audio, "
            "and adds human review for up to 99% accuracy.\n\n"
            "The voices in this demo are generated with AI, and the people shown are illustrations.\n\n"
            f"#transcription #AItools #{tags.split(',')[0].strip().replace(' ', '')}")


def main() -> None:
    names = sys.argv[1:] or list(PACK)
    OUT.mkdir(parents=True, exist_ok=True)
    notes = []
    for scenario in names:
        stem, title, page, what, tags = PACK[scenario]
        src = DEMO / f"vid-{scenario}-en.mp4"
        if not src.exists():
            raise SystemExit(f"missing {src}")
        label = demo_content.SCENARIOS[scenario]["label"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            intro_card(label, demo_content.SCENARIOS[scenario]["headline"], tmp / "intro.png")
            end_card(tmp / "end.png")
            encode_still(tmp / "intro.png", 2.6, tmp / "a.mp4")
            encode_main(src, tmp / "b.mp4")
            encode_still(tmp / "end.png", 3.6, tmp / "c.mp4")
            (tmp / "list.txt").write_text("".join(f"file '{tmp / n}'\n" for n in ("a.mp4", "b.mp4", "c.mp4")), encoding="utf-8")
            final = OUT / f"{stem}-demo.mp4"
            run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", tmp / "list.txt", "-c", "copy", "-movflags", "+faststart", final])
        thumbnail(scenario, label, OUT / f"{stem}-thumbnail.jpg")
        notes.append({"file": final.name, "thumbnail": f"{stem}-thumbnail.jpg", "title": title, "tags": tags, "description": description(scenario)})
        print("made", final.name)
    (OUT / "uploads.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["# Kauli demo videos for YouTube\n", "Upload each file with its thumbnail. Paste the title, description and tags exactly as shown.\n"]
    for n in notes:
        lines += [f"\n---\n\n## {n['file']}\n", f"**Thumbnail:** `{n['thumbnail']}`\n", f"**Title:**\n```\n{n['title']}\n```\n",
                  f"**Description:**\n```\n{n['description']}\n```\n", f"**Tags:**\n```\n{n['tags']}\n```\n"]
    (OUT / "upload-pack.md").write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT / "upload-pack.md")


if __name__ == "__main__":
    main()
