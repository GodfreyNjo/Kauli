"""Market pricing, the instant-AI tier's billing rules, Paystack config helpers
and the transcript exports. Pure-Python: no web server, no database."""
import io
import os
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp import billing, transcript_export  # noqa: E402
from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES, TranskriptorASR  # noqa: E402


def test_approved_price_card():
    assert billing.rate_for("transcribe", "intl", "ai") == 0.12
    assert billing.rate_for("translate", "intl", "ai") == 0.30
    assert billing.rate_for("dub", "intl", "ai") == 0.80
    assert billing.rate_for("transcribe", "intl", "human") == 1.79
    assert billing.rate_for("dub", "intl", "human") == 3.99
    assert billing.rate_for("transcribe", "ea", "ai") == 0.06
    assert billing.rate_for("dub", "ea", "human") == 1.20


def test_legacy_accounts_keep_old_human_rates_and_get_ea_ai_rates():
    for sl, old in (("transcribe", 0.40), ("translate", 0.90), ("dub", 1.50)):
        assert billing.rate_for(sl, "legacy", "human") == old
        assert billing.rate_for(sl, None, "human") == old
        assert billing.rate_for(sl, "legacy", "ai") == billing.PRICE_CARD["ea"][sl]["ai"]


def test_unknown_review_tier_is_treated_as_human():
    assert billing.rate_for("transcribe", "intl", "nonsense") == 1.79


def test_market_for_country():
    assert billing.market_for_country("KE") == "ea"
    assert billing.market_for_country("ug") == "ea"
    assert billing.market_for_country("US") == "intl"
    assert billing.market_for_country(None) == "intl"
    assert billing.market_for_country("") == "intl"


def test_instant_ai_every_ai_price_is_far_below_human():
    for market in ("intl", "ea"):
        for sl in billing.SERVICE_LEVELS:
            assert billing.rate_for(sl, market, "ai") < billing.rate_for(sl, market, "human")


def test_order_cost_uses_market_and_tier_and_records_them():
    c = billing.order_cost_usd(10, "transcribe", "free", 0.0, market="intl", review_tier="ai")
    assert c["rate_per_min"] == 0.12 and c["total_usd"] == 1.20
    assert c["market"] == "intl" and c["review_tier"] == "ai"
    h = billing.order_cost_usd(10, "dub", "pro", 0.0, market="intl", review_tier="human")
    assert h["gross_usd"] == 39.9 and h["total_usd"] == 39.9  # no plan discount: Pro pays the same per-minute price as everyone


def test_default_call_is_unchanged_legacy_behaviour():
    c = billing.order_cost_usd(10, "translate", "free", 0.0)
    assert c["rate_per_min"] == 0.90 and c["total_usd"] == 9.0


def test_free_minutes_cover_an_instant_ai_order_fully():
    c = billing.order_cost_usd(5, "transcribe", "free", 5.0, market="intl", review_tier="ai")
    assert c["total_usd"] == 0.0 and c["free_minutes_applied"] == 5.0


def test_price_card_for_shape():
    card = billing.price_card_for("intl")
    assert set(card) == set(billing.SERVICE_LEVELS)
    assert set(card["dub"]) == {"ai", "human"}


def test_paystack_currency_defaults_to_kes_and_switches_to_usd(monkeypatch):
    monkeypatch.delenv("PAYSTACK_CURRENCY", raising=False)
    assert billing.paystack_currency() == "KES"
    monkeypatch.setenv("PAYSTACK_CURRENCY", "usd")
    assert billing.paystack_currency() == "USD"
    monkeypatch.setenv("PAYSTACK_CURRENCY", "EUR")
    assert billing.paystack_currency() == "KES"


def test_paystack_plan_code_inert_until_configured(monkeypatch):
    monkeypatch.delenv("PAYSTACK_PLAN_PRO", raising=False)
    assert billing.paystack_plan_code("pro") is None
    monkeypatch.setenv("PAYSTACK_PLAN_PRO", " PLN_abc ")
    assert billing.paystack_plan_code("pro") == "PLN_abc"


def _sub(plan="pro", end_in_days=-1, code=None):
    return {"plan": plan, "status": "active", "current_period_end": time.time() + end_in_days * 86400,
            "paystack_subscription_code": code}


def test_lapsed_plain_subscription_drops_to_free_but_recurring_gets_grace():
    user = {"email": "someone@example.com"}
    assert billing.effective_plan(user, _sub(end_in_days=-1)) == "free"
    assert billing.effective_plan(user, _sub(end_in_days=-1, code="SUB_x")) == "pro"
    assert billing.effective_plan(user, _sub(end_in_days=-(billing.RECURRING_GRACE_DAYS + 1), code="SUB_x")) == "free"


def test_transkriptor_languages_are_consistent():
    assert len(TRANSKRIPTOR_LANGUAGES) >= 70
    for key, (label, code) in TRANSKRIPTOR_LANGUAGES.items():
        assert label and "-" in code, key
    assert TranskriptorASR.LANG_CODES["sw"] == "sw-KE"
    assert TranskriptorASR.LANG_CODES["en"] == "en-US"
    assert TranskriptorASR.LANG_CODES["es"] == "es-US"


class _Seg:
    def __init__(self, start_ms, text, speaker=None, kind="speech", final=""):
        self.start_ms, self.source_transcript, self.source_final_text = start_ms, text, final
        self.speaker_id, self.segment_type = speaker, kind


class _Job:
    segments = [_Seg(0, "Hello & welcome", "Speaker 1"), _Seg(65000, "raw", final="Edited <line>"),
                _Seg(70000, ""), _Seg(80000, "[music]", kind="music")]


def test_txt_export_prefers_reviewed_text_and_skips_non_speech():
    out = transcript_export.to_txt(_Job(), "Transcript - a.mp4").decode()
    assert "[00:00] Speaker 1: Hello & welcome" in out
    assert "[01:05] Edited <line>" in out
    assert "music" not in out and "raw" not in out


def test_docx_export_is_a_valid_package_with_escaped_text():
    data = transcript_export.to_docx(_Job(), "Transcript - a.mp4")
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
    import xml.dom.minidom
    xml.dom.minidom.parseString(z.read("word/document.xml"))  # well-formed
    assert b"Hello &amp; welcome" in z.read("word/document.xml")


def test_charge_currency_is_per_market(monkeypatch):
    monkeypatch.delenv("PAYSTACK_CURRENCY", raising=False)
    assert billing.paystack_currency_for_market("intl") == "KES"  # USD not switched on yet
    monkeypatch.setenv("PAYSTACK_CURRENCY", "USD")
    assert billing.paystack_currency_for_market("intl") == "USD"
    assert billing.paystack_currency_for_market("ea") == "KES"  # M-Pesa only works in KES
    assert billing.paystack_currency_for_market("legacy") == "KES"
    assert billing.paystack_currency_for_market(None) == "KES"


# ---- multilingual SEO data -------------------------------------------------
def test_every_transcription_language_has_seo_facts_and_a_unique_slug():
    from webapp import seo_languages
    labels = {code: label for code, (label, _t) in TRANSKRIPTOR_LANGUAGES.items()}
    assert all(code in seo_languages.LANGUAGE_FACTS for code in labels)
    slugs = seo_languages.build_slug_index(labels)
    assert len(slugs) == len(labels)  # no two languages collapse onto one URL
    assert seo_languages.slugify("Chinese (Mandarin)") == "chinese-mandarin"
    assert seo_languages.display_name("Chinese (Mandarin)") == "Mandarin Chinese"


def test_every_locale_has_every_string_and_valid_placeholders():
    from webapp import locales
    fmt = dict(t="0.12", tr="0.30", d="0.80", ht="1.20", htr="1.40", hd="2.00", n=77, free=5)
    for code, strings in locales.STRINGS.items():
        assert set(strings) == set(locales.KEYS), code
        for value in strings.values():
            value.format(**fmt)  # raises on a stray or misspelled placeholder
    for code in locales.LOCALES:
        assert code in locales.STRINGS
    assert locales.LOCALES["ar"][2] == "rtl"


def test_localized_copy_never_claims_review_or_translation_for_all_languages():
    """The honest-scope guard: every locale must still say AI output is not
    reviewed by a person and that translation/dubbing/review are limited."""
    from webapp import locales
    for code, s in locales.STRINGS.items():
        assert "{n}" in s["a1"] and "{free}" in s["a3"], code
        assert s["ai_3"] and s["price_note"] and s["a2"], code
