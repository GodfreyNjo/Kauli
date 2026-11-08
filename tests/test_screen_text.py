"""Captions keep clear of text that is already on the screen: lower-thirds, speaker names, titles and graphics."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli import caption_formats, screen_text, subtitles  # noqa: E402
from test_captions import S, job_with  # noqa: E402
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed to make the test video")


# --------------------------------------------------------------------------------------------- the detector on plain numbers
def _series(n, base=0.08, noise=0.004):
    return [base + (noise if i % 2 else -noise) for i in range(n)]


def test_a_graphic_that_appears_and_goes_is_found_with_its_timing():
    zone, upper = _series(40), _series(40, 0.2)
    for i in range(10, 18):                                   # a lower-third from 5.0s to 9.0s (two samples a second)
        zone[i] += 0.22
    found = screen_text.ranges_from_measures(zone, upper)
    assert len(found) == 1
    start, end = found[0]
    assert 4000 <= start <= 6000 and 8000 <= end <= 9500


def test_a_scene_cut_is_not_a_graphic():
    zone, upper = _series(40), _series(40, 0.2)
    for i in range(20, 40):                                   # everything changes at once and stays changed
        zone[i] += 0.25
        upper[i] += 0.3
    assert screen_text.ranges_from_measures(zone, upper) == []


def test_a_one_frame_flicker_and_a_permanent_logo_are_ignored():
    zone, upper = _series(40, 0.3), _series(40, 0.2)          # a logo or a busy studio wall: high, but always there
    zone[12] += 0.3                                           # one odd frame
    assert screen_text.ranges_from_measures(zone, upper) == []


def test_two_graphics_a_moment_apart_become_one_stretch():
    zone, upper = _series(60), _series(60, 0.2)
    for i in list(range(10, 16)) + list(range(17, 24)):
        zone[i] += 0.2
    assert len(screen_text.ranges_from_measures(zone, upper)) == 1


# ----------------------------------------------------------------------------------------------------- a real video
def _make_video(path: Path, seconds=10, graphic=(3.0, 6.0), fps=10):
    from PIL import Image, ImageDraw, ImageFont
    import random
    w, h = 640, 360
    rnd = random.Random(4)
    background = Image.new("RGB", (w, h), (70, 90, 120))
    d = ImageDraw.Draw(background)
    for _ in range(40):                                         # a still, busy "studio wall"
        x, y = rnd.randrange(0, w - 60), rnd.randrange(0, h - 40)
        d.rectangle((x, y, x + rnd.randrange(20, 60), y + rnd.randrange(10, 40)), fill=tuple(rnd.randrange(40, 160) for _ in range(3)))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(path)], stdin=subprocess.PIPE)
    for i in range(seconds * fps):
        frame = background.copy()
        if graphic and graphic[0] <= i / fps < graphic[1]:
            g = ImageDraw.Draw(frame)
            g.rectangle((30, 250, 520, 330), fill=(250, 250, 250))
            g.text((46, 258), "Dr. Amina Odhiambo", fill=(10, 10, 10), font=font)
            g.text((46, 292), "Head of Clinical Research", fill=(10, 10, 10), font=font)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0


@needs_ffmpeg
def test_a_lower_third_in_a_real_video_is_found(tmp_path):
    video = tmp_path / "talk.mp4"
    _make_video(video, graphic=(3.0, 6.0))
    found = screen_text.detect(str(video))
    assert len(found) == 1
    start, end = found[0]
    assert start <= 4000 and end >= 5000 and (end - start) >= 2000                          # about 3s to 6s, give or take a second
    assert start >= 1500 and end <= 7500


@needs_ffmpeg
def test_a_video_with_nothing_on_screen_gives_no_ranges_and_the_answer_is_remembered(tmp_path):
    video = tmp_path / "plain.mp4"
    _make_video(video, graphic=None)
    assert screen_text.detect(str(video)) == []
    assert screen_text.analyse(str(video), tmp_path) == [] and screen_text.cached(tmp_path) == []        # looked at, nothing found
    assert screen_text.cached(tmp_path / "elsewhere") is None                                               # never looked at


# -------------------------------------------------------------------------------------------- which captions move, and how
def test_a_caption_moves_up_when_a_graphic_covers_a_third_of_it_or_more():
    ranges = [(4000, 8000)]
    assert screen_text.is_top(5000, 7000, ranges) and screen_text.is_top(3000, 5500, ranges)        # inside / overlapping by more than a third
    assert not screen_text.is_top(0, 3000, ranges) and not screen_text.is_top(8500, 10000, ranges)
    assert not screen_text.is_top(3000, 7000, [(6800, 9000)])                                       # a sliver does not move it
    assert not screen_text.is_top(0, 1000, None) and not screen_text.is_top(0, 1000, [])


def _job():
    job = job_with(S(0, 3000, "Welcome back to the show."), S(4500, 7500, "Tonight we cover the economy."), S(9000, 12000, "Thanks for watching."))
    job.top_ranges = [(4000, 8000)]
    return job


def test_webvtt_puts_those_captions_at_the_top_and_leaves_the_rest():
    vtt = subtitles.to_vtt(_job())
    top = [l for l in vtt.splitlines() if "-->" in l and "line:8%" in l]
    other = [l for l in vtt.splitlines() if "-->" in l and "line:" not in l]
    assert len(top) == 1 and "00:00:04.500" in top[0] and "position:50% align:center" in top[0]
    assert len(other) == 2


def test_without_any_graphics_the_files_are_exactly_as_before():
    job = job_with(S(0, 3000, "Welcome back."))
    assert "line:" not in subtitles.to_vtt(job) and "Top" not in caption_formats.to_ass(job) and "region" not in caption_formats.to_ttml(job)


def test_ttml_gets_a_top_region_and_ass_a_top_style():
    ttml = caption_formats.to_ttml(_job())
    assert 'xml:id="rTop"' in ttml and 'region="rTop"' in ttml and ttml.count('region="rBottom"') == 2
    ass = caption_formats.to_ass(_job(), "classic")
    assert "Style: Top," in ass and ass.count(",Top,,") == 1 and ass.count(",Default,,") == 2
    assert ",8," in ass.split("Style: Top,")[1].split("\n")[0]                                       # alignment 8 is top centre


def test_the_live_preview_marks_which_captions_to_lift():
    cues = subtitles.cue_dicts(_job())
    assert [c["top"] for c in cues] == [False, True, False]


# ---------------------------------------------------------------------------------------------------------- in the app
def test_the_order_page_hands_the_ranges_to_the_player(env, wav_bytes):
    order = _finished(env, wav_bytes)
    screen_text.save(order["outdir"], [(0, 2500)])
    page = env.client.get(f"/account/orders/{order['id']}").text
    cues = json.loads(page.split('id="order-cues">')[1].split("</script>")[0])
    assert cues and cues[0]["top"] is True and cues[1]["top"] is False
    assert 'classList.toggle("is-top"' in page
    css = env.client.get("/static/style.css").text
    assert ".order-media-wrap .order-caption-overlay.is-top" in css and "max(11%, 60px)" in css           # clear of the player's own controls too
    assert "bottom: 5%; transition: top" in css                                                      # and low, out of the picture, until the controls show


def test_a_customer_who_says_the_video_already_has_captions_gets_theirs_at_the_top_all_the_time(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert env.app._top_ranges(order) is None
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET instr_existing_subs = 'match_existing' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    assert env.app._top_ranges(env.db.get_order(order["id"])) == [(0, 10 ** 9)]


def test_the_worker_looks_at_videos_only_and_redoes_the_positioned_caption_file(env, wav_bytes, monkeypatch, tmp_path):
    from webapp import worker
    order = _finished(env, wav_bytes)
    started = []
    monkeypatch.setattr(screen_text, "analyse_in_background", lambda path, outdir, then=None: started.append((path, then)))
    worker._look_for_screen_text(order)
    assert started == []                                                                       # an audio upload has no picture to look at
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET audio_path = ? WHERE id = ?", (str(video), order["id"]))
    conn.commit()
    conn.close()
    vtt = Path(order["outdir"]) / f"subs_{order['target_lang']}.vtt"
    vtt.write_text("WEBVTT\n", encoding="utf-8")
    worker._look_for_screen_text(env.db.get_order(order["id"]))
    assert len(started) == 1
    started[0][1]([(0, 2500)])                                                                 # the analysis finishes: the file is redone
    assert "line:8%" in vtt.read_text(encoding="utf-8")


def test_the_captioned_video_job_finds_the_graphics_first(env, wav_bytes, monkeypatch, tmp_path):
    src = (Path(__file__).parent.parent / "webapp/app.py").read_text(encoding="utf-8")
    assert "job.top_ranges = screen_text.analyse(str(video_path), outdir) or None" in src
