"""Create (or find) Kauli's monthly and yearly plans on Paystack and print the environment lines that switch them on.

    python -m webapp.paystack_setup                      # dry run: shows what exists and what would be created
    python -m webapp.paystack_setup --apply              # creates only the plans that are missing
    python -m webapp.paystack_setup --currency USD KES   # which currencies to set plans up in (default: USD)

It uses the same key slots as the rest of billing (PAYSTACK_LIVE_SECRET_KEY when PAYSTACK_LIVE_MODE is on, otherwise
PAYSTACK_SECRET_KEY), never changes or deletes an existing plan, and never prints the key. Paystack gives every plan a
single currency and interval, so Kauli keeps one plan per plan, currency and interval and picks the one that matches how
the client is charged: PAYSTACK_PLAN_PRO_USD (monthly) and PAYSTACK_PLAN_PRO_YEARLY_USD (yearly), ..._KES for shillings.
"""
from __future__ import annotations

import argparse

import httpx

from . import billing

SUBSCRIPTION_PLANS = ("pro", "premium")      # Enterprise is arranged by hand, never self-serve
INTERVALS = ("monthly", "annually")


def plan_name(plan: str, interval: str = "monthly") -> str:
    return f"Kauli {billing.PLANS[plan]['name']} ({'yearly' if interval == 'annually' else 'monthly'})"


def plan_amount_minor(plan: str, currency: str, interval: str = "monthly") -> int:
    """The plan price in the currency's smallest unit. Dollars are exact; shillings are converted once, now, and the
    plan keeps that price until it is replaced (a Paystack plan has a fixed amount)."""
    usd = billing.PLANS[plan]["price_usd_year" if interval == "annually" else "price_usd"]
    if currency == "USD":
        return int(round(usd * 100))
    if currency == "KES":
        kes, _source = billing.usd_to_kes(usd)
        return int(round(round(kes / 10.0) * 10 * 100))      # whole 10-shilling steps look like a real price
    raise ValueError(f"unsupported plan currency: {currency}")


def env_name(plan: str, currency: str, interval: str = "monthly") -> str:
    return f"PAYSTACK_PLAN_{plan.upper()}_{'YEARLY_' if interval == 'annually' else ''}{currency}"


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def list_plans(key: str) -> list[dict]:
    plans, page = [], 1
    while True:
        r = httpx.get("https://api.paystack.co/plan", headers=_headers(key), params={"perPage": 100, "page": page}, timeout=20)
        body = r.json()
        if not body.get("status"):
            raise RuntimeError(body.get("message", "Paystack refused to list plans."))
        plans += body.get("data") or []
        meta = body.get("meta") or {}
        if page >= int(meta.get("pageCount") or 1):
            return plans
        page += 1


def create_plan(key: str, plan: str, currency: str, interval: str = "monthly") -> dict:
    body = {
        "name": plan_name(plan, interval), "interval": interval, "amount": plan_amount_minor(plan, currency, interval), "currency": currency,
        "description": f"Kauli {billing.PLANS[plan]['name']} plan: {billing.PLANS[plan]['blurb']}"[:250],
        "send_invoices": True, "send_sms": False,
    }
    r = httpx.post("https://api.paystack.co/plan", headers=_headers(key), json=body, timeout=20)
    data = r.json()
    if not data.get("status"):
        return {"error": data.get("message", "Paystack rejected the plan.")}
    return {"plan_code": data["data"]["plan_code"]}


def ensure_plans(key: str, currencies: list[str], apply: bool) -> list[dict]:
    """One result per (plan, interval, currency): exists / created / would_create / mismatch / error, with the plan code when known."""
    existing = list_plans(key)
    results = []
    for plan in SUBSCRIPTION_PLANS:
        for interval in INTERVALS:
            for currency in currencies:
                name, amount = plan_name(plan, interval), plan_amount_minor(plan, currency, interval)
                same_name = [p for p in existing if (p.get("name") or "").strip() == name and (p.get("currency") or "").upper() == currency
                             and p.get("interval") == interval and not p.get("is_archived")]
                row = {"plan": plan, "interval": interval, "currency": currency, "env": env_name(plan, currency, interval), "amount_minor": amount}
                if same_name:
                    match = next((p for p in same_name if int(p.get("amount") or 0) == amount), None)
                    if match:
                        row.update(status="exists", plan_code=match["plan_code"])
                    else:
                        # Never edit a live plan behind someone's back: say what is different and let a person decide.
                        row.update(status="mismatch", plan_code=same_name[0]["plan_code"], found_amount_minor=int(same_name[0].get("amount") or 0))
                elif apply:
                    made = create_plan(key, plan, currency, interval)
                    row.update(status="error", error=made["error"]) if "error" in made else row.update(status="created", plan_code=made["plan_code"])
                else:
                    row.update(status="would_create")
                results.append(row)
    return results


def render(results: list[dict]) -> str:
    lines = []
    for r in results:
        major = r["amount_minor"] / 100
        head = f"{r['plan']:<8} {r.get('interval', 'monthly'):<9} {r['currency']}  {major:>10,.2f}  {r['status']}"
        if r["status"] == "mismatch":
            head += f"  (Paystack has {r['found_amount_minor'] / 100:,.2f}; change it in the dashboard or archive it and re-run)"
        if r["status"] == "error":
            head += f"  - {r['error']}"
        lines.append(head)
    env = [f"{r['env']}={r['plan_code']}" for r in results if r["status"] in ("exists", "created", "mismatch") and r.get("plan_code")]
    if env:
        lines += ["", "Add these to the server's .env (then redeploy):"] + env
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="create the missing plans (default is a dry run)")
    ap.add_argument("--currency", nargs="+", default=["USD"], choices=["USD", "KES"])
    args = ap.parse_args()
    key = billing._paystack_key()
    if not key:
        raise SystemExit("No Paystack secret key is set (PAYSTACK_SECRET_KEY, or PAYSTACK_LIVE_SECRET_KEY with PAYSTACK_LIVE_MODE=true).")
    mode = "LIVE" if billing._paystack_live_mode() else "test"
    print(f"Paystack {mode} account - {'creating missing plans' if args.apply else 'dry run, nothing will be created'}")
    print(render(ensure_plans(key, args.currency, args.apply)))


if __name__ == "__main__":
    main()
