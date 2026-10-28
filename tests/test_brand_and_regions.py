"""The two offices in the footer, the brand colour tokens, and region-based prices."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from webapp import billing  # noqa: E402

ROOT = Path(__file__).parent.parent
CSS = (ROOT / "webapp/static/style.css").read_text(encoding="utf-8")


def test_the_footer_lists_both_offices_on_every_public_page():
    import os
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    client = TestClient(app_mod.app, follow_redirects=False)
    for path in ("/", "/try", "/languages", "/human-review", "/human-review/swahili", "/developers", "/demo"):
        html = client.get(path).text
        assert "9th floor, Nation Centre, Kimathi Street, Nairobi, Kenya" in html, path
        assert "3710 Rawlins St, Suite 1420, Dallas, TX 75219, USA" in html, path
    home = client.get("/").text
    assert '"addressLocality": "Nairobi"' in home and '"postalCode": "75219"' in home      # for search engines too


def test_the_brand_colours_come_from_the_logo_and_are_used_through_tokens():
    blue = re.search(r"--brand-blue:\s*(#[0-9a-f]{6});", CSS).group(1)
    orange = re.search(r"--brand-orange:\s*(#[0-9a-f]{6});", CSS).group(1)
    navy = re.search(r"--brand-navy:\s*(#[0-9a-f]{6});", CSS).group(1)
    assert (blue, orange, navy) == ("#0878c8", "#f47a0a", "#082848")
    assert "--accent: #0878c8;" in CSS                         # the client area uses the same blue as the public site
    for old in ("#556b2f", "#9ab255", "#46581f", "#eaf0d8", "#2262ec"):
        assert old not in CSS.lower(), old                     # olive and the old blue are gone everywhere
    # orange stays an accent: it is never a background colour or a text colour on light surfaces
    assert not re.search(r"background(?:-color)?:\s*(?:#f47a0a|var\(--brand-orange\))", CSS)


def test_human_review_is_priced_just_under_the_mainstream_services_and_the_lower_price_card_is_unchanged():
    human = {k: billing.PRICE_CARD["intl"][k]["human"] for k in ("transcribe", "translate", "dub")}
    assert human == {"transcribe": 1.79, "translate": 2.99, "dub": 3.99}
    assert human["transcribe"] < 1.99                          # below Rev ($1.99) and Happy Scribe ($2.00) for human transcription
    assert billing.PRICE_CARD["ea"]["transcribe"]["human"] == 0.60 and billing.PRICE_CARD["ea"]["dub"]["human"] == 1.20


def test_lower_prices_are_for_lower_income_regions_only():
    for country in ("KE", "UG", "NG", "IN", "PK", "BD", "PH", "GH", "ZM", "EG"):
        assert billing.market_for_country(country) == "ea", country
    for country in ("US", "GB", "DE", "FR", "CA", "AU", "JP", "AE", "ZA", "BR", None, ""):
        assert billing.market_for_country(country) == "intl", country
    assert billing.rate_for("transcribe", billing.market_for_country("NG"), "human") == 0.60
    assert billing.rate_for("transcribe", billing.market_for_country("US"), "human") == 1.79


def test_only_clients_in_kenya_are_charged_in_shillings(monkeypatch):
    monkeypatch.setenv("PAYSTACK_CURRENCY", "USD")
    f = billing.paystack_currency_for_market
    assert f("ea", "KE") == "KES" and f("legacy", "KE") == "KES"
    assert f("ea", "NG") == "USD" and f("ea", "IN") == "USD" and f("intl", "KE") == "USD"
    assert f("ea") == "KES" and f("intl") == "USD"             # without a country the old rule applies
    monkeypatch.delenv("PAYSTACK_CURRENCY")
    assert f("intl", "US") == "KES"                            # dollars not switched on: unchanged
