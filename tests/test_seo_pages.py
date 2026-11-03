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
