"""The service pages speak for the pivot: no language pair in any title, a clear way to choose between options, a demo to try,
guides to read, the 99% human-review promise worded as "up to", and five guides that are seeded once and published."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import blog_guides, blog_seed, demo_content  # noqa: E402

REBUILT = ("ngos", "youtubers", "media-broadcast", "e-learning", "voice-over", "captioning", "video-translation",
           "video-transcription", "video-dubbing", "video-localization")


def test_no_service_page_names_a_language_pair_in_its_title_heading_or_description(env):
    for slug, page in env.app.SOLUTION_PAGES.items():
        for field in ("title", "kicker", "h1", "meta_description"):
            assert not re.search(r"swahili|kikuyu", page[field], re.I), (slug, field, page[field])


def test_every_service_page_helps_a_visitor_choose_try_a_demo_and_read_on(env):
    guide_slugs = {p["slug"] for p in blog_seed.POSTS} | {g["slug"] for g in blog_guides.GUIDES}
    for slug, page in env.app.SOLUTION_PAGES.items():
        assert len(page["choose"]) >= 2 and all(c["name"] and c["best"] and c["get"] for c in page["choose"]), slug
        assert page["demo"] in demo_content.SCENARIOS, slug
        assert page["guides"] and set(page["guides"]) <= guide_slugs, (slug, set(page["guides"]) - guide_slugs)
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for slug in REBUILT:
        html = env.client.get(f"/solutions/{slug}").text
        assert 'id="choose"' in html and "Choose what fits" in html and f"/try?use={env.app.SOLUTION_PAGES[slug]['demo']}" in html, slug
        assert "up to 99%" in html.lower(), slug


def test_the_ninety_nine_percent_claim_is_always_tied_to_human_review(env):
    for slug, page in env.app.SOLUTION_PAGES.items():
        blob = repr(page)
        for m in re.finditer(r"99%", blob):
            window = blob[max(0, m.start() - 160): m.end() + 160].lower()
            assert "up to 99%" in window and ("human" in window or "editor" in window or "review" in window), (slug, window)


def test_the_course_page_has_a_demo_and_the_podcast_page_explains_webhooks(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    course = env.client.get("/solutions/e-learning").text
    assert "/try?use=course" in course and "module_3_lesson.mp4" in course                # the hero tour plus the full demo
    podcast = env.client.get("/solutions/podcast-transcription").text
    assert "webhook" in podcast.lower() and "podcast-transcripts-to-your-website-with-webhooks" in repr(env.app.SOLUTION_PAGES["podcast-transcription"]["guides"])
    meetings = env.app.SOLUTION_PAGES["interview-transcription"]["choose"]
    assert any("Send Kauli to the call" in c["name"] for c in meetings)


def test_the_guides_follow_the_house_style_and_link_only_to_real_pages(env):
    assert len(blog_guides.GUIDES) == 6 and len({g["slug"] for g in blog_guides.GUIDES}) == 6
    known_posts = {p["slug"] for p in blog_seed.POSTS} | {g["slug"] for g in blog_guides.GUIDES}
    for g in blog_guides.GUIDES:
        body = g["body_html"]
        takeaways = re.search(r'<div class="m-key-takeaways">(.*?)</div>', body, re.S)
        assert takeaways and takeaways.group(1).count("<li>") == 3, g["slug"]
        assert body.index("m-key-takeaways") < body.index("<h2>"), g["slug"]
        assert g["category"] in env.db.BLOG_CATEGORIES and 80 <= len(g["description"]) <= 300, g["slug"]
        for href in re.findall(r'href="(/[^"#?]*)', body):
            if href.startswith("/blog/"):
                assert href[len("/blog/"):] in known_posts, (g["slug"], href)
            elif href.startswith("/solutions/"):
                assert href[len("/solutions/"):] in env.app.SOLUTION_PAGES, (g["slug"], href)
            else:
                assert href in ("/developers", "/demo", "/try", "/human-review", "/languages", "/blog"), (g["slug"], href)
        for qs in re.findall(r"/try\?use=(\w+)", body):
            assert qs in demo_content.SCENARIOS, (g["slug"], qs)
        assert not re.search(r"\b\d{2,3}% (faster|cheaper|more)", body), g["slug"]       # no invented statistics


def test_the_webhook_guide_matches_what_the_product_really_sends(env):
    body = next(g for g in blog_guides.GUIDES if g["slug"].startswith("podcast-transcripts-to-your-website"))["body_html"]
    developers = env.client.get("/developers").text
    for fact in ("X-Kauli-Signature", "X-Kauli-Timestamp", "sha256=", "Zapier", "Make", "n8n"):
        assert fact in body and fact in developers or fact in ("Make",), fact
    assert "order.ready" not in body or "order.ready" in developers


def test_guides_are_seeded_once_published_and_never_put_back_after_a_delete(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    first = dict(blog_guides.seed_guides("s1"))
    assert set(first.values()) == {"published"} and len(first) == 6
    assert all(p["status"] == "published" for p in env.db.list_blog_posts(published_only=False) if p["slug"] in first)
    assert set(dict(blog_guides.seed_guides("s1")).values()) == {"already seeded"}
    post = env.db.get_blog_post_by_slug("ngos-and-civil-society-transcription-and-translation", published_only=True)
    env.db.get_conn().execute("DELETE FROM blog_posts WHERE id = ?", (post["id"],)).connection.commit()
    assert dict(blog_guides.seed_guides("s1"))["ngos-and-civil-society-transcription-and-translation"] == "already seeded"
    assert env.db.get_blog_post_by_slug("ngos-and-civil-society-transcription-and-translation", published_only=False) is None


def test_published_guides_show_on_their_pages_and_in_the_sitemap(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    blog_guides.seed_guides("s1")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    page = env.client.get("/solutions/interview-transcription").text
    assert "/blog/record-meetings-and-interviews-with-ai-post-processing" in page and "Guides to read next" in page
    assert "/blog/media-and-broadcast-partner-with-kauli-fast-turnarounds" in env.client.get("/solutions/media-broadcast").text
    sitemap = env.client.get("/sitemap.xml").text
    for g in blog_guides.GUIDES:
        assert f"/blog/{g['slug']}" in sitemap
        post = env.client.get(f"/blog/{g['slug']}")
        assert post.status_code == 200 and g["title"].split(":")[0][:20] in post.text
