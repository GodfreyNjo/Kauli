"""End-to-end checks of the money paths against a THROWAWAY database: order
creation (tiers, languages, Lara selection, frozen prices), the delivery
worker, the unlock/upgrade payments and Paystack subscription renewals.

Nothing here touches the real database, the real providers or the network:
the DB path is redirected to a temp file, the worker's queue and providers are
stubbed, and the signed-in user is a stand-in (no session cookie is forged)."""
import os
import shutil
import subprocess
import re
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
                "PAYSTACK_PLAN_PREMIUM", "PAYSTACK_CURRENCY", "GROQ_API_KEY", "ASSEMBLYAI_API_KEY", "KAULI_CHEAP_ASR", "KAULI_CHEAP_ASR_ORDER")


@pytest.fixture(scope="module")
def wav_bytes(tmp_path_factory):
    out = tmp_path_factory.mktemp("audio") / "clip.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-ar", "16000", "-ac", "1", str(out)], check=True)
    return out.read_bytes()


@pytest.fixture
def env(tmp_path, monkeypatch):
    before = dict(os.environ)
    from webapp import app as app_mod, db, sudo_mode
    for key in set(os.environ) - set(before):  # importing the app loads the real .env - undo that
        del os.environ[key]
    os.environ.update(before)
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    for kind_key in ("provider_default_asr", "provider_default_mt"):             # the house choice (Transkriptor, Azure) is tested on its own; the routing tests start from Automatic
        db.set_flag(kind_key, "")
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
    monkeypatch.setattr(sudo_mode, "confirmed_recently", lambda request: True)   # tests are past "confirm it is you"
    client = TestClient(app_mod.app, follow_redirects=False)

    def place(wav, **overrides):
        data = {"source_lang": "en", "target_lang": "en", "service_level": "transcribe", "review_tier": "ai",
                "instr_verbatim_level": "clean_read"}
        data.update(overrides)
        return client.post("/account/orders", data=data, files={"audio": ("clip.wav", wav, "audio/wav")})

    def order_from(resp):
        if resp.status_code != 303:
            import re
            shown = re.findall(r'class="error"[^>]*>(.*?)</p>', resp.text, flags=re.S)
            raise AssertionError(f"order was refused: {shown or resp.text[:200]}")
        order_id = resp.headers["location"].split("/account/orders/")[1].split("/")[0].split("?")[0]
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
    assert abs(order["cost_usd"] - 1.79) < 0.01               # 1.0 minute x $1.79
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
    env.monkeypatch.setenv("KAULI_REVIEW_EXTRA_LANGUAGES", "none")        # Chinese, German and Spanish review switched off
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


def test_free_minute_results_unlock_at_their_full_price_and_have_no_upgrade(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'ready_for_delivery', duration_minutes = 10 WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    row = env.db.get_order(order["id"])
    prices = env.app._order_upsell_prices(row)
    assert prices["upgrade"] is None and prices["unlock"] and prices["unlock"] == env.app._unlock_price(row) > 0      # free minutes never unlock a download


def test_upgrade_to_human_review_charges_the_difference_and_queues_review(env, wav_bytes):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    prices = env.app._order_upsell_prices(env.db.get_order(order["id"]))
    assert prices["unlock"] is None and abs(prices["upgrade"] - (2.99 - 0.30)) < 0.01
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
    page = env.client.get(f"/account/orders/{paid['id']}")
    assert page.status_code == 200
    assert "Instant AI" in page.text and "not reviewed by a person" not in page.text
    assert "Add human review" in page.text and "Transcript (.docx)" in page.text
    assert "human-verified" not in page.text.lower()

    # a free-minutes order can be read but not downloaded until it is paid for
    free = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'ready_for_delivery', duration_minutes = 10 WHERE id = ?", (free["id"],))
    conn.commit()
    conn.close()
    page = env.client.get(f"/account/orders/{free['id']}")
    assert page.status_code == 200 and "Transcript (.docx)" not in page.text and "Unlock this order" in page.text

    # a human-reviewed order never shows the Instant AI banner or upsells
    human = env.order_from(env.place(wav_bytes, review_tier="human"))
    env.db.update_order_status(human["id"], "ready_for_delivery")
    page = env.client.get(f"/account/orders/{human['id']}")
    assert page.status_code == 200 and "Instant AI" not in page.text and "Add human review" not in page.text

    dash = env.client.get("/account")
    assert dash.status_code == 200
    assert 'name="review_tier"' in dash.text and 'value="ai"' in dash.text
    assert "Instant AI translation only" in dash.text          # the wider-language target group (Lara on)
    billing_page = env.client.get("/account/billing")
    assert billing_page.status_code == 200 and "Instant AI" in billing_page.text


def test_every_account_opens_on_instant_ai_so_orders_ship_by_themselves(env):
    import re
    for market in ("legacy", "ea", "intl"):                                          # older accounts used to open on Human review: their orders waited for staff
        env.db.set_billing_market("u1", market)
        page = env.client.get("/account").text
        assert re.search(r'name="review_tier" value="ai"\s+checked', page), market
        assert not re.search(r'name="review_tier" value="human"\s+checked', page), market


# ------------------------------------------------ the service follows the languages
def test_a_transcription_order_with_different_languages_is_taken_as_a_translation(env, wav_bytes):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="en", target_lang="de", service_level="transcribe", review_tier="ai"))
    assert order["service_level"] == "translate" and order["target_lang"] == "de"
    cost = order["cost_usd"] or 0
    plain = env.order_from(env.place(wav_bytes, source_lang="en", target_lang="en", service_level="transcribe", review_tier="ai"))
    assert plain["service_level"] == "transcribe" and plain["target_lang"] == "en"
    assert cost >= (plain["cost_usd"] or 0)


def test_same_language_or_nothing_chosen_stays_a_plain_transcript(env, wav_bytes):
    for target in ("same", ""):
        order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang=target, service_level="transcribe", review_tier="ai"))
        assert order["service_level"] == "transcribe" and order["target_lang"] == "sw", target


def test_swahili_to_english_chosen_under_transcription_is_a_translation(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level="transcribe", review_tier="ai"))
    assert order["service_level"] == "translate" and order["target_lang"] == "en"


def test_the_form_offers_same_language_and_the_wizard_switches_the_service_itself(env):
    page = env.client.get("/account").text
    assert 'value="same"' in page and "Same language (transcript only)" in page and 'id="translation-switch-note"' in page
    js = env.client.get("/static/client_wizard.js").text
    assert "translation-switch-note" in js and 'value="translate"' in js



# ------------------------------------------------ the human-verification offer on a delivered Instant AI translation
def _delivered_translation(env, wav_bytes, **overrides):
    env.monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    _set_lara(env)
    data = {"source_lang": "sw", "target_lang": "en", "service_level": "translate"}
    data.update(overrides)
    order = env.order_from(env.place(wav_bytes, **data))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    return env.db.get_order(order["id"])


def test_a_delivered_automatic_translation_offers_human_verification_with_its_benefits_and_the_difference_as_the_price(env, wav_bytes):
    order = _delivered_translation(env, wav_bytes)
    offer = env.app._human_check_offer(order)
    assert offer and abs(offer["price"] - env.app._order_upsell_prices(order)["upgrade"]) < 0.005 and offer["turnaround"].startswith("within about")
    page = env.client.get(f"/account/orders/{order['id']}").text
    assert 'id="human-check"' in page and "Want a person to check this translation?" in page
    for benefit in ("Fast", "Up to 99% accuracy", "Context, not just words", "Marked ready only after review"):
        assert benefit in page, benefit
    assert f"${offer['price']:.2f}" in page and "You pay only the difference" in page and "Add human review" in page and 'id="hc-later"' in page
    assert page.count('action="/account/orders/' + order["id"] + '/upgrade-review"') == 1                  # one offer, not the old button as well


def test_the_offer_is_only_for_automatic_translations_that_can_really_be_human_reviewed(env, wav_bytes):
    _set_lara(env)
    plain = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="sw", service_level="transcribe"))
    env.db.update_order_status(plain["id"], "ready_for_delivery")
    assert env.app._human_check_offer(env.db.get_order(plain["id"])) is None                              # a transcript: the plain upgrade button as before
    pending = _delivered_translation(env, wav_bytes)
    env.db.update_order_status(pending["id"], "processing")
    assert env.app._human_check_offer(env.db.get_order(pending["id"])) is None                            # not delivered yet
    human = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    env.db.update_order_status(human["id"], "ready_for_delivery")
    assert env.app._human_check_offer(env.db.get_order(human["id"])) is None                              # already human-reviewed
    german = _delivered_translation(env, wav_bytes, source_lang="en", target_lang="de")
    env.monkeypatch.setenv("KAULI_REVIEW_EXTRA_LANGUAGES", "none")
    assert env.app._human_check_offer(env.db.get_order(german["id"])) is None                             # no human reviewers for German when it is switched off
    env.monkeypatch.setenv("KAULI_REVIEW_EXTRA_LANGUAGES", "zh,de,es")
    assert env.app._human_check_offer(env.db.get_order(german["id"]))



# ------------------------------------------------ the payment page by market, and honest steps
def test_the_payment_page_offers_mobile_money_only_in_kenya(env, wav_bytes):
    env.monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    order = env.order_from(env.place(wav_bytes, review_tier="human"))                      # waits for payment
    abroad = env.client.get(f"/account/orders/{order['id']}/pay", headers={"cf-ipcountry": "DE"}).text
    assert "Secure checkout by Paystack" in abroad or "Paystack" in abroad
    for word in ("M-Pesa", "Airtel Money", "M-Pesa Till"):
        assert word not in abroad, word                                                    # a customer outside Kenya sees a card payment, nothing local
    kenya = env.client.get(f"/account/orders/{order['id']}/pay", headers={"cf-ipcountry": "KE"}).text
    assert "M-Pesa" in kenya and "Airtel Money" in kenya


def test_the_upload_steps_do_not_say_starting_when_payment_comes_first_and_the_page_is_not_rewritten(env):
    js = env.client.get("/static/client_wizard.js").text
    assert 'kT("Preparing your payment")' in js and 'kT("Confirming your order")' in js and "finalStepLabel()" in js
    assert 'labels = [first, kT("Checking your audio"), kT("Working out your price"), finalStepLabel()]' in js
    assert js.count('kT("Starting your order")') == 1                                       # only inside the one function that decides the label
    assert "window.location.replace(landedUrl)" in js                                       # success is an ordinary navigation, so no flash of unstyled icons
    page = env.client.get("/account/home").text
    assert re.search(r'<svg class="icon" viewBox="0 0 24 24" width="22" height="22"', page)  # the header bell does not depend on the stylesheet for its size
