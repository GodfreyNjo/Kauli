"""The accessibility and compliance landing page: published like the other service pages, careful about what it promises."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401

SLUG = "accessibility-compliance"


def test_the_page_is_live_in_the_sitemap_and_the_footer(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    page = env.client.get(f"/solutions/{SLUG}")
    assert page.status_code == 200
    assert f"/solutions/{SLUG}" in env.client.get("/sitemap.xml").text
    assert f'href="/solutions/{SLUG}"' in env.client.get("/blog").text                         # the footer link


def test_it_says_what_we_do_and_what_we_do_not(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    html = env.client.get(f"/solutions/{SLUG}").text
    for text in ("Captions and transcripts your accessibility team can stand behind", "1.2.2", "SRT, VTT, TTML or SBV",
                 "up to 99%", "Do captions from Kauli make us compliant?", "we are not a law firm",
                 "Do you provide audio description or live captions?", "Not today."):
        assert text in html, text


def test_it_never_claims_compliance_certification_or_a_finished_vpat(env):
    page = env.app.SOLUTION_PAGES[SLUG]
    blob = repr(page).lower()
    for banned in ("guarantee", "certified", "fully compliant", "100% compliant", "wcag compliant", "soc 2", "hipaa", "vpat is available", "download our vpat"):
        assert banned not in blob, banned
    vpat = next(f for f in page["faq"] if "vpat" in f["q"].lower())
    assert "preparing" in vpat["a"].lower()
