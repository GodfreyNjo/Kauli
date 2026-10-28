"""Every finished order has something to play next to its transcript, with captions that follow along."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes, _run_worker  # noqa: E402,F401


def _finished(env, wav, **place):
    order = env.order_from(env.place(wav, **place))
    _run_worker(env, order)
    # a real manifest with two speech segments so the transcript panel renders
    from kauli.models import Job, Segment
    outdir = Path(order["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    job = Job(source_path=order["audio_path"], source_language="en", target_language="en", providers={})
    job.segments = [Segment(segment_id="s1", index=0, start_ms=0, end_ms=2000, source_transcript="Hello there", spoken="Hello there"),
                    Segment(segment_id="s2", index=1, start_ms=2000, end_ms=4000, source_transcript="Second line", spoken="Second line")]
    job.save(outdir / "manifest.json")
    return env.db.get_order(order["id"])


def test_an_audio_upload_plays_with_live_captions_and_a_transcript_that_follows(env, wav_bytes):
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'class="order-audio-stage"' in page and f'src="/client/orders/{order["id"]}/download/source"' in page
    assert 'id="order-caption-overlay"' in page and "Press play to preview your result" in page
    assert "highlight(media.currentTime * 1000)" in page and 'row.addEventListener("click"' in page
    import os
    if os.environ.get("ORDER_DUMP_PATH"):
        Path(os.environ["ORDER_DUMP_PATH"]).write_text(page, encoding="utf-8")


def test_the_original_file_is_served_inline_with_range_support(env, wav_bytes):
    order = _finished(env, wav_bytes)
    r = env.client.get(f"/client/orders/{order['id']}/download/source", headers={"range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"].startswith("audio")
    assert "attachment" not in r.headers.get("content-disposition", "")
    full = env.client.get(f"/client/orders/{order['id']}/download/source")
    assert full.status_code == 200 and full.content == wav_bytes


def test_a_video_upload_plays_as_video_with_subtitles_over_it(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    video = Path(order["audio_path"]).with_suffix(".mp4")
    video.write_bytes(wav_bytes[:2000])
    conn.execute("UPDATE orders SET audio_path = ? WHERE id = ?", (str(video), order["id"]))
    conn.commit()
    conn.close()
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert re.search(r'<video controls preload="metadata" class="order-preview-media" src="[^"]+/download/source"', page)
    assert 'id="order-caption-overlay"' in page and 'class="order-audio-stage"' not in page


def test_a_youtube_order_shows_the_video_with_captions_synced_through_the_player_api(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET source_youtube_id = 'dQw4w9WgXcQ' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'id="order-yt"' in page and "enablejsapi=1" in page and 'id="order-caption-overlay"' in page
    assert "onYouTubeIframeAPIReady" in page and "getCurrentTime" in page


def test_nothing_plays_and_nothing_breaks_when_the_original_is_gone(env, wav_bytes):
    order = _finished(env, wav_bytes)
    Path(order["audio_path"]).unlink()
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'class="order-audio-stage"' not in page and "Transcript &amp; translation" in page
    assert env.client.get(f"/client/orders/{order['id']}/download/source").status_code == 404
