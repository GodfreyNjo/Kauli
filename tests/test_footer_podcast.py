"""The public pages point people to the podcast on Spotify."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401

SHOW = "https://open.spotify.com/show/0mILTmuoriNngEix8XzWyf"


def test_google_search_console_verification_file_is_served_exactly(env):
    r = env.client.get("/google5dd3b740dee7aa1f.html")
    assert r.status_code == 200 and r.text == "google-site-verification: google5dd3b740dee7aa1f.html"
    assert env.client.get("/google5dd3b740dee7aa1f.html", headers={"accept-language": "sw"}).text == r.text      # never translated


def test_the_footer_links_to_the_podcast_on_spotify(env):
    page = env.client.get("/blog").text
    assert f'href="{SHOW}"' in page and "Podcast on Spotify" in page
    assert 'rel="noopener"' in page.split(SHOW, 1)[1][:80]


def test_no_public_page_claims_to_be_a_shop_product(env):
    """Google reads a Product with prices as a merchant listing and demands an image, shipping and returns. Kauli is a software service, so the
    price list is a Service (and the plans stay on the SoftwareApplication)."""
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for path in ("/", "/blog", "/podcast", "/developers", "/solutions/e-learning", "/human-review"):
        ld = [json.loads(b.split("</script>")[0]) for b in env.client.get(path).text.split('<script type="application/ld+json">')[1:]]
        flat = json.dumps(ld)
        assert '"@type": "Product"' not in flat and '"Product"' not in flat, path
    home = [json.loads(b.split("</script>")[0]) for b in env.client.get("/").text.split('<script type="application/ld+json">')[1:]]
    service = next(d for d in home if d.get("@type") == "Service" and d.get("offers"))
    assert service["provider"]["@id"].endswith("/#organization") and len(service["offers"]) >= 2
    assert any(d.get("@type") == "SoftwareApplication" for d in home)


def test_the_home_page_tells_search_engines_which_profiles_are_ours(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)                  # the public home page, as a visitor sees it
    ld = [json.loads(b.split("</script>")[0]) for b in env.client.get("/").text.split('<script type="application/ld+json">')[1:]]
    org = next(d for d in ld if d.get("@type") == "Organization")
    assert "https://www.linkedin.com/company/106627832/" in org["sameAs"] and "https://www.youtube.com/@kauli-forgemedia" in org["sameAs"]


def test_the_footer_links_to_the_youtube_channel(env):
    page = env.client.get("/blog").text
    channel = "https://www.youtube.com/@kauli-forgemedia"
    assert page.count(f'href="{channel}"') == 2                      # the icon beside the logo and the text link under Company
    assert 'aria-label="Kauli on YouTube"' in page and "Kauli on YouTube" in page
    assert 'rel="noopener"' in page.split(channel, 1)[1][:60]


def test_the_footer_links_to_the_public_linkedin_page_not_the_admin_dashboard(env):
    page = env.client.get("/blog").text
    company = "https://www.linkedin.com/company/106627832/"
    assert page.count(f'href="{company}"') == 2 and "Kauli on LinkedIn" in page
    assert "/admin" not in page.split(company, 1)[1][:40]
