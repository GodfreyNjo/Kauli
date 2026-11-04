"""How a caption is laid out in everything we deliver: two lines at most, 42 characters a line, a comfortable reading speed, on
screen while it is spoken and gone when the speech is, and never on screen for more than 6 seconds."""
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli import subtitles  # noqa: E402
from kauli.models import Job, Segment  # noqa: E402
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def job_with(*segments, lang="en"):
    job = Job(source_path="x.wav", source_language=lang, target_language=lang, providers={})
    job.segments = [Segment(segment_id=f"s{i}", index=i, start_ms=s, end_ms=e, source_transcript=t, spoken=t,
                            segment_type=kind) for i, (s, e, t, kind) in enumerate(segments)]
    return job


def S(start, end, text, kind="speech"):
    return (start, end, text, kind)


LONG = ("Thanks everyone for joining today because our launch moves to the fifteenth of March, so marketing needs the final "
        "assets by next Friday and Priya will share the updated budget this afternoon before the review meeting.")


def test_a_long_sentence_becomes_several_captions_of_two_short_lines():
    cues = subtitles.build_cues(job_with(S(0, 12000, LONG)))
    assert len(cues) >= 2
    for start, end, text in cues:
        lines = text.split("\n")
        assert len(lines) <= 2 and all(len(l) <= 42 for l in lines), text
    assert " ".join(c[2].replace("\n", " ") for c in cues) == LONG                   # no word lost, none repeated
    assert cues[0][0] == 0 and cues[-1][1] <= 12000                                   # they follow the speech


def test_no_caption_is_ever_on_screen_for_more_than_six_seconds():
    cues = subtitles.build_cues(job_with(S(0, 30000, "[MUSIC PLAYING]", "gap"), S(30000, 50000, "Welcome back.")))
    assert cues[0] == (0, 6000, "[MUSIC PLAYING]")                                    # a long interlude is not covered by stale text
    assert all(e - s <= 6000 for s, e, _ in cues)
    slow = subtitles.build_cues(job_with(S(0, 15000, "Well.")))                       # one word, then a long pause inside the segment
    assert slow[0][1] - slow[0][0] <= 6000


def test_a_caption_goes_when_the_speech_does():
    start, end, _ = subtitles.build_cues(job_with(S(1000, 3000, "Hello there everyone.")))[0]
    assert (start, end) == (1000, 3000)                                               # not a moment longer than it was said
    c = subtitles.build_cues(job_with(S(0, 2000, "Yes."), S(9000, 11000, "Next point.")))
    assert c[0][1] == 2000 and c[1][0] == 9000                                         # the pause between them is empty


def test_a_caption_that_would_be_read_too_fast_stays_up_a_little_longer_but_only_into_silence():
    quick = "This sentence is forty characters long."
    alone = subtitles.build_cues(job_with(S(0, 500, quick)))[0]
    assert alone[1] >= 1900                                                           # about 20 characters a second
    crowded = subtitles.build_cues(job_with(S(0, 500, quick), S(1000, 4000, "Next one.")))
    assert crowded[0][1] <= 1000 - subtitles.CUE_GAP_MS                               # never over the next caption
    assert crowded[0][1] > crowded[0][0]


def test_captions_never_overlap_and_stay_in_order():
    rng = random.Random(7)
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
    t, segs = 0, []
    for _ in range(60):
        t += rng.randint(0, 4000)
        dur = rng.randint(300, 14000)
        segs.append(S(t, t + dur, " ".join(rng.choice(words) for _ in range(rng.randint(1, 40)))))
        t += dur
    cues = subtitles.build_cues(job_with(*segs))
    for (s1, e1, _), (s2, e2, _) in zip(cues, cues[1:]):
        assert s1 <= s2 and e1 <= s2, (s1, e1, s2)
    for s, e, text in cues:
        assert e > s and e - s <= 6000 and len(text.split("\n")) <= 2
        assert all(len(l) <= 42 for l in text.split("\n"))


def test_chinese_japanese_and_korean_are_cut_by_character_not_by_space():
    zh = "我们的发布时间推迟到三月十五日，所以市场部需要在下周五之前拿到最终素材，普丽娅今天下午会分享更新后的预算。"
    cues = subtitles.build_cues(job_with(S(0, 9000, zh), lang="zh"))
    assert len(cues) >= 2 and all(len(l) <= 20 for _, _, t in cues for l in t.split("\n"))
    assert "".join(t.replace("\n", "") for _, _, t in cues) == zh


def test_the_srt_and_vtt_files_carry_the_same_captions():
    job = job_with(S(0, 12000, LONG))
    srt, vtt = subtitles.to_srt(job), subtitles.to_vtt(job)
    cues = subtitles.build_cues(job)
    assert srt.startswith("1\n00:00:00,000 --> ") and vtt.startswith("WEBVTT\n\n00:00:00.000 --> ")
    assert len(re.findall(r"^\d+$", srt, re.M)) == len(cues) and vtt.count("-->") == len(cues)
    for _, _, text in cues:
        assert text in srt and text in vtt


def test_burned_in_captions_sit_lower_centre_away_from_the_bottom_edge(monkeypatch, tmp_path):
    from webapp import app as app_mod
    seen = {}
    monkeypatch.setattr(app_mod.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd))
    app_mod.render_burned_captions(tmp_path / "v.mp4", tmp_path / "s.srt", tmp_path / "o.mp4")
    vf = seen["cmd"][seen["cmd"].index("-vf") + 1]
    assert "force_style=" in vf and "Alignment=2" in vf and re.search(r"MarginV=(2\d|3\d|4\d)", vf)


def test_the_order_page_paints_live_captions_from_the_same_cues(env, wav_bytes):
    from test_media_preview import _finished
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'id="order-cues"' in page and "function paintCue" in page
    cues = __import__("json").loads(re.search(r'id="order-cues">(.*?)</script>', page, re.S).group(1))
    assert cues and all({"s", "e", "t"} <= set(c) and c["e"] - c["s"] <= 6000 for c in cues)
    assert "dataset.targetText) { captionOverlay" not in page            # the old per-segment overlay is gone


from test_order_flow import env, wav_bytes  # noqa: E402,F401



# ------------------------------------------------ the editor decides when a caption drops
def _cues(segments, duration=0, source=False):
    from kauli.models import Job
    from kauli.subtitles import build_cues
    return build_cues(Job(segments=segments, source_duration_ms=duration), source=source)


def _seg(i, start, end, text, **kw):
    from kauli.models import Segment
    return Segment(segment_id=f"s{i}", index=i, start_ms=start, end_ms=end, source_transcript=text, spoken=text, **kw)


def test_a_sentence_the_editor_ended_drops_with_its_speech():
    (start, end, _), = _cues([_seg(0, 0, 2000, "Thanks for joining us today.")])
    assert (start, end) == (0, 2000)                                                       # a full stop: gone when the speech is


def test_a_caption_the_editor_has_not_ended_stays_its_full_six_seconds():
    (start, end, _), = _cues([_seg(0, 0, 2000, "and the last word is")], duration=60000)
    assert end - start == 6000                                                             # no sentence end: not dropped early


def test_a_sound_tag_drops_after_six_seconds_not_after_the_whole_silence():
    cues = _cues([_seg(0, 0, 3000, "Welcome."), _seg(1, 3000, 33000, "[MUSIC PLAYING]", segment_type="gap")], duration=33000)
    tag = [c for c in cues if c[2] == "[MUSIC PLAYING]"][0]
    assert tag[0] == 3000 and tag[1] - tag[0] == 6000


def test_the_last_words_before_a_long_silence_do_not_stay_for_the_whole_silence():
    # the video runs five minutes, the talking stops at 4:30: the last caption goes after six seconds, not at 5:00
    (start, end, _), = _cues([_seg(0, 266000, 270000, "and that is how it ends")], duration=300000)
    assert end - start == 6000 and end < 300000


def test_a_paragraph_break_closes_the_caption_before_it():
    cues = _cues([_seg(0, 0, 2000, "and we are done with this part"), _seg(1, 2500, 8000, "Next part begins here.", para_start_target=True)], duration=9000)
    assert cues[0][1] == 2000 + 0 or cues[0][1] <= 2500                                    # closed: it goes with its speech (or the reading minimum), not 6 s
    assert cues[0][1] - cues[0][0] < 6000
    unbroken = _cues([_seg(0, 0, 2000, "and we are done with this part"), _seg(1, 9000, 12000, "Next part begins here.")], duration=13000)
    assert unbroken[0][1] - unbroken[0][0] == 6000                                         # the same text with no break: stays its full time


def test_an_unclosed_caption_is_still_cut_by_the_next_caption_and_by_the_end_of_the_media():
    cues = _cues([_seg(0, 0, 1500, "and then we"), _seg(1, 2000, 4000, "went home.")], duration=4000)
    assert cues[0][1] <= 2000                                                              # the next caption starts at 2 s: nothing overlaps
    (start, end, _), = _cues([_seg(0, 58000, 59000, "the very last words")], duration=60000)
    assert end <= 60000                                                                    # never past the end of the media


def test_which_endings_close_a_caption():
    from kauli.subtitles import closes_caption
    for closed in ("It works.", "Does it?", "Stop!", "He said \"yes.\"", "<i>Really.</i>", "Sawa。", "[Applause.]"):
        assert closes_caption(closed), closed
    for open_ in ("and then", "[MUSIC PLAYING]", "Hello,", "", "one two three"):
        assert not closes_caption(open_), open_


# ------------------------------------------------ every second of audio is a cell
def test_a_silence_is_cut_into_one_second_cells_and_a_remainder_is_kept():
    from kauli.pipeline import split_gap_span, _insert_non_speech_segments
    from kauli.models import Segment
    assert split_gap_span(0, 5500) == [(0, 1000), (1000, 2000), (2000, 3000), (3000, 4000), (4000, 5500)]
    assert split_gap_span(0, 1000) == [(0, 1000)] and split_gap_span(0, 1999) == [(0, 1999)]
    one = Segment(segment_id="s", index=0, start_ms=3000, end_ms=6000, source_transcript="hi")
    filled = _insert_non_speech_segments([one], 36000)                                     # 3 s before, 3 s of speech, 30 s after
    gaps = [g for g in filled if g.segment_type == "gap"]
    assert len(gaps) == 3 + 30 and all(g.end_ms - g.start_ms >= 1000 for g in gaps)
    assert [g.index for g in filled] == list(range(len(filled)))


def test_a_very_long_silence_is_capped_so_the_editor_page_stays_light():
    from kauli.pipeline import split_gap_span, MAX_GAP_CELLS
    cells = split_gap_span(0, 3_600_000)                                                   # an hour of nothing
    assert len(cells) == MAX_GAP_CELLS and cells[-1][1] == 3_600_000 and cells[0] == (0, 1000)


def test_an_order_made_before_this_gets_its_long_silences_cut_when_the_editor_opens(env, wav_bytes):
    from kauli.models import Job, Segment
    job = Job(segments=[Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript="Hello."),
                        Segment(segment_id="g0", index=1, start_ms=3000, end_ms=8000, segment_type="gap", source_edited_transcript="[MUSIC PLAYING]"),
                        Segment(segment_id="g1", index=2, start_ms=8000, end_ms=14000, segment_type="gap", audio_path="/rendered/bed.wav")],
              source_duration_ms=14000)
    assert env.app._split_long_gaps(job) is True
    gaps = [s for s in job.segments if s.segment_type == "gap"]
    assert [(g.start_ms, g.end_ms) for g in gaps if g.segment_id != "g1"] == [(3000, 4000), (4000, 5000), (5000, 6000), (6000, 7000), (7000, 8000)]
    assert gaps[0].segment_id == "g0" and gaps[0].source_final_text == "[MUSIC PLAYING]"        # the tag stays on the first second
    assert [s.segment_id for s in job.segments if s.segment_id == "g1"] == ["g1"] and job.segments[-1].end_ms == 14000    # rendered audio: left whole
    assert [s.index for s in job.segments] == list(range(len(job.segments)))
    assert env.app._split_long_gaps(job) is False                                          # nothing more to cut the second time
