"""A failed order tells staff which provider failed and why, lists the connected alternatives, and switches the order to one of them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import _set_lara, env, wav_bytes  # noqa: E402,F401
from webapp import provider_health  # noqa: E402

QUOTA = '(HTTP 429) ApiQuotaExceededError: You have exceeded your "api_translation_chars" quota'


def _staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


def _failed(env, wav_bytes, error=QUOTA, stage="translate"):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    env.db.set_processing_stage(order["id"], stage)
    env.db.update_order_status(order["id"], "dead_letter", error=error)
    return env.db.get_order(order["id"])


def test_a_quota_error_names_the_step_and_the_provider(env, wav_bytes):
    order = _failed(env, wav_bytes)
    d = provider_health.diagnose(order)
    assert d["kind"] == "mt" and d["provider"] == "lara" and d["reason"] == "out of quota or rate-limited"
    assert provider_health.diagnose(env.db.get_order(env.order_from(env.place(wav_bytes))["id"])) is None   # nothing failed


def test_only_connected_providers_that_fit_the_languages_can_be_switched_to(env, wav_bytes, monkeypatch):
    order = _failed(env, wav_bytes)
    for key in ("AZURE_TRANSLATOR_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(key, raising=False)
    assert provider_health.valid_switch(order, "mt", "azure-translate") == "Azure Translator is not connected on this server."
    assert provider_health.valid_switch(order, "mt", "local") is None                       # sw -> en, always available
    assert provider_health.valid_switch(order, "mt", "nonsense")
    assert provider_health.valid_switch(order, "tts", "piper")                              # a dub-only step this order does not offer
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    assert provider_health.valid_switch(order, "mt", "azure-translate") is None
    en_es = env.db.get_order(env.order_from(env.place(wav_bytes, source_lang="en", target_lang="es", service_level="translate"))["id"])
    assert provider_health.valid_switch(en_es, "mt", "local") == "Local translation model cannot take this language pair."


def test_switching_points_the_order_at_the_new_provider_and_retries_it(env, wav_bytes, monkeypatch):
    order = _failed(env, wav_bytes)
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    _staff(env)
    submitted = []
    monkeypatch.setattr(env.app.worker, "submit_job", lambda oid, resume=False: submitted.append((oid, resume)))
    r = env.client.post(f"/staff/orders/{order['id']}/switch-provider", data={"kind": "mt", "provider": "azure-translate"}, follow_redirects=False)
    assert r.status_code == 303
    after = env.db.get_order(order["id"])
    assert after["mt"] == "azure-translate" and after["status"] == "queued" and after["retry_count"] == 0
    assert submitted == [(order["id"], True)]                                                # resumed: earlier steps are not paid for twice
    note = [m for m in env.db.list_messages(order["id"], include_internal=True) if m["visibility"] == "internal"]
    assert note and "Switched MT from Lara Translate to Azure Translator" in note[-1]["body"]


def test_a_provider_that_is_not_connected_is_refused_and_nothing_changes(env, wav_bytes, monkeypatch):
    order = _failed(env, wav_bytes)
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    _staff(env)
    submitted = []
    monkeypatch.setattr(env.app.worker, "submit_job", lambda oid, resume=False: submitted.append(oid))
    r = env.client.post(f"/staff/orders/{order['id']}/switch-provider", data={"kind": "mt", "provider": "azure-translate"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert env.db.get_order(order["id"])["mt"] == "lara" and env.db.get_order(order["id"])["status"] == "dead_letter" and not submitted
    r = env.client.post(f"/staff/orders/{order['id']}/switch-provider", data={"kind": "status; DROP TABLE orders", "provider": "x"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_only_staff_can_switch(env, wav_bytes):
    order = _failed(env, wav_bytes)
    r = env.client.post(f"/staff/orders/{order['id']}/switch-provider", data={"kind": "mt", "provider": "local"}, follow_redirects=False)
    assert r.headers["location"].endswith("/login") and env.db.get_order(order["id"])["mt"] == "lara"


def test_the_failed_order_page_shows_the_provider_the_options_and_the_resources(env, wav_bytes, monkeypatch):
    order = _failed(env, wav_bytes)
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    monkeypatch.setattr(provider_health, "elevenlabs_credits", lambda: {"limit": 100000, "used": 99000, "left": 1000, "tier": "starter"})
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    _staff(env)
    html = env.client.get(f"/staff/orders/{order['id']}").text
    assert "What failed" in html and "Translation" in html and "Lara Translate" in html and "out of quota or rate-limited" in html
    assert 'name="provider" value="azure-translate"' in html and "Switch and retry" in html and "Retry with this one" in html
    assert "1,000 of 100,000 characters left" in html and "pf-low" in html
    page = env.client.get("/staff/providers").text
    assert "Providers" in page and "Azure Translator" in page and "Connected" in page
