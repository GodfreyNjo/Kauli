"""The account home's seven action cards, the in-browser recorder, and the other pieces added to the order pages."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402

STATIC = Path(__file__).parent.parent / "webapp" / "static"


def _hrefs(page):
    return set(re.findall(r'<a class="hm-card[^"]*" href="([^"]+)"', page))


def test_home_has_the_seven_action_cards_with_the_right_destinations(env):
    env.monkeypatch.setenv("RECALL_API_KEY", "test-key")           # the meeting card links only when it is on
    page = env.client.get("/account/home").text
    hrefs = _hrefs(page)
    for expected in ("/account#order-form", "/account?source=record#order-form", "/account?source=youtube#order-form",
                     "/account/meetings", "/try", "/account/integrations"):
        assert expected in hrefs, expected
    for label in ("Upload &amp; Process", "Record &amp; Process", "Process from YouTube", "Process Meeting",
                  "Process from Cloud", "See sample", "Connect tools"):
        assert f"<strong>{label}</strong>" in page, label


def test_the_cloud_card_needs_no_setup_because_pasting_a_share_link_works_without_keys(env):
    env.monkeypatch.setenv("RECALL_API_KEY", "test-key")
    page = env.client.get("/account/home").text
    assert "is-soon" not in page and "/account?source=cloud#order-form" in _hrefs(page)


def test_the_meeting_card_says_soon_and_does_not_link_to_meetings_while_the_service_is_off(env):
    env.monkeypatch.delenv("RECALL_API_KEY", raising=False)
    page = env.client.get("/account/home").text
    assert 'href="/account/meetings"' not in page and "is-soon" in page


def test_the_upload_step_has_a_recorder_that_hands_its_file_to_the_wizard(env):
    page = env.client.get("/account").text
    assert 'id="mk-rec"' in page and 'id="rec-toggle"' in page and "/static/recorder.js" in page
    recorder = (STATIC / "recorder.js").read_text(encoding="utf-8")
    assert "kauliAddFiles" in recorder and "kauliSeconds" in recorder and "getUserMedia" in recorder
    wizard = (STATIC / "client_wizard.js").read_text(encoding="utf-8")
    assert "window.kauliAddFiles = addFiles" in wizard and "file.kauliSeconds" in wizard
    assert env.client.get("/static/recorder.js").status_code == 200


def test_the_review_step_can_switch_the_delivery_and_instant_ai_says_fast(env):
    page = env.client.get("/account").text
    assert 'id="rv-tiers"' in page and 'data-tier="ai"' in page and 'data-tier="human"' in page
    assert 'class="tier-badge tier-fast">Fast<' in page and 'id="rv-type"' in page and 'id="rv-rate"' in page


def test_a_finished_order_has_the_shortcut_cards_and_the_ready_download_button(env, wav_bytes):
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/account/orders/{order['id']}").text
    assert 'class="mk-quick"' in page and "Summary" in page and "Notes" in page and "Ask AI" in page
    assert 'class="od-dl" href="#order-files"' in page and 'id="order-files"' in page
