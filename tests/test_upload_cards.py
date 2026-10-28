"""The order wizard's add-your-media step: two cards (upload / YouTube link), with every id the
wizard script depends on still present, and the source state rendered on the server."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)


def test_step_one_shows_two_cards_with_the_ids_the_script_needs(env):
    html = env.client.get("/client").text
    for needed in ('id="card-file"', 'id="card-youtube"', 'id="source-switch"', 'id="dropzone"', 'id="dropzone-text"',
                   'id="file-input"', 'id="youtube-url-input"', 'id="youtube-go"', 'id="source-file"', 'id="source-youtube"',
                   'data-source="file"', 'data-source="youtube"', 'name="audio"', 'name="youtube_url"'):
        assert needed in html, needed
    assert "Upload audio or video" in html and "Paste a YouTube link" in html
    for chip in ("MP3", "WAV", "M4A", "FLAC", "MP4", "MOV", "MKV", "Transcript", "Subtitles", "Translation", "Dubbing"):
        assert f"<li>{chip}</li>" in html
    # both sources are on screen at once: neither pane is hidden any more
    assert not re.search(r'id="source-(file|youtube)"[^>]*hidden', html)
    assert 'id="source-switch" hidden' in html                              # the switch buttons still exist, unseen
    assert "Up to 2 GB per file" in html                                    # the size limit is still stated


def test_the_file_card_is_active_by_default(env):
    html = env.client.get("/client").text
    assert re.search(r'class="src-card is-active" id="card-file"', html)
    assert re.search(r'class="src-card " id="card-youtube"', html)


def test_a_failed_youtube_fetch_comes_back_with_the_link_kept_and_that_card_active(env, wav_bytes):
    def boom(url, out_dir):
        raise RuntimeError("nope")
    env.monkeypatch.setattr(env.app, "_download_youtube", boom)
    r = env.client.post("/client/orders", data={"source_lang": "en", "target_lang": "en", "service_level": "transcribe",
                                                 "review_tier": "ai", "youtube_url": "https://youtu.be/abc123XYZ00"})
    assert r.status_code == 200 and "Couldn" in r.text
    assert 'value="https://youtu.be/abc123XYZ00"' in r.text
    assert re.search(r'class="src-card is-active" id="card-youtube"', r.text)
    assert re.search(r'class="src-card " id="card-file"', r.text)
    assert re.search(r'data-source="youtube"', r.text) and 'class="active" data-source="youtube"' in r.text


def test_the_wizard_script_drives_the_cards(env):
    js = (Path(__file__).parent.parent / "webapp" / "static" / "client_wizard.js").read_text(encoding="utf-8")
    assert "setSource" in js and "card-youtube" in js and "youtube-go" in js
    assert "pane.hidden" not in js and "filePane" not in js and "ytPane" not in js    # the old show/hide is gone
