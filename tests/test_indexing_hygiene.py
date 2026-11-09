"""Things Google Search Console flagged: a page that rewrites its own address, and links into the sign-in pages."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401

DEMO_JS = Path(__file__).parent.parent / "webapp" / "static" / "hear_demo.js"


def test_the_demo_page_leaves_its_address_alone_until_a_visitor_changes_something():
    js = DEMO_JS.read_text(encoding="utf-8")
    assert js.index("if (!addressSync) return;") < js.index("window.history.replaceState")
    assert js.count("addressSync = true;") == 3                      # the scenario, the language and the direction
    assert "var addressSync = false;" in js


def test_public_links_into_the_sign_in_pages_are_not_followed_by_crawlers(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)         # a visitor who is not signed in
    page = env.client.get("/").text
    links = re.findall(r"<a [^>]*href=\"/login(?:/google)?\?next=[^>]*>", page)
    assert links, "the public pages are expected to have sign-in links"
    assert all('rel="nofollow"' in link for link in links), links


def test_the_google_sign_in_button_is_not_followed_either(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    buttons = re.findall(r"<a [^>]*href=\"/login/google[^>]*>", env.client.get("/login").text)
    assert buttons and all('rel="nofollow"' in b for b in buttons)


def test_sign_in_pages_stay_out_of_the_index(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for path in ("/login", "/forgot-password"):
        assert "noindex" in env.client.get(path).headers.get("x-robots-tag", ""), path


# ---------------------------------------------------------------- links to articles that are not published
def _post(env, slug, body="<p>text</p>", status="published"):
    env.db.get_or_create_user("author1", "author@example.com", "staff")
    title = slug.replace("-", " ").title()
    return env.db.create_blog_post(slug, title, "A description of it.", body, "author1", status)


def test_only_this_sites_unpublished_article_addresses_count_as_dead(env):
    from webapp import blog_links
    live = {"live-post"}
    assert blog_links.link_ok("/blog/live-post", live) and not blog_links.link_ok("/blog/draft-post", live)
    assert not blog_links.link_ok("https://www.kauli-forgemedia.com/blog/draft-post#top", live)
    assert blog_links.link_ok("/blog", live) and blog_links.link_ok("/solutions/captions", live)
    assert blog_links.link_ok("https://medium.com/blog/draft-post", live)               # another site: never touched


def test_the_rewrite_keeps_live_links_and_other_sites_and_unlinks_only_the_unpublished(env):
    from webapp import blog_links
    _post(env, "live-post")
    html = ('<p><a href="/blog/live-post">Live</a> <a href="/blog/draft-post" class="x">Draft one</a> '
            '<a href="https://kauli-forgemedia.com/blog/draft-post#top">Draft two</a> <a href="/blog">All</a> '
            '<a href="https://medium.com/blog/draft-post">Elsewhere</a></p>')
    out = blog_links.keep_published_links(html)
    assert '<a href="/blog/live-post">Live</a>' in out and ' Draft one ' in out and ' Draft two ' in out
    assert 'href="/blog/draft-post"' not in out and "kauli-forgemedia.com/blog/draft-post" not in out
    assert '<a href="/blog">All</a>' in out and '<a href="https://medium.com/blog/draft-post">Elsewhere</a>' in out


def test_a_published_article_does_not_link_to_a_draft_but_keeps_the_words(env):
    _post(env, "linked-live")
    _post(env, "still-a-draft", status="draft")
    body = ('<p>Read <a href="/blog/linked-live">the live one</a> and '
            '<a href="/blog/still-a-draft">the draft one</a>.</p>')
    _post(env, "the-reader", body=body)
    page = env.client.get("/blog/the-reader").text
    assert 'href="/blog/linked-live"' in page and "the live one" in page
    assert 'href="/blog/still-a-draft"' not in page and "the draft one" in page


def test_the_home_page_faq_links_to_the_guide_only_once_it_is_published(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)         # the home page for a visitor
    slug = "ai-vs-human-transcription-which-do-you-need"
    assert f'href="/blog/{slug}"' not in env.client.get("/").text
    _post(env, slug)
    assert f'href="/blog/{slug}"' in env.client.get("/").text
