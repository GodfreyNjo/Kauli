"""Listenable blog posts: the script, the narration job, the audio file route, the podcast feed, the staff page, the player, and Resources."""
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import blog_audio  # noqa: E402

BODY = ('<p>Publishing a transcript helps readers.</p>'
        '<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>'
        '<li>A webhook sends your site a message when a transcript is ready.</li>'
        '<li>Zapier, Make and n8n can connect it without code.</li>'
        '<li>Choose human review for what you publish.</li></ul></div>'
        '<h2>What a webhook does</h2><p>It is a message one service sends to another. See <a href="https://example.com/docs">the docs</a>.</p>'
        '<pre>curl -X POST https://example.com</pre><ul><li>Step one</li><li>Step two</li></ul>')


def _mp3(seconds: float = 1.0) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-f", "mp3", "-"],
                          capture_output=True, check=True).stdout


def _post(env, slug="webhooks-guide", title="How to Publish Transcripts with Webhooks", published=True):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    pid = env.db.create_blog_post(slug=slug, title=title, description="Send every transcript to your website.", body_html=BODY,
                                  author_id="s1", status="published" if published else "draft", category="Transcription Guides")
    return env.db.get_blog_post(pid)


def _make(env, monkeypatch, post, mode="summary", calls=None):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(blog_audio, "remaining_credits", lambda: 100000)
    monkeypatch.setattr(blog_audio.order_ai_assist, "ai_assist_configured", lambda: False)

    def fake(text):
        if calls is not None:
            calls.append(text)
        return _mp3(1.5)
    monkeypatch.setattr(blog_audio, "tts", fake)
    conn = env.db.get_conn()
    conn.execute("INSERT OR REPLACE INTO blog_audio (post_id, mode, status, created_at, updated_at) VALUES (?, ?, 'queued', 1, 1)", (post["id"], mode))
    conn.commit()
    conn.close()
    blog_audio.make_episode(post["id"], mode)
    return env.db.get_conn().execute("SELECT * FROM blog_audio WHERE post_id = ?", (post["id"],)).fetchone()


def _as_staff(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


# ------------------------------------------------------------------------------------------------ the script
def test_the_summary_reads_the_key_takeaways_and_adds_only_the_intro_and_outro(env):
    post = _post(env)
    text = blog_audio.plain_script(post, "summary")
    assert text.startswith("How to Publish Transcripts with Webhooks. This is a Kauli guide.")
    assert "Number one. A webhook sends your site a message when a transcript is ready." in text
    assert "Number two. Zapier, Make and n8n can connect it without code." in text                    # names keep their capitals
    assert "Number three. Choose human review for what you publish." in text
    assert text.rstrip().endswith("kauli dash forge media dot com.")
    assert "Key takeaways" not in text.replace("Here are the key takeaways.", "")


def test_the_full_script_is_speakable_text_without_urls_code_or_labels(env):
    post = _post(env)
    text = blog_audio.plain_script(post, "full")
    assert "What a webhook does." in text and "It is a message one service sends to another." in text and "the docs" in text
    assert "https://" not in text and "curl" not in text and "Key takeaways" not in text and "<" not in text
    assert "Step one." in text and "Step two." in text


def test_claude_rewrites_for_the_ear_only_when_it_stays_faithful(env, monkeypatch):
    post = _post(env)
    monkeypatch.setattr(blog_audio.order_ai_assist, "ai_assist_configured", lambda: True)
    good = ("How to publish transcripts with webhooks. This is a Kauli guide. First, a webhook tells your site when a transcript is ready, "
            "second, tools like Zapier connect it without code, third, choose human review for what you publish. "
            "You can read the full guide at kauli dash forge media dot com. " * 2)

    class R:
        content = [type("B", (), {"type": "text", "text": good})()]

    class C:
        class messages:
            @staticmethod
            def create(**k):
                assert "never add a fact" in k["system"].lower()
                return R()
    monkeypatch.setattr(blog_audio.order_ai_assist, "_client", lambda: C())
    script, how = blog_audio.ear_script(post, "summary")
    assert how == "claude" and script.startswith("How to publish transcripts")
    R.content = [type("B", (), {"type": "text", "text": "Too short."})()]
    assert blog_audio.ear_script(post, "summary")[1] == "plain"                       # a doubtful rewrite is not used

    class Boom:
        class messages:
            @staticmethod
            def create(**k):
                raise RuntimeError("down")
    monkeypatch.setattr(blog_audio.order_ai_assist, "_client", lambda: Boom())
    assert blog_audio.ear_script(post, "summary")[1] == "plain"


def test_long_text_is_split_into_paragraph_sized_pieces_under_the_limit():
    paras = ["Sentence number %d is here. " % i * 30 for i in range(12)]
    pieces = blog_audio.chunks("\n\n".join(paras), limit=2000)
    assert all(len(p) <= 2000 for p in pieces) and len(pieces) > 3
    assert "".join(pieces).replace("\n", "").replace(" ", "") == "".join(paras).replace(" ", "")


# ------------------------------------------------------------------------------------------------ the job
def test_an_episode_is_narrated_joined_levelled_and_recorded(env, monkeypatch):
    post = _post(env)
    calls = []
    row = _make(env, monkeypatch, post, "summary", calls)
    assert row["status"] == "ready" and row["audio_file"] == "webhooks-guide-summary.mp3" and row["script_by"] == "plain"
    f = blog_audio.audio_dir() / row["audio_file"]
    assert f.exists() and row["bytes"] == f.stat().st_size > 1000 and 1.0 < row["duration_s"] < 4.0
    assert len(calls) == 1 and calls[0].startswith("How to Publish Transcripts") and row["chars"] == len(calls[0])
    assert blog_audio.ready_for(post["id"]) is not None


def test_nothing_is_spent_when_the_credits_are_short_or_unknown(env, monkeypatch):
    post = _post(env)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(blog_audio.order_ai_assist, "ai_assist_configured", lambda: False)
    spent = []
    monkeypatch.setattr(blog_audio, "tts", lambda t: spent.append(t) or _mp3())
    monkeypatch.setattr(blog_audio, "remaining_credits", lambda: 10)
    try:
        blog_audio.make_episode(post["id"], "full")
    except RuntimeError as exc:
        assert "only 10 are left" in str(exc) and "Nothing was spent" in str(exc)
    else:
        raise AssertionError("should have refused")
    monkeypatch.setattr(blog_audio, "remaining_credits", lambda: None)
    try:
        blog_audio.make_episode(post["id"], "full")
    except RuntimeError as exc:
        assert "balance" in str(exc)
    else:
        raise AssertionError("should have refused")
    assert spent == []
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    try:
        blog_audio.make_episode(post["id"], "full")
    except RuntimeError as exc:
        assert "not set up" in str(exc)


def test_the_queue_records_failures_and_a_restart_marks_stuck_jobs(env, monkeypatch):
    post = _post(env)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(blog_audio, "remaining_credits", lambda: 10)
    blog_audio._queue.clear()
    assert blog_audio.enqueue([post["id"]], "summary") == 1
    import time
    for _ in range(100):
        row = blog_audio.get_row(post["id"])
        if row["status"] == "failed":
            break
        time.sleep(0.1)
    assert row["status"] == "failed" and "credits" in row["error"]
    assert blog_audio.enqueue(["no-such-post"], "summary") == 0
    conn = env.db.get_conn()
    conn.execute("UPDATE blog_audio SET status = 'voicing' WHERE post_id = ?", (post["id"],))
    conn.commit()
    conn.close()
    blog_audio.mark_stale_jobs_failed()
    assert "restart" in blog_audio.get_row(post["id"])["error"]


# ------------------------------------------------------------------------------------------------ the public side
def test_the_audio_file_is_served_only_for_a_published_post_with_ready_audio(env, monkeypatch):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    post = _post(env)
    _make(env, monkeypatch, post)
    r = env.client.get("/blog-audio/webhooks-guide-summary.mp3")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and len(r.content) > 1000
    part = env.client.get("/blog-audio/webhooks-guide-summary.mp3", headers={"range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert env.client.get("/blog-audio/webhooks-guide-full.mp3").status_code == 404           # a different file than the one made
    assert env.client.get("/blog-audio/nope-summary.mp3").status_code == 404
    assert env.client.get("/blog-audio/..%2Fkauli_demo.db").status_code == 404
    conn = env.db.get_conn()
    conn.execute("UPDATE blog_posts SET status = 'draft' WHERE id = ?", (post["id"],))
    conn.commit()
    conn.close()
    assert env.client.get("/blog-audio/webhooks-guide-summary.mp3").status_code == 404        # unpublished again: not served


def test_the_post_page_has_a_player_only_when_there_is_audio(env, monkeypatch):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    post = _post(env)
    assert 'id="blog-listen"' not in env.client.get("/blog/webhooks-guide").text
    _make(env, monkeypatch, post)
    page = env.client.get("/blog/webhooks-guide").text
    assert 'id="blog-listen"' in page and 'src="/blog-audio/webhooks-guide-summary.mp3"' in page and "Narrated with an AI voice" in page
    assert '"@type": "AudioObject"' in page and "blog_audio_play" in page


def test_the_podcast_feed_lists_only_episodes_with_audio_and_follows_the_rules_of_the_apps(env, monkeypatch):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    a = _post(env)
    b = _post(env, slug="second", title="A second guide")
    _post(env, slug="no-audio", title="Has no audio")
    _make(env, monkeypatch, a)
    _make(env, monkeypatch, b)
    blog_audio._set(b["id"], in_podcast=0)
    r = env.client.get("/podcast.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    root = ET.fromstring(r.text)
    ch = root.find("channel")
    assert ch.find("title").text == "Kauli Guides" and ch.find("language").text == "en-us"
    assert ch.find("itunes:explicit", ns).text == "false" and ch.find("itunes:category", ns).get("text") == "Technology"
    assert ch.find("itunes:owner/itunes:email", ns).text == "hello@kauli-forgemedia.com"
    assert ch.find("itunes:image", ns).get("href").endswith("/static/podcast-cover.jpg")
    items = ch.findall("item")
    assert [i.find("title").text for i in items] == ["How to Publish Transcripts with Webhooks"]
    item = items[0]
    enc = item.find("enclosure")
    assert enc.get("type") == "audio/mpeg" and enc.get("url").endswith("/blog-audio/webhooks-guide-summary.mp3")
    assert int(enc.get("length")) == (blog_audio.audio_dir() / "webhooks-guide-summary.mp3").stat().st_size
    assert re.fullmatch(r"\d+:\d{2}", item.find("itunes:duration", ns).text) and "AI voice" in item.find("description").text
    assert item.find("guid").get("isPermaLink") == "false" and "GMT" in item.find("pubDate").text or "+0000" in item.find("pubDate").text
    assert 'rel="alternate" type="application/rss+xml" title="Kauli Guides (podcast)"' in env.client.get("/").text
    cover = env.client.get("/static/podcast-cover.jpg")
    assert cover.status_code == 200
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(cover.content)).size == (3000, 3000)


# ------------------------------------------------------------------------------------------------ staff
def test_only_staff_can_use_the_blog_audio_page_and_its_actions(env, monkeypatch):
    post = _post(env)
    r = env.client.get("/staff/blog-audio")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert env.client.post("/staff/blog-audio/make", data={"post_ids": post["id"], "mode": "summary"}).status_code == 303
    assert blog_audio.get_row(post["id"]) is None
    _as_staff(env)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(blog_audio, "remaining_credits", lambda: 12345)
    page = env.client.get("/staff/blog-audio").text
    assert "12,345" in page and "How to Publish Transcripts with Webhooks" in page and "Make audio for the ticked posts" in page
    assert "/podcast.xml" in page and 'href="/staff/blog-audio"' in page
    assert "Tick at least one" in env.client.get(env.client.post("/staff/blog-audio/make", data={"mode": "summary"}).headers["location"]).text
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert "not set up" in env.client.get(env.client.post("/staff/blog-audio/make", data={"post_ids": post["id"]}).headers["location"]).text


def test_staff_can_take_an_episode_out_of_the_podcast_and_delete_it(env, monkeypatch):
    post = _post(env)
    _make(env, monkeypatch, post)
    _as_staff(env)
    env.client.post(f"/staff/blog-audio/{post['id']}/podcast", data={"include": "0"})
    assert blog_audio.get_row(post["id"])["in_podcast"] == 0
    assert "<item>" not in env.client.get("/podcast.xml").text
    f = blog_audio.audio_dir() / "webhooks-guide-summary.mp3"
    assert f.exists()
    env.client.post(f"/staff/blog-audio/{post['id']}/delete")
    assert not f.exists() and blog_audio.get_row(post["id"]) is None


# ------------------------------------------------------------------------------------------------ Resources
def test_resources_is_inside_the_account_with_the_promo_and_guides_you_can_hear(env, monkeypatch):
    post = _post(env)
    _make(env, monkeypatch, post)
    page = env.client.get("/client/resources").text
    assert "Resources" in page and 'data-video-id="i6R3flz8V1I"' in page and "/static/promo-thumbnail.jpg" in page
    assert "<iframe" not in page.split("<script>")[0] and "youtube-nocookie.com/embed/" in page
    assert "How to Publish Transcripts with Webhooks" in page and "/blog-audio/webhooks-guide-summary.mp3" in page and "/podcast.xml" in page
    assert 'href="/help"' in page and 'href="/docs/api"' in page and 'href="/client/resources"' in page


def test_the_promo_is_not_on_any_public_page(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for path in ("/", "/blog", "/solutions/call-centers", "/try"):
        html = env.client.get(path).text
        assert "i6R3flz8V1I" not in html and "promo-video" not in html, path
