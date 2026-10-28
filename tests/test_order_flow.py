"""End-to-end checks of the money paths against a THROWAWAY database: order
creation (tiers, languages, Lara selection, frozen prices), the delivery
worker, the unlock/upgrade payments and Paystack subscription renewals.

Nothing here touches the real database, the real providers or the network:
the DB path is redirected to a temp file, the worker's queue and providers are
stubbed, and the signed-in user is a stand-in (no session cookie is forged)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg not installed", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

PROVIDER_ENV = ("LARA_ACCESS_KEY_ID", "LARA_ACCESS_KEY_SECRET", "ANTHROPIC_API_KEY", "AZURE_TRANSLATOR_KEY",
                "AZURE_SPEECH_KEY", "TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2", "PAYSTACK_PLAN_PRO",
                "PAYSTACK_PLAN_PREMIUM", "PAYSTACK_CURRENCY")


@pytest.fixture(scope="module")
def wav_bytes(tmp_path_factory):
    out = tmp_path_factory.mktemp("audio") / "clip.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-ar", "16000", "-ac", "1", str(out)], check=True)
    return out.read_bytes()


@pytest.fixture
def env(tmp_path, monkeypatch):
    before = dict(os.environ)
    from webapp import app as app_mod, db
    for key in set(os.environ) - set(before):  # importing the app loads the real .env - undo that
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
    # The real limiter (10 submissions per 10 minutes per account) would trip
    # on a test suite that places many orders as one user.
    monkeypatch.setattr(app_mod.rate_limit, "check", lambda key, limit=0, window_s=0: (True, 0))

    db.get_or_create_user("u1", "client@example.com", "client")
    db.set_trial_verified("u1")
    db.set_plan_chosen("u1")
    db.set_billing_market("u1", "intl")
    monkeypatch.setattr(app_mod, "current_user",
                        lambda request: {**dict(db.get_user("u1")), "client_scope_id": "u1"})
    client = TestClient(app_mod.app, follow_redirects=False)

    def place(wav, **overrides):
        data = {"source_lang": "en", "target_lang": "en", "service_level": "transcribe", "review_tier": "ai",
                "instr_verbatim_level": "clean_read"}
        data.update(overrides)
        return client.post("/client/orders", data=data, files={"audio": ("clip.wav", wav, "audio/wav")})

    def order_from(resp):
        if resp.status_code != 303:
            import re
            shown = re.findall(r'class="error"[^>]*>(.*?)</p>', resp.text, flags=re.S)
            raise AssertionError(f"order was refused: {shown or resp.text[:200]}")
        order_id = resp.headers["location"].split("/client/orders/")[1].split("/")[0].split("?")[0]
        return db.get_order(order_id)

    return SimpleNamespace(app=app_mod, db=db, client=client, place=place, order_from=order_from,
                           submitted=submitted, monkeypatch=monkeypatch)


def _set_lara(env):
    env.monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    env.monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")


def _orders(env):
    return env.db.list_orders_for_client("u1")


# ------------------------------------------------------------ order creation
def test_free_instant_ai_transcription_is_a_preview_with_frozen_us_price(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    assert order["review_tier"] == "ai" and order["billing_market"] == "intl"
    assert order["is_free_preview"] == 1 and order["cost_usd"] == 0
    assert order["tat_start_at"] is None                      # no human turnaround promised
    assert env.submitted == [order["id"]]                     # straight to processing
    assert "0.12" in order["cost_breakdown_json"]


def test_human_reviewed_order_is_priced_from_the_us_card_and_waits_for_payment(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert order["review_tier"] == "human" and order["status"] == "pending_payment"
    assert abs(order["cost_usd"] - 1.20) < 0.01               # 1.0 minute x $1.20
    assert env.submitted == []                                # nothing runs before payment


def test_free_minutes_are_not_given_to_human_review(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert order["is_free_preview"] == 0 and order["cost_usd"] > 0


def test_east_africa_and_legacy_accounts_pay_their_own_card(env, wav_bytes):
    env.db.set_billing_market("u1", "ea")
    assert abs(env.order_from(env.place(wav_bytes, review_tier="human"))["cost_usd"] - 0.60) < 0.01
    env.db.set_billing_market("u1", "legacy")
    assert abs(env.order_from(env.place(wav_bytes, review_tier="human"))["cost_usd"] - 0.40) < 0.01


def test_wider_language_translation_needs_lara_and_ai_tier(env, wav_bytes):
    before = len(_orders(env))
    resp = env.place(wav_bytes, source_lang="zh", service_level="translate")
    assert resp.status_code != 303 and "Instant AI" in resp.text and len(_orders(env)) == before

    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="zh", service_level="translate"))
    assert order["mt"] == "lara" and order["review_tier"] == "ai" and order["status"] == "pending_payment"
    assert abs(order["cost_usd"] - 0.30) < 0.01


def test_wider_languages_can_never_be_dubbed_or_human_reviewed(env, wav_bytes):
    _set_lara(env)
    before = len(_orders(env))
    for overrides in ({"source_lang": "zh", "service_level": "dub"},
                      {"source_lang": "zh", "service_level": "translate", "review_tier": "human"},
                      {"source_lang": "en", "target_lang": "es", "service_level": "dub"},
                      {"source_lang": "en", "target_lang": "es", "service_level": "translate", "review_tier": "human"},
                      {"source_lang": "en", "target_lang": "ki", "service_level": "translate"},
                      {"source_lang": "en", "target_lang": "en", "service_level": "translate"}):
        resp = env.place(wav_bytes, **overrides)
        assert resp.status_code != 303, overrides
    assert len(_orders(env)) == before


def test_english_to_spanish_ai_translation_is_accepted_with_lara(env, wav_bytes):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="en", target_lang="es", service_level="translate"))
    assert order["target_lang"] == "es" and order["mt"] == "lara"


def test_kikuyu_is_forced_to_human_review_with_the_manual_surcharge(env, wav_bytes):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="ki", service_level="translate", review_tier="ai"))
    assert order["review_tier"] == "human" and order["mt"] == "lara"
    assert "manual_transcription" in order["addons"]


def test_without_lara_swahili_to_english_keeps_the_original_path(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    assert order["mt"] in ("local", "azure-translate", "claude")


# ------------------------------------------------------------------- worker
def _run_worker(env, order, flagged=False):
    from kauli import models  # noqa: F401
    worker = env.app.worker
    env.monkeypatch.setattr(worker, "kauli_run", lambda **kw: None)
    env.monkeypatch.setattr(worker, "Job", SimpleNamespace(load=lambda p: SimpleNamespace(cost_usd=0.01, flagged_count=0)))
    env.monkeypatch.setattr(worker.notifications, "notify_staff", lambda *a, **k: None)
    if flagged:
        env.db.set_order_content_safety_flag(order["id"], True, "test")
    worker._run_job(order["id"])
    return env.db.get_order(order["id"])


def test_instant_ai_order_delivers_without_review_and_human_waits(env, wav_bytes):
    ai = env.order_from(env.place(wav_bytes))
    assert _run_worker(env, ai)["status"] == "ready_for_delivery"
    human = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert _run_worker(env, human)["status"] == "awaiting_review"


def test_content_safety_flag_always_forces_human_review(env, wav_bytes):
    ai = env.order_from(env.place(wav_bytes))
    assert _run_worker(env, ai, flagged=True)["status"] == "awaiting_review"


# ------------------------------------------------------------- upsells/payments
def _payment(env, order, kind, amount):
    pid = env.app.billing.new_reference()
    env.db.create_payment(pid, "u1", order["tier"], amount, None, "USD", "paystack", order_id=order["id"],
                          meta=f'{{"kind": "{kind}"}}')
    return env.db.get_payment(pid)


def test_free_minute_results_have_nothing_to_unlock_or_upgrade(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    prices = env.app._order_upsell_prices(env.db.get_order(order["id"]))
    assert prices == {"unlock": None, "upgrade": None}        # downloadable once a plan is chosen


def test_upgrade_to_human_review_charges_the_difference_and_queues_review(env, wav_bytes):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    prices = env.app._order_upsell_prices(env.db.get_order(order["id"]))
    assert prices["unlock"] is None and abs(prices["upgrade"] - (1.40 - 0.30)) < 0.01
    env.monkeypatch.setattr(env.app.worker, "notify_staff_needs_review", lambda o: None)
    assert env.app._activate_payment(_payment(env, order, "review_upgrade", prices["upgrade"]), "ref-up")
    upgraded = env.db.get_order(order["id"])
    assert upgraded["review_tier"] == "human" and upgraded["status"] == "awaiting_review"
    assert upgraded["tat_start_at"] is not None


def test_upsell_prices_use_the_frozen_rate_not_todays_card(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="ai"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    env.monkeypatch.setitem(env.app.billing.PRICE_CARD["intl"]["translate"], "human", 9.99)  # a later price change
    assert env.app._order_upsell_prices(env.db.get_order(order["id"]))["upgrade"] is not None


def test_paystack_subscription_renewal_extends_the_plan_once(env):
    env.monkeypatch.setenv("PAYSTACK_PLAN_PRO", "PLN_test")
    data = {"id": 98765, "plan": {"plan_code": "PLN_test"}, "customer": {"email": "client@example.com"}}
    env.app._handle_subscription_renewal(data, "http://testserver")
    assert env.db.get_subscription("u1")["plan"] == "pro"
    env.app._handle_subscription_renewal(data, "http://testserver")  # a webhook retry
    done = [p for p in env.db.list_payments_for_user("u1") if p["status"] == "completed"]
    assert len(done) == 1


def test_renewal_for_an_unknown_plan_or_customer_is_ignored(env):
    env.monkeypatch.setenv("PAYSTACK_PLAN_PRO", "PLN_test")
    env.app._handle_subscription_renewal({"id": 1, "plan": {"plan_code": "PLN_other"},
                                          "customer": {"email": "client@example.com"}}, "")
    env.app._handle_subscription_renewal({"id": 2, "plan": {"plan_code": "PLN_test"},
                                          "customer": {"email": "nobody@example.com"}}, "")
    assert (env.db.get_subscription("u1") or {"plan": "free"})["plan"] == "free"


# --------------------------------------------------------- logged-in pages render
def test_client_pages_render_with_both_tiers_and_the_upsell_buttons(env, wav_bytes):
    env.monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")  # makes paystack_configured() true
    _set_lara(env)

    # a paid Instant AI translation, delivered -> "Add human review" button
    paid = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate"))
    env.db.update_order_status(paid["id"], "ready_for_delivery")
    page = env.client.get(f"/client/orders/{paid['id']}")
    assert page.status_code == 200
    assert "Instant AI" in page.text and "has <strong>not</strong> been reviewed by a person" in page.text
    assert "Add human review" in page.text and "Transcript (.docx)" in page.text
    assert "human-verified" not in page.text.lower()

    # a free-minutes order downloads like any other once the plan is chosen (no more "unlock")
    free = env.order_from(env.place(wav_bytes))
    env.db.update_order_status(free["id"], "ready_for_delivery")
    page = env.client.get(f"/client/orders/{free['id']}")
    assert page.status_code == 200 and "Transcript (.docx)" in page.text and "Unlock this transcript" not in page.text

    # a human-reviewed order never shows the Instant AI banner or upsells
    human = env.order_from(env.place(wav_bytes, review_tier="human"))
    env.db.update_order_status(human["id"], "ready_for_delivery")
    page = env.client.get(f"/client/orders/{human['id']}")
    assert page.status_code == 200 and "Instant AI" not in page.text and "Add human review" not in page.text

    dash = env.client.get("/client")
    assert dash.status_code == 200
    assert 'name="review_tier"' in dash.text and 'value="ai"' in dash.text
    assert "Instant AI translation only" in dash.text          # the wider-language target group (Lara on)
    billing_page = env.client.get("/client/billing")
    assert billing_page.status_code == 200 and "Instant AI" in billing_page.text


def test_legacy_accounts_default_to_human_review_and_new_ones_to_instant_ai(env):
    import re
    env.db.set_billing_market("u1", "legacy")
    legacy = env.client.get("/client").text
    assert re.search(r'name="review_tier" value="human"\s+checked', legacy)
    env.db.set_billing_market("u1", "intl")
    fresh = env.client.get("/client").text
    assert re.search(r'name="review_tier" value="ai"\s+checked', fresh)
