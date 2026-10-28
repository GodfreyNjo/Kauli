"""Search and AI-engine hygiene for every public page in the sitemap.

One crawl, one list of problems: each public page needs a title, one description, one canonical that points at itself,
one h1, a language, social tags, valid structured data, alt text on images and links that go somewhere. Private pages
must tell crawlers to stay out. The AI-facing files (robots.txt, llms.txt, llms-full.txt) must describe the business as it
is now, without publishing anything the price card keeps quiet."""
import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

BASE = "http://testserver"


def _tags(html, pattern):
    return re.findall(pattern, html, re.S | re.I)


def _public_paths(env):
    xml = env.client.get("/sitemap.xml").text
    return sorted({urlparse(u).path or "/" for u in re.findall(r"<loc>([^<]+)</loc>", xml)})


def _audit(path, html):
    problems = []
    titles = [unescape(t) for t in _tags(html, r"<title>(.*?)</title>")]
    if len(titles) != 1 or not (10 <= len(titles[0].strip()) <= 80):
        problems.append(f"title {titles!r}")
    desc = _tags(html, r'<meta\s+name="description"\s+content="([^"]*)"')
    if len(desc) != 1 or not (50 <= len(desc[0]) <= 330):
        problems.append(f"description x{len(desc)} len={[len(d) for d in desc]}")
    canon = _tags(html, r'<link\s+rel="canonical"\s+href="([^"]+)"')
    if len(canon) != 1 or urlparse(canon[0]).path.rstrip("/") != path.rstrip("/") or "?" in canon[0]:
        problems.append(f"canonical {canon}")
    if len(_tags(html, r"<h1[\s>]")) != 1:
        problems.append(f"h1 x{len(_tags(html, r'<h1[\s>]'))}")
    if not re.search(r'<html lang="[a-zA-Z-]+"', html):
        problems.append("html lang")
    for prop in ("og:title", "og:description", "og:image", "og:url", "og:site_name"):
        if len(_tags(html, rf'<meta\s+property="{prop}"')) != 1:
            problems.append(f"{prop} x{len(_tags(html, rf'<meta\s+property={chr(34)}{prop}{chr(34)}'))}")
    if len(_tags(html, r'<meta\s+name="twitter:card"')) != 1:
        problems.append("twitter:card")
    for block in _tags(html, r'<script type="application/ld\+json">(.*?)</script>'):
        try:
            json.loads(block)
        except ValueError as exc:
            problems.append(f"json-ld {exc}")
    if re.search(r'<meta\s+name="robots"[^>]*noindex', html, re.I):
        problems.append("noindex on a public page")
    for img in _tags(html, r"<img\b[^>]*>"):
        if " alt=" not in img:
            problems.append(f"img without alt {img[:60]}")
    return problems


def test_every_public_page_is_ready_for_search_and_ai_engines(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    env.client.cookies.clear()
    report = {}
    for path in _public_paths(env):
        r = env.client.get(path)
        if r.status_code != 200:
            report[path] = [f"status {r.status_code}"]
            continue
        found = _audit(path, r.text)
        if found:
            report[path] = found
    assert report == {}, "\n" + "\n".join(f"{p}: {v}" for p, v in report.items())


def test_internal_links_on_the_public_pages_all_resolve(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    seen, broken = set(), []
    for path in ["/", "/human-review", "/languages", "/try", "/developers", "/blog", "/fr", "/sw"]:
        html = env.client.get(path).text
        for href in _tags(html, r'<a\s[^>]*href="(/[^"#?]*)'):
            if href in seen or href.startswith(("/static/", "/lang/", "/login", "/client", "/staff", "/blog/")):
                continue
            seen.add(href)
            if env.client.get(href, follow_redirects=False).status_code >= 400:
                broken.append((path, href))
    assert broken == []


def test_private_and_utility_pages_tell_crawlers_to_stay_out(env):
    for path in ("/client/home", "/client/pending", "/settings", "/login", "/forgot-password", "/lang/fr"):
        r = env.client.get(path, follow_redirects=False)
        assert "noindex" in r.headers.get("x-robots-tag", ""), path
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for path in ("/", "/human-review", "/languages", "/fr"):
        assert "noindex" not in env.client.get(path).headers.get("x-robots-tag", ""), path


def test_robots_txt_keeps_crawlers_out_of_private_places_and_welcomes_ai_crawlers(env):
    body = env.client.get("/robots.txt").text
    for private in ("/client", "/staff", "/settings", "/lang/", "/actor", "/api/"):
        assert f"Disallow: {private}" in body, private
    for bot in ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "OAI-SearchBot", "ChatGPT-User", "Applebot-Extended", "CCBot"):
        assert f"User-agent: {bot}\nAllow: /" in body, bot
    assert "Sitemap: http://testserver/sitemap.xml" in body


def test_llms_txt_describes_the_business_as_it_is_and_hides_what_the_price_card_hides(env):
    short = env.client.get("/llms.txt").text
    full = env.client.get("/llms-full.txt").text
    assert short.startswith("# Kauli") and "/llms-full.txt" in short
    for text in (short, full):
        assert "Kenya & East Africa" not in text and "East Africa:" not in text and "$0.60" not in text and "$0.75" not in text
        assert "contact" in text.lower() or "@" in text
    for needle in ("Nairobi", "Dallas", "Human review", "Pro", "Premium", "yearly", "/human-review/", "/languages", "/sw", "/zh",
                   "Swahili", "Kikuyu", "meeting", "API"):
        assert needle.lower() in full.lower(), needle
    assert full.count("\n") > short.count("\n")


def test_each_office_has_its_own_local_page_with_structured_data(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for slug, city, street in (("nairobi", "Nairobi", "9th floor, Nation Centre, Kimathi Street"), ("dallas", "Dallas", "3710 Rawlins St")):
        r = env.client.get(f"/locations/{slug}")
        assert r.status_code == 200, slug
        assert city in r.text and street in r.text and '"@type": "LocalBusiness"' in r.text.replace('"@type":"LocalBusiness"', '"@type": "LocalBusiness"')
        assert f"/locations/{slug}" in env.client.get("/sitemap.xml").text
    assert env.client.get("/locations/atlantis").status_code == 404
    assert "/locations/nairobi" in env.client.get("/").text            # linked from the footer, so crawlers find it


def test_localized_pages_declare_their_language_to_crawlers(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    r = env.client.get("/de")
    assert r.headers.get("content-language") == "de"
    assert '<html lang="de"' in r.text and 'hreflang="x-default"' in r.text
    assert env.client.get("/").headers.get("content-language") == "en"
