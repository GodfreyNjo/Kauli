"""SRT / WebVTT writers. Deliverable formats, not an afterthought -
half your early clients will want the subtitle file more than the audio."""
from __future__ import annotations

from .models import Job


def _ts(ms: int, sep: str = ",") -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


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
        out.append(f"{i}\n{_ts(seg.start_ms)} --> {_ts(seg.end_ms)}\n{text.strip()}\n")
    return "\n".join(out)


def to_vtt(job: Job, source: bool = False) -> str:
    out = ["WEBVTT", ""]
    for seg in job.segments:
        text = seg.source_final_text if source else seg.final_text
        if not text.strip():
            continue
        out.append(f"{_ts(seg.start_ms, '.')} --> {_ts(seg.end_ms, '.')}")
        out.append(text.strip())
        out.append("")
    return "\n".join(out)
