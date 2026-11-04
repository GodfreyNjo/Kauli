"""The house model defaults and the staff switch for them, and the Podcast page: pulling feeds, playing episodes, and a transcript arriving by
Kauli's own signed webhook."""
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import RedirectResponse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import podcast, provider_health  # noqa: E402

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Kauli Guides</title>
<item><title>Episode 1: Check your numbers</title><guid>ep-1</guid><pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate>
<description>&lt;p&gt;A short guide to <b>numbers</b>.&lt;/p&gt;</description><itunes:duration>23:04</itunes:duration>
<enclosure url="https://cdn.example.com/ep1.mp3" length="1000" type="audio/mpeg"/></item>
<item><title>Episode 0: Welcome</title><guid>ep-0</guid><pubDate>Mon, 02 Nov 2026 10:00:00 +0000</pubDate><description>Hello.</description>
<enclosure url="http://insecure.example.com/ep0.mp3" length="1" type="audio/mpeg"/></item></channel></rss>"""


def _staff(env, user_id="s1"):
    env.db.get_or_create_user(user_id, "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user(user_id)), "client_scope_id": user_id})


# ------------------------------------------------ the house defaults and the switch
def test_transkriptor_and_azure_are_the_house_choice_until_staff_pick_something_else(env):
    assert provider_health.default_name("asr") == "transkriptor" and provider_health.default_name("mt") == "azure-translate" and provider_health.default_name("tts") == ""
    assert provider_health.is_house_default("asr") and not provider_health.is_house_default("tts")
    provider_health.set_default("asr", "", by="s1")                                           # staff chose Automatic on purpose: it stays Automatic
    assert provider_health.default_name("asr") == "" and not provider_health.is_house_default("asr")
    provider_health.set_default("mt", "claude", by="s1")
    assert provider_health.default_name("mt") == "claude"


def test_azure_takes_the_automatic_jobs_but_not_a_paid_upgrade_or_a_human_reviewed_job_unless_staff_chose_it(env, monkeypatch):
    monkeypatch.setattr(provider_health, "usable", lambda name: True)
    monkeypatch.setattr(provider_health, "is_paused", lambda name: False)
    assert env.app._pick_mt("en", "sw", "", "ai")[0] == "azure-translate"                       # an Instant AI job
    assert env.app._pick_mt("en", "sw", "claude", "ai")[0] != "azure-translate"                  # a Premium plan keeps its Claude translation
    assert env.app._pick_mt("en", "sw", "", "human")[0] != "azure-translate"                     # a human-reviewed job is routed as before
    provider_health.set_default("mt", "azure-translate", by="s1")                                # picked on purpose: for everything
    assert env.app._pick_mt("en", "sw", "claude", "ai")[0] == "azure-translate" and env.app._pick_mt("en", "sw", "", "human")[0] == "azure-translate"
    assert env.app._route_asr("transkriptor", source_lang="en", free_covered=True) == "transkriptor"


def test_staff_can_switch_models_on_the_models_and_review_tabs_and_come_back_to_the_same_tab(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))                                              # a customer places it first, then staff look
    _staff(env)
    page = env.client.get("/staff/models").text
    assert "Models in use" in page and 'name="next" value="/staff/models"' in page and "Azure Translator" in page and "(house choice)" in page
    r = env.client.post("/staff/providers/default", data={"kind": "mt", "name": "claude", "next": "/staff/models"}, follow_redirects=False)
    assert r.headers["location"].startswith("/staff/models?notice=") and provider_health.default_name("mt") == "claude"
    assert "(house choice)" in env.client.get("/staff/models").text                                # transcription still on the house choice
    review = env.client.get(f"/staff/orders/{order['id']}").text
    assert "Models in use" in review and f'name="next" value="/staff/orders/{order["id"]}"' in review
    for bad in ("https://evil.example.com/x", "//evil.example.com", "/client/home", "/staff/models\nx"):
        r = env.client.post("/staff/providers/default", data={"kind": "tts", "name": "piper", "next": bad}, follow_redirects=False)
        assert r.headers["location"].startswith("/staff/providers?"), bad                          # never sent anywhere but a staff page
    assert env.client.post("/staff/providers/default", data={"kind": "asr", "name": "nonsense", "next": "/staff/models"}, follow_redirects=False).headers["location"].startswith("/staff/models?error=")


# ------------------------------------------------ feeds, Spotify and the public page
def test_a_feed_is_read_into_episodes_and_unsafe_files_are_refused():
    title, eps = podcast.parse_feed(FEED)
    assert title == "Kauli Guides" and [e["title"] for e in eps] == ["Episode 1: Check your numbers", "Episode 0: Welcome"]
    assert eps[0]["audio_url"] == "https://cdn.example.com/ep1.mp3" and eps[0]["duration_s"] == 23 * 60 + 4 and "A short guide to numbers." == eps[0]["description"]
    assert eps[1]["audio_url"] == ""                                                             # only https audio is ever played
    for bad in (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>', b"<html></html>", b"not xml"):
        try:
            podcast.parse_feed(bad)
            raise AssertionError("accepted")
        except ValueError:
            pass
    assert podcast.spotify_ref("https://open.spotify.com/show/0mILTmuoriNngEix8XzWyf?si=abc") == ("show", "0mILTmuoriNngEix8XzWyf")
    assert podcast.spotify_ref("https://open.spotify.com.evil.com/show/0mILTmuoriNngEix8XzWyf") is None and podcast.spotify_ref("http://open.spotify.com/show/0mILTmuoriNngEix8XzWyf") is None


def test_the_podcast_page_plays_pulled_and_spotify_episodes_and_shows_transcripts(env, monkeypatch):
    _staff(env)
    monkeypatch.setattr(podcast, "fetch_feed", lambda url: FEED)
    r = env.client.post("/staff/podcast/shows", data={"kind": "feed", "url": "https://cdn.example.com/feed.xml"}, follow_redirects=False)
    assert "notice=" in r.headers["location"]
    assert "already+added" in env.client.post("/staff/podcast/shows", data={"kind": "feed", "url": "https://cdn.example.com/feed.xml"}, follow_redirects=False).headers["location"].replace("%20", "+")
    assert "error=" in env.client.post("/staff/podcast/shows", data={"kind": "spotify", "url": "https://example.com/x"}, follow_redirects=False).headers["location"]
    env.client.post("/staff/podcast/shows", data={"kind": "spotify", "url": "https://open.spotify.com/show/0mILTmuoriNngEix8XzWyf", "title": "Kauli on Spotify"})
    env.client.post("/staff/podcast/shows", data={"kind": "spotify", "url": "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk", "title": "A Spotify-only episode"})
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)                         # now as a visitor
    page = env.client.get("/podcast").text
    assert "Episode 1: Check your numbers" in page and 'src="https://cdn.example.com/ep1.mp3"' in page and "23 min" in page
    assert "http://insecure.example.com" not in page                                                # no unsafe audio is offered
    assert "https://open.spotify.com/embed/show/0mILTmuoriNngEix8XzWyf" in page and "https://open.spotify.com/embed/episode/4rOoJ6Egrf8K2IrywzwOMk" in page
    assert 'href="/podcast"' in page and 'href="/podcast.xml"' in page                              # Resources menu and the feed
    ep = next(e for e in podcast.list_episodes() if e["title"].startswith("Episode 1"))
    _staff(env)
    env.client.post(f"/staff/podcast/episodes/{ep['id']}/hide", data={"hidden": "1"})
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert "Episode 1: Check your numbers" not in env.client.get("/podcast").text and env.client.get(f"/podcast/{ep['id']}").status_code == 404
    sitemap = env.client.get("/sitemap.xml").text
    assert "/podcast</loc>" in sitemap and f"/podcast/{ep['id']}" not in sitemap


# ------------------------------------------------ a transcript arrives by Kauli's own webhook
def _setup_publisher(env, monkeypatch):
    email = env.db.get_user("u1")["email"]
    monkeypatch.setenv("PODCAST_PUBLISHER_EMAIL", email)
    monkeypatch.setenv("KAULI_PUBLIC_BASE_URL", "https://kauli.test")
    monkeypatch.setattr(podcast, "fetch_feed", lambda url: FEED)
    _staff(env)
    env.client.post("/staff/podcast/shows", data={"kind": "feed", "url": "https://cdn.example.com/feed.xml"})
    return next(e for e in podcast.list_episodes() if e["title"].startswith("Episode 1"))


def _post_hook(env, order_id, secret, link="https://kauli.test/download/abc/txt?sig=1", event="order.ready", age=0, signer=None):
    body = json.dumps({"event": event, "order": {"id": order_id, "deliverables": {"txt": link}}}).encode()
    stamp = str(int(time.time()) - age)
    sig = "sha256=" + hmac.new((signer or secret).encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return env.client.post("/webhooks/podcast-transcripts", content=body, headers={"X-Kauli-Timestamp": stamp, "X-Kauli-Signature": sig, "Content-Type": "application/json"})


def test_a_transcript_is_ordered_and_arrives_on_the_episode_through_the_signed_webhook(env, wav_bytes, monkeypatch):
    order = env.order_from(env.place(wav_bytes))
    ep = _setup_publisher(env, monkeypatch)
    page = env.client.get("/staff/podcast").text
    assert "not connected yet" in page and "Make transcript" in page
    assert "Connect the webhook first" in env.client.post(f"/staff/podcast/episodes/{ep['id']}/transcribe", data={"source_lang": "en"}, follow_redirects=False).headers["location"].replace("%20", " ").replace("+", " ")
    env.client.post("/staff/podcast/webhook/connect")
    hook = env.db.get_client_webhook("u1")
    assert hook["url"] == "https://kauli.test/webhooks/podcast-transcripts" and "connected</strong>" in env.client.get("/staff/podcast").text
    monkeypatch.setattr(podcast, "_download", lambda url, dest: dest.write_bytes(wav_bytes) or len(wav_bytes))
    seen = {}
    monkeypatch.setattr(env.app, "create_order", lambda request, **kw: seen.update(kw, who=request.state.api_user["id"]) or RedirectResponse(f"/client/orders/{order['id']}", status_code=303))
    r = env.client.post(f"/staff/podcast/episodes/{ep['id']}/transcribe", data={"source_lang": "en"}, follow_redirects=False)
    assert "notice=" in r.headers["location"] and seen["who"] == "u1" and seen["idempotency_key"] == f"podcast-{ep['id']}" and seen["review_tier"] == "ai"
    assert podcast.get_episode(ep["id"])["transcript_status"] == "working" and podcast.get_episode(ep["id"])["transcript_order_id"] == order["id"]

    fetched = []
    monkeypatch.setattr(podcast.httpx, "get", lambda url, **k: fetched.append(url) or SimpleNamespace(text="AMARA: Welcome.\r\n\r\nDAN: Always check your numbers.", raise_for_status=lambda: None))
    assert _post_hook(env, order["id"], hook["secret"], signer="wrong-secret").status_code == 401                 # not signed by the account's secret
    assert _post_hook(env, order["id"], hook["secret"], age=3600).status_code == 401                                # an old message cannot be replayed
    assert _post_hook(env, "nosuchorder", hook["secret"]).status_code == 401
    assert podcast.get_episode(ep["id"])["transcript_status"] == "working"
    assert _post_hook(env, order["id"], hook["secret"], event="order.failed").json()["ignored"] is True
    r = _post_hook(env, order["id"], hook["secret"])
    assert r.status_code == 200 and r.json()["ok"] is True and fetched == ["https://kauli.test/download/abc/txt?sig=1"]
    done = podcast.get_episode(ep["id"])
    assert done["transcript_status"] == "published" and "\r" not in done["transcript_text"] and done["transcript_text"].startswith("AMARA: Welcome.")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    page = env.client.get(f"/podcast/{ep['id']}").text
    assert "AMARA: Welcome." in page and "DAN: Always check your numbers." in page and 'translate="no"' in page
    assert "Read the transcript" in env.client.get("/podcast").text


def test_a_message_that_points_somewhere_else_or_cannot_be_fetched_never_publishes(env, wav_bytes, monkeypatch):
    order = env.order_from(env.place(wav_bytes))
    ep = _setup_publisher(env, monkeypatch)
    env.client.post("/staff/podcast/webhook/connect")
    hook = env.db.get_client_webhook("u1")
    conn = env.db.get_conn()
    conn.execute("UPDATE podcast_episodes SET transcript_order_id = ?, transcript_status = 'working' WHERE id = ?", (order["id"], ep["id"]))
    conn.commit()
    conn.close()
    r = _post_hook(env, order["id"], hook["secret"], link="https://evil.example.com/steal")
    assert r.status_code == 422 and podcast.get_episode(ep["id"])["transcript_status"] == "failed"
    monkeypatch.setattr(podcast.httpx, "get", lambda url, **k: (_ for _ in ()).throw(RuntimeError("timed out")))
    assert _post_hook(env, order["id"], hook["secret"]).status_code == 502
    failed = podcast.get_episode(ep["id"])
    assert failed["transcript_status"] == "failed" and "timed out" in failed["transcript_error"] and not failed["transcript_text"]
    assert "Try again" in env.client.get("/staff/podcast").text


def test_the_podcast_page_is_translated_for_visitors_but_the_feed_is_not(env):
    assert "/podcast.xml" in env.app._LOCALIZE_SKIP and "/podcast" not in env.app._LOCALIZE_SKIP
