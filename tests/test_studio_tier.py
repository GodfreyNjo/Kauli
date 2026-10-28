"""The Studio tier (human review + ElevenLabs' best), Scribe recognition, and human review for Chinese, German and
Spanish by outside contractors. No network: httpx is replaced, the database is a throwaway."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kauli.providers import asr as asr_mod  # noqa: E402
from kauli.providers import elevenlabs_audio as ea  # noqa: E402
from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)
from webapp import billing  # noqa: E402

VOICE = "21m00Tcm4TlvDq8ikWAM"


def w(text, start, end, speaker="speaker_0", logprob=-0.05, kind="word"):
    return {"text": text, "type": kind, "start": start, "end": end, "speaker_id": speaker, "logprob": logprob}


# ------------------------------------------------------------------ Scribe segments
def test_words_become_segments_at_pauses_speaker_changes_and_sentence_ends():
    words = [w("Hello", 0.0, 0.4), w("there.", 0.5, 0.9), w("Good", 1.0, 1.3, "speaker_1"),     # speaker change
             w("morning", 1.4, 1.9, "speaker_1"), w("everyone", 3.0, 3.5, "speaker_1"),              # 1.1 s pause
             w("and", 3.6, 3.8, "speaker_1"), w("welcome.", 3.9, 6.4, "speaker_1"), w("Next", 6.5, 6.9, "speaker_1"),
             w(" ", 0.9, 1.0, kind="spacing"), w("(laughter)", 1.0, 1.2, None, kind="audio_event")]
    segs = ea.build_segments(words, "en")
    assert [s.source_transcript for s in segs] == ["Hello there.", "Good morning", "everyone and welcome.", "Next"]
    assert [s.speaker_id for s in segs] == ["Speaker 1", "Speaker 2", "Speaker 2", "Speaker 2"]
    assert segs[0].segment_id == "seg_0001" and segs[1].index == 1 and segs[0].source_language == "en"
    assert [(x.text, x.start_ms) for x in segs[0].words] == [("Hello", 0), ("there.", 500)]
    assert 0.9 < segs[0].source_confidence <= 1.0


def test_a_single_speaker_recording_gets_no_labels_and_long_runs_are_cut():
    words = [w(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(60)]                       # 30 s of unbroken speech
    segs = ea.build_segments(words, "sw")
    assert all(s.speaker_id is None for s in segs) and len(segs) >= 3
    assert all(s.end_ms - s.start_ms <= 12500 for s in segs)
    assert ea.build_segments([], "en") == []


def test_scribe_is_asked_for_the_language_and_speakers(monkeypatch, tmp_path):
    import subprocess
    clip = tmp_path / "c.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", str(clip)], check=True)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    seen = {}

    def post(url, headers=None, files=None, data=None, timeout=None):
        seen.update(url=url, data=data)
        return SimpleNamespace(status_code=200, json=lambda: {"words": [w("Habari", 0, 0.5), w("yako.", 0.6, 1.0)]})

    segs = ea.scribe_segments(str(clip), "sw", post=post)
    assert seen["url"].endswith("/v1/speech-to-text")
    assert seen["data"]["language_code"] == "sw" and seen["data"]["diarize"] == "true" and seen["data"]["model_id"] == "scribe_v2"
    assert [s.source_transcript for s in segs] == ["Habari yako."]


def test_the_studio_recogniser_falls_back_to_whisper_and_says_so(monkeypatch):
    monkeypatch.setattr(ea, "scribe_segments", lambda path, language: (_ for _ in ()).throw(RuntimeError("ElevenLabs 429: slow down")))
    marker = [SimpleNamespace(segment_id="seg_0001")]
    monkeypatch.setattr(asr_mod.FasterWhisperASR, "transcribe", lambda self, path, language="sw": marker)
    provider = asr_mod.get_asr("elevenlabs-scribe")
    assert provider.name == "elevenlabs-scribe" and provider.transcribe("x.wav", "en") is marker
    assert provider.fallback_used and "429" in provider.fallback_reason
    monkeypatch.setattr(ea, "scribe_segments", lambda path, language: [])
    again = asr_mod.get_asr("elevenlabs-scribe")
    again.transcribe("x.wav", "en")
    assert again.fallback_used and "no speech" in again.fallback_reason


# ----------------------------------------------------------------------- pricing guards
def test_studio_extras_are_priced_per_part_and_clear_the_margin_floor():
    assert billing.studio_extras_per_min("transcribe") == 0.40 and billing.studio_extras_per_min("translate") == 0.40
    assert billing.studio_extras_per_min("dub") == 0.90
    assert billing.premium_asr_margin_ok() and billing.audio_cleanup_margin_ok() and billing.premium_voice_margin_ok()
    cost = billing.audio_cleanup_unit_cost_usd() + billing.SPEAKER_LABELS_USD_PER_MIN
    assert (0.40 * (1 - billing.PAYMENT_FEE_RATE) - cost) / 0.40 > 0.6                  # the markup on the ElevenLabs part


def test_extra_language_review_is_only_offered_above_the_contractor_cost_floor(monkeypatch):
    assert billing.extra_review_min_total_rate() == pytest.approx(1.5 / (1 - 0.039 - 0.30))
    assert billing.review_extra_margin_ok(billing.rate_for("transcribe", "intl", "human"))          # $1.20 + $1.20
    assert billing.review_extra_margin_ok(billing.rate_for("dub", "intl", "human"))
    assert not billing.review_extra_margin_ok(billing.rate_for("transcribe", "ea", "human"))        # Kenya/East Africa prices are lower
    monkeypatch.setenv("KAULI_EXTRA_REVIEW_COST_PER_MIN", "3.00")                                    # a dearer contractor quote
    assert not billing.review_extra_margin_ok(billing.rate_for("transcribe", "intl", "human"))
    monkeypatch.setenv("KAULI_EXTRA_REVIEW_COST_PER_MIN", "0.50")
    assert billing.review_extra_margin_ok(billing.rate_for("transcribe", "ea", "human"))


# ------------------------------------------------------------------------- ordering Studio
@pytest.fixture
def studio(env, wav_bytes, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "s")
    monkeypatch.setattr(env.app, "_audio_cleanup_capacity_ok", lambda minutes: True)
    monkeypatch.setattr(env.app, "_premium_capacity_ok", lambda minutes: True)
    monkeypatch.setattr(env.app, "_premium_voices", lambda force=False: [{"id": VOICE, "name": "Rachel", "description": ""}])
    env.wav = wav_bytes
    env.go = lambda **kw: env.place(wav_bytes, **{"source_lang": "sw", "target_lang": "en", "service_level": "translate",
                                                   "review_tier": "studio", **kw})
    return env


def _addons(order):
    return json.loads(order["addons"] or "[]")


def test_studio_is_a_human_reviewed_order_with_elevenlabs_recognition(studio):
    order = studio.order_from(studio.go())
    assert order["review_tier"] == "human" and order["asr"] == "elevenlabs-scribe"
    assert set(_addons(order)) == {"audio_cleanup", "premium_asr"}
    human = studio.order_from(studio.go(review_tier="human"))
    assert human["asr"] != "elevenlabs-scribe" and _addons(human) == []                   # today's tier is untouched
    extra = order["cost_usd"] - human["cost_usd"]
    assert extra == pytest.approx(0.40 * order["duration_minutes"], abs=0.01)             # human rate + $0.40/min, nothing more


def test_studio_dubbing_adds_the_premium_voice_and_needs_a_chosen_voice(studio):
    order = studio.order_from(studio.go(service_level="dub", premium_voice_id=VOICE))
    assert set(_addons(order)) == {"audio_cleanup", "premium_asr", "premium_voice"}
    assert order["tts"] == "elevenlabs" and order["dub_voice"] == f"elevenlabs:{VOICE}" and order["review_tier"] == "human"
    human_dub = studio.order_from(studio.go(service_level="dub", review_tier="human"))
    assert order["cost_usd"] - human_dub["cost_usd"] == pytest.approx(0.90 * order["duration_minutes"], abs=0.01)
    r = studio.go(service_level="dub", premium_voice_id="NOTAVOICEID123456")
    assert "listed premium voices" in r.text


def test_studio_is_refused_where_it_cannot_be_delivered(studio, monkeypatch):
    for kw, text in (({"source_lang": "ki"}, "language pair"), ({"source_lang": "fr", "target_lang": "en"}, "language pair"),
                     ({"target_lang": "fr"}, "language pair")):
        r = studio.go(**kw)
        assert r.status_code == 200 and text in r.text, kw
    monkeypatch.setenv("ELEVENLABS_ISOLATOR_USD_PER_MIN", "0.50")                          # would lose money
    assert "available right now" in studio.go().text
    monkeypatch.delenv("ELEVENLABS_ISOLATOR_USD_PER_MIN")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert "available right now" in studio.go().text
    assert studio.db.list_orders_for_client("u1") == []


def test_the_wizard_offers_studio_only_when_it_can_be_sold(studio, monkeypatch):
    html = studio.client.get("/client").text
    assert 'value="studio"' in html and "Top tier" in html and 'data-premium-asr-rate="0.1"' in html
    assert "$0.40/audio-minute" in html and "$0.90" in html and 'id="price-studio-row"' in html
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert 'value="studio"' not in studio.client.get("/client").text


def test_the_worker_does_not_double_label_speakers_for_studio_and_refunds_a_failed_recogniser(studio):
    worker = studio.app.worker
    calls = []
    studio.monkeypatch.setattr(worker, "kauli_run", lambda **kw: calls.append(kw))
    state = {"providers": {"asr": "elevenlabs-scribe", "audio_cleanup": "elevenlabs"}}
    studio.monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(
        cost_usd=0.01, flagged_count=0, providers=state["providers"])))
    studio.monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    order = studio.order_from(studio.go(instr_speaker_ids="1"))
    before = studio.db.wallet_credits_remaining("u1")
    worker._run_job(order["id"])
    assert calls[0]["asr"] == "elevenlabs-scribe" and calls[0]["clean_audio"] is True and calls[0]["label_speakers"] is False
    assert studio.db.wallet_credits_remaining("u1") == before                              # delivered as sold
    state["providers"] = {"asr": "faster-whisper", "audio_cleanup": "elevenlabs"}          # Scribe failed, whisper used
    worker._run_job(order["id"])
    assert studio.db.wallet_credits_remaining("u1") - before == pytest.approx(billing.usd_to_credits(0.10 * order["duration_minutes"]), abs=0.01)
    assert studio.db.get_order(order["id"])["premium_asr_refunded_at"]
    worker._run_job(order["id"])                                                           # never twice
    assert studio.db.wallet_credits_remaining("u1") - before == pytest.approx(billing.usd_to_credits(0.10 * order["duration_minutes"]), abs=0.01)


# --------------------------------------------------------- Chinese, German and Spanish
def test_extra_languages_are_off_until_the_owner_turns_them_on(studio):
    assert studio.app._review_extra_languages() == set()
    r = studio.go(source_lang="zh", target_lang="en", review_tier="human")
    assert "Instant AI" in r.text and studio.db.list_orders_for_client("u1") == []         # as before: AI only
    assert 'data-extra-review-langs=""' in studio.client.get("/client").text


@pytest.fixture
def extras(studio, monkeypatch):
    monkeypatch.setenv("KAULI_REVIEW_EXTRA_LANGUAGES", "zh,de,es,xx,ki")                    # junk and Kikuyu are ignored
    return studio


def test_the_switch_only_accepts_chinese_german_and_spanish(extras):
    assert extras.app._review_extra_languages() == {"zh", "de", "es"}
    assert extras.app._studio_languages() == {"en", "sw", "zh", "de", "es"}


def test_chinese_german_and_spanish_can_be_human_reviewed_with_the_specialist_add_on(extras):
    base = extras.order_from(extras.go(source_lang="sw", target_lang="en", review_tier="human"))
    for code in ("zh", "de", "es"):
        order = extras.order_from(extras.go(source_lang=code, target_lang="en", review_tier="human"))
        assert order["review_tier"] == "human" and "review_extra" in _addons(order)
        assert order["cost_usd"] - base["cost_usd"] == pytest.approx(1.20 * order["duration_minutes"], abs=0.01)
    into = extras.order_from(extras.go(source_lang="en", target_lang="de", review_tier="human"))
    assert "review_extra" in _addons(into) and into["target_lang"] == "de"
    ai = extras.order_from(extras.go(source_lang="zh", target_lang="en", review_tier="ai"))
    assert ai["review_tier"] == "ai" and _addons(ai) == []                                  # Instant AI is exactly as before


def test_studio_works_for_the_extra_languages_including_dubbing_with_a_premium_voice(extras):
    order = extras.order_from(extras.go(source_lang="es", target_lang="en"))
    assert order["review_tier"] == "human" and set(_addons(order)) == {"audio_cleanup", "premium_asr", "review_extra"}
    dub = extras.order_from(extras.go(source_lang="en", target_lang="zh", service_level="dub", premium_voice_id=VOICE))
    assert dub["tts"] == "elevenlabs" and dub["target_lang"] == "zh"
    assert set(_addons(dub)) == {"audio_cleanup", "premium_asr", "premium_voice", "review_extra"}


def test_dubbing_into_an_extra_language_needs_a_premium_voice(extras):
    r = extras.go(source_lang="en", target_lang="de", service_level="dub", review_tier="human")
    assert "needs a premium voice" in r.text
    ok = extras.order_from(extras.go(source_lang="en", target_lang="de", service_level="dub", review_tier="human",
                                     addon_premium_voice="1", premium_voice_id=VOICE))
    assert ok["tts"] == "elevenlabs"


def test_extra_language_review_is_refused_where_it_would_lose_money(extras, monkeypatch):
    extras.db.set_billing_market("u1", "ea")                                                # lower East Africa prices
    r = extras.go(source_lang="zh", target_lang="en", review_tier="human")
    assert "pricing region" in r.text and extras.db.list_orders_for_client("u1") == []
    assert extras.order_from(extras.go(source_lang="zh", target_lang="en", review_tier="ai"))["review_tier"] == "ai"
    extras.db.set_billing_market("u1", "intl")
    monkeypatch.setenv("KAULI_EXTRA_REVIEW_COST_PER_MIN", "5.00")                           # a contractor quote we cannot cover
    assert "pricing region" in extras.go(source_lang="de", target_lang="en", review_tier="human").text


def test_the_wizard_lists_the_extra_languages_for_human_review_when_on(extras):
    html = extras.client.get("/client").text
    assert 'label="Human review by language specialists"' in html and 'data-extra-review-langs="de,es,zh"' in html
    assert 'data-premium-langs="de,en,es,sw,zh"' in html
    ai_group = html.split('label="Instant AI transcription only"')[1].split("</optgroup>")[0]
    assert 'value="zh"' not in ai_group and 'value="fr"' in ai_group                          # moved out of the AI-only group


def test_languages_show_their_real_names_not_just_english_and_swahili(extras):
    assert extras.app.templates.env.globals["language_names"]["zh"] and extras.app.LANGUAGE_NAMES["de"] == "German"


# ------------------------------------------------------------------ the home page's blue band
def test_the_blue_band_names_only_the_languages_that_are_really_human_reviewed(extras, monkeypatch):
    html = extras.client.get("/").text if False else None
    monkeypatch.setattr(extras.app, "current_user", lambda request: None)
    band = extras.client.get("/").text.split('class="tk-band"')[1].split("</section>")[0]
    assert "Audio to text in minutes. Checked by an expert when it matters." in band
    assert "a real expert checks it before it is marked ready" in band and "editor" not in band
    assert "In selected languages, choose Human-reviewed" in band and 'href="/human-review"' in band


def test_the_blue_band_before_the_extra_languages_are_switched_on(studio, monkeypatch):
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    band = studio.client.get("/").text.split('class="tk-band"')[1].split("</section>")[0]
    assert "In selected languages, choose Human-reviewed" in band and "Swahili" not in band and "Chinese" not in band


# ------------------------------------------------------------------- the services section
def test_services_show_transcription_starting_from_zero_without_hiding_the_real_rate(studio, monkeypatch):
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    html = studio.client.get("/").text
    section = html.split('id="services"')[1].split("</section>")[0]
    assert "m-eyebrow-lined" in section and "Everything your video or audio needs to travel" in section
    assert "and selected languages can add human review." in section and 'href="/human-review"' in section
    prices = [p.strip() for p in __import__("re").findall(r'<span class="svc-price">(.*?)</span>', section, __import__("re").S)]
    assert prices[0].startswith("From $0") and "first 5 minutes free" in prices[0] and "/min" in prices[0]   # honest: free minutes, then a rate
    assert prices[1].startswith("From $") and prices[1].startswith("From $0 ") is False                      # translation and dubbing still show their rates
    assert prices[2].startswith("From $") and "per audio-minute" in prices[2]


# ------------------------------------------------------------------ the integrations section
def test_integrations_section_has_descriptions_chevrons_and_honest_statuses(studio, monkeypatch):
    import re
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    html = studio.client.get("/").text
    section = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
    assert "m-eyebrow-lined" in section and "Connect Kauli to the tools you already use" in section
    for desc in ("Integrate with your developer tools and automate your workflow.", "Publish and distribute your content automatically.",
                 "Keep your customer relationships in sync.", "Schedule and stay on track."):
        assert desc in section
    assert section.count('class="int-card"') == 9 and section.count('class="int-go"') == section.count('class="int-chip"')
    live = re.findall(r'<span class="int-name">([^<]+)</span>\s*<span class="int-badge live">', section)
    assert set(live) == {"API", "MCP server", "Webhooks", "Zapier, Make, n8n", "YouTube captions", "Notion", "Slack", "Microsoft Teams"}
    for name in ("Zoom", "Google Drive", "Dropbox", "HubSpot", "Salesforce", "Asana", "Trello", "Discord"):
        assert re.search(rf'<span class="int-name">{re.escape(name)}</span>\s*<span class="int-badge soon">', section), name


def test_every_integration_shows_a_real_company_logo_that_ships_with_the_site(studio, monkeypatch):
    import re
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    html = studio.client.get("/").text
    section = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
    srcs = re.findall(r'<img src="(/static/integrations/[a-z0-9]+\.svg)"', section)
    assert section.count('class="int-chip"') == 22 and len(set(srcs)) >= 20           # every row has a logo; Zapier row has three
    for src in sorted(set(srcs)):
        r = studio.client.get(src)
        body = r.text
        assert r.status_code == 200 and "svg" in r.headers["content-type"], src      # served by us, not a third party
        assert body.count("<svg") == 1 and not re.search(r"<script|on\w+=|<image|href=|<style", body), src   # plain logos only
    assert "http" not in " ".join(re.findall(r'<img src="([^"]+)"', section)).replace("/static/", "")
    for brand in ("slack", "notion", "zoom", "microsoftteams", "googledrive", "hubspot", "zapier", "make", "n8n"):
        assert f"/static/integrations/{brand}.svg" in section, brand
    assert "Logos are trademarks of their respective owners" in section and "does not imply endorsement" in section


def test_the_integrations_section_is_dynamic(studio, monkeypatch):
    import re
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    html = studio.client.get("/").text
    section = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
    live = len(re.findall(r'<li data-state="live">', section))
    soon = len(re.findall(r'<li data-state="soon">', section))
    assert live == 8 and soon == 14 and live + soon == section.count('class="int-chip"')
    # the counts on the filter buttons are computed from the data, so they can never drift from what is shown
    counts = re.findall(r'data-filter="(\w+)" aria-pressed="(?:true|false)">[^<]*<span>(\d+)</span>', section)
    assert counts == [("all", "22"), ("live", "8"), ("soon", "14")]
    assert section.count('aria-pressed="true"') == 1 and 'role="status" aria-live="polite"' in section
    assert section.count("--i:") == 9 and "IntersectionObserver" in section and "prefers-reduced-motion" in section
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert "@keyframes int-pulse" in css and "#integrations.int-anim .int-card:not(.is-in)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css and ".int-badge.live::before { animation: none; }" in css


def test_integration_cards_fold_into_dropdowns_that_open_on_their_own(studio, monkeypatch):
    import re
    monkeypatch.setattr(studio.app, "current_user", lambda request: None)
    html = studio.client.get("/").text
    section = re.search(r'<section class="m-section" id="integrations">.*?</section>', html, re.S).group(0)
    # every card has a real disclosure button wired to its panel, and the page works with the panel open when JS is off
    toggles = re.findall(r'<button type="button" class="int-toggle" id="int-btn-(\d)" aria-expanded="true" aria-controls="int-panel-(\d)">', section)
    assert len(toggles) == 9 and all(a == b for a, b in toggles)
    assert section.count('class="int-panel"') == 9 and section.count('role="region"') == 9
    assert "setInterval(advance" in section and "prefers-reduced-motion" in section and "stopAuto" in section
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".int-js .int-card:not(.is-open) .int-panel" in css and "grid-template-rows: 0fr" in css
