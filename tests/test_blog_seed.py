"""The seeded blog posts follow the house style and every internal link
resolves - a broken link or a missing takeaways block is a content bug."""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.blog_seed import POSTS  # noqa: E402

# Posts that already exist on the live blog (referenced from the new ones).
EXISTING_POST_SLUGS = {
    "sheng-vs-standard-swahili-machine-translation",
    "the-swahili-clock-why-saa-tisa-asubuhi-isn-t-9-am",
    "why-ai-cant-transcribe-kikuyu",
}


def test_slugs_unique_and_descriptions_fit():
    slugs = [p["slug"] for p in POSTS]
    assert len(slugs) == len(set(slugs)) == 16
    for p in POSTS:
        assert re.fullmatch(r"[a-z0-9-]+", p["slug"]), p["slug"]
        assert 60 <= len(p["description"]) <= 160, (p["slug"], len(p["description"]))
        assert p["category"] == "Transcription Guides"


def test_each_post_has_exactly_three_takeaways_before_the_first_h2():
    for p in POSTS:
        body = p["body_html"]
        blocks = re.findall(r'<div class="m-key-takeaways">(.*?)</div>', body, flags=re.S)
        assert len(blocks) == 1, p["slug"]
        assert len(re.findall(r"<li>", blocks[0])) == 3, p["slug"]
        assert body.index("m-key-takeaways") < body.index("<h2>"), p["slug"]


def test_paragraphs_stay_short():
    """House style: never three or more sentences in one paragraph."""
    for p in POSTS:
        for para in re.findall(r"<p>(.*?)</p>", p["body_html"], flags=re.S):
            text = re.sub(r"<[^>]+>", "", para)
            sentences = re.findall(r"[.!?](?:\s+[A-Z\"'(])", text)
            assert len(sentences) <= 2, (p["slug"], text[:80])


def test_no_unbacked_claims():
    banned = ["99%", "100% accurate", "we guarantee", "guaranteed", "best in the world", "#1",
              "thousands of customers"]
    for p in POSTS:
        for phrase in banned:
            assert phrase.lower() not in p["body_html"].lower(), (p["slug"], phrase)


def test_internal_links_resolve():
    from webapp import seo_languages
    from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES
    ai_slugs = seo_languages.build_slug_index({c: v[0] for c, v in TRANSKRIPTOR_LANGUAGES.items()})
    own = {p["slug"] for p in POSTS} | EXISTING_POST_SLUGS
    solution_slugs = set()
    try:
        before = dict(os.environ)
        from webapp import app as app_mod  # needs the web stack; skipped without it
        for key in set(os.environ) - set(before):
            del os.environ[key]
        solution_slugs = set(app_mod.SOLUTION_PAGES)
    except ImportError:
        pytest.skip("web stack not installed")
    for p in POSTS:
        for href in re.findall(r'href="(/[^"#?]*)', p["body_html"]):
            if href.startswith("/blog/"):
                assert href[len("/blog/"):] in own, (p["slug"], href)
            elif href.startswith("/solutions/"):
                assert href[len("/solutions/"):] in solution_slugs, (p["slug"], href)
            elif href.startswith("/transcribe/"):
                assert href[len("/transcribe/"):] in ai_slugs, (p["slug"], href)
            else:
                assert href in {"/", "/languages", "/privacy", "/formatting-standards"}, (p["slug"], href)
