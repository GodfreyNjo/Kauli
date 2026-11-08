"""Captioned video in a chosen look, and the extra caption formats, on a finished order."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _job():
    segs = [SimpleNamespace(final_text="Hello there, my friend.", source_final_text="Habari rafiki yangu.", start_ms=500, end_ms=2500, speaker_id=None, segment_type="speech")]
    return SimpleNamespace(segments=segs)


def _ready_order(env, wav_bytes, video=True, entitled=True):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level="translate", review_tier="ai"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    env.db.set_order_free_preview(order["id"], False)
    if entitled:
        conn = env.db.get_conn()
        conn.execute("UPDATE orders SET addons = ? WHERE id = ?", (json.dumps(["video_deliverables"]), order["id"]))
        conn.commit()
        conn.close()
    env.monkeypatch.setattr(env.app, "_plan_chosen", lambda client_id: True)
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: _job())
    env.monkeypatch.setattr(env.app, "is_video_file", lambda p: video)
    return env.db.get_order(order["id"])


def test_ttml_and_sbv_download_as_translated_captions(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    ttml = env.client.get(f"/account/orders/{order['id']}/download/ttml")
    assert ttml.status_code == 200 and "application/ttml+xml" in ttml.headers["content-type"] and ".ttml" in ttml.headers["content-disposition"]
    assert 'xml:lang="en"' in ttml.text and "Hello there, my friend." in ttml.text
    sbv = env.client.get(f"/account/orders/{order['id']}/download/sbv")
    assert sbv.status_code == 200 and sbv.text.startswith("0:00:00.500,") and ".sbv" in sbv.headers["content-disposition"]


def test_a_transcription_only_order_gets_its_transcript_as_ttml(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="sw", service_level="transcribe", review_tier="ai"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    env.db.set_order_free_preview(order["id"], False)
    env.monkeypatch.setattr(env.app, "_plan_chosen", lambda client_id: True)
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: _job())
    ttml = env.client.get(f"/account/orders/{order['id']}/download/ttml").text
    assert 'xml:lang="sw"' in ttml and "Habari rafiki yangu." in ttml and "Hello there" not in ttml


def test_creating_a_look_starts_one_background_render_and_the_file_downloads(env, wav_bytes, monkeypatch):
    order = _ready_order(env, wav_bytes)
    made = []

    def fake_render(o, style):
        made.append(style)
        env.app._captioned_path(o, style).write_bytes(b"mp4")
        env.app._captioned_path(o, style).with_suffix(".rendering").unlink(missing_ok=True)
    monkeypatch.setattr(env.app, "_render_captioned_video", fake_render)
    r = env.client.post(f"/account/orders/{order['id']}/captioned-video", data={"style": "social"}, follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"] and "#captioned-video" in r.headers["location"]
    for _ in range(40):
        if made:
            break
        time.sleep(0.05)
    assert made == ["social"]
    assert env.app._captioned_state(order, "social") == "ready" and env.app._captioned_state(order, "box") is None
    got = env.client.get(f"/account/orders/{order['id']}/download/captioned_social")
    assert got.status_code == 200 and got.content == b"mp4" and "captioned_social_en.mp4" in got.headers["content-disposition"]
    again = env.client.post(f"/account/orders/{order['id']}/captioned-video", data={"style": "social"}, follow_redirects=False)
    assert "already+ready" in again.headers["location"] and made == ["social"]               # no second render


def test_the_page_lists_each_look_with_its_state(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    env.app._captioned_path(order, "box").write_bytes(b"mp4")
    marker = env.app._captioned_path(order, "social").with_suffix(".rendering")
    marker.write_text("x")
    html = env.client.get(f"/account/orders/{order['id']}").text
    assert 'id="captioned-video"' in html and "More caption formats" in html
    assert 'data-captioned="ready"' in html and 'data-captioned="rendering"' in html and "Creating your video" in html
    assert f"/download/captioned_box" in html and "words light up as they are spoken" in html
    again = env.client.post(f"/account/orders/{order['id']}/captioned-video", data={"style": "social"}, follow_redirects=False)
    assert "already+being+made" in again.headers["location"]


def test_refusals_leave_nothing_behind(env, wav_bytes, monkeypatch):
    started = []
    monkeypatch.setattr(env.app, "_render_captioned_video", lambda o, s: started.append(s))
    plain = _ready_order(env, wav_bytes, entitled=False)
    r = env.client.post(f"/account/orders/{plain['id']}/captioned-video", data={"style": "classic"}, follow_redirects=False)
    assert "upgrade_for=video" in r.headers["location"]
    assert "upgrade_for=video" in env.client.get(f"/account/orders/{plain['id']}/download/captioned_classic", follow_redirects=False).headers["location"]
    html = env.client.get(f"/account/orders/{plain['id']}").text
    assert "Captioned videos come with Premium and up" in html
    entitled = _ready_order(env, wav_bytes)
    assert "error=" in env.client.post(f"/account/orders/{entitled['id']}/captioned-video", data={"style": "neon"}, follow_redirects=False).headers["location"]
    env.monkeypatch.setattr(env.app, "is_video_file", lambda p: False)
    audio = env.client.post(f"/account/orders/{entitled['id']}/captioned-video", data={"style": "classic"}, follow_redirects=False)
    assert "audio+only" in audio.headers["location"]
    assert started == [] and env.app._captioned_state(entitled, "classic") is None
    assert env.client.get(f"/account/orders/{entitled['id']}/download/captioned_classic").status_code == 404


def test_a_stale_render_marker_from_a_restart_does_not_block_a_retry(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    marker = env.app._captioned_path(order, "classic").with_suffix(".rendering")
    marker.write_text("x")
    old = time.time() - 3 * 3600
    import os
    os.utime(marker, (old, old))
    assert env.app._captioned_state(order, "classic") == "failed" and not marker.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed for the real render")
def test_a_real_render_produces_a_playable_video_with_the_chosen_look(env, wav_bytes, tmp_path):
    clip = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3", "-f", "lavfi", "-i", "sine=duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
    order = _ready_order(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET audio_path = ? WHERE id = ?", (str(clip), order["id"]))
    conn.commit()
    conn.close()
    order = env.db.get_order(order["id"])
    env.app._render_captioned_video(order, "social")
    out = env.app._captioned_path(order, "social")
    assert out.exists() and env.app._captioned_state(order, "social") == "ready" and not out.with_suffix(".rendering").exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    assert "h264" in probe and "640,360" in probe and "aac" in probe
    assert "{\\k" in out.with_suffix(".ass").read_text(encoding="utf-8")                      # the social look is word by word
