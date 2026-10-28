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
    assert demo_content.MUSIC_BEDS == {"video": "bright", "course": "calm"}
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


def test_the_video_is_only_claimed_as_ai_and_illustrated():
    js = (Path(__file__).parent.parent / "webapp/static/hear_demo.js").read_text(encoding="utf-8")
    assert "An illustrated speaker, an AI voice (ElevenLabs)" in js and "burned into the picture" in js
