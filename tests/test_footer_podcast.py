"""The public pages point people to the podcast on Spotify."""
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
