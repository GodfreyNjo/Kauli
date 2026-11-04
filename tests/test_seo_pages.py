"""Pages for the searches where a visitor already needs what Kauli sells: language plus job, a sample, a price, a promise, and blog posts that lead to them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import seo_pages  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def anonymous(env):
    """These are public pages: look at them as a visitor who is not signed in."""
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)


def test_a_reviewed_language_page_has_the_comparison_sample_and_a_single_clear_button(env):
    page = env.client.get("/transcribe/spanish").text
    for text in ("Instant AI or human-reviewed?", "What a Spanish editor fixes", "Who reviews it and what we promise", "Transcribe your first file free", "Book a demo",
                 "Spanish interview transcription", "German", "illustration of the kinds of corrections"):
        assert text in page, text
    assert "$0.12" in page and "$1.79" in page                                                    # the real prices, in plain text
    assert "Start a project" not in page.split('id="get-started"')[0]                              # one primary button, not competing ones


def test_other_ai_languages_keep_the_simple_page(env):
    page = env.client.get("/transcribe/italian").text
    assert "Instant AI or human-reviewed?" not in page and "What a" not in page.split("Italian")[0]


def test_each_language_and_job_has_its_own_page_with_its_own_words(env):
    seen = set()
    for code, (slug, _hr) in seo_pages.REVIEWED.items():
        for task in seo_pages.TASKS:
            r = env.client.get(f"/transcribe/{slug}/{task}")
            assert r.status_code == 200, (slug, task)
            page = r.text
            label = seo_pages.LANG_BLOCKS[code]["label"]
            assert label in page and "Transcribe your first" in page and "Related pages" in page and "$0.12" in page
            assert f"/transcribe/{slug}" in page
            intro = page.split('class="m-hero-sub">')[1].split("</p>")[0]
            seen.add(intro)
    assert len(seen) == len(seo_pages.REVIEWED) * len(seo_pages.TASKS)                          # no two pages share an introduction


def test_unknown_languages_and_jobs_are_not_pages(env):
    assert env.client.get("/transcribe/italian/interview-transcription").status_code == 404
    assert env.client.get("/transcribe/spanish/knitting").status_code == 404


def test_human_review_is_only_promised_where_it_can_really_be_ordered(env, monkeypatch):
    monkeypatch.setattr(env.app, "_human_review_live", lambda: {"en", "sw", "ke"})
    page = env.client.get("/transcribe/spanish/interview-transcription").text
    assert "is being opened up" in page and "we correct it free of charge" not in page
    monkeypatch.setattr(env.app, "_human_review_live", lambda: {"en", "sw", "ke", "es"})
    assert "we correct it free of charge" in env.client.get("/transcribe/spanish/interview-transcription").text


def test_the_sitemap_lists_the_job_pages_in_every_language(env):
    monkeypatch_env = env.monkeypatch
    monkeypatch_env.setenv("AZURE_TRANSLATOR_KEY", "k")
    monkeypatch_env.setenv("AZURE_TRANSLATOR_REGION", "r")
    xml = env.client.get("/sitemap.xml").text
    assert "/transcribe/german/podcast-transcription</loc>" in xml and "/zh/transcribe/german/podcast-transcription</loc>" in xml


def test_a_blog_post_gets_a_call_to_action_in_the_middle_and_at_the_end_that_matches_its_topic(env):
    body = "".join(f"<p>Paragraph {i} about how to run a good Spanish interview.</p>" for i in range(8))
    env.db.create_blog_post(slug="spanish-interview-tips", title="Spanish interview tips", description="d", body_html=body, author_id="u1", status="published")
    page = env.client.get("/blog/spanish-interview-tips").text
    assert page.count('class="blog-cta blog-cta-') == 2 and "blog-cta-mid" in page and "blog-cta-end" in page
    assert "/transcribe/spanish/interview-transcription?utm_source=blog&amp;utm_medium=cta&amp;utm_campaign=spanish-interview-tips" in page or \
           "/transcribe/spanish/interview-transcription?utm_source=blog&utm_medium=cta&utm_campaign=spanish-interview-tips" in page
    assert "Transcribe your first file free" in page


def test_a_post_leads_to_the_page_that_matches_what_it_is_about():
    assert seo_pages.cta_target("How to add subtitles to a YouTube video")[0] == "/solutions/youtube-video-to-text"
    assert seo_pages.cta_target("German podcast transcripts that rank")[0] == "/transcribe/german/podcast-transcription"
    assert seo_pages.cta_target("Mandarin to English subtitles")[0] == "/transcribe/chinese-mandarin/youtube-subtitles"
    assert seo_pages.cta_target("Kenya's new tax rules")[0] == "/solutions/audio-to-text"


def test_search_console_and_bing_verification_tags_appear_when_configured(env, monkeypatch):
    assert "google-site-verification" not in env.client.get("/human-review").text
    monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "abc123")
    monkeypatch.setenv("BING_SITE_VERIFICATION", "xyz789")
    page = env.client.get("/human-review").text
    assert '<meta name="google-site-verification" content="abc123">' in page and '<meta name="msvalidate.01" content="xyz789">' in page


def test_the_home_try_band_has_upload_and_youtube_and_the_use_case_index_exists(env):
    home = env.client.get("/").text
    for text in ('id="try-card"', "Upload audio or video", 'id="yt-link"', 'id="yt-go"', "Generate subtitles", "Built for every workflow", "See all use cases",
                 "Turn conversations into global opportunities", "Start Free Now"):
        assert text in home, text
    page = env.client.get("/solutions")
    assert page.status_code == 200 and "Built for every workflow" in page.text and "/solutions/interview-transcription" in page.text
    assert "/solutions</loc>" in env.client.get("/sitemap.xml").text


def test_the_hero_picture_rotates_scenes_languages_and_moves_the_waveform(env):
    page = env.client.get("/").text
    for text in ('id="hx-app"', 'data-scene="3"', 'id="hx-langs"', 'id="hx-play"', 'id="hx-time"', "Dubbed voice track", "prefers-reduced-motion"):
        assert text in page, text
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert "@keyframes hx-pulse" in css and ".hx-visual.is-paused" in css


def test_the_home_demo_is_the_real_demo_video_with_the_words_the_video_says(env):
    page = env.client.get("/").text
    assert 'id="hx-demo"' in page and "/static/demo/vid-video-en.mp4" in page and "/static/demo/vid-video-en.jpg" in page
    for line in ("Meet the new Aurora backpack.", "Conoce la nueva mochila Aurora.", "Lernen Sie den neuen Aurora-Rucksack kennen.", "全新极光背包闪亮登场。", "Kutana na mkoba mpya wa Aurora."):
        assert line in page, line
    for code in ("en", "es", "de", "zh", "sw"):                                                # every version the buttons play really exists
        root = Path(__file__).parent.parent / "webapp" / "static" / "demo"
        assert (root / f"vid-video-{code}.mp4").is_file() and (root / f"vid-video-{code}.jpg").is_file(), code
        assert env.client.get(f"/static/demo/vid-video-{code}.mp4").status_code == 200


def test_no_language_mentions_a_card_verification_charge():
    """The $1 card step was retired; no hand-written page in any language may still promise or describe it."""
    from webapp import locales, ui_text
    banned = ("1 $", "US$ 1", "1 美元", "1 دولار", "1 डॉलर", "1ドル", "1달러", "dola 1", "1 dollar", "$1 ", "$1.", "$1,", "1 USD")
    for code, block in locales.STRINGS.items():
        for key, value in block.items():
            assert not any(b in str(value) for b in banned), (code, key)
    for lang, catalog in {**ui_text.CATALOG, "en": ui_text.EN}.items():
        for key, value in catalog.items():
            assert not any(b in str(value) for b in banned), (lang, key)
