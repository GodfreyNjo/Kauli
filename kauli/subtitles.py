"""SRT / WebVTT writers. Deliverable formats, not an afterthought -
half your early clients will want the subtitle file more than the audio.

How a caption is laid out is one set of rules, applied to every file we deliver (SRT, VTT, the captions burned into a
video and the live captions in the order preview):

* at most 2 lines on screen at a time;
* each line at most 42 characters, spaces included (20 for Chinese, Japanese and Korean, which have no spaces);
* a reading speed of about 20 characters a second, never asked to go past 30: a caption that would be read too fast
  stays up a little longer, but only into silence, never over the next caption;
* a caption is on screen while it is being said, and goes when the speech does: nothing lingers, and no caption is ever
  on screen for more than 6 seconds, so a long pause (or a sound tag such as [MUSIC PLAYING] over a long interlude) is
  not covered by stale text;
* a long sentence is split into several captions that follow the speech, not squeezed into one;
* no word is ever dropped: only the layout changes, never the text.
"""
from __future__ import annotations

import math
import re

from .models import Job
from .screen_text import is_top

MAX_CAPTION_DISPLAY_MS = 6000
MAX_LINES = 2
MAX_LINE_CHARS = 42
MAX_LINE_CHARS_CJK = 20
MIN_CAPTION_MS = 1000
READING_CPS = 20          # the comfortable speed we aim for
MAX_CPS = 30              # the most we ever ask a viewer to read
CUE_GAP_MS = 40           # a clear frame between two captions

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_CJK_BREAKS = "。，、；：？！,.;:?!"


def _ts(ms: int, sep: str = ",") -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _display_end_ms(seg) -> int:
    """The editor's view of one segment's caption window: its own end, never more than 6 seconds from its start."""
    return min(seg.end_ms, seg.start_ms + MAX_CAPTION_DISPLAY_MS)


def _is_cjk(text: str) -> bool:
    letters = [c for c in text if not c.isspace()]
    return bool(letters) and sum(1 for c in letters if _CJK.match(c)) / len(letters) > 0.3


def _wrap_lines(text: str) -> list[str]:
    """Greedy word-wrap (never mid-word) at the line limit. Chinese, Japanese and Korean are cut by characters, at a
    punctuation mark when one is close to the limit."""
    text = " ".join(text.split())
    if not text:
        return []
    if _is_cjk(text):
        lines, rest = [], text
        while len(rest) > MAX_LINE_CHARS_CJK:
            cut = MAX_LINE_CHARS_CJK
            for i in range(MAX_LINE_CHARS_CJK, MAX_LINE_CHARS_CJK - 7, -1):
                if rest[i - 1] in _CJK_BREAKS:
                    cut = i
                    break
            lines.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            lines.append(rest)
        return lines
    lines, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= MAX_LINE_CHARS or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _wrap_caption_text(text: str) -> str:
    """All of a segment's text, wrapped to the line limit. (Delivery cuts it into captions of two lines each.)"""
    lines = _wrap_lines(text)
    return "\n".join(lines) if lines else text


def _groups(text: str) -> list[str]:
    """The text as captions of at most two lines each, ending a caption at a comma or full stop when one falls on the
    first line rather than splitting a clause in half."""
    lines = _wrap_lines(text)
    groups, i = [], 0
    while i < len(lines):
        take = lines[i:i + MAX_LINES]
        if len(take) == MAX_LINES and i + MAX_LINES < len(lines):
            ends_clause = lambda l: l.rstrip()[-1:] in _CJK_BREAKS            # noqa: E731
            if ends_clause(take[0]) and not ends_clause(take[1]):
                take = take[:1]
        groups.append("\n".join(take))
        i += len(take)
    return groups


def _speaker_included(job: Job, seg) -> bool:
    """A real, explicit exclusion, not a re-transcription - see
    Job.speaker_subtitle_included's own docstring. A segment with no
    speaker_id (diarization never ran, or it's a gap) is always included;
    a speaker_id missing from the dict is included by default too - only
    a human explicitly toggling a speaker off in Ereri ever removes their
    lines from the delivered caption file (e.g. a documentary that wants
    the local Swahili subject subtitled but not the English narrator)."""
    speaker = getattr(seg, "speaker_id", None)
    if not speaker:
        return True
    return getattr(job, "speaker_subtitle_included", {}).get(speaker, True)


def build_cues(job: Job, source: bool = False) -> list[tuple[int, int, str]]:
    """The captions to deliver, as (start_ms, end_ms, text) in time order, following the rules in the module docstring.

    source_final_text, not source_transcript - a human's correction to the ASR output must win here too, same as
    final_text already does for the translated side."""
    raw: list[list] = []
    for seg in job.segments:
        if not _speaker_included(job, seg):
            continue
        text = (seg.source_final_text if source else seg.final_text).strip()
        if not text:
            continue
        groups = _groups(text)
        if not groups:
            continue
        span = max(seg.end_ms - seg.start_ms, MIN_CAPTION_MS)
        weights = [max(1, len(g.replace("\n", ""))) for g in groups]
        t = float(seg.start_ms)
        for g, w in zip(groups, weights):
            share = span * w / sum(weights)
            raw.append([round(t), round(t + share), g])
            t += share
    raw.sort(key=lambda c: c[0])
    cues: list[tuple[int, int, str]] = []
    for i, (start, end, text) in enumerate(raw):
        next_start = raw[i + 1][0] if i + 1 < len(raw) else None
        chars = len(text.replace("\n", ""))
        wanted = max(MIN_CAPTION_MS, math.ceil(chars / READING_CPS * 1000))     # time to read it comfortably
        end = max(end, start + wanted)                                           # extend for reading ...
        if next_start is not None:
            end = min(end, max(start + 1, next_start - CUE_GAP_MS))              # ... but only into silence
        end = min(end, start + MAX_CAPTION_DISPLAY_MS)                           # and never past 6 seconds
        cues.append((start, max(end, start + 1), text))
    return cues


def cue_dicts(job: Job, source: bool = False) -> list[dict]:
    """The same captions for the live preview in the browser. A caption marked "top" moves to the top of the picture while the video has
    its own text or a graphic at the bottom (see kauli/screen_text.py)."""
    ranges = getattr(job, "top_ranges", None)
    return [{"s": s, "e": e, "t": t, "top": is_top(s, e, ranges)} for s, e, t in build_cues(job, source)]


def to_srt(job: Job, source: bool = False) -> str:
    return "\n".join(f"{i}\n{_ts(s)} --> {_ts(e)}\n{text}\n" for i, (s, e, text) in enumerate(build_cues(job, source), start=1))


VTT_TOP = " line:8% position:50% align:center"          # the top of the picture, clear of a lower-third


def to_vtt(job: Job, source: bool = False) -> str:
    out = ["WEBVTT", ""]
    ranges = getattr(job, "top_ranges", None)
    for s, e, text in build_cues(job, source):
        out.append(f"{_ts(s, '.')} --> {_ts(e, '.')}{VTT_TOP if is_top(s, e, ranges) else ''}")
        out.append(text)
        out.append("")
    return "\n".join(out)
