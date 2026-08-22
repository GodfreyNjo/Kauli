"""Core data model. A deliberately small subset of the full manifest schema —
we add fields when we actually need them, not before."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Word:
    text: str
    start_ms: int
    end_ms: int
    confidence: float = 1.0


@dataclass
class Segment:
    segment_id: str
    index: int
    start_ms: int
    end_ms: int
    speaker_id: str | None = None

    # --- source (ASR) ---
    source_language: str = "sw"
    source_transcript: str = ""
    source_confidence: float = 0.0
    words: list[Word] = field(default_factory=list)
    source_edited_transcript: str | None = None  # human correction of the ASR text

    # --- translation ---
    target_language: str = "en"
    literal: str = ""
    spoken: str = ""
    alternates: list[dict[str, Any]] = field(default_factory=list)
    selected: str = "spoken"
    translation_confidence: float = 0.0
    cultural_notes: str | None = None

    # --- timing ---
    budget_ms: int = 0
    est_duration_ms: int = 0
    fit_ratio: float = 0.0
    fit_status: str = "unknown"

    # --- tts ---
    voice_id: str | None = None
    audio_path: str | None = None
    rendered_duration_ms: int | None = None
    time_stretch_pct: float = 0.0

    # --- review ---
    review_flag: bool = False
    review_reasons: list[str] = field(default_factory=list)
    edited_text: str | None = None
    approved: bool = False
    # Set when the Swahili source is corrected after this segment's current
    # translation was produced - the English shown is real, it's just not
    # a translation of the text you're now looking at in step 1. Cleared
    # by actually re-translating, or by hand-editing the English yourself
    # (either one means a human has now reconciled the two). See
    # app.py's editor_save_source / editor_retranslate / editor_save_target.
    translation_stale: bool = False

    notes: str | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def source_final_text(self) -> str:
        """The ASR transcript actually used going forward - a human's
        correction to it always wins, same pattern as final_text below."""
        return self.source_edited_transcript or self.source_transcript

    @property
    def final_text(self) -> str:
        """What actually gets synthesised. A human edit always wins."""
        if self.edited_text:
            return self.edited_text
        if self.selected == "literal":
            return self.literal
        return self.spoken or self.literal


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now)
    status: str = "created"
    source_path: str = ""
    source_duration_ms: int = 0
    source_language: str = "sw"
    target_language: str = "en"
    segments: list[Segment] = field(default_factory=list)
    providers: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    # ---- quality rollups ----
    @property
    def fit_rate(self) -> float:
        if not self.segments:
            return 0.0
        ok = sum(1 for s in self.segments if s.fit_status == "fits")
        return round(ok / len(self.segments), 3)

    @property
    def flagged_count(self) -> int:
        return sum(1 for s in self.segments if s.review_flag)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fit_rate"] = self.fit_rate
        d["flagged_count"] = self.flagged_count
        return d

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "Job":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d.pop("fit_rate", None)
        d.pop("flagged_count", None)
        segs = []
        for s in d.pop("segments", []):
            s["words"] = [Word(**w) for w in s.get("words", [])]
            segs.append(Segment(**s))
        return cls(segments=segs, **d)
