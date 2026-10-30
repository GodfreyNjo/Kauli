"""The public pages point people to the podcast on Spotify."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401

SHOW = "https://open.spotify.com/show/0mILTmuoriNngEix8XzWyf"


def test_the_footer_links_to_the_podcast_on_spotify(env):
    page = env.client.get("/blog").text
    assert f'href="{SHOW}"' in page and "Podcast on Spotify" in page
    assert 'rel="noopener"' in page.split(SHOW, 1)[1][:80]
