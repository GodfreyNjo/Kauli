"""Our own models go first for Instant AI and the free minutes where they were measured good, with the outside providers behind them; and
ElevenLabs (the realistic voices) is on the staff switchboard."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli.providers import own_quality  # noqa: E402
from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401
from tests.test_premium_voice import premium  # noqa: E402,F401
from webapp import billing, provider_health  # noqa: E402


# --------------------------------------------------------------------------------------------------- what was measured
def test_only_languages_that_were_measured_accurate_are_approved_for_transcription():
    ok = own_quality.asr_languages()
    assert {"en", "de", "es", "fr", "it"} <= ok
    assert not ({"sw", "ki", "am", "ka", "km", "jv"} & ok)                                 # Swahili and the languages it is weak in stay with the providers built for them


def test_only_measured_good_translation_directions_are_approved():
    ok = own_quality.mt_ok
    assert ok("en", "de") and ok("en", "fr") and ok("de", "en") and ok("fr", "en")
    assert ok("sw", "en")                                                                    # checked by hand on real sentences
    assert not ok("en", "sw") and not ok("en", "ki") and not ok("ki", "en")                  # known to be wrong
    assert not ok("de", "fr")                                                                # pairs without English were not measured
    assert not ok("en", "xx") and not ok("xx", "en")                                         # never measured: never approved


def test_a_measured_threshold_decides_it(monkeypatch):
    monkeypatch.setattr(own_quality, "_data", lambda: {"asr": {"aa": 0.24, "bb": 0.26}, "mt": {"aa": {"from_en": 55.0, "to_en": 54.9}}})
    assert own_quality.asr_languages() == {"aa"}
    assert own_quality.mt_ok("en", "aa") and not own_quality.mt_ok("aa", "en")


# ------------------------------------------------------------------------------------------------------ translation
def test_instant_ai_translation_uses_our_own_model_first_where_it_is_good(env, monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setattr(provider_health, "configured", lambda name: name == "madlad")              # our own model is installed here
    provider_health._paused.clear()
    pick = env.app._pick_mt
    assert pick("en", "de", "local", "ai", minutes=5) == ("madlad", None)
    assert pick("en", "es", "local", "ai", minutes=5) == ("lara", None)                        # not measured good enough: the paid engine
    assert pick("en", "sw", "local", "ai", minutes=5) == ("lara", None)
    assert pick("en", "de", "local", "ai", minutes=60) == ("lara", None)                       # a long file would take our server too long
    assert pick("en", "de", "local", "human", minutes=5) == ("lara", None)                     # human-reviewed work keeps the paid engine
    provider_health.mark_failed("madlad", "503 service unavailable")
    assert pick("en", "de", "local", "ai", minutes=5) == ("lara", None)                        # set aside after a failure: the next one takes it
    provider_health._paused.clear()
    env.db.set_provider_setting("madlad", enabled=False)
    assert pick("en", "de", "local", "ai", minutes=5) == ("lara", None)                        # staff turned it off


def test_premium_still_gets_claude_before_our_own_model(env, monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(provider_health, "madlad_ok", lambda s, t: True)
    provider_health._paused.clear()
    assert env.app._pick_mt("en", "de", "claude", "ai", minutes=5) == ("claude", None)


def test_a_new_account_no_longer_gets_a_special_first_upload():
    assert billing.FIRST_ORDER_QUALITY_MINUTES == 0


# -------------------------------------------------------------------------------------------------------- ElevenLabs
def test_elevenlabs_is_on_the_switchboard_under_voices(env):
    assert "elevenlabs" in provider_health.TOGGLEABLE["tts"]
    voices = next(s for s in provider_health.switchboard() if s["kind"] == "tts")
    assert [r["name"] for r in voices["rows"]] == ["piper", "azure", "elevenlabs"]
    assert provider_health.cost_per_min("elevenlabs") == billing.premium_voice_unit_cost_usd()      # from its published price
    env.db.set_provider_setting("elevenlabs", cost_per_min=0.2)
    assert provider_health.cost_per_min("elevenlabs") == 0.2                                         # or the real figure staff enter


def test_the_realistic_voice_margin_is_shown(env):
    row = next(e for e in provider_health.cost_estimates() if e["level"] == "premium_voice")
    add_on = billing.ADDONS["premium_voice"]["rate_per_min"]
    assert row["price"] == add_on and abs(row["margin"] - (add_on - billing.premium_voice_unit_cost_usd()) / add_on * 100) < 0.01


def test_turning_elevenlabs_off_takes_the_realistic_voice_away_everywhere(premium):
    premium.db.set_subscription_plan("u1", "premium", 30)
    assert premium.order_from(premium.dub())["tts"] == "elevenlabs"
    assert premium.app._elevenlabs_configured()
    premium.db.set_provider_setting("elevenlabs", enabled=False)
    assert not premium.app._elevenlabs_configured()
    assert premium.order_from(premium.dub())["tts"] != "elevenlabs"                                 # Premium dubs still work, with the standard voice
    assert 'name="addon_premium_voice"' not in premium.client.get("/account").text                    # and the wizard stops offering it


def test_the_switchboard_page_lists_elevenlabs_with_the_others(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/providers").text
    voices = page.split("Voices for dubbing")[1].split("Everything else")[0]
    assert "ElevenLabs" in voices and "Piper" in voices and "Azure Speech" in voices
    assert "Realistic voice (add-on, included on Premium)" in page


def test_staff_can_download_our_transcription_model_without_waiting_for_the_first_order(env, monkeypatch):
    started = []
    monkeypatch.setattr(provider_health, "warm_own_model", lambda: started.append(1) or True)
    assert env.client.post("/staff/providers/download-own-model", follow_redirects=False).status_code in (302, 303, 307) and not started   # staff only
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    assert env.client.post("/staff/providers/download-own-model", follow_redirects=False).status_code == 303 and started == [1]
    monkeypatch.setattr(provider_health, "own_model_status", lambda: {"ready": False, "mb": 0, "downloading": False})
    assert "Download now" in env.client.get("/staff/providers").text
    monkeypatch.setattr(provider_health, "own_model_status", lambda: {"ready": True, "mb": 480, "downloading": False})
    assert "Model on this server (480 MB)" in env.client.get("/staff/providers").text
