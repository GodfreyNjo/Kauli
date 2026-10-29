"""When a provider fails (quota, rejected key, outage) it is set aside, the order moves to the next connected provider on its retry, and new
orders skip the paused provider."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import _set_lara, env, wav_bytes  # noqa: E402,F401
from webapp import provider_health  # noqa: E402

QUOTA = '(HTTP 429) ApiQuotaExceededError: You have exceeded your "api_translation_chars" quota'


@pytest.fixture(autouse=True)
def _clean():
    provider_health._paused.clear()
    yield
    provider_health._paused.clear()


def _translation_order(env, wav_bytes, monkeypatch, azure=True):
    _set_lara(env)
    if azure:
        monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    else:
        monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    env.db.set_processing_stage(order["id"], "translate")
    return env.db.get_order(order["id"])


def test_a_quota_failure_moves_the_order_to_the_next_connected_provider_and_pauses_the_failed_one(env, wav_bytes, monkeypatch):
    order = _translation_order(env, wav_bytes, monkeypatch)
    assert provider_health.auto_switch(order, QUOTA) == ("mt", "lara", "azure-translate")
    assert env.db.get_order(order["id"])["mt"] == "azure-translate"
    assert provider_health.is_paused("lara") and not provider_health.is_paused("azure-translate")


def test_the_next_provider_is_skipped_when_it_is_not_connected_or_is_itself_paused(env, wav_bytes, monkeypatch):
    order = _translation_order(env, wav_bytes, monkeypatch, azure=False)
    monkeypatch.setattr(provider_health, "configured", lambda name: name in ("lara", "local", "madlad"))
    assert provider_health.auto_switch(order, QUOTA) == ("mt", "lara", "madlad")                 # Azure is not connected: our own model is next
    again = env.db.get_order(order["id"])
    provider_health._paused["madlad"] = time.time() + 600
    assert provider_health.auto_switch(again, "madlad: 503 service unavailable")[2] == "local"   # the paused one is passed over


def test_an_error_that_is_not_a_providers_own_changes_nothing(env, wav_bytes, monkeypatch):
    order = _translation_order(env, wav_bytes, monkeypatch)
    assert provider_health.auto_switch(order, "ValueError: segment has no text") is None
    assert env.db.get_order(order["id"])["mt"] == "lara" and not provider_health.is_paused("lara")


def test_the_worker_switches_before_retrying_and_tells_staff(env, wav_bytes, monkeypatch):
    order = _translation_order(env, wav_bytes, monkeypatch)
    worker = env.app.worker
    seen = []

    def run(**kw):
        seen.append(kw["mt"])
        if kw["mt"] == "lara":
            raise RuntimeError(QUOTA)
    monkeypatch.setattr(worker, "kauli_run", run)
    monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(cost_usd=0.0, flagged_count=0, warnings=[])))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    worker._run_job(order["id"])
    assert seen == ["lara", "azure-translate"]                                                   # the retry used the other provider
    assert env.db.get_order(order["id"])["status"] == "awaiting_review"
    conn = env.db.get_conn()
    notes = conn.execute("SELECT title FROM notifications WHERE kind = 'provider_switched'").fetchall()
    conn.close()
    assert notes and "switched to Azure Translator" in notes[0]["title"]


def test_a_run_that_fell_back_inside_itself_still_pauses_the_first_provider(monkeypatch):
    job = SimpleNamespace(warnings=['lara failed ((HTTP 429) ApiQuotaExceededError: quota) - used azure-translate instead', "something else"])
    provider_health.note_job_fallbacks(job)
    assert provider_health.is_paused("lara") and not provider_health.is_paused("azure-translate")


def test_new_translation_orders_skip_a_paused_lara(env, wav_bytes, monkeypatch):
    _set_lara(env)
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    assert env.app._pick_mt("sw", "en", "local", "ai") == ("lara", None)
    provider_health._paused["lara"] = time.time() + 600
    assert env.app._pick_mt("sw", "en", "local", "ai") == ("azure-translate", None)
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY")
    assert env.app._pick_mt("sw", "en", "local", "ai") == ("lara", None)                        # nothing better connected: Lara is still tried
    provider_health._paused["lara"] = time.time() - 1
    assert not provider_health.is_paused("lara")                                                 # the pause ends by itself


def test_the_providers_page_says_when_one_is_set_aside(env, monkeypatch):
    _set_lara(env)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    provider_health.mark_failed("lara", QUOTA)
    assert "set aside for" in env.client.get("/staff/providers").text


# ---------------------------------------------------------------------------------------------- voices: free first, premium only when sold
def test_voices_are_tried_cheapest_first_and_elevenlabs_only_for_premium_orders(monkeypatch):
    from kauli.providers import tts as tts_mod
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setattr(tts_mod, "_tts_fallback_configured", lambda name: True)
    assert provider_health.AUTO_ORDER["tts"] == ["piper", "azure"]
    en = tts_mod.get_tts("piper", target_lang="en")
    assert isinstance(en, tts_mod.ResilientTTS) and en.fallback_name == "azure"                  # our own free voice, Azure behind it
    assert en.fallback.locale == "en-US" and en.fallback.default_voice == "en-US-JennyNeural"      # an English voice, not the Swahili one
    sw = tts_mod.get_tts("azure", target_lang="sw")
    assert sw.name == "azure" and sw.locale == "sw-KE" and not isinstance(sw, tts_mod.ResilientTTS)   # Piper has no Swahili voice
    en_azure = tts_mod.get_tts("azure", target_lang="en")
    assert isinstance(en_azure, tts_mod.ResilientTTS) and en_azure.fallback_name == "piper"
    premium = tts_mod.get_tts("elevenlabs", target_lang="en")
    assert premium.fallback_name == "piper"                                                      # a failed premium voice still ends in the free one


def test_an_english_dub_starts_on_our_own_voice_and_moves_to_azure_only_while_piper_is_set_aside(env, wav_bytes, monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    _set_lara(env)
    assert env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level="dub", review_tier="human"))["tts"] == "piper"
    provider_health._paused["piper"] = time.time() + 600
    assert env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level="dub", review_tier="human"))["tts"] == "azure"


def test_an_instant_ai_order_that_switches_provider_ships_by_itself_with_no_staff_step(env, wav_bytes, monkeypatch):
    _set_lara(env)
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="ai"))
    assert order["review_tier"] == "ai" and order["mt"] == "lara"
    env.db.set_processing_stage(order["id"], "translate")
    worker = env.app.worker
    ran, hooked = [], []

    def run(**kw):
        ran.append(kw["mt"])
        if kw["mt"] == "lara":
            raise RuntimeError(QUOTA)
    monkeypatch.setattr(worker, "kauli_run", run)
    monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(cost_usd=0.0, flagged_count=0, warnings=[])))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    monkeypatch.setattr(worker, "notify_client_order_ready", lambda o, **k: hooked.append(("client", o["id"])))
    monkeypatch.setattr(worker, "ai_order_ready_hook", lambda o: hooked.append(("webhooks", o["id"])))
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    worker._run_job(order["id"])
    assert ran == ["lara", "azure-translate"]
    assert env.db.get_order(order["id"])["status"] == "ready_for_delivery"                      # delivered, never waited for staff
    assert hooked == [("client", order["id"]), ("webhooks", order["id"])]                        # the customer is told and integrations fire


def test_every_instant_ai_service_ships_without_staff_transcription_translation_and_dubbing(env, wav_bytes, monkeypatch):
    _set_lara(env)
    worker = env.app.worker
    monkeypatch.setattr(worker, "kauli_run", lambda **kw: None)
    monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(cost_usd=0.0, flagged_count=3, warnings=[])))
    monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    for service in ("transcribe", "translate", "dub"):
        order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level=service, review_tier="ai"))
        assert order["review_tier"] == "ai", service
        worker._run_job(order["id"])
        assert env.db.get_order(order["id"])["status"] == "ready_for_delivery", service       # even with segments flagged, no staff step
