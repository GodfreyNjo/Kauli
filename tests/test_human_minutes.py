"""Plans include some human-reviewed minutes every month (the way a subscription at Rev does): a person checks those with no per-minute
charge, and beyond them the normal human-review price per minute applies."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import billing  # noqa: E402


def _plan(env, plan):
    env.db.set_subscription_plan("u1", plan, 30)


def test_paid_plans_include_human_minutes_and_the_free_plan_does_not():
    assert billing.plan_human_minutes("free") == 0
    assert 0 < billing.plan_human_minutes("pro") < billing.plan_human_minutes("premium") < billing.plan_human_minutes("enterprise")
    assert (billing.plan_human_minutes("pro"), billing.plan_human_minutes("premium")) == (5, 15)


def test_the_included_human_minutes_only_cover_human_reviewed_transcription():
    covers = billing.human_allowance_covers
    for plan in ("pro", "premium", "enterprise"):
        assert covers(plan, "transcribe", "human")
        assert not covers(plan, "translate", "human") and not covers(plan, "dub", "human") and not covers(plan, "transcribe", "ai")
    assert not covers("free", "transcribe", "human")


def test_a_plan_with_every_included_minute_used_still_makes_money():
    for plan in ("pro", "premium"):
        p = billing.PLANS[plan]
        ai = p["quality_minutes"] * 0.033 + (p["included_ai_minutes"] - p["quality_minutes"]) * 0.004      # the dearer providers all month
        human = p["included_human_minutes"] * billing.human_review_cost_per_min()
        assert ai + human < p["price_usd"] * (1 - billing.PAYMENT_FEE_RATE), plan


def test_a_human_reviewed_order_inside_the_included_minutes_costs_nothing_and_goes_to_work(env, wav_bytes):
    _plan(env, "pro")
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert order["cost_usd"] == 0 and order["status"] != "pending_payment" and env.submitted == [order["id"]]
    assert order["is_free_preview"] == 0                                                   # a person's review is delivered in full, never a preview
    assert '"human_minutes_included": 1.0' in order["cost_breakdown_json"]
    assert order["tat_start_at"] is not None                                               # it still gets a human turnaround and a reviewer
    sub = env.db.get_subscription_current("u1")
    assert billing.human_minutes_remaining(sub) == 4 and billing.free_minutes_remaining(sub) == 2500      # the AI minutes are untouched


def test_beyond_the_included_minutes_the_normal_human_price_applies(env, wav_bytes):
    _plan(env, "pro")
    for _ in range(5):
        assert env.order_from(env.place(wav_bytes, review_tier="human"))["cost_usd"] == 0
    sixth = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert sixth["status"] == "pending_payment" and abs(sixth["cost_usd"] - 1.79) < 0.01


def test_a_partly_covered_order_is_charged_only_for_the_rest(env, wav_bytes):
    _plan(env, "pro")
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET human_minutes_used_this_period = 4.5 WHERE user_id = 'u1'")      # half a minute left
    conn.commit()
    conn.close()
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    assert abs(order["cost_usd"] - 0.5 * 1.79) < 0.02 and order["status"] == "pending_payment"


def test_translation_and_dubbing_are_not_covered_and_the_free_plan_pays(env, wav_bytes):
    _plan(env, "premium")
    translate = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    assert translate["cost_usd"] > 2.5 and billing.human_minutes_remaining(env.db.get_subscription_current("u1")) == 15
    env.db.set_subscription_plan("u1", "free", None)
    assert abs(env.order_from(env.place(wav_bytes, review_tier="human"))["cost_usd"] - 1.79) < 0.01


def test_instant_ai_orders_do_not_use_the_human_minutes(env, wav_bytes):
    _plan(env, "pro")
    env.order_from(env.place(wav_bytes, review_tier="ai"))
    sub = env.db.get_subscription_current("u1")
    assert billing.human_minutes_remaining(sub) == 5 and billing.free_minutes_remaining(sub) == 2499


def test_human_minutes_renew_with_the_period_and_cannot_be_double_spent(env):
    _plan(env, "pro")
    assert env.db.reserve_human_minutes("u1", 3, 5) == 3 and env.db.reserve_human_minutes("u1", 3, 5) == 2 and env.db.reserve_human_minutes("u1", 3, 5) == 0
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET period_started_at = ? WHERE user_id = 'u1'", (time.time() - 40 * 86400,))
    conn.commit()
    conn.close()
    assert billing.human_minutes_remaining(env.db.get_subscription_current("u1")) == 5            # a new month, a fresh allowance


def test_the_pages_show_the_human_minutes(env):
    _plan(env, "pro")
    assert "human-reviewed minutes" in env.client.get("/client/plans").text
    billing_page = env.client.get("/client/billing").text
    assert "Human-reviewed minutes left" in billing_page and "Human-reviewed minutes / month" in billing_page
    assert 'data-human-minutes="5.0"' in env.client.get("/client").text
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)                  # a visitor sees the public pricing
    home = env.client.get("/").text
    assert "5 human-reviewed minutes" in home and "15 human-reviewed minutes" in home
    js = (Path(__file__).parent.parent / "webapp/static/client_wizard.js").read_text(encoding="utf-8")
    assert "humanIncludedEligible" in js and "dataset.humanMinutes" in js


def test_instant_ai_orders_cannot_ask_for_speaker_labels_but_human_review_can(env, wav_bytes):
    ai = env.order_from(env.place(wav_bytes, review_tier="ai", instr_speaker_ids="1"))
    assert ai["instr_speaker_ids"] == 0                                                     # labels like NARRATOR: are an editor's work
    human = env.order_from(env.place(wav_bytes, review_tier="human", instr_speaker_ids="1"))
    assert human["instr_speaker_ids"] == 1
    meeting = env.order_from(env.place(wav_bytes, review_tier="ai", instr_speaker_ids="1", idempotency_key="meeting-abc"))
    assert meeting["instr_speaker_ids"] == 1                                                # the meeting notetaker identifies its own speakers
    page = env.client.get("/client").text
    assert 'id="speaker-label-field"' in page and "to label speakers" in page
    js = (Path(__file__).parent.parent / "webapp/static/client_wizard.js").read_text(encoding="utf-8")
    assert "speaker-label-field" in js


def test_flash_messages_fade_away_but_lasting_facts_stay(env):
    js = env.client.get("/static/flash.js").text
    assert "data-persist" in js and "mouseenter" in js and 'searchParams.has(k)' in js
    # accessibility: notices fade (and wait while hovered or focused), errors stay until dismissed, every message has a dismiss button and is announced
    assert "SHOW_MS = 10000" in js and "errors stay until dismissed" in js and "flash-x" in js and "kpAnnounce" in js
    page = env.client.get("/client").text
    assert "/static/flash.js" in page
    root = Path(__file__).parent.parent / "webapp/templates"
    for name, text in (("order_detail.html", "Ready. Download your files below."), ("order_detail.html", "Something went wrong on our end"),
                       ("staff_review.html", "Pipeline failed:")):
        src = (root / name).read_text(encoding="utf-8")
        line = next(l for l in src.splitlines() if text in l)
        assert "data-persist" in line, (name, text)                                         # a failed order's explanation never disappears
    # the order of the Orders page: start a new project first, the earlier orders below it
    assert page.index('id="new-project"') < page.index('id="my-orders"')


# ---------------------------------------------------------------------------------------- a new customer's first order
def _fresh(env):
    conn = env.db.get_conn()
    conn.execute("DELETE FROM orders WHERE client_id = 'u1'")
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    conn.close()


def test_a_new_customers_first_order_is_processed_even_when_it_goes_past_the_free_minutes(env, wav_bytes, monkeypatch):
    _fresh(env)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 79.8)                  # the 80-minute file in the report: $5.98 beyond the free minutes
    first = env.order_from(env.place(wav_bytes))
    assert first["status"] != "pending_payment" and first["cost_usd"] == 0 and env.submitted == [first["id"]]
    assert '"first_order_courtesy_usd": 5.98' in first["cost_breakdown_json"] or '"first_order_courtesy_usd": 5.97' in first["cost_breakdown_json"]


def test_only_the_first_order_is_free_and_only_for_short_enough_instant_ai(env, wav_bytes, monkeypatch):
    _fresh(env)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 79.8)
    env.order_from(env.place(wav_bytes))
    second = env.order_from(env.place(wav_bytes))
    assert second["status"] == "pending_payment" and second["cost_usd"] > 0                   # after that it is pay as you go
    _fresh(env)
    assert env.order_from(env.place(wav_bytes, review_tier="human"))["status"] == "pending_payment"        # human review is not part of it
    _fresh(env)
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 300.0)
    assert env.order_from(env.place(wav_bytes))["status"] == "pending_payment"               # a very long file is not either
    _fresh(env)
    env.db.set_plan_chosen("u1")
    monkeypatch.setattr(env.app, "probe_duration_minutes", lambda path: 79.8)
    assert env.order_from(env.place(wav_bytes))["status"] == "pending_payment"               # someone who already chose a plan is not "new"


def test_unlocking_is_one_simple_step_with_the_new_wording(env, wav_bytes):
    _fresh(env)
    env.monkeypatch.setattr(env.app.billing, "paystack_configured", lambda: True)
    page = env.client.get("/client/verify-trial").text
    for text in ("Get started for free", "Add a card to unlock your free downloads.", "card check", "back immediately", "Start downloading",
                 "Confirm card and start free", "Prefer a paid plan?", "See plans"):
        assert text in page, text
    home = env.client.get("/client/home").text
    assert "Unlock your files in one step" in home and 'href="/client/verify-trial"' in home and "Unlock now" in home
    assert "not reviewed by a person" not in env.client.get("/client").text
