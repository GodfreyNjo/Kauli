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
