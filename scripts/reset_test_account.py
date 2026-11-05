"""Make one customer account brand new again, for testing sign-up, the free trial and payments.

    python scripts/reset_test_account.py someone@example.com            # shows what would be removed, changes nothing
    python scripts/reset_test_account.py someone@example.com --yes      # does it (after backing the database up)

On the live server (the script is piped in, nothing to install):
    Get-Content -Raw scripts\\reset_test_account.py | ssh -i ~/.ssh/kauli_deploy root@$env:KAULI_SSH_HOST "docker exec -i kauli python3 - someone@example.com --yes"

Kept: the sign-in itself (same email and id, so the same Google or password login works), display name, picture, language and two-step
sign-in. Removed: orders and everything attached to them, folders, notes, vocabulary, integrations, API keys, webhooks, rewards,
referrals, support threads. Reset: plan back to Free, minutes used, credit balance, free-trial and sign-up progress. The pricing region is
set to US dollars so card payments are charged in USD.

Payment and receipt records are KEPT unless you add --include-payments (they are financial records). Staff and admin accounts are refused.
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "/app/webapp/data/kauli_demo.db"
# never touched: the sign-in row itself (reset separately), financial records, audit trails and sign-in security
KEEP = {"users", "payments", "receipts", "security_events", "data_deletion_requests", "mfa_backup_codes", "webhook_deliveries"}
FINANCIAL = {"payments", "receipts"}


def tables(conn):
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        out[name] = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--db", default=DEFAULT_DB if Path(DEFAULT_DB).exists() else str(Path(__file__).resolve().parent.parent / "webapp" / "data" / "kauli_demo.db"))
    ap.add_argument("--yes", action="store_true", help="actually do it")
    ap.add_argument("--include-payments", action="store_true", help="also remove this account's payment and receipt records")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (a.email.strip(),)).fetchone()
    if not user:
        print(f"No account with the email {a.email}.")
        return 1
    if user["role"] != "client" or user["is_admin"]:
        print("Refusing: this is a staff or admin account.")
        return 1
    uid = user["id"]
    schema = tables(conn)
    order_ids = [r[0] for r in conn.execute("SELECT id FROM orders WHERE client_id = ?", (uid,))]
    skip = KEEP - (FINANCIAL if a.include_payments else set())
    plan = []                                           # (table, where, params)
    marks = ",".join("?" * len(order_ids))
    for t, cols in schema.items():
        if t in skip or t == "orders":
            continue
        if t == "subscriptions":
            continue
        if order_ids and "order_id" in cols:
            plan.append((t, f'"order_id" IN ({marks})', order_ids))
        for col in ("client_id", "user_id"):
            if col in cols:
                plan.append((t, f'"{col}" = ?', [uid]))
    plan.append(("orders", '"client_id" = ?', [uid]))

    print(f"Account: {user['email']}  (id {uid})  orders: {len(order_ids)}")
    todo = []
    for t, where, params in plan:
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {where}', params).fetchone()[0]
        if n:
            todo.append((t, where, params, n))
    for t, _w, _p, n in todo:
        print(f"  remove {n:>5} row(s) from {t}")
    print("  reset  subscription (Free plan, 0 minutes used, 0 credits) and sign-up progress; pricing region -> US dollars")
    print("  kept   payments and receipts" if not a.include_payments else "  remove payments and receipts too")
    if not a.yes:
        print("\nDry run only. Add --yes to do it.")
        return 0

    backup = Path(a.db).with_name(f"backup-before-reset-{time.strftime('%Y%m%d-%H%M%S')}.db")
    dst = sqlite3.connect(str(backup))
    conn.backup(dst)
    dst.close()
    if sqlite3.connect(str(backup)).execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        print("Backup failed its integrity check; nothing was changed.")
        return 1
    print(f"Backed up to {backup}")

    now = time.time()
    with conn:
        for t, where, params, _n in todo:
            conn.execute(f'DELETE FROM "{t}" WHERE {where}', params)
        sets = {"minutes_used_this_period": 0, "period_started_at": now, "plan": "free", "status": "active", "current_period_end": None,
                "bonus_minutes": 0, "bonus_note": None, "wallet_credits": 0, "reward_minutes": 0}
        have = {k: v for k, v in sets.items() if k in schema.get("subscriptions", [])}
        if have:
            conn.execute("UPDATE subscriptions SET " + ", ".join(f'"{k}" = ?' for k in have) + " WHERE user_id = ?", [*have.values(), uid])
        resets = {"onboarding_status": "new", "account_status": "active", "trial_verified_at": None, "plan_chosen_at": None,
                  "onboarding_tour_seen_at": None, "trusted_submitter_until": None, "staff_notes": None, "referred_by": None,
                  "referred_by_blog_post_id": None, "default_source_lang": None, "default_target_lang": None, "default_service_level": None,
                  "default_rush": 0, "default_addon_video": 0, "billing_market": "intl", "created_at": now, "last_seen_at": None}
        have = {k: v for k, v in resets.items() if k in schema["users"]}
        conn.execute("UPDATE users SET " + ", ".join(f'"{k}" = ?' for k in have) + " WHERE id = ?", [*have.values(), uid])
    print("Done. The account is new again: sign in as usual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
