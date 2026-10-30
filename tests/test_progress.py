"""Progress you can see, and pages that update in place instead of reloading."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _order(env, wav_bytes, status="ready_for_delivery"):
    o = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, o["id"]))
    conn.commit()
    conn.close()
    return env.db.get_order(o["id"])


def test_every_page_gets_the_busy_button_and_the_in_place_refresh(env):
    page = env.client.get("/login").text
    assert "/static/busy.js" in page and "/static/softswap.js" in page
    for name in ("busy.js", "softswap.js"):
        assert env.client.get(f"/static/{name}").status_code == 200


def test_a_captioned_video_in_the_making_reports_a_real_percentage(env, wav_bytes):
    order = _order(env, wav_bytes)
    out = env.app._captioned_path(order, "classic")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".rendering").write_text("x")
    out.with_suffix(".progress").write_text("42")
    r = env.client.get(f"/client/orders/{order['id']}/captioned-video/status?style=classic")
    assert r.json() == {"state": "rendering", "pct": 42}
    out.with_suffix(".progress").unlink()
    assert env.client.get(f"/client/orders/{order['id']}/captioned-video/status?style=classic").json() == {"state": "rendering", "pct": None}
    out.write_bytes(b"video")
    assert env.client.get(f"/client/orders/{order['id']}/captioned-video/status?style=classic").json() == {"state": "ready", "pct": 100}


def test_the_status_of_someone_elses_order_is_not_shown(env, wav_bytes):
    order = _order(env, wav_bytes)
    env.db.get_or_create_user("other", "other@example.com", "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET client_id = 'other' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    assert env.client.get(f"/client/orders/{order['id']}/captioned-video/status?style=classic").status_code == 404
    assert env.client.get(f"/client/orders/{order['id']}/captioned-video/status?style=nope").status_code == 404


def test_a_processing_order_shows_a_spinner_and_updates_without_reloading(env, wav_bytes, monkeypatch):
    order = _order(env, wav_bytes, "processing")
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "kpSoftRefresh" in page and "live-progress" in page
    assert "Refresh this page" not in page
    assert "setTimeout(function () { location.reload(); }, 600)" not in page


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed for the real render")
def test_the_render_reports_how_far_it_has_got(env, tmp_path):
    clip, ass, out = tmp_path / "in.mp4", tmp_path / "c.ass", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=4", "-f", "lavfi", "-i", "sine=duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
    ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
                   "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                   "Style: Default,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n\n[Events]\n"
                   "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,Hello\n", encoding="utf-8")
    seen = []
    env.app.render_styled_captions(clip, ass, out, progress=seen.append)
    assert out.exists() and seen and all(0 <= p <= 99 for p in seen) and seen == sorted(seen)


def test_signed_in_pages_carry_the_saved_work_scripts_and_the_wizard_uses_them(env):
    page = env.client.get("/client").text
    assert 'name="kp-user"' in page and "/static/persist.js" in page
    assert 'name="kp-user"' not in env.client.get("/login").text                                # nothing is kept for people who are signed out
    wizard = env.client.get("/static/client_wizard.js").text
    for hook in ("kpUploads", "kpFiles", "kpDrafts.hooks", "attachToRunningUpload", "stillHere(form)"):
        assert hook in wizard, hook
    persist = env.client.get("/static/persist.js").text
    assert "password|hidden|file" in persist and "kp:softnav" in persist                       # secrets are never saved
