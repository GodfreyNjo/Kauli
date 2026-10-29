"""In the demo, when something is translated or dubbed the speaker labels are translated too ("Speaker 1" -> "Hablante 1")."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from webapp import demo_content  # noqa: E402

ROOT = Path(__file__).parent.parent


def test_labels_read_in_each_language_and_english_is_unchanged():
    assert demo_content.label_in("Speaker 1", "en") == "Speaker 1" and demo_content.label_in("Narrator", None) == "Narrator"
    assert demo_content.label_in("Speaker 2", "es") == "Hablante 2"
    assert demo_content.label_in("Speaker 1", "de") == "Sprecher 1"
    assert demo_content.label_in("Speaker 1", "zh") == "说话人 1"
    assert demo_content.label_in("Speaker 2", "sw") == "Msemaji 2"
    assert demo_content.label_in("Narrator", "es") == "Narrador" and demo_content.label_in("Instructor", "sw") == "Mwalimu"


def test_every_name_in_every_cast_has_a_translation_in_every_demo_language():
    for lang in demo_content.LANGUAGES:
        for scenario in demo_content.SCENARIOS.values():
            for person in scenario["cast"]:
                assert demo_content.label_in(person["name"], lang) != person["name"], (lang, person["name"])


def test_the_page_hands_the_translated_labels_to_the_script():
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    page = TestClient(app_mod.app).get("/try").text
    data = json.loads(re.search(r'<script type="application/json" id="hd-data">(.*?)</script>', page, re.S).group(1))
    assert data["labels"]["es"]["Speaker 1"] == "Hablante 1" and data["labels"]["zh"]["Narrator"] == "旁白"
    js = (ROOT / "webapp/static/hear_demo.js").read_text(encoding="utf-8")
    assert "D.labels" in js


def test_the_videos_draw_the_translated_labels():
    src = (ROOT / "scripts/make_demo_videos.py").read_text(encoding="utf-8")
    assert "label_in(cast[who][\"name\"], lang)" in src and "label_in(person[\"name\"], caption_lang)" in src
