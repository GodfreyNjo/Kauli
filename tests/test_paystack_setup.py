"""Paystack plan setup: the plan resolver, the renewal mapping and the setup tool (no network, no real account)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from webapp import billing, paystack_setup  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for plan in ("PRO", "PREMIUM"):
        for suffix in ("", "_USD", "_KES"):
            monkeypatch.delenv(f"PAYSTACK_PLAN_{plan}{suffix}", raising=False)


def test_the_plan_that_matches_the_charge_currency_is_used(monkeypatch):
    monkeypatch.setenv("PAYSTACK_PLAN_PRO_USD", "PLN_usd")
    monkeypatch.setenv("PAYSTACK_PLAN_PRO_KES", "PLN_kes")
    assert billing.paystack_plan_code("pro", "USD") == "PLN_usd"
    assert billing.paystack_plan_code("pro", "KES") == "PLN_kes"
    assert billing.paystack_plan_code("premium", "USD") is None


def test_a_plan_is_never_offered_in_a_currency_it_was_not_made_for(monkeypatch):
    monkeypatch.setenv("PAYSTACK_PLAN_PRO_USD", "PLN_usd")
    assert billing.paystack_plan_code("pro", "KES") is None        # charged once in shillings instead of mismatching
    assert billing.paystack_plan_code("pro", "USD") == "PLN_usd"


def test_the_old_unsuffixed_setting_keeps_working(monkeypatch):
    monkeypatch.setenv("PAYSTACK_PLAN_PRO", " PLN_old ")
    assert billing.paystack_plan_code("pro") == "PLN_old" and billing.paystack_plan_code("pro", "USD") == "PLN_old"


def test_a_renewal_is_matched_to_the_plan_whichever_currency_it_was_bought_in(monkeypatch):
    monkeypatch.setenv("PAYSTACK_PLAN_PREMIUM_USD", "PLN_pu")
    monkeypatch.setenv("PAYSTACK_PLAN_PREMIUM_KES", "PLN_pk")
    monkeypatch.setenv("PAYSTACK_PLAN_PRO", "PLN_p")
    assert billing.paystack_plan_codes("premium") == {"PLN_pu", "PLN_pk"} and billing.paystack_plan_codes("pro") == {"PLN_p"}


class FakePaystack:
    def __init__(self, existing=None, reject=False):
        self.existing, self.posted, self.reject = existing or [], [], reject

    def get(self, url, headers=None, params=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer ") and url.endswith("/plan")
        return type("R", (), {"json": lambda s: {"status": True, "data": self.existing, "meta": {"pageCount": 1}}})()

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted.append(json)
        if self.reject:
            return type("R", (), {"json": lambda s: {"status": False, "message": "Currency not supported by merchant"}})()
        return type("R", (), {"json": lambda s, n=len(self.posted): {"status": True, "data": {"plan_code": f"PLN_new{n}"}}})()


def test_dry_run_creates_nothing_and_apply_creates_only_what_is_missing(monkeypatch):
    fake = FakePaystack(existing=[{"name": "Kauli Pro (monthly)", "currency": "USD", "interval": "monthly", "amount": 1900, "plan_code": "PLN_have"}])
    monkeypatch.setattr(paystack_setup.httpx, "get", fake.get)
    monkeypatch.setattr(paystack_setup.httpx, "post", fake.post)
    dry = paystack_setup.ensure_plans("sk_test_x", ["USD"], apply=False)
    assert fake.posted == [] and {(r["plan"], r["status"]) for r in dry} == {("pro", "exists"), ("premium", "would_create")}
    done = paystack_setup.ensure_plans("sk_test_x", ["USD"], apply=True)
    assert len(fake.posted) == 1
    body = fake.posted[0]
    assert body["name"] == "Kauli Premium (monthly)" and body["amount"] == billing.PLANS["premium"]["price_usd"] * 100
    assert body["interval"] == "monthly" and body["currency"] == "USD"
    out = paystack_setup.render(done)
    assert "PAYSTACK_PLAN_PRO_USD=PLN_have" in out and "PAYSTACK_PLAN_PREMIUM_USD=PLN_new1" in out and "sk_test_x" not in out


def test_a_different_price_is_reported_never_overwritten(monkeypatch):
    fake = FakePaystack(existing=[{"name": "Kauli Pro (monthly)", "currency": "USD", "interval": "monthly", "amount": 1500, "plan_code": "PLN_old"}])
    monkeypatch.setattr(paystack_setup.httpx, "get", fake.get)
    monkeypatch.setattr(paystack_setup.httpx, "post", fake.post)
    rows = paystack_setup.ensure_plans("sk_test_x", ["USD"], apply=True)
    pro = next(r for r in rows if r["plan"] == "pro")
    assert pro["status"] == "mismatch" and pro["found_amount_minor"] == 1500
    assert all(p["name"] != "Kauli Pro (monthly)" for p in fake.posted)
    assert "change it in the dashboard" in paystack_setup.render(rows)


def test_a_refused_currency_is_shown_as_an_error(monkeypatch):
    fake = FakePaystack(reject=True)
    monkeypatch.setattr(paystack_setup.httpx, "get", fake.get)
    monkeypatch.setattr(paystack_setup.httpx, "post", fake.post)
    rows = paystack_setup.ensure_plans("sk_test_x", ["USD"], apply=True)
    assert {r["status"] for r in rows} == {"error"} and "Currency not supported by merchant" in paystack_setup.render(rows)


def test_shilling_plans_are_priced_in_whole_ten_shilling_steps(monkeypatch):
    monkeypatch.setattr(billing, "usd_to_kes", lambda usd: (usd * 129.37, "test"))
    assert paystack_setup.plan_amount_minor("pro", "KES") == 246000      # 19 x 129.37 = 2458.03 -> KES 2,460.00
    with pytest.raises(ValueError):
        paystack_setup.plan_amount_minor("pro", "EUR")
