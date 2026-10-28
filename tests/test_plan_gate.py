"""Downloads wait for a chosen plan: web, billing page, payments and the Free
plan's card confirmation, against a throwaway database."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401  (fixtures)


def _unchoose(env):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL WHERE id = 'u1'")
    conn.commit()


def _ready_order(env, wav, **kw):
    order = env.order_from(env.place(wav, **kw))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    outdir = Path(order["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "transcript_en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    return env.db.get_order(order["id"])


def test_no_download_until_a_plan_is_chosen_then_free_minute_results_download(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    assert order["is_free_preview"] == 1
    _unchoose(env)
    r = env.client.get(f"/client/orders/{order['id']}/download/transcript")
    assert r.status_code == 303 and r.headers["location"] == "/client/billing?choose_plan=1"
    env.db.set_plan_chosen("u1")
    r = env.client.get(f"/client/orders/{order['id']}/download/transcript")
    assert r.status_code == 200 and "Hello." in r.text            # a free-minutes order, now downloadable


def test_paid_orders_are_gated_the_same_way(env, wav_bytes):
    order = _ready_order(env, wav_bytes, review_tier="human")
    assert order["is_free_preview"] == 0
    _unchoose(env)
    assert env.client.get(f"/client/orders/{order['id']}/download/transcript").status_code == 303


def test_order_page_and_banner_tell_the_user_what_to_do(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    _unchoose(env)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "Choose a plan to download" in page and "download/docx" not in page
    assert "plan-banner" in page                                   # the global notice
    assert "plan-banner" not in env.client.get("/client/billing").text
    env.db.set_plan_chosen("u1")
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "download/docx" in page and "plan-banner" not in page


def test_files_page_lists_nothing_until_a_plan_is_chosen(env, wav_bytes):
    _ready_order(env, wav_bytes, review_tier="human")
    _unchoose(env)
    assert "download/" not in env.client.get("/client/files").text
    env.db.set_plan_chosen("u1")
    assert "download/" in env.client.get("/client/files").text


def test_billing_page_offers_the_free_plan_until_it_is_chosen(env):
    _unchoose(env)
    page = env.client.get("/client/billing?choose_plan=1").text
    assert "Choose Pay as you go" in page and "Choose a plan to download your files" in page
    env.db.set_plan_chosen("u1")
    page = env.client.get("/client/billing").text
    assert "Choose Free plan" not in page and "Active" in page


def test_choosing_free_with_a_verified_card_just_records_it(env):
    _unchoose(env)
    r = env.client.post("/client/billing/choose-free")
    assert r.status_code == 303 and "Downloads+are+unlocked" in r.headers["location"]
    assert env.db.get_user("u1")["plan_chosen_at"]


def test_choosing_free_without_a_verified_card_goes_to_the_credited_one_dollar_step(env):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    r = env.client.post("/client/billing/choose-free")
    assert r.status_code == 303 and r.headers["location"] == "/client/verify-trial"
    assert not env.db.get_user("u1")["plan_chosen_at"]
    page = env.client.get("/client/verify-trial").text
    assert "Choose the Free plan" in page and "$1.00" in page and "credited" in page


def test_the_card_confirmation_payment_unlocks_downloads_and_credits_the_dollar(env):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    pid = env.app.billing.new_reference()
    env.db.create_payment(pid, "u1", "free", 1.0, None, "USD", "paystack", order_id=None,
                          meta='{"kind": "trial_verification"}')
    assert env.app._activate_payment(env.db.get_payment(pid), "ref-verify")
    user = env.db.get_user("u1")
    assert user["plan_chosen_at"] and user["trial_verified_at"]
    assert env.db.wallet_credits_remaining("u1") > 0                # the $1 is credited, not a fee


def test_any_real_payment_counts_as_choosing_a_plan(env):
    _unchoose(env)
    pid = env.app.billing.new_reference()
    env.db.create_payment(pid, "u1", "free", 5.0, None, "USD", "paystack", order_id=None,
                          meta='{"kind": "credits_topup:50"}')
    assert env.app._activate_payment(env.db.get_payment(pid), "ref-topup")
    assert env.db.get_user("u1")["plan_chosen_at"]


def test_ordering_with_free_minutes_no_longer_waits_for_a_card_check(env, wav_bytes):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL, trial_verified_at = NULL WHERE id = 'u1'")
    conn.commit()
    r = env.place(wav_bytes)
    assert r.status_code == 303 and "/client/orders/" in r.headers["location"]
    assert "verify-trial" not in r.headers["location"]
    order = env.order_from(r)
    assert order["is_free_preview"] == 1 and env.submitted == [order["id"]]


def test_the_migration_grandfathers_existing_accounts_only(env):
    # a brand-new account has not chosen; accounts present at migration time already have
    env.db.get_or_create_user("new1", "new@example.com", "client")
    assert not env.db.get_user("new1")["plan_chosen_at"]
    env.db.set_plan_chosen("new1")
    first = env.db.get_user("new1")["plan_chosen_at"]
    env.db.set_plan_chosen("new1")                                  # idempotent
    assert env.db.get_user("new1")["plan_chosen_at"] == first


# ------------------------------------------------------------ transcript teaser
from types import SimpleNamespace  # noqa: E402

_SENTENCES = ["The harvest started early this year.", "Farmers gathered near the river at dawn.",
              "Zebra herds crossed the valley slowly.", "Nobody expected such abundant rainfall.",
              "Quetzal feathers decorated the market stalls."]


def _fake_job(sentences=_SENTENCES):
    return SimpleNamespace(segments=[SimpleNamespace(segment_type="speech", source_final_text=s) for s in sentences]
                           + [SimpleNamespace(segment_type="music", source_final_text="")])


def test_teaser_shows_the_first_part_and_scrambles_the_rest(env):
    out = env.app._transcript_teaser(_fake_job(), "abc123")
    real = " ".join(out["visible"])
    assert real.startswith("The harvest") and "Quetzal" not in real and "Zebra" not in real
    hidden = " ".join(out["hidden"])
    for word in ("Zebra", "Quetzal", "abundant", "rainfall", "market"):
        assert word.lower() not in hidden.lower()                    # nothing real survives in what is sent
    assert len(out["hidden"]) >= 2 and 40 <= out["hidden_pct"] <= 80
    # same shape as the text it stands in for, and stable between page loads
    assert [len(p) for p in out["hidden"]] == [len(p) for p in _SENTENCES[-len(out["hidden"]):]]
    assert out == env.app._transcript_teaser(_fake_job(), "abc123")
    assert out["hidden"] != env.app._transcript_teaser(_fake_job(), "other")["hidden"]


def test_teaser_splits_a_single_long_segment_on_a_word_boundary(env):
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa"
    out = env.app._transcript_teaser(_fake_job([text]), "x")
    assert out["visible"] and out["hidden"]
    assert text.startswith(out["visible"][0]) and "papa" not in " ".join(out["hidden"])
    assert out["visible"][0].split()[-1] in text.split()               # no half-words


def test_teaser_with_nothing_to_show(env):
    assert env.app._transcript_teaser(None, "x") == {"visible": [], "hidden": [], "hidden_pct": 0}


def test_order_page_never_sends_the_hidden_text_before_a_plan_is_chosen(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: _fake_job())
    _unchoose(env)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "The harvest started early" in page and "transcript-teaser" in page
    assert "Quetzal" not in page and "Zebra herds" not in page and "abundant" not in page   # not in the HTML at all
    assert "is hidden" in page and "Choose the Free plan" in page


def test_the_ai_tools_wait_for_a_plan_too(env, wav_bytes):
    order = _ready_order(env, wav_bytes)
    _unchoose(env)
    r = env.client.post(f"/client/orders/{order['id']}/ai-summary")
    assert r.status_code == 403 and "Choose a plan" in r.json()["error"]
    r = env.client.post(f"/client/orders/{order['id']}/ai-ask", json={"question": "what happened?"})
    assert r.status_code == 403
