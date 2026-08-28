"""Core data model. A deliberately small subset of the full manifest schema —
we add fields when we actually need them, not before."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A speaker tag is a name (or short title) at the very START of a
# segment's text, ending in a colon - "RIGATHI GACHAGUA:", "NARRATOR:",
# "Audience:". Anchored to the start of the string on purpose: that's
# what actually distinguishes a speaker label from an ordinary sentence
# that happens to contain a colon somewhere in the middle (a time
# expression, a list, a quote) - only the leading one is ever a speaker
# tag. Shared by webapp/app.py's cell-builders (so "RIGATHI GACHAGUA:"
# renders as ONE cell, not two separate word-cells, and stays that way
# through every save/reload instead of a client-side-only trick that gets
# undone the next time cells are rebuilt from the saved text) and
# kauli/pipeline.py's TTS step (so the tag is captioned but never spoken
# aloud - a real voice actor reading a transcript doesn't say the
# character's name before their own line either).
SPEAKER_TAG_RE = re.compile(r"^([A-Z][A-Za-z'.\- ]{0,40}:)\s*")


def split_off_speaker_tag(text: str) -> tuple[str | None, str]:
    """Returns (tag, remainder) if text starts with a speaker tag, else
    (None, text) unchanged."""
    m = SPEAKER_TAG_RE.match(text or "")
    if not m:
        return None, text
    return m.group(1), text[m.end():]


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
    # "speech" - a real ASR-detected segment. "gap" - synthetic, inserted
    # where ASR found no speech at all for GAP_THRESHOLD_MS or longer
    # (before the first segment, between two segments, or after the last -
    # see kauli/pipeline.py's _insert_non_speech_segments). A gap segment
    # never goes through translation or TTS on its own; it exists so
    # Ereri has a real, taggable cell there (music, laughter, sfx) instead
    # of that stretch having no cell at all. webapp/app.py's
    # _find_gaps/_build_source_cells already render an empty segment's
    # full span as one gap-cell - no editor changes needed for this.
    segment_type: str = "speech"

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
    # --- editor voice direction (see webapp/app.py's voice-direction route) ---
    # A human editor's deliberate override, layered ON TOP of the automatic
    # fit-to-slot stretch above - e.g. "read this 15% slower, it's rushed."
    # 0.0 = no override, automatic fit-to-slot behaves exactly as before.
    # Positive = slower, negative = faster - same sign convention as
    # time_stretch_pct/timing.required_stretch_pct ("positive = must slow
    # down").
    manual_pace_pct: float = 0.0
    # Forces this segment's ENTIRE spoken audio to be read out letter by
    # letter (see kauli.pipeline.spell_out_text) instead of as a word -
    # real workaround for an acronym or name a TTS voice keeps misreading.
    # Applies to the whole segment, not one word inside a longer sentence -
    # this system renders/times audio per-segment, not per-word, so this
    # only makes real sense on a segment that mostly IS the name/acronym.
    spell_out: bool = False

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
    # Multi-speaker consistency: seg.speaker_id -> a TTS voice id (a Piper
    # PIPER_VOICES key, e.g. "amy"/"ryan" - see webapp/app.py's
    # _resolve_voice_for_segment). A speaker_id only ever exists here if
    # something actually set it - Transkriptor's real diarization labels
    # (TranskriptorASR), or a human correcting/assigning one by ear in
    # Ereri (see editor_set_segment_speaker) - faster-whisper alone never
    # sets one. Once a speaker has a voice here, EVERY segment tagged with
    # that speaker_id uses it, permanently, until a human changes it -
    # that's the actual guarantee behind "the same character keeps the
    # same voice throughout," achieved without any new diarization model.
    speaker_voices: dict[str, str] = field(default_factory=dict)

    # Same idea as speaker_voices above, but for Azure: a Piper voice-model
    # PATH means nothing to Azure's synthesize() (it wants a named voice
    # like "sw-KE-RafikiNeural"), and resolve_voice_for_segment already
    # refuses to hand a Piper path to a non-Piper provider - see that
    # function's own docstring on why that split has to be a real, separate
    # dict, not a shared one keyed the same way with values that mean two
    # different things depending on which provider reads them. Populated
    # either automatically (see kauli.speaker_gender's pitch-based
    # male/female default) or by a human overriding it by ear in Ereri
    # (webapp/app.py's editor_assign_speaker_voice_azure) - the automatic
    # guess is a default, never the only way to set this.
    speaker_voice_names: dict[str, str] = field(default_factory=dict)

    # seg.speaker_id -> whether THIS speaker's lines appear in the
    # delivered subtitle file - a real request: a documentary with a
    # Swahili subject and an English narrator often only wants the
    # subject subtitled, not the narrator or a music-only "speaker". A
    # speaker_id missing from this dict is INCLUDED by default (so a
    # normal single/no-diarization order behaves exactly as before this
    # existed) - only an EXPLICIT False, set by a human in Ereri
    # (webapp/app.py's editor_set_speaker_subtitle_inclusion), ever
    # excludes one. kauli.subtitles.to_srt/to_vtt are what actually apply
    # this - see their own docstrings for why this is a real filter, not
    # a re-transcription.
    speaker_subtitle_included: dict[str, bool] = field(default_factory=dict)

    # ---- quality rollups ----
    @property
    def fit_rate(self) -> float:
        # Gap segments never go through fit-checking (nothing to fit -
        # there's no translated speech to time), so their fit_status stays
        # at the dataclass default "unknown" forever. Counting them in the
        # denominator would silently understate the real fit rate on any
        # job that has non-speech stretches.
        speech = [s for s in self.segments if s.segment_type != "gap"]
        if not speech:
            return 0.0
        ok = sum(1 for s in speech if s.fit_status == "fits")
        return round(ok / len(speech), 3)

    @property
    def flagged_count(self) -> int:
        return sum(1 for s in self.segments if s.review_flag)

    @property
    def edited_pct(self) -> int:
        """How much of the real editing work is actually done - the staff
        queue's "% edited" column (see webapp/app.py's staff_approve,
        which refuses to ship until this hits 100). seg.approved is the
        one real signal for "a human has confirmed this segment's English
        is right" (set by _apply_segment_edit on every save, whether or
        not the text itself changed - even confirming an already-correct
        segment needs one save/Ctrl+S to register). Gap segments are
        excluded from both sides of the fraction, same reasoning as
        fit_rate above: an untouched gap might genuinely have nothing
        worth tagging, so it's not fair to count it as "incomplete" work -
        completion is measured against the real speech that needs review."""
        speech = [s for s in self.segments if s.segment_type != "gap"]
        if not speech:
            return 100
        done = sum(1 for s in speech if s.approved)
        return round(100 * done / len(speech))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fit_rate"] = self.fit_rate
        d["flagged_count"] = self.flagged_count
        d["edited_pct"] = self.edited_pct
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
        d.pop("edited_pct", None)
        segs = []
        for s in d.pop("segments", []):
            s["words"] = [Word(**w) for w in s.get("words", [])]
            segs.append(Segment(**s))
        return cls(segments=segs, **d)
