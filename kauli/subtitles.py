"""SRT / WebVTT writers. Deliverable formats, not an afterthought -
half your early clients will want the subtitle file more than the audio."""
from __future__ import annotations

from .models import Job

# Professional captioning practice (3Play/industry norm) keeps a caption on
# screen 3-6 seconds - long enough to read, never so long it lingers stale.
# Most real speech segments never approach this (natural pauses keep them
# short already); the case this actually matters for is a GAP/sound-tag
# segment ("[MUSIC PLAYING]") that can legitimately span a long musical
# interlude - without a cap, that one caption would sit on screen for the
# entire interlude, well past the point it's still useful information.
# Caps only the DISPLAYED out-point, never the real segment timing data
# itself (start_ms/end_ms on the Segment stay exactly what ASR/gap-
# detection found - this only affects how long the caption line lingers).
MAX_CAPTION_DISPLAY_MS = 6000


def _ts(ms: int, sep: str = ",") -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _display_end_ms(seg) -> int:
    return min(seg.end_ms, seg.start_ms + MAX_CAPTION_DISPLAY_MS)


# Industry-standard caption readability limit (3Play, Netflix, BBC all use
# a number in this range) - long enough for a real sentence, short enough
# to read in the time it's on screen. Applied as real line-wrapping on
# delivery, not a truncation - no word is ever dropped. A caption needing
# more than the usual 2-3 lines to fit is really a sign the segment itself
# is too long for its slot - the fit-status review flags (duration_overflow
# / unfittable / stretch_cap_exceeded) already catch that for a human to
# split or shorten in Ereri; this only controls how the text is LAID OUT,
# never how much of it survives.
MAX_LINE_CHARS = 32


def _wrap_caption_text(text: str) -> str:
    """Greedy word-wrap at MAX_LINE_CHARS, breaking at whitespace (never
    mid-word) - the same approach every real captioning tool uses. A long
    caption still gets every word, just spread across more lines (see this
    module's own comment above on why dropping text would be worse)."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= MAX_LINE_CHARS or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def to_srt(job: Job, source: bool = False) -> str:
    # source_final_text, not source_transcript - a human's correction to
    # the ASR output must win here too, same as final_text already does
    # for the translated side. Using the raw, uncorrected transcript was a
    # real bug: a staff correction to the Swahili text would never reach
    # the delivered transcript file, no matter how carefully it was edited.
    out = []
    for i, seg in enumerate(job.segments, start=1):
        text = seg.source_final_text if source else seg.final_text
        if not text.strip():
            continue
        out.append(f"{i}\n{_ts(seg.start_ms)} --> {_ts(_display_end_ms(seg))}\n{_wrap_caption_text(text.strip())}\n")
    return "\n".join(out)


def to_vtt(job: Job, source: bool = False) -> str:
    out = ["WEBVTT", ""]
    for seg in job.segments:
        text = seg.source_final_text if source else seg.final_text
        if not text.strip():
            continue
        out.append(f"{_ts(seg.start_ms, '.')} --> {_ts(_display_end_ms(seg), '.')}")
        out.append(_wrap_caption_text(text.strip()))
        out.append("")
    return "\n".join(out)
