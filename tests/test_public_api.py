"""The public REST API (POST /api/v1/orders and friends) against a THROWAWAY
database. Same isolation as test_order_flow: no real DB, providers or network,
and no session cookie - every call here authenticates with a real API key,
which is the only way the API accepts a caller."""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg not installed", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

PROVIDER_ENV = ("LARA_ACCESS_KEY_ID", "LARA_ACCESS_KEY_SECRET", "ANTHROPIC_API_KEY", "AZURE_TRANSLATOR_KEY",
                "AZURE_SPEECH_KEY", "TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2", "PAYSTACK_CURRENCY")
KEY = "kauli_live_testkey_0123456789"


@pytest.fixture(scope="module")
def wav_bytes(tmp_path_factory):
    out = tmp_path_factory.mktemp("audio") / "clip.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-ar", "16000", "-ac", "1", str(out)], check=True)
    return out.read_bytes()


@pytest.fixture
def api(tmp_path, monkeypatch):
    before = dict(os.environ)
    from webapp import app as app_mod, db
    for key in set(os.environ) - set(before):
        del os.environ[key]
    os.environ.update(before)
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path / "output")
    submitted = []
    monkeypatch.setattr(app_mod.worker, "submit_job", lambda order_id, resume=False: submitted.append(order_id))
    monkeypatch.setattr(app_mod.rate_limit, "check", lambda key, limit=0, window_s=0: (True, 0))
    db.get_or_create_user("u1", "client@example.com", "client")
    db.set_trial_verified("u1")
    db.set_billing_market("u1", "intl")
    db.set_client_api_key("u1", hashlib.sha256(KEY.encode()).hexdigest(), KEY[:18])
    client = TestClient(app_mod.app, follow_redirects=False)

    def post(wav=None, key=KEY, **data):
        form = {"source_lang": "en", "service_level": "transcribe", "review_tier": "ai"}
        form.update(data)
        files = {"audio": ("clip.wav", wav, "audio/wav")} if wav else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return client.post("/api/v1/orders", data=form, files=files, headers=headers)

    return type("Api", (), dict(app=app_mod, db=db, client=client, post=post, submitted=submitted))


def test_a_missing_or_wrong_key_is_refused(api, wav_bytes):
    assert api.post(wav_bytes, key=None).status_code == 401
    assert api.post(wav_bytes, key="kauli_live_nope").status_code == 401
    assert api.db.list_orders_for_client("u1") == []


def test_creating_a_free_instant_ai_order_over_the_api(api, wav_bytes):
    r = api.post(wav_bytes)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] != "pending_payment" and body["review_tier"] == "ai"
    assert body["service_level"] == "transcribe" and body["source_lang"] == "en"
    assert body["deliverables"] == {}                       # nothing downloads until the account has chosen a plan
    assert body["plan_required"] is True and body["choose_plan_url"].endswith("/client/billing?choose_plan=1")
    assert api.submitted == [body["id"]]
    assert api.db.get_order(body["id"])["is_free_preview"] == 1


def test_an_order_that_needs_payment_comes_back_402_with_a_payment_link(api, wav_bytes):
    r = api.post(wav_bytes, review_tier="human")
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["payment_required"] is True and body["status"] == "pending_payment"
    assert body["payment_url"].endswith(f"/client/orders/{body['id']}/pay")
    assert abs(body["cost_usd"] - 1.20) < 0.01              # same price card as the dashboard
    assert api.submitted == []                              # nothing runs before payment
    assert body["deliverables"] == {}


def test_validation_errors_are_json_not_a_rendered_page(api, wav_bytes):
    r = api.post(wav_bytes, source_lang="xx")
    assert r.status_code == 400 and "language" in r.json()["error"].lower()
    r = api.post(None)                                       # neither a file nor a link
    assert r.status_code == 400 and "file" in r.json()["error"].lower()
    r = api.post(wav_bytes, service_level="nonsense")
    assert r.status_code == 400
    assert api.db.list_orders_for_client("u1") == []


def test_the_same_pricing_rules_apply_as_in_the_dashboard(api, wav_bytes):
    # a wider language can only be Instant AI - the API must not be a way round that
    r = api.post(wav_bytes, source_lang="fr", review_tier="human")
    assert r.status_code == 400 and "Instant AI" in r.json()["error"]


def test_idempotency_key_returns_the_same_order(api, wav_bytes):
    first = api.post(wav_bytes, idempotency_key="abc-123")
    second = api.post(wav_bytes, idempotency_key="abc-123")
    assert first.status_code == 201 and second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]
    assert len(api.db.list_orders_for_client("u1")) == 1


def test_free_minutes_work_straight_away_but_downloads_wait_for_a_plan(api, wav_bytes):
    conn = api.db.get_conn()
    conn.execute("UPDATE users SET trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    r = api.post(wav_bytes)
    assert r.status_code == 201 and r.json()["plan_required"] is True      # no card check before ordering
    order_id = r.json()["id"]
    assert api.submitted == [order_id]


def test_links_appear_once_the_plan_is_chosen(api, wav_bytes):
    order_id = api.post(wav_bytes).json()["id"]
    api.db.update_order_status(order_id, "ready_for_delivery")
    headers = {"Authorization": f"Bearer {KEY}"}
    before = api.client.get(f"/api/v1/orders/{order_id}", headers=headers).json()
    assert before["ready"] and before["deliverables"] == {} and before["plan_required"] is True
    api.db.set_plan_chosen("u1")
    after = api.client.get(f"/api/v1/orders/{order_id}", headers=headers).json()
    assert "txt" in after["deliverables"] and "plan_required" not in after       # free-minute results are downloadable


def test_staff_keys_cannot_create_client_orders(api, wav_bytes):
    api.db.get_or_create_user("s1", "staff@example.com", "staff")
    conn = api.db.get_conn()
    conn.execute("UPDATE users SET role = 'staff' WHERE id = 's1'")
    conn.commit()
    key = "kauli_live_staffkey_0123456789"
    api.db.set_client_api_key("s1", hashlib.sha256(key.encode()).hexdigest(), key[:18])
    assert api.post(wav_bytes, key=key).status_code == 403


def test_the_api_identity_never_leaks_into_browser_requests(api, wav_bytes):
    api.post(wav_bytes)
    # a plain browser request after an API call has no user, key or not
    assert api.client.get("/client", follow_redirects=False).status_code in (302, 303, 307)


def test_languages_list_is_public(api):
    r = api.client.get("/api/v1/languages")
    assert r.status_code == 200
    langs = {x["code"]: x for x in r.json()["languages"]}
    assert {"sw", "en", "ki", "fr"} <= set(langs)
    assert langs["sw"]["human_review"] and langs["sw"]["dubbing"]
    assert langs["fr"]["transcription"] and not langs["fr"]["human_review"]
    assert not langs["ki"]["translation_target"]
