"""The /try demo has real videos: an illustrated speaker, the ElevenLabs voice, burned-in captions, translated and dubbed
versions, and a music bed under some of them."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from webapp import demo_content  # noqa: E402

DEMO = Path(__file__).parent.parent / "webapp" / "static" / "demo"


def test_every_scenario_has_the_original_and_one_video_per_language():
    for key, s in demo_content.SCENARIOS.items():
        for lang in ["en", *s["tr"]]:
            f = DEMO / demo_content.video_name(key, lang)
            assert f.exists() and 30_000 < f.stat().st_size < 600_000, f.name           # a real, small, web-friendly file
            assert f.read_bytes()[4:8] == b"ftyp"                                       # an MP4 container


def test_the_music_beds_exist_for_the_demos_that_use_them_and_not_for_the_meeting():
    assert demo_content.MUSIC_BEDS == {"video": "bright", "course": "calm", "podcast": "calm"}
    for bed in demo_content.MUSIC_BEDS.values():
        assert (DEMO / f"bed-{bed}.mp3").stat().st_size > 100_000
    assert "meeting" not in demo_content.MUSIC_BEDS


def test_the_try_page_hands_the_videos_to_the_player_and_serves_them():
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    client = TestClient(app_mod.app)
    page = client.get("/try").text
    assert 'id="hd-video-box"' in page and 'id="hd-video"' in page and "Watch it as a video" in page
    data = __import__("json").loads(re.search(r'<script type="application/json" id="hd-data">(.*?)</script>', page, re.S).group(1))
    for key, s in data["scenarios"].items():
        assert set(s["video"]) == {"en", "es", "de", "zh", "sw"}, key
        assert s["music"] is (key in demo_content.MUSIC_BEDS)
        for url in s["video"].values():
            r = client.get(url)
            assert r.status_code == 200 and r.headers["content-type"].startswith("video/"), url


def test_the_demo_carries_no_small_print_under_the_player():
    js = (Path(__file__).parent.parent / "webapp/static/hear_demo.js").read_text(encoding="utf-8")
    page = (Path(__file__).parent.parent / "webapp/templates/_hear_demo.html").read_text(encoding="utf-8")
    assert "hd-video-note" not in js + page and "Sample audio is made with" not in page


# ElevenLabs' premade voices we use, and the gender each is described with there.
VOICE_GENDER = {
    "EXAVITQu4vr4xnSDxMaL": "f", "Xb7hH8MSUJpSbSDYk0k2": "f", "XrExE9yKIg1WjnnlVkGX": "f", "cgSgspJ2msm6clMCkdW9": "f",
    "hpp4J3VqNfWAUOO0d1Us": "f",
    "iP95p4xoKVk53GoZ742B": "m", "cjVigY5qzO86Huf0OWal": "m", "bIHbv24MWmeRgasZH58o": "m", "onwK4e9ZLuTAKqWW03F9": "m",
    "TX3LPaxmHKxFdv7VOQHJ": "m",
}


def _videos_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_demo_videos", Path(__file__).parent.parent / "scripts" / "make_demo_videos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_mans_voice_is_a_man_on_screen_and_a_womans_a_woman():
    looks = _videos_module().LOOKS
    for key, s in demo_content.SCENARIOS.items():
        for i, person in enumerate(s["cast"]):
            assert VOICE_GENDER[person["voice"]] == person["gender"], (key, person)         # the cast says what the voice is
            assert (key, i) in looks, (key, i)                                              # and every person has a look
            if person["gender"] == "f":
                assert looks[(key, i)]["style"] in ("long", "bob", "bun") and not looks[(key, i)].get("beard"), (key, i)
            else:
                assert looks[(key, i)]["style"] == "short" and not looks[(key, i)].get("headscarf"), (key, i)
    assert {(k, i) for (k, i) in looks} == {(k, i) for k, s in demo_content.SCENARIOS.items() for i in range(len(s["cast"]))}


def test_there_is_a_demo_for_every_use_case_and_every_page_points_at_a_real_one(env=None):
    from webapp import app as app_mod
    assert set(demo_content.SCENARIOS) == {"meeting", "call", "interview", "podcast", "course", "news", "field", "video"}
    for slug, page in app_mod.SOLUTION_PAGES.items():
        assert page["demo"] in demo_content.SCENARIOS, slug
    used = {p["demo"] for p in app_mod.SOLUTION_PAGES.values()}
    assert used == set(demo_content.SCENARIOS)                                              # nothing built that no page shows


def test_speakers_are_labelled_in_the_transcripts_and_timed_in_the_audio():
    import json
    timing = json.loads((DEMO / "timing.json").read_text(encoding="utf-8"))
    for name, scenario, lang, text in demo_content.all_clips():
        turns = demo_content.turns(scenario, text)
        cast = demo_content.SCENARIOS[scenario]["cast"]
        assert len(timing[name]) == len(turns), name                                        # one timed mark for every turn
        for (who, body), (mark_who, start, end) in zip(turns, timing[name]):
            assert who == mark_who and end > start and body and ":" not in body[:12], (name, body)
        if len(cast) > 1:
            assert all(text.split("\n")[i].startswith(cast[w]["name"] + ": ") for i, (w, _) in enumerate(turns)), name
    marks = timing["src-meeting.mp3"]
    assert [m[0] for m in marks] == [0, 1] and marks[1][1] > marks[0][2]                       # two speakers, a pause between them


def test_the_demo_page_hands_the_cast_and_timing_to_the_player():
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    client = TestClient(app_mod.app)
    page = client.get("/try?use=interview").text
    data = __import__("json").loads(re.search(r'<script type="application/json" id="hd-data">(.*?)</script>', page, re.S).group(1))
    interview = data["scenarios"]["interview"]
    assert interview["multi"] is True and interview["cast"] == ["Speaker 1", "Speaker 2"]
    assert data["scenarios"]["news"]["multi"] is False and "src-interview.mp3" in data["timing"]
    assert "Speaker 1: " in interview["en"] and "hd-spk" in client.get("/static/style.css").text
