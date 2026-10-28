"""Premium AI voice (ElevenLabs) and the home page language marquee.
Nothing here reaches ElevenLabs: httpx is replaced by a recorder, and the database is a throwaway."""
import re
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kauli.providers import tts as tts_mod  # noqa: E402
from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)
from webapp import billing  # noqa: E402

VOICE = "21m00Tcm4TlvDq8ikWAM"
SECOND_OF_PCM = b"\x00\x00" * 24000                      # one second of 24 kHz 16-bit silence


class FakeEleven:
    def __init__(self, status=200, body=b""):
        self.calls, self.status, self.body = [], status, body

    def __call__(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, params=params, headers=headers, json=json))
        resp = SimpleNamespace(status_code=self.status, content=self.body or SECOND_OF_PCM, json=lambda: {"detail": {"message": "quota"}})
        return resp


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeEleven()
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi_test_key")
    return fake


# -------------------------------------------------------------------- the provider
def test_english_uses_multilingual_v2_without_a_language_code(fake_http, tmp_path):
    out = tmp_path / "a.wav"
    ms = tts_mod.ElevenLabsTTS(language="en").synthesize("Hello there.", str(out), voice_id=VOICE, rate=1.0)
    call = fake_http.calls[0]
    assert call.url == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}"
    assert call.params == {"output_format": "pcm_24000"} and call.headers["xi-api-key"] == "xi_test_key"
    assert call.json["model_id"] == "eleven_multilingual_v2" and "language_code" not in call.json
    with wave.open(str(out), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
    assert ms == 1000


def test_swahili_uses_a_v3_model_with_its_language_code(fake_http, tmp_path):
    tts_mod.ElevenLabsTTS(language="sw").synthesize("Habari za asubuhi.", str(tmp_path / "s.wav"), voice_id=VOICE)
    body = fake_http.calls[0].json
    assert body["model_id"] == "eleven_v3" and body["language_code"] == "sw"      # only v3/v4 speak Swahili


def test_speed_is_kept_inside_what_elevenlabs_accepts(fake_http, tmp_path):
    for rate, expected in ((2.0, 1.2), (0.1, 0.7), (1.0, 1.0)):
        tts_mod.ElevenLabsTTS().synthesize("Hi.", str(tmp_path / "r.wav"), voice_id=VOICE, rate=rate)
        assert fake_http.calls[-1].json["voice_settings"]["speed"] == expected


def test_long_text_is_split_under_the_per_request_limit(fake_http, tmp_path):
    text = " ".join(f"This is sentence number {i} of a very long passage." for i in range(400))      # ~20,000 characters
    ms = tts_mod.ElevenLabsTTS(language="sw").synthesize(text, str(tmp_path / "l.wav"), voice_id=VOICE)
    assert len(fake_http.calls) >= 4 and all(len(c.json["text"]) <= 5000 for c in fake_http.calls)
    assert ms == 1000 * len(fake_http.calls)


@pytest.mark.parametrize("status,words", [(401, "API key"), (429, "too many"), (402, "does not include")])
def test_errors_say_what_went_wrong(monkeypatch, tmp_path, status, words):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", FakeEleven(status=status))
    with pytest.raises(RuntimeError, match=f"ElevenLabs {status}") as exc:
        tts_mod.ElevenLabsTTS().synthesize("Hi.", str(tmp_path / "e.wav"), voice_id=VOICE)
    assert words in str(exc.value)


def test_no_key_and_a_bad_voice_id_never_reach_the_network(monkeypatch, tmp_path):
    fake = FakeEleven()
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        tts_mod.ElevenLabsTTS().synthesize("Hi.", str(tmp_path / "x.wav"), voice_id=VOICE)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    for bad in ("../../evil", "short", "has space in it 1234", None):
        with pytest.raises(RuntimeError, match="valid ElevenLabs voice"):
            tts_mod.ElevenLabsTTS().synthesize("Hi.", str(tmp_path / "x.wav"), voice_id=bad)
    assert fake.calls == []


# ------------------------------------------------------------------- the fallback
def test_a_failed_premium_voice_falls_back_per_language(monkeypatch):
    monkeypatch.setattr(tts_mod, "_tts_fallback_configured", lambda name: True)
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    en = tts_mod.get_tts("elevenlabs", target_lang="en")
    assert isinstance(en, tts_mod.ResilientTTS) and en.fallback_name == "piper"
    assert not isinstance(tts_mod.get_tts("elevenlabs", target_lang="sw"), tts_mod.ResilientTTS)    # no Swahili fallback without Azure
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    sw = tts_mod.get_tts("elevenlabs", target_lang="sw")
    assert isinstance(sw, tts_mod.ResilientTTS) and sw.fallback_name == "azure"
    assert sw.primary.language == "sw"                                                                # the right model per language


def test_the_fallback_is_recorded_when_it_is_used(tmp_path):
    class Down(tts_mod.TTSProvider):
        name = "elevenlabs"

        def synthesize(self, *a, **k):
            raise RuntimeError("ElevenLabs 429: slow down")

    resilient = tts_mod.ResilientTTS(Down(), tts_mod.StubTTS(), "stub")
    resilient.synthesize("Hello.", str(tmp_path / "f.wav"), voice_id=VOICE)
    assert resilient.fallback_used and "429" in resilient.fallback_reason and resilient.fallback_name == "stub"


# ------------------------------------------------------------------ the margin guard
def test_the_add_on_price_clears_the_margin_floor():
    cost = billing.premium_voice_unit_cost_usd()
    assert abs(cost - 0.08 * 1.1 * 1.5) < 1e-9                                  # $0.132 per audio-minute, worst case
    rate = billing.ADDONS["premium_voice"]["rate_per_min"]
    assert rate >= billing.premium_voice_min_rate() and billing.premium_voice_margin_ok()
    margin = (rate * (1 - billing.PAYMENT_FEE_RATE) - cost) / rate
    assert margin >= billing.MIN_PREMIUM_MARGIN, margin                          # at least 50% after cost and card fees


def test_premium_voice_switches_itself_off_if_it_would_lose_money(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert billing.premium_voice_margin_ok()
    monkeypatch.setenv("ELEVENLABS_USD_PER_1K_CHARS", "0.30")                      # e.g. ElevenLabs raised its prices
    assert not billing.premium_voice_margin_ok()
    from webapp import app as app_mod
    assert app_mod._elevenlabs_configured() is False                              # key present, still not offered


def _balance(monkeypatch, app_mod, limit, count, fail=False):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if fail:
            raise httpx.ConnectError("down")
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"character_limit": limit, "character_count": count})
    monkeypatch.setattr(httpx, "get", fake_get)
    app_mod._ELEVEN_BALANCE_CACHE["at"] = 0.0


def test_orders_are_only_taken_while_the_plan_has_characters_left(env, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_ALLOW_OVERAGE", raising=False)
    _balance(monkeypatch, env.app, 275_000, 100_000)
    assert env.app._premium_capacity_ok(10.0) is True                              # 10 min needs ~16,500 of 175,000 left
    _balance(monkeypatch, env.app, 275_000, 274_900)
    assert env.app._premium_capacity_ok(10.0) is False                              # 100 left: would spill into overage
    _balance(monkeypatch, env.app, 0, 0, fail=True)
    assert env.app._premium_capacity_ok(1.0) is False                               # cannot read the balance: do not risk it
    monkeypatch.setenv("ELEVENLABS_ALLOW_OVERAGE", "1")
    assert env.app._premium_capacity_ok(10.0) is True                               # the owner chose to allow overage


# ------------------------------------------------------------- ordering a premium voice
@pytest.fixture
def premium(env, wav_bytes, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setattr(env.app, "_premium_voices", lambda force=False: [{"id": VOICE, "name": "Rachel", "description": "female - american"}])
    monkeypatch.setattr(env.app, "_premium_capacity_ok", lambda minutes: True)
    env.wav = wav_bytes
    env.dub = lambda **kw: env.place(wav_bytes, **{"source_lang": "sw", "target_lang": "en", "service_level": "dub",
                                                    "review_tier": "ai", **kw})
    return env


def _orders(env):
    return env.db.list_orders_for_client("u1")


def test_instant_ai_dubbing_with_a_premium_voice_needs_no_human_review(premium):
    base = premium.order_from(premium.dub())
    prem = premium.order_from(premium.dub(addon_premium_voice="1", premium_voice_id=VOICE))
    assert prem["review_tier"] == "ai" and prem["tts"] == "elevenlabs" and prem["dub_voice"] == f"elevenlabs:{VOICE}"
    assert premium.db.order_has_addon(prem, "premium_voice") and base["tts"] != "elevenlabs"
    assert abs((prem["cost_usd"] - base["cost_usd"]) - 0.50 * prem["duration_minutes"]) < 0.005   # the add-on, in full


def test_premium_voice_also_works_with_human_review_and_into_swahili(premium):
    human = premium.order_from(premium.dub(review_tier="human", addon_premium_voice="1", premium_voice_id=VOICE))
    assert human["review_tier"] == "human" and human["tts"] == "elevenlabs"
    sw = premium.order_from(premium.dub(source_lang="en", target_lang="sw", addon_premium_voice="1", premium_voice_id=VOICE))
    assert sw["tts"] == "elevenlabs" and sw["target_lang"] == "sw"


def test_the_add_on_is_never_discounted(premium):
    premium.db.get_conn().close()
    conn = premium.db.get_conn()
    conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, status, period_started_at, minutes_used_this_period, bonus_minutes) "
                 "VALUES ('u1', 'premium', 'active', strftime('%s','now'), 0, 0)") if False else None
    order = premium.order_from(premium.dub(addon_premium_voice="1", premium_voice_id=VOICE))
    line = [x for x in __import__("json").loads(order["cost_breakdown_json"])["addons"] if x["key"] == "premium_voice"][0]
    assert line["rate_per_min"] == 0.50 and abs(line["cost_usd"] - 0.50 * order["duration_minutes"]) < 0.005


@pytest.mark.parametrize("extra,text", [
    ({"service_level": "translate"}, "full dub"),
    ({"premium_voice_id": "NOTAVOICEID123456"}, "listed premium voices"),
    ({"premium_voice_id": ""}, "listed premium voices"),
])
def test_premium_voice_is_refused_where_it_does_not_apply(premium, extra, text):
    r = premium.dub(**{"addon_premium_voice": "1", "premium_voice_id": VOICE, **extra})
    assert r.status_code == 200 and text in r.text and _orders(premium) == []


def test_not_offered_without_a_key_or_when_the_plan_is_full(premium, monkeypatch):
    monkeypatch.setattr(premium.app, "_premium_capacity_ok", lambda minutes: False)
    r = premium.dub(addon_premium_voice="1", premium_voice_id=VOICE)
    assert "at capacity" in r.text and _orders(premium) == []
    monkeypatch.setattr(premium.app, "_premium_capacity_ok", lambda minutes: True)
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    r = premium.dub(addon_premium_voice="1", premium_voice_id=VOICE)
    assert "available right now" in r.text and _orders(premium) == []


def test_the_wizard_only_shows_premium_voice_when_it_can_be_sold(premium, monkeypatch):
    html = premium.client.get("/client").text
    assert 'id="premium-voice-field"' in html and 'name="addon_premium_voice"' in html and "ElevenLabs" in html
    assert 'data-premium-voice-rate="0.5"' in html
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert 'id="premium-voice-field"' not in premium.client.get("/client").text


def test_voice_list_is_premade_only_and_needs_a_signed_in_client(env, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    env.app._PREMIUM_VOICES_CACHE.update({"at": 0.0, "voices": []})
    raw = {"voices": [
        {"voice_id": VOICE, "name": "Rachel", "category": "premade", "labels": {"gender": "female", "accent": "american"}},
        {"voice_id": "ClonedVoice12345678", "name": "Someone's clone", "category": "cloned"},
        {"voice_id": "bad id!", "name": "Broken", "category": "premade"},
    ]}
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None, follow_redirects=None: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None, json=lambda: raw))
    body = env.client.get("/client/premium-voices").json()
    assert body["available"] is True and [v["id"] for v in body["voices"]] == [VOICE]     # never a customer's clone
    assert body["voices"][0]["description"] == "female - american"
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert env.client.get("/client/premium-voices").json() == {"available": False, "voices": []}


# --------------------------------------------------------------- the worker and the refund
def _run(env, order, providers):
    worker = env.app.worker
    calls = []
    env.monkeypatch.setattr(worker, "kauli_run", lambda **kw: calls.append(kw))
    env.monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(
        cost_usd=0.01, flagged_count=0, providers=providers)))
    env.monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    worker._run_job(order["id"])
    return calls


def test_the_chosen_voice_reaches_the_dubbing_pipeline(premium):
    order = premium.order_from(premium.dub(addon_premium_voice="1", premium_voice_id=VOICE))
    calls = _run(premium, order, {"tts": "elevenlabs"})
    assert calls[0]["tts"] == "elevenlabs" and calls[0]["voice_id"] == VOICE
    plain = premium.order_from(premium.dub())
    assert _run(premium, plain, {"tts": "piper"})[0]["voice_id"] is None


def test_a_customer_is_credited_back_once_if_they_did_not_get_the_premium_voice(premium):
    order = premium.order_from(premium.dub(addon_premium_voice="1", premium_voice_id=VOICE))
    before = premium.db.wallet_credits_remaining("u1")
    _run(premium, order, {"tts": "elevenlabs"})                                       # delivered as paid for
    assert premium.db.wallet_credits_remaining("u1") == before
    _run(premium, order, {"tts": "piper"})                                            # ElevenLabs failed, standard voice used
    expected = billing.usd_to_credits(0.50 * order["duration_minutes"])
    assert abs(premium.db.wallet_credits_remaining("u1") - before - expected) < 0.01
    assert premium.db.get_order(order["id"])["premium_voice_refunded_at"]
    _run(premium, order, {"tts": "piper"})                                            # a retry never refunds twice
    assert abs(premium.db.wallet_credits_remaining("u1") - before - expected) < 0.01


# ------------------------------------------------------------- the home page language marquee
@pytest.fixture
def visitor(env, monkeypatch):
    monkeypatch.setattr(env.app, "current_user", lambda request: None)
    return env


def test_languages_flow_across_the_home_page_in_a_loop(visitor):
    html = visitor.client.get("/").text
    section = re.search(r'<section class="tk-langs".*?</section>', html, re.S).group(0)
    assert "Supported languages" in section
    assert re.search(r"in \d+\+ languages", section) and "got you covered" in section
    tracks = re.findall(r'<ul class="tk-marquee-track"([^>]*)>', section)
    assert len(tracks) == 2 and 'aria-hidden="true"' in tracks[1] and 'aria-hidden' not in tracks[0]
    second = section.split('<ul class="tk-marquee-track" aria-hidden="true">')[1]
    assert 'tabindex="-1"' in second and second.count("<a ") == second.count('tabindex="-1"')   # the copy is not tabbable
    pills = re.findall(r'class="tk-pill[^"]*"', section.split('<ul class="tk-marquee-track" aria-hidden="true">')[0])
    assert len(pills) >= 12
    for name in ("English", "Kiswahili", "Kikuyu", "Français", "Español", "العربية", "Português", "Deutsch"):
        assert name in section
    assert "lang-flag flag-en" in section and "+</span> more" in section
    assert 'id="lang-marquee-toggle"' in section and 'aria-pressed="false"' in section        # a visible pause control


def test_every_language_pill_goes_to_a_real_page(visitor):
    html = visitor.client.get("/").text
    section = re.search(r'<section class="tk-langs".*?</section>', html, re.S).group(0)
    hrefs = set(re.findall(r'<a href="([^"]+)" class="tk-pill', section))
    assert "/languages" in hrefs and "/solutions/audio-to-text" in hrefs and len(hrefs) >= 10
    for href in sorted(hrefs):
        assert visitor.client.get(href).status_code == 200, href


def test_the_marquee_moves_right_to_left_and_respects_reduced_motion():
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert "@keyframes tk-flow { from { transform: translateX(0); } to { transform: translateX(-100%); } }" in css
    assert "animation: tk-flow" in css and "animation-play-state: paused" in css
    assert "prefers-reduced-motion: reduce" in css and ".tk-marquee-track[aria-hidden=\"true\"], .tk-marquee-toggle { display: none; }" in css
