"""The 2026-10 redesign (Kauli_UI_UX_Audit_and_Redesign_Plan): page structure,
one consistent primary action, login accessibility, a WCAG contrast guard read
from the REAL design tokens, and the service-worker / logo fixes."""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
CSS = (ROOT / "webapp/static/style.css").read_text(encoding="utf-8")


# ----------------------------------------------------------- design tokens
def _tokens(block_start: str, block_end: str) -> dict:
    block = CSS[CSS.index(block_start):CSS.index(block_end)]
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


LIGHT = _tokens(":root {", 'html[data-theme="dark"]')
DARK = _tokens('html[data-theme="dark"]', "* { box-sizing")


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_palette_is_the_documented_one():
    assert LIGHT["navy"] == "#0f2342"            # primary navy
    assert LIGHT["accent"] == "#1e5abe"          # action blue
    assert LIGHT["bg"] == "#f4f7fb"              # light background
    assert LIGHT["muted"] == "#4b5b73"           # muted text
    assert LIGHT["panel"] == "#ffffff"


@pytest.mark.parametrize("theme,tokens,text_tokens,surface_tokens", [
    ("light", LIGHT, ["ink", "heading-ink", "muted", "faint", "accent-text-resolved"],
     ["bg", "sidebar-bg", "panel", "surface", "hover", "active-bg"]),
    ("dark", DARK, ["ink", "heading-ink", "muted", "faint", "accent-text", "err-text"],
     ["bg", "sidebar-bg", "panel", "surface", "hover", "active-bg"]),
])
def test_text_meets_wcag_aa_on_every_surface(theme, tokens, text_tokens, surface_tokens):
    merged = dict(tokens)
    merged["accent-text-resolved"] = tokens["accent"]  # light --accent-text is var(--accent)
    for t in text_tokens:
        for s in surface_tokens:
            r = _ratio(merged[t], merged[s])
            assert r >= 4.5, f"{theme}: {t} {merged[t]} on {s} {merged[s]} = {r:.2f}:1"


def test_buttons_and_navy_surfaces_meet_contrast():
    assert _ratio("#ffffff", LIGHT["accent"]) >= 4.5            # white on action blue
    assert _ratio("#ffffff", LIGHT["accent-hover"]) >= 4.5
    assert _ratio("#ffffff", "#3070e4") >= 4.5                  # hero/cta-band button on navy
    assert _ratio("#ffffff", LIGHT["navy"]) >= 4.5
    assert _ratio(LIGHT["on-navy-muted"], LIGHT["navy"]) >= 4.5
    assert _ratio(LIGHT["on-navy-accent"], LIGHT["navy"]) >= 4.5


# ------------------------------------------------- service worker + assets
def test_service_worker_is_network_first_and_cache_is_versioned():
    sw = (ROOT / "webapp/static/sw.js").read_text(encoding="utf-8")
    assert 'kauli-shell-v2' in sw                  # old cache-first v1 entries get purged on activate
    assert "network-first" in sw.lower()
    assert "caches.match(req).then((cached) => cached || fetch(req))" not in sw  # the stale-CSS bug


def test_manifest_uses_the_brand_navy():
    import json
    m = json.loads((ROOT / "webapp/static/manifest.json").read_text(encoding="utf-8"))
    assert m["theme_color"] == "#0f2342" and m["background_color"] == "#0f2342"


def test_logo_variants_are_small_and_present():
    static = ROOT / "webapp/static"
    for f in ("kauli-logo-nav-sm.webp", "kauli-logo-nav-light.webp"):
        assert (static / f).stat().st_size < 30 * 1024, f
    for f in ("kauli-logo-nav-sm.png", "kauli-logo-nav-light.png"):
        assert (static / f).stat().st_size < 60 * 1024, f


# ------------------------------------------------------------------- pages
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


def test_homepage_structure_follows_the_plan(client):
    html = client.get("/").text
    assert len(re.findall(r"<h1[ >]", html)) == 1
    assert re.search(r"<h1[^>]*>Transcribe Audio to Text in \d+ Languages</h1>", html)
    assert "Nairobi" not in html
    ids = re.findall(r'<section[^>]*id="([a-z-]+)"', html)
    assert ids[:5] == ["services", "demo", "why", "integrations", "pricing"], ids   # services before pricing, as specified
    for needed in ("how", "faq", "book", "start"):
        assert needed in ids
    # the plan's removed/condensed sections stay gone
    for gone in ("who", "choose"):
        assert gone not in ids
    assert "AI Drafted, Human Verified" not in html


def test_one_consistent_primary_action(client):
    html = client.get("/").text
    assert html.count(">Start a project<") >= 2          # nav and final band; the hero says "Try Free Transcription"
    assert "Get started free" not in html
    assert 'data-cta="hero"' in html and 'data-cta="nav"' in html and 'data-cta="final"' in html
    for path in ("/solutions/audio-to-text", "/languages"):
        page = client.get(path).text
        assert ">Start a project<" in page and "Start free - first" not in page


def test_pricing_has_a_clear_hierarchy(client):
    html = client.get("/").text
    i_payg, i_extras, i_plans = (html.index(x) for x in ("Pay as you go", "Optional extras", "Monthly plans"))
    assert i_payg < i_extras < i_plans
    assert "Estimate your cost" in html


def test_primary_nav_matches_the_plan(client):
    nav = re.search(r'<nav class="m-nav".*?</nav>', client.get("/").text, re.S).group(0)
    positions = [nav.index(label) for label in ("Services", "Solutions", "Pricing", "Resources", "Sign in", "Start a project")]
    assert positions == sorted(positions)


def test_login_is_task_focused_and_accessible(client):
    html = client.get("/login").text
    assert "Welcome back" in html and "Sign in to manage your projects and downloads." in html
    assert 'autocomplete="current-password"' in html and 'autocomplete="new-password"' in html
    assert 'autocomplete="username"' in html and 'autocomplete="email"' in html
    assert "Forgot your password?" in html and "Create an account" in html
    assert 'href="/terms"' in html and 'href="/privacy"' in html
    assert "Human expertise. Global reach." in html
    assert "AI Drafted, Human Verified" not in html
    # every input is wrapped in a visible label
    assert html.count('class="auth-field-label"') >= 4
    assert "Continue with Google" in html  # implemented (/login/google) - the plan says only show it if it works


def test_login_errors_are_announced(client):
    html = client.get("/login?mode=signin&error=Wrong+email+or+password").text
    assert 'role="alert"' in html and "Wrong email or password" in html


def test_footer_shows_contact_details(client):
    html = client.get("/").text
    assert 'href="tel:' in html and "mailto:" in html


def test_developer_docs_page_is_public_and_linked(client):
    page = client.get("/developers")
    assert page.status_code == 200
    html = page.text
    for needed in ("POST /api/v1/orders", "Authorization: Bearer", "X-Kauli-Signature", "/api/v1/languages",
                   "claude mcp add", "kauli_create_order"):
        assert needed in html
    assert 'href="/developers"' in client.get("/").text            # reachable from the nav and footer
    assert "/developers" in client.get("/sitemap.xml").text


def test_integrations_section_is_honest_about_what_exists(client):
    html = client.get("/").text
    block = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
    # what is really built is marked Available and linked to its docs...
    for name in ("API", "MCP server", "Webhooks"):
        assert re.search(rf'<a href="/developers[^"]*" class="int-chip">.*?{re.escape(name)}.*?Available', block, re.S)
    # ...and nothing unbuilt is ever presented as available
    for name in ("Zoom", "Slack", "Notion", "Google Drive", "HubSpot", "Salesforce"):
        m = re.search(rf'<span class="int-name">{re.escape(name)}</span>\s*<span class="int-badge (\w+)">', block)
        assert m and m.group(1) == "soon", name
