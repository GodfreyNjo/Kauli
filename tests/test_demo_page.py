"""The /demo page (book a demo), the removal of the personal phone/WhatsApp from everything
customers see, and the reply-to address in outgoing emails. Throwaway database; no network."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)

GOOD = {"first_name": "Amina", "last_name": "Otieno", "email": "amina@acme-media.org", "company": "Acme Media",
        "job_title": "Head of Content", "interest": "localization", "languages": "English, Swahili",
        "volume_estimate": "100min_plus", "org_type": "media_broadcast", "message": "We publish weekly shows."}


@pytest.fixture
def demo(env, monkeypatch):
    notified = []
    monkeypatch.setattr(env.app, "_notify_internal_new_lead", lambda *a, **k: notified.append(a))
    monkeypatch.delenv("KAULI_CALENDLY_URL", raising=False)
    # a logged-out visitor: a signed-in client is redirected away from the public home page
    monkeypatch.setattr(env.app, "current_user", lambda request: None)
    env.notified = notified
    return env


def _leads(env):
    conn = env.db.get_conn()
    return conn.execute("SELECT * FROM leads WHERE source = 'demo'").fetchall()


def test_demo_page_content_and_form(demo):
    page = demo.client.get("/demo")
    assert page.status_code == 200
    html = page.text
    assert "<h1>See Kauli on your own audio and video</h1>" in html
    for field in ("first_name", "last_name", "email", "company", "job_title", "interest", "languages",
                  "volume_estimate", "org_type", "message", "website"):
        assert f'name="{field}"' in html, field
    assert 'name="website"' in html and "demo-honeypot" in html            # spam trap, hidden off-screen
    assert "Your details" in html and "Pick a time" in html
    assert "hello@kauli-forgemedia.com" in html
    assert 'href="/privacy"' in html


def test_submitting_creates_a_demo_lead_with_everything_staff_needs(demo):
    r = demo.client.post("/demo", data=GOOD)
    assert r.status_code == 303 and r.headers["location"] == "/demo"
    (lead,) = _leads(demo)
    assert lead["source"] == "demo" and lead["name"] == "Amina Otieno" and lead["email"] == "amina@acme-media.org"
    assert lead["company"] == "Acme Media" and lead["volume_estimate"] == "100min_plus" and lead["org_type"] == "media_broadcast"
    for part in ("Head of Content", "Full localization", "English, Swahili", "We publish weekly shows."):
        assert part in lead["message"]
    assert lead["personal_email_flag"] == 0
    assert len(demo.notified) == 1 and demo.notified[0][2] == "amina@acme-media.org"


def test_a_personal_email_is_flagged_for_triage_but_accepted(demo):
    demo.client.post("/demo", data={**GOOD, "email": "someone@gmail.com"})
    (lead,) = _leads(demo)
    assert lead["personal_email_flag"] == 1


def test_step_two_without_a_calendar_says_when_we_will_reply(demo):
    demo.client.post("/demo", data=GOOD)
    html = demo.client.get("/demo").text
    assert "Thanks, Amina" in html and "amina@acme-media.org" in html and "within one business day" in html
    assert "calendly-inline-widget" not in html and 'name="first_name"' not in html


def test_step_two_with_a_calendar_embeds_it_prefilled(demo, monkeypatch):
    monkeypatch.setenv("KAULI_CALENDLY_URL", "https://calendly.com/kauli-team/demo")
    demo.client.post("/demo", data=GOOD)
    html = demo.client.get("/demo").text
    embed = re.search(r'data-url="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert embed.startswith("https://calendly.com/kauli-team/demo?")
    assert "name=Amina+Otieno" in embed and "email=amina%40acme-media.org" in embed
    assert "assets.calendly.com/assets/external/widget.js" in html


def test_use_a_different_email_resets_to_the_form(demo):
    demo.client.post("/demo", data=GOOD)
    r = demo.client.get("/demo/new")
    assert r.status_code == 303
    assert 'name="first_name"' in demo.client.get("/demo").text


def test_validation_keeps_what_was_typed_and_creates_nothing(demo):
    for bad, text in (({"first_name": ""}, "first and last name"), ({"email": "not-an-email"}, "valid work email"),
                      ({"company": ""}, "company"), ({"interest": ""}, "interested in"),
                      ({"volume_estimate": ""}, "monthly"), ({"org_type": ""}, "organization")):
        r = demo.client.post("/demo", data={**GOOD, **bad})
        assert r.status_code == 400 and text in r.text, bad
        assert 'value="Acme Media"' in r.text or bad.get("company") == ""        # earlier answers are kept
    assert _leads(demo) == [] and demo.notified == []


def test_the_honeypot_pretends_to_succeed_without_creating_a_lead(demo):
    r = demo.client.post("/demo", data={**GOOD, "website": "http://spam.example"})
    assert r.status_code == 303 and _leads(demo) == [] and demo.notified == []


def test_what_visitors_type_is_escaped(demo):
    demo.client.post("/demo", data={**GOOD, "first_name": "<script>alert(1)</script>"})
    html = demo.client.get("/demo").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_demo_is_in_the_sitemap_and_every_demo_link_points_at_it(demo):
    assert "/demo" in demo.client.get("/sitemap.xml").text
    home = demo.client.get("/").text
    assert 'data-cta="hero-demo"' in home and 'href="/demo" class="btn secondary" data-cta="hero-demo"' in home
    assert 'href="#book"' not in home and 'href="/#book"' not in demo.client.get("/solutions/audio-to-text").text
    assert 'href="/demo"' in demo.client.get("/solutions/audio-to-text").text


# ---------------------------------------------------- no personal phone / WhatsApp anywhere customers look
_CONTACT_LEAK = re.compile(r"wa\.me/254|\b0712\b|712 531|\+254712|tel:|WhatsApp us|message me on WhatsApp|WhatsApp me", re.I)


def test_no_public_page_shows_a_phone_number_or_our_whatsapp(demo):
    paths = {"/", "/demo", "/login", "/blog", "/languages", "/terms", "/privacy", "/developers", "/llms.txt",
             "/formatting-standards", "/status", "/help"}
    sitemap = demo.client.get("/sitemap.xml").text
    paths |= {re.sub(r"^https?://[^/]+", "", u) for u in re.findall(r"<loc>([^<]+)</loc>", sitemap)
              if "/blog/" not in u}                                          # posts are database content
    offenders = []
    for path in sorted(paths):
        r = demo.client.get(path)
        if r.status_code == 200 and _CONTACT_LEAK.search(r.text):
            offenders.append((path, _CONTACT_LEAK.search(r.text).group(0)))
    assert not offenders, offenders
    assert '"telephone"' not in demo.client.get("/").text                    # nor in the structured data
    assert "hello@kauli-forgemedia.com" in demo.client.get("/").text


def test_the_app_and_staff_pages_have_no_whatsapp_link_either(demo, monkeypatch):
    monkeypatch.setattr(demo.app, "current_user",
                        lambda request: {**dict(demo.db.get_user("u1")), "client_scope_id": "u1"})
    for path in ("/account", "/account/billing", "/account/files", "/settings"):
        html = demo.client.get(path).text
        assert not _CONTACT_LEAK.search(html), (path, _CONTACT_LEAK.search(html).group(0))
        assert "mailto:hello@kauli-forgemedia.com" in html, path


def test_outgoing_emails_reply_to_the_work_address_and_never_mention_whatsapp():
    root = Path(__file__).parent.parent / "webapp"
    for name in ("app.py", "worker.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "CONTACT_PHONE" not in text and "wa.me/{" not in text, name
        assert "kahunyurogodfrey@gmail.com" not in text.replace('INTERNAL_NOTIFY_EMAIL = "kahunyurogodfrey@gmail.com"', ""), name
    assert 'CONTACT_EMAIL = "hello@kauli-forgemedia.com"' in (root / "worker.py").read_text(encoding="utf-8")
    for template in (root / "templates").glob("*.html"):
        if template.name.startswith("staff_lead") or template.name in ("blog_post.html",):
            continue                                    # staff lead tools and post share buttons
        body = template.read_text(encoding="utf-8")
        assert "contact_whatsapp_url" not in body and "whatsapp_url" not in body, template.name
