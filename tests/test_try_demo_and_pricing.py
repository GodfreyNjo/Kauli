"""The /try demo, the simplified pricing section with two monthly plans, the dynamic how-it-works steps, the studio
wave and the sign-in tidy-ups."""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    before = dict(os.environ)
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    for key in set(os.environ) - set(before):
        del os.environ[key]
    os.environ.update(before)
    return TestClient(app_mod.app, follow_redirects=False)


def _demo_data(html: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="hd-data">(.*?)</script>', html, re.S).group(1))


# ----------------------------------------------------------------- the demo
def test_demo_picks_language_and_service_for_the_visitor(client):
    kenya = _demo_data(client.get("/try", headers={"cf-ipcountry": "KE"}).text)
    assert kenya["language"] == "sw" and kenya["language_why"] == "location"
    spain = _demo_data(client.get("/try", headers={"cf-ipcountry": "ES"}).text)
    assert spain["language"] == "es"
    germany = _demo_data(client.get("/try", headers={"cf-ipcountry": "DE", "accept-language": "en"}).text)
    assert germany["language"] == "de"
    china = _demo_data(client.get("/try", headers={"cf-ipcountry": "CN"}).text)
    assert china["language"] == "zh"
    # no country: the browser language, then a sensible default
    assert _demo_data(client.get("/try", headers={"accept-language": "de-DE,de;q=0.9"}).text)["language"] == "de"
    assert _demo_data(client.get("/try").text)["language"] == "es"
    # an explicit choice always wins; the service follows the page the visitor came from
    chosen = _demo_data(client.get("/try?lang=zh&use=course", headers={"cf-ipcountry": "KE"}).text)
    assert chosen["language"] == "zh" and chosen["scenario"] == "course" and chosen["suggested"] == "sw"
    from_podcast = _demo_data(client.get("/try", headers={"referer": "https://kauli-forgemedia.com/solutions/podcast-transcription"}).text)
    assert from_podcast["scenario"] == "meeting" and from_podcast["scenario_why"] == "page"
    from_video = _demo_data(client.get("/try", headers={"referer": "https://kauli-forgemedia.com/solutions/video-dubbing"}).text)
    assert from_video["scenario"] == "video"
    junk = _demo_data(client.get("/try?lang=klingon&use=nope").text)
    assert junk["language"] in junk["languages"] and junk["scenario"] in junk["scenarios"]


def test_every_demo_clip_exists_and_dubbing_is_labelled_honestly(client):
    data = _demo_data(client.get("/try").text)
    assert set(data["languages"]) == {"es", "de", "zh", "sw"}
    video = data["scenarios"]["video"]
    assert video["service"] == "dub" and set(video["dub"]) == {"es", "de", "zh", "sw"}
    for scenario in data["scenarios"].values():
        r = client.get(scenario["src"])
        assert r.status_code == 200 and r.headers["content-type"].startswith("audio") and len(r.content) > 10_000
        for url in scenario["dub"].values():
            assert client.get(url).status_code == 200
        assert set(scenario["tr"]) == {"es", "de", "zh", "sw"} and all(scenario["tr"].values())
    # dubbing is only "available now" where the product really dubs; the rest is a preview
    assert data["languages"]["sw"]["dub_live"] is True
    assert data["languages"]["es"]["dub_live"] is False or "es" in os.environ.get("KAULI_REVIEW_EXTRA_LANGUAGES", "")
    html = client.get("/try").text
    assert "machine-generated and not reviewed by a person" in html and "ElevenLabs" in html
    assert 'data-cta="try-signup"' in html and 'href="/demo"' in html
    # nothing here can spend ElevenLabs characters at request time
    app_source = (ROOT / "webapp" / "app.py").read_text(encoding="utf-8")
    assert "demo_content" in app_source and "text-to-speech" not in re.search(r"def _try_context.*?def try_page", app_source, re.S).group(0)


def test_demo_is_linked_from_home_and_the_sitemap(client):
    home = client.get("/").text
    section = re.search(r'<section class="m-section" id="demo">.*?</section>', home, re.S).group(0)
    assert 'id="hd-data"' in section and 'name="hd-scenario"' in section and 'href="/try"' in section
    assert "See a sample" not in home and "demo-player" not in home            # the old Swahili sample is gone
    assert home.count('id="hd-data"') == 1
    assert "/try</loc>" in client.get("/sitemap.xml").text


# ------------------------------------------------------------------ pricing
def test_pricing_is_one_clear_choice_with_exactly_two_monthly_plans(client):
    html = client.get("/").text
    section = re.search(r'<section class="m-section" id="pricing">.*?</section>', html, re.S).group(0)
    assert "Simple pricing. Pay only for what you use." in section
    assert section.count('class="pz-plan') == 2 or section.count('<article class="pz-plan') == 2
    assert re.findall(r'<article class="pz-plan[^"]*"[^>]*>\s*(?:<span[^>]*>[^<]*</span>\s*)?<h4>([^<]+)</h4>', section) == ["Pro", "Premium"]
    for gone in ("Enterprise", "Free</div>"):
        assert gone not in section
    # no region is named in the public pricing, and no local currency is pushed on everyone
    for region in ("Kenya", "East Africa", "US &amp; international", "KES", "KSh"):
        assert region not in section, region
    assert "pz-market" not in section and "?market=" not in section
    assert "All prices are in US dollars" in section
    # the safety message sits next to the price
    for promise in ("Safe payments", "No surprises", "Private by default", "No lock-in"):
        assert promise in section


def test_kes_is_only_mentioned_to_visitors_in_kenya(client):
    kenya = client.get("/", headers={"cf-ipcountry": "KE"}).text
    elsewhere = client.get("/", headers={"cf-ipcountry": "DE"}).text
    assert "pay in KES with M-Pesa" in kenya and "M-Pesa PINs" in kenya
    assert "KES" not in elsewhere.split('id="pricing"', 1)[1].split("</section>", 1)[0] and "M-Pesa" not in elsewhere.split('id="pricing"', 1)[1].split("</section>", 1)[0]


# --------------------------------------------------------------- how it works
def test_how_it_works_steps_play_through_and_stay_readable_without_script(client):
    html = client.get("/").text
    section = re.search(r'<section class="m-section" id="how".*?</section>', html, re.S).group(0)
    assert section.count('class="how-step"') == 4 and section.count('class="how-tick"') == 4
    assert "prefers-reduced-motion" in section and "IntersectionObserver" in section and "setInterval(next" in section
    css = (ROOT / "webapp/static/style.css").read_text(encoding="utf-8")
    assert ".how-js .how-step.is-active" in css and ".how-js .how-step.done .how-tick" in css


# ------------------------------------------------------ studio wave + sign-in
def test_studio_wave_runs_on_public_pages_and_respects_reduced_motion(client):
    for path in ("/", "/try", "/languages", "/login"):
        assert "/static/studio_wave.js" in client.get(path).text, path
    js = (ROOT / "webapp/static/studio_wave.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js and "visibilitychange" in js and "IntersectionObserver" in js
    assert client.get("/static/studio_wave.js").status_code == 200


def test_signin_consent_is_short_and_password_rules_start_folded(client):
    html = client.get("/login?mode=signup").text
    assert "service emails needed to run your account.</p>" in html and "order status, receipts" not in html
    css = (ROOT / "webapp/static/style.css").read_text(encoding="utf-8")
    assert ".auth-shell .password-checklist.is-open" in css and "toggle('is-open', v.length > 0)" in html


def test_pay_as_you_go_replaces_the_free_plan_label_and_enterprise_is_not_a_public_tier():
    billing_html = (ROOT / "webapp/templates/billing.html").read_text(encoding="utf-8")
    assert "Pay as you go' if key == 'free'" in billing_html and "key != 'enterprise' or key == current_plan" in billing_html
