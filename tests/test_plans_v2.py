"""Plans that reward commitment: included Instant AI minutes, a yearly price, a custom tier, and no plan that can lose money."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from webapp import billing  # noqa: E402

# the worst case we plan for per Instant AI minute (the dearer end of the Transkriptor range) plus a minute of meeting capture
WORST_CASE_COST_PER_AI_MINUTE = 0.033 + 0.012 + 0.004          # ... plus translating that minute (the included minutes cover translation too)


def _sub(plan, status="active", end_in_days=20, used=0.0, bonus=0.0):
    return {"plan": plan, "status": status, "current_period_end": time.time() + end_in_days * 86400,
            "minutes_used_this_period": used, "bonus_minutes": bonus, "paystack_subscription_code": None}


def test_each_plan_includes_instant_ai_minutes_worth_about_what_it_costs():
    assert billing.plan_included_minutes("free") == billing.FREE_MINUTES_PER_MONTH == 60 and billing.plan_included_minutes("pro") == 150 and billing.plan_included_minutes("premium") == 400
    ai_rate = billing.PRICE_CARD["intl"]["transcribe"]["ai"]
    for plan in ("pro", "premium"):
        value = billing.plan_included_minutes(plan) * ai_rate
        assert 0.85 * billing.PLANS[plan]["price_usd"] <= value <= 1.0 * billing.PLANS[plan]["price_usd"] + 1, plan   # "equivalent to the price"


def test_the_allowance_follows_the_plan_in_force_and_resets_with_the_period():
    assert billing.free_minutes_remaining(None) == 60
    assert billing.free_minutes_remaining(_sub("pro")) == 150
    assert billing.free_minutes_remaining(_sub("premium", used=100)) == 300
    assert billing.free_minutes_remaining(_sub("pro", used=500)) == 0
    assert billing.free_minutes_remaining(_sub("pro", bonus=10)) == 160
    assert billing.free_minutes_remaining(_sub("pro", status="cancelled")) == 60          # not active: back to the trial allowance
    assert billing.free_minutes_remaining(_sub("pro", end_in_days=-30)) == 60             # lapsed: back to the trial allowance


def test_yearly_prices_save_real_money_and_never_lose_money():
    for plan in ("pro", "premium"):
        p = billing.PLANS[plan]
        assert 25 <= billing.yearly_saving_pct(plan) <= 45, plan                          # a clear incentive, like the industry leaders
        monthly_revenue = p["price_usd_year"] / 12 * (1 - billing.PAYMENT_FEE_RATE)
        worst_case_cost = p["included_ai_minutes"] * WORST_CASE_COST_PER_AI_MINUTE
        assert worst_case_cost <= 0.65 * monthly_revenue, (plan, worst_case_cost, monthly_revenue)   # at least 35% of the money is left even if every included minute is used at the worst-case cost
        assert billing.yearly_monthly_equivalent(plan) < p["price_usd"]


def test_the_discount_on_everything_else_still_applies_after_the_included_minutes():
    one_hour = billing.order_cost_usd(60, "dub", "premium", 0.0, market="intl", review_tier="human")
    assert one_hour["gross_usd"] == 239.4 and one_hour["total_usd"] == pytest.approx(239.4 * 0.8, abs=0.01)


def test_yearly_checkout_activates_a_year_and_uses_the_yearly_plan_code(monkeypatch):
    from fastapi.testclient import TestClient
    from webapp import app as app_mod, db
    monkeypatch.setenv("PAYSTACK_PLAN_PRO_YEARLY_USD", "PLN_pro_year")
    monkeypatch.setenv("PAYSTACK_PLAN_PRO_USD", "PLN_pro_month")
    assert billing.paystack_plan_code("pro", "USD", yearly=True) == "PLN_pro_year"
    assert billing.paystack_plan_code("pro", "USD") == "PLN_pro_month"
    assert billing.paystack_plan_code("pro", "KES", yearly=True) is None                 # never in a currency it was not made for
    assert billing.paystack_plan_is_yearly("PLN_pro_year") and not billing.paystack_plan_is_yearly("PLN_pro_month")
    assert billing.paystack_plan_codes("pro") == {"PLN_pro_year", "PLN_pro_month"}


def test_activating_a_yearly_payment_sets_a_365_day_period(tmp_path, monkeypatch):
    from webapp import app as app_mod, db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.get_or_create_user("u9", "year@example.com", "client")
    pid = billing.new_reference()
    db.create_payment(pid, "u9", "premium", billing.PLANS["premium"]["price_usd_year"], None, "USD", "paystack", meta=json.dumps({"kind": "plan_year"}))
    assert app_mod._activate_payment(db.get_payment(pid), "ref-year-1")
    sub = db.get_subscription_current("u9")
    days = (sub["current_period_end"] - time.time()) / 86400
    assert sub["plan"] == "premium" and 360 < days <= 365.1
    assert billing.free_minutes_remaining(sub) == 400
    pid2 = billing.new_reference()
    db.create_payment(pid2, "u9", "pro", billing.PLANS["pro"]["price_usd"], None, "USD", "paystack", meta=json.dumps({"kind": "order"}))
    assert app_mod._activate_payment(db.get_payment(pid2), "ref-month-1")
    sub = db.get_subscription_current("u9")
    assert sub["plan"] == "pro" and 25 < (sub["current_period_end"] - time.time()) / 86400 <= 30.1


def test_the_pricing_page_has_a_yearly_toggle_included_minutes_and_a_custom_tier():
    from fastapi.testclient import TestClient
    from webapp import app as app_mod
    html = TestClient(app_mod.app, follow_redirects=False).get("/").text
    section = html.split('id="pz-plans"')[1].split("</section>")[0]
    assert 'data-interval="month"' in section and 'data-interval="year"' in section
    assert "150 Instant AI minutes" in section and "400 Instant AI minutes" in section
    assert "$12.42" in section and "billed $149 a year" in section and "$31.58" in section
    assert "Enterprise" in section and "Custom" in section and "/demo?interest=enterprise" in section


def test_included_minutes_cover_instant_ai_transcription_and_translation_on_paid_plans_only():
    covers = billing.allowance_covers
    for plan in ("pro", "premium", "enterprise"):
        assert covers(plan, "transcribe", "ai") and covers(plan, "translate", "ai"), plan
        assert not covers(plan, "dub", "ai") and not covers(plan, "transcribe", "human") and not covers(plan, "translate", "human"), plan
    assert covers("free", "transcribe", "ai") and not covers("free", "translate", "ai")        # the free trial stays transcription-only
    cost = billing.order_cost_usd(60, "translate", "pro", 150, review_tier="ai", market="intl")
    assert cost["total_usd"] == 0 and cost["free_minutes_applied"] == 60
    over = billing.order_cost_usd(200, "translate", "pro", 150, review_tier="ai", market="intl")
    assert over["free_minutes_applied"] == 150 and over["billable_minutes"] == 50 and over["total_usd"] > 0
