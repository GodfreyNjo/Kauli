"""Every page in the visitor's own language: the engine (webapp/page_translate.py) and the /xx/ addresses."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import page_translate  # noqa: E402


@pytest.fixture
def fake(env, monkeypatch):
    """A translator that marks what it translated, and counts how often it was asked."""
    calls = []
    monkeypatch.delenv("KAULI_PUBLIC_BASE_URL", raising=False)          # another test file may have loaded the real address into the environment
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "r")
    monkeypatch.setattr(page_translate, "_mem", {})

    def batch(texts, lang, timeout):
        calls.append((lang, list(texts)))
        return [f"«{lang}» {t}" for t in texts]
    monkeypatch.setattr(page_translate, "_azure_batch", batch)
    return calls


PAGE = """<!doctype html><html lang="en"><head><title>Pricing for everyone</title>
<meta name="description" content="Instant AI transcripts and subtitles"><link rel="canonical" href="https://x.test/pricing"></head>
<body><h1>Welcome back</h1><p>Upload your file and we start straight away.</p>
<img src="a.png" alt="A person speaking" title="Speaker"><input placeholder="Search your files">
<table><tr><td lang="es">Hola, esto es lo que dijo</td><td translate="no">Interview with Amara</td></tr></table>
<script>var s = "Not translated";</script><style>.a{content:"Hello"}</style><code>pip install kauli</code>
<span class="price">$0.30</span><a href="/pricing">See pricing</a></body></html>"""


def test_the_words_a_person_reads_are_translated_and_everything_else_is_left_alone(fake):
    out, complete = page_translate.localize_html(PAGE, "zh")
    assert complete
    for original in ("Pricing for everyone", "Welcome back", "Upload your file and we start straight away.", "See pricing", "A person speaking", "Search your files",
                     "Instant AI transcripts and subtitles"):
        assert f"«zh» {original}" in out, original
    assert "Hola, esto es lo que dijo" in out and "Interview with Amara" in out                 # a transcript (lang=) or translate="no" is never touched
    assert 'var s = "Not translated"' in out and 'content:"Hello"' in out and "pip install kauli" in out
    assert "$0.30" in out and "«zh» $0.30" not in out                                           # a price is not a sentence
    assert 'src="a.png"' in out and 'href="/pricing"' in out                                    # the markup is intact


def test_a_sentence_is_translated_once_and_remembered(fake):
    page_translate.localize_html(PAGE, "ja")
    first = sum(len(t) for _, t in fake)
    page_translate._mem.clear()                                                               # even after a restart it comes from the database
    out, _ = page_translate.localize_html(PAGE, "ja")
    assert sum(len(t) for _, t in fake) == first and "«ja» Welcome back" in out


def test_brand_names_are_fenced_off_and_text_already_in_the_language_is_skipped():
    fenced = page_translate._fence("Pay with M-Pesa on Kauli & Paystack")
    assert fenced.count('class="notranslate"') == 3 and "&amp;" in fenced
    assert page_translate._unfence('Pagar con <span class="notranslate">M-Pesa</span> &amp; más') == "Pagar con M-Pesa & más"
    need = page_translate.needs_translation
    assert need("Upload your file", "es") and need("Billing and plans", "de")
    assert not need("Sube tu archivo y empezamos de inmediato", "es") and not need("欢迎回来", "zh") and not need("12.50", "fr")
    assert not need("https://kauli-forgemedia.com/pricing", "ja") and not need("hello@kauli-forgemedia.com", "ja") and not need("SPK_1", "ja")
    assert not need("Hoy es un buen día para empezar", "es")                                   # Spanish already


def test_structured_data_for_search_is_translated_too(fake):
    page = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Kauli",'
            '"description":"Accurate transcripts in 76+ languages","url":"https://x.test/"}</script></head><body><p>Hi there friend</p></body></html>')
    out, _ = page_translate.localize_html(page, "fr")
    assert "«fr» Accurate transcripts in 76+ languages" in out and '"url": "https://x.test/"' in out and '"@type": "Organization"' in out


def test_messages_in_a_json_answer_are_translated_and_the_rest_is_not(fake):
    out = page_translate.localize_json({"error": "Choose a file first.", "id": "abc", "items": [{"message": "Saved"}]}, "ko")
    assert out["error"] == "«ko» Choose a file first." and out["id"] == "abc" and out["items"][0]["message"] == "«ko» Saved"


def test_when_the_translator_is_down_the_page_still_comes_and_says_it_is_incomplete(env, monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "r")
    monkeypatch.setattr(page_translate, "_mem", {})
    monkeypatch.setattr(page_translate, "_azure_batch", lambda texts, lang, timeout: None)
    out, complete = page_translate.localize_html(PAGE, "de")
    assert not complete and "Welcome back" in out


# ------------------------------------------------------------------------------------------------------------ the /xx/ addresses
def test_a_public_page_has_an_address_in_each_language_with_the_right_tags(env, fake):
    r = env.client.get("/zh/human-review")
    assert r.status_code == 200
    page = r.text
    assert 'lang="zh-Hans"' in page and "«zh»" in page
    assert '<link rel="canonical" href="http://testserver/zh/human-review">' in page
    for code in ("es", "fr", "ar", "ja"):
        assert f'hreflang="{code}" href="http://testserver/{code}/human-review"' in page
    assert 'hreflang="en" href="http://testserver/human-review"' in page and 'hreflang="x-default"' in page
    assert re.search(r'<a\b[^>]*href="/zh/[^"]+"', page)                                      # links stay in the language
    assert r.headers["content-language"] == "zh-Hans" and "kauli_lang=zh" in r.headers.get("set-cookie", "")


def test_arabic_pages_read_right_to_left_and_private_pages_have_no_language_address(env, fake):
    assert 'dir="rtl"' in env.client.get("/ar/human-review").text
    assert env.client.get("/zh/account/home", follow_redirects=False).status_code in (404, 303, 401)
    assert env.client.get("/zh/staff").status_code == 404


def test_a_half_translated_page_is_never_listed_by_search(env, monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "r")
    monkeypatch.setattr(page_translate, "_mem", {})
    monkeypatch.setattr(page_translate, "_azure_batch", lambda texts, lang, timeout: None)
    r = env.client.get("/de/human-review")
    assert r.status_code == 200 and "noindex" in r.headers.get("x-robots-tag", "") and r.headers["cache-control"] == "no-store"


def test_a_browser_set_to_chinese_is_sent_to_the_chinese_address_once(env, fake):
    r = env.client.get("/human-review", headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/zh/human-review" and "kauli_lang=zh" in r.headers["set-cookie"]
    env.client.cookies.clear()
    home = env.client.get("/", headers={"Accept-Language": "ja"}, follow_redirects=False)
    assert home.status_code == 302 and home.headers["location"] == "/ja"
    env.client.cookies.clear()
    assert env.client.get("/human-review", headers={"Accept-Language": "zh-CN"}, cookies={"kauli_lang": "en"}, follow_redirects=False).status_code == 200      # they chose English
    assert env.client.get("/human-review", headers={"Accept-Language": "zh-CN", "User-Agent": "Googlebot/2.1"}, follow_redirects=False).status_code == 200   # crawlers are never redirected
    assert env.client.get("/human-review", headers={"Accept-Language": "en-US,en"}, follow_redirects=False).status_code == 200
    assert env.client.get("/human-review", follow_redirects=False).status_code == 200


def test_the_signed_in_account_is_translated_end_to_end(env, fake):
    env.db.set_ui_lang("u1", "zh")
    page = env.client.get("/account/billing").text
    assert "«zh»" in page
    assert 'lang="zh-Hans"' in page


def test_the_logo_is_written_in_the_readers_script_for_chinese_japanese_korean_arabic_and_hindi(env, fake):
    zh = env.client.get("/zh/human-review").text
    assert "logo-local" in zh and "考利" in zh and "kauli-emblem.png" in zh and "kauli-logo-nav-sm.png" not in zh
    es = env.client.get("/es/human-review").text
    assert "logo-local" not in es and "kauli-logo-nav-sm" in es                                  # Latin-script readers keep the logo as it is
    assert "카울리" in env.client.get("/ko/human-review").text and "كاولي" in env.client.get("/ar/human-review").text


def test_the_sitemap_lists_every_public_page_in_every_language_with_alternates(env, fake):
    xml = env.client.get("/sitemap.xml").text
    assert "<loc>http://testserver/zh/human-review</loc>" in xml and "<loc>http://testserver/ja/solutions/youtubers</loc>" in xml
    assert 'hreflang="zh-Hans" href="http://testserver/zh/human-review"' in xml and 'hreflang="x-default" href="http://testserver/human-review"' in xml
    assert "/zh/terms" not in xml and "/zh/account" not in xml                                     # legal and private pages have no translated address


def test_an_email_goes_out_in_the_customers_language(env, fake):
    from webapp import mailer
    env.db.set_ui_lang("u1", "fr")
    email = env.db.get_user("u1")["email"]
    subject, html, text = mailer._in_recipients_language(email, "Your order is ready", "<html><body><p>Open your transcript now</p></body></html>", "Open your transcript now")
    assert subject == "«fr» Your order is ready" and "«fr» Open your transcript now" in html and "«fr» Open your transcript now" in text
    env.db.set_ui_lang("u1", "en")
    assert mailer._in_recipients_language(email, "Your order is ready", "<p>x</p>", None)[0] == "Your order is ready"
