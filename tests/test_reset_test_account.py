"""scripts/reset_test_account.py makes a customer account new again without touching anyone else or the financial records."""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

SCRIPT = Path(__file__).parent.parent / "scripts" / "reset_test_account.py"


def _run(env, *args):
    spec = importlib.util.spec_from_file_location("reset_test_account", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    old = sys.argv
    sys.argv = ["reset_test_account.py", *args, "--db", str(env.db.DB_PATH)]
    try:
        return mod.main()
    finally:
        sys.argv = old


def _count(env, sql, *p):
    conn = env.db.get_conn()
    n = conn.execute(sql, p).fetchone()[0]
    conn.close()
    return n


def test_a_dry_run_changes_nothing_and_a_real_run_makes_the_account_new(env, wav_bytes, capsys):
    order = env.order_from(env.place(wav_bytes))
    env.db.add_wallet_credits("u1", 40)
    env.db.set_trial_verified("u1")
    env.db.create_payment("p1", "u1", "pro", 6.72, None, "USD", "paystack", order_id=order["id"])
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET plan = 'pro', minutes_used_this_period = 12 WHERE user_id = 'u1'")
    conn.commit()
    conn.close()

    assert _run(env, "client@example.com") == 0
    assert "Dry run only" in capsys.readouterr().out
    assert _count(env, "SELECT COUNT(*) FROM orders WHERE client_id = 'u1'") == 1

    assert _run(env, "client@example.com", "--yes") == 0
    assert "Done. The account is new again" in capsys.readouterr().out
    assert _count(env, "SELECT COUNT(*) FROM orders WHERE client_id = 'u1'") == 0
    sub = env.db.get_subscription("u1")
    assert sub["plan"] == "free" and sub["minutes_used_this_period"] == 0 and sub["wallet_credits"] == 0
    user = env.db.get_user("u1")
    assert user["trial_verified_at"] is None and user["onboarding_status"] == "new" and user["billing_market"] == "intl"
    assert _count(env, "SELECT COUNT(*) FROM payments WHERE id = 'p1'") == 1                  # financial records are kept
    assert list(Path(env.db.DB_PATH).parent.glob("backup-before-reset-*.db"))                 # and a backup was taken first

    assert env.client.get("/account").status_code == 200                                       # the account works as a new one
    assert env.order_from(env.place(wav_bytes))["client_id"] == "u1"


def test_it_only_touches_the_named_account_and_refuses_staff(env, wav_bytes, capsys):
    env.db.get_or_create_user("u2", "other@example.com", "client")
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.order_from(env.place(wav_bytes))
    assert _run(env, "other@example.com", "--yes") == 0
    assert _count(env, "SELECT COUNT(*) FROM orders WHERE client_id = 'u1'") == 1             # u1 untouched
    assert _run(env, "staff@example.com", "--yes") == 1 and "staff or admin" in capsys.readouterr().out
    assert _run(env, "nobody@example.com", "--yes") == 1
