"""The staff switchboard: staff see every provider, turn them on and off, record what each really costs and pick how orders are routed
(spend less, balanced, best quality). A provider that is off is passed over everywhere."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import provider_health  # noqa: E402

QUOTA = "429 quota exceeded"


@pytest.fixture(autouse=True)
def _clean():
    provider_health._paused.clear()
    yield
    provider_health._paused.clear()


def _staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


def _off(env, name):
    env.db.set_provider_setting(name, enabled=False)


# ---------------------------------------------------------------------------------------------------------- the switches
def test_everything_is_on_and_balanced_until_staff_change_it(env):
    assert provider_health.mode() == "balanced"
    for names in provider_health.TOGGLEABLE.values():
        assert all(provider_health.is_enabled(n) for n in names)


def test_only_staff_can_open_or_change_the_switchboard(env):
    assert env.client.get("/staff/providers", follow_redirects=False).status_code in (302, 303, 307)
    for path, data in (("/staff/providers/set", {"name": "lara", "action": "off"}), ("/staff/providers/mode", {"mode": "saver"})):
        assert env.client.post(path, data=data, follow_redirects=False).status_code in (302, 303, 307)
    assert provider_health.is_enabled("lara") and provider_health.mode() == "balanced"


def test_staff_turn_a_provider_off_and_on_and_it_is_logged(env):
    _staff(env)
    r = env.client.post("/staff/providers/set", data={"name": "azure-translate", "action": "off"}, follow_redirects=False)
    assert r.status_code == 303 and not provider_health.is_enabled("azure-translate")
    assert "Turned off" in env.client.get("/staff/providers").text
    env.client.post("/staff/providers/set", data={"name": "azure-translate", "action": "on"})
    assert provider_health.is_enabled("azure-translate")
    conn = env.db.get_conn()
    kinds = [r["detail"] for r in conn.execute("SELECT detail FROM security_events WHERE kind = 'provider_setting_changed' ORDER BY rowid").fetchall()]
    conn.close()
    assert kinds == ["azure-translate off", "azure-translate on"]


def test_unknown_providers_and_bad_costs_are_refused(env):
    _staff(env)
    assert "error=" in env.client.post("/staff/providers/set", data={"name": "nope", "action": "off"}, follow_redirects=False).headers["location"]
    assert "error=" in env.client.post("/staff/providers/set", data={"name": "lara", "action": "cost", "cost": "abc"}, follow_redirects=False).headers["location"]
    assert "error=" in env.client.post("/staff/providers/set", data={"name": "lara", "action": "cost", "cost": "-1"}, follow_redirects=False).headers["location"]
    assert provider_health.cost_per_min("lara") is None


def test_a_cost_entered_by_staff_wins_over_the_estimate_and_can_be_reset(env):
    _staff(env)
    assert provider_health.cost_per_min("groq-whisper") == provider_health.COST_DEFAULTS["groq-whisper"]
    env.client.post("/staff/providers/set", data={"name": "groq-whisper", "action": "cost", "cost": "$0.0042"})
    assert provider_health.cost_per_min("groq-whisper") == 0.0042
    env.client.post("/staff/providers/set", data={"name": "groq-whisper", "action": "cost", "cost": ""})
    assert provider_health.cost_per_min("groq-whisper") == provider_health.COST_DEFAULTS["groq-whisper"]


def test_the_mode_is_saved_and_must_be_one_of_the_three(env):
    _staff(env)
    env.client.post("/staff/providers/mode", data={"mode": "saver"})
    assert provider_health.mode() == "saver"
    assert "error=" in env.client.post("/staff/providers/mode", data={"mode": "free-for-all"}, follow_redirects=False).headers["location"]
    assert provider_health.mode() == "saver"


# --------------------------------------------------------------------------------------------------------- transcription
def _chain(env, mode, *, free_covered=False, lang="en", default="transkriptor"):
    env.db.set_flag("provider_mode", mode)
    return env.app._route_asr_outside(default, source_lang=lang, free_covered=free_covered)


def test_balanced_keeps_todays_routing_exactly(env):
    env.monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: ["groq-whisper"])
    assert _chain(env, "balanced") == "chain:transkriptor,groq-whisper,faster-whisper"
    assert _chain(env, "balanced", free_covered=True) == "chain:groq-whisper,transkriptor,faster-whisper"


def test_spend_less_sends_everyone_in_the_widely_spoken_languages_to_the_cheap_providers_first(env):
    env.monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: ["groq-whisper", "assemblyai"])
    assert _chain(env, "saver") == "chain:groq-whisper,assemblyai,transkriptor,faster-whisper"
    assert _chain(env, "saver", lang="sw") == "chain:transkriptor,groq-whisper,assemblyai,faster-whisper"       # quality stays where the cheap ones are weak


def test_best_quality_puts_transkriptor_first_even_for_free_orders(env):
    env.monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: ["groq-whisper"])
    assert _chain(env, "quality", free_covered=True) == "chain:transkriptor,groq-whisper,faster-whisper"


def test_a_transcription_provider_that_is_off_is_left_out(env):
    env.monkeypatch.setattr(env.app, "cheap_providers_configured", lambda: ["groq-whisper", "assemblyai"])
    _off(env, "groq-whisper")
    assert _chain(env, "balanced") == "chain:transkriptor,assemblyai,faster-whisper"
    _off(env, "transkriptor")
    assert _chain(env, "balanced") == "chain:assemblyai,faster-whisper"
    _off(env, "assemblyai")
    assert _chain(env, "balanced") == "faster-whisper"                                                # our own server is the last stop


def test_our_own_model_is_not_used_when_it_is_off(env):
    env.monkeypatch.setenv("KAULI_OWN_ASR", "on")
    assert env.app._own_asr_first("en")
    _off(env, "own")
    assert not env.app._own_asr_first("en")


# ----------------------------------------------------------------------------------------------------------- translation
def _mt(env, plan_mt="local", tier="ai", src="en", tgt="es"):
    return env.app._pick_mt(src, tgt, plan_mt, tier)


def test_a_translation_engine_that_is_off_is_passed_over(env):
    env.monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    env.monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    env.monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    env.monkeypatch.setattr(provider_health, "madlad_ok", lambda s, t: False)
    assert _mt(env) == ("lara", None)
    _off(env, "lara")
    assert _mt(env) == ("azure-translate", None)                                                      # Lara off: the next connected engine
    _off(env, "azure-translate")
    assert _mt(env) == ("lara", None)                                                                 # nothing else connected: the last resort, as before


def test_turning_claude_off_drops_the_premium_upgrade_to_the_usual_engines(env):
    env.monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    env.monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    env.monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert _mt(env, plan_mt="claude", tier="human") == ("claude", None)
    _off(env, "claude")
    assert _mt(env, plan_mt="claude", tier="human") == ("lara", None)
    env.monkeypatch.delenv("LARA_ACCESS_KEY_ID")
    env.monkeypatch.setattr(provider_health, "madlad_ok", lambda s, t: False)
    assert _mt(env, plan_mt="claude", tier="human")[0] is None                                        # nothing left: a clear message, not a broken order


def test_our_own_translation_model_is_not_offered_when_it_is_off(env):
    env.monkeypatch.setattr(provider_health, "configured", lambda name: name == "madlad")
    from kauli.providers.mt import MadladMT
    env.monkeypatch.setattr(MadladMT, "pair_ok", staticmethod(lambda s, t: True))
    assert provider_health.madlad_ok("en", "es")
    _off(env, "madlad")
    assert not provider_health.madlad_ok("en", "es")


# ----------------------------------------------------------------------------------------- automatic and manual switching
def test_the_automatic_switch_and_the_failed_order_switch_skip_providers_that_are_off(env, wav_bytes):
    env.monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    env.monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    env.monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    order = env.order_from(env.place(wav_bytes, source_lang="en", target_lang="es", service_level="translate"))
    assert order["mt"] == "lara"
    _off(env, "azure-translate")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET processing_stage = 'translate' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    order = env.db.get_order(order["id"])
    switched = provider_health.auto_switch(order, QUOTA + " lara")
    assert switched is None or switched[2] != "azure-translate"
    assert "turned off" in (provider_health.valid_switch(order, "mt", "azure-translate") or "")
    assert provider_health.valid_switch(order, "mt", "azure-translate").startswith("Azure")


# ------------------------------------------------------------------------------------------------------------- the money
def test_the_estimate_needs_every_cost_on_the_route_and_then_shows_the_margin(env):
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "k")
    first = {e["level"]: e for e in provider_health.cost_estimates()}
    assert first["transcribe"]["known"] is False and first["transcribe"]["margin"] is None            # Transkriptor's real cost is not known yet
    env.db.set_provider_setting("transkriptor", cost_per_min=0.03)
    second = {e["level"]: e for e in provider_health.cost_estimates()}
    assert second["transcribe"]["known"] is True and abs(second["transcribe"]["cost"] - 0.03) < 1e-9
    assert abs(second["transcribe"]["margin"] - (0.12 - 0.03) / 0.12 * 100) < 0.01
    assert second["transcribe"]["price"] == 0.12                                                      # priced the same for every visitor


def test_spend_less_mode_prices_the_route_at_the_cheaper_provider(env):
    env.monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "k")
    env.monkeypatch.setenv("GROQ_API_KEY", "k")
    env.db.set_provider_setting("transkriptor", cost_per_min=0.03)
    env.db.set_flag("provider_mode", "balanced")
    balanced = provider_health.cost_estimates()[0]["cost"]
    env.db.set_flag("provider_mode", "saver")
    saver = provider_health.cost_estimates()[0]["cost"]
    assert saver < balanced and abs(saver - provider_health.COST_DEFAULTS["groq-whisper"]) < 1e-9


def test_the_page_shows_modes_costs_and_switches(env):
    _staff(env)
    page = env.client.get("/staff/providers").text
    for text in ("How orders are routed", "Spend less", "Balanced", "Best quality", "Cost per audio-minute", "Turn off",
                 "What one audio-minute of Instant AI costs us", "Turning speech into text", "Translating", "Voices for dubbing"):
        assert text in page, text
