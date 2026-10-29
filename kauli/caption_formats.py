"""More ways to deliver the same captions (kauli/subtitles.py decides what the captions are; this only writes them out):

  TTML  - the XML standard behind broadcast and many web and streaming players
  SBV   - what YouTube's caption uploader reads
  ASS   - a styled caption file, used to burn captions into a video in one of the looks below

Looks for burned-in captions:
  classic - clean white text with an outline, low in the picture
  social  - big bold text a little higher up (clear of social apps' buttons) where each word lights up as it is spoken
  box     - bold text on a dark box, easiest to read over busy pictures

The word-by-word timing is spread over each caption by word length. It is a close match to speech, not a promise of exact timing.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .subtitles import build_cues

STYLES = {"classic": "Classic", "social": "Social: words light up as they are spoken", "box": "Bold on a dark box"}


def _clock(ms: int, sep: str = ".") -> str:
    ms = max(int(ms), 0)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


def to_sbv(job, source: bool = False) -> str:
    out = []
    for s, e, text in build_cues(job, source):
        h1, h2 = _clock(s)[1:], _clock(e)[1:]                  # YouTube writes the hour without its leading zero: 0:00:01.000
        out.append(f"{h1},{h2}\n{text}\n")
    return "\n".join(out)


def to_ttml(job, lang: str = "en", source: bool = False) -> str:
    paras = []
    for s, e, text in build_cues(job, source):
        body = "<br/>".join(escape(line) for line in text.split("\n"))
        paras.append(f'      <p begin="{_clock(s)}" end="{_clock(e)}" style="s1">{body}</p>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<tt xml:lang="{escape(lang)}" xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling">\n'
            '  <head>\n    <styling>\n'
            '      <style xml:id="s1" tts:color="white" tts:textAlign="center" tts:fontSize="100%" tts:textOutline="black 1px"/>\n'
            '    </styling>\n  </head>\n  <body>\n    <div>\n' + "\n".join(paras) + "\n    </div>\n  </body>\n</tt>\n")


# ---------------------------------------------------------------------------------------------------- styled captions (ASS)
def _ass_time(ms: int) -> str:
    ms = max(int(ms), 0)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h}:{m:02d}:{s:02d}.{milli // 10:02d}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _karaoke(text: str, duration_ms: int) -> str:
    """Each word switches from white to the highlight colour as it is spoken. The timing is spread by word length over most of the
    caption, so the last word has lit up before the caption goes."""
    lines = [ln.split() for ln in _clean(text).split("\n")]
    words = [w for ln in lines for w in ln]
    if not words:
        return ""
    weights = [len(w) + 1 for w in words]
    total_cs = max(int(duration_ms * 0.85 / 10), len(words))
    shares = [max(1, round(total_cs * w / sum(weights))) for w in weights]
    out, i = [], 0
    for ln in lines:
        out.append(" ".join(f"{{\\k{shares[i + j]}}}{w}" for j, w in enumerate(ln)))
        i += len(ln)
    return "\\N".join(out)


def to_ass(job, style: str = "classic", source: bool = False, width: int = 1280, height: int = 720) -> str:
    style = style if style in STYLES else "classic"
    w, h = max(int(width), 2), max(int(height), 2)
    portrait = h > w
    base = min(w, h) if portrait else h                      # size the text by the short side so a vertical video is not tiny
    side = round(w * 0.06)
    look = {
        "classic": dict(size=0.055, bold=0, primary="&H00FFFFFF", secondary="&H00FFFFFF", outline="&H00000000", back="&H80000000",
                        border=1, outline_w=0.004, shadow=1, margin_v=0.07),
        "social": dict(size=0.075, bold=-1, primary="&H0000E5FF", secondary="&H00FFFFFF", outline="&H00000000", back="&H00000000",
                       border=1, outline_w=0.011, shadow=0, margin_v=0.16),
        "box": dict(size=0.06, bold=-1, primary="&H00FFFFFF", secondary="&H00FFFFFF", outline="&H30000000", back="&H30000000",
                    border=3, outline_w=0.014, shadow=0, margin_v=0.08),
    }[style]
    size = max(round(base * look["size"]), 10)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{size},{look['primary']},{look['secondary']},{look['outline']},{look['back']},{look['bold']},0,0,0,"
        f"100,100,0,0,{look['border']},{max(round(h * look['outline_w'], 1), 0.5)},{look['shadow']},2,{side},{side},{round(h * look['margin_v'])},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for s, e, text in build_cues(job, source):
        if style == "social":
            body = "{\\fad(80,60)}" + _karaoke(text, e - s)
        else:
            body = _clean(text).replace("\n", "\\N")
        if body:
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{body}")
    return head + "\n".join(lines) + "\n"
