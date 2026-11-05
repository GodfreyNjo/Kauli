"""The messages after a sale: who they come from, who gets them, that nobody gets one twice, and the Trustpilot review requests."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import lifecycle, mailer, worker  # noqa: E402

DAY = 86400


def _mail(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "email_configured", lambda: True)
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, html, text=None, **k: sent.append({"to": to, "subject": subject, "html": html, "text": text or "", **k}) or (True, "id"))
    return sent


def _deliver(env, order_id, days_ago, created_days_ago=None, notes=None):
    now = time.time()
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'ready_for_delivery', status_changed_at = ?, updated_at = ?, created_at = ?, instr_notes = COALESCE(?, instr_notes) WHERE id = ?",
                 (now - days_ago * DAY, now - days_ago * DAY, now - (created_days_ago if created_days_ago is not None else days_ago + 1) * DAY, notes, order_id))
    conn.commit()
    conn.close()
    return env.db.get_order(order_id)


def _order(env, wav_bytes):
    return env.order_from(env.place(wav_bytes))


def test_each_kind_of_email_comes_from_the_person_a_customer_would_expect(env, monkeypatch):
    sent = _mail(monkeypatch)
    assert mailer.sender("orders") == "Kauli Orders" and mailer.sender("billing") == "Kauli Billing" and mailer.sender("support") == "Kauli Customer Support"
    assert mailer.sender("success") == "Kauli Customer Success" and mailer.sender("nonsense") == "Kauli Operations"
    mailer.send_as("billing", "a@example.com", "Your Kauli receipt", "<p>x</p>", "x")
    mailer.send_as("support", "a@example.com", "Reply", "<p>x</p>", "x", sender_name="Someone Else")                   # an explicit name still wins
    assert [s["sender_name"] for s in sent] == ["Kauli Billing", "Someone Else"]
    order = env.db.get_order(env.order_from(env.place(b"RIFF" + bytes(60))) ["id"]) if False else None
    client = env.db.get_user("u1")
    monkeypatch.setattr(worker.db, "create_notification", lambda *a, **k: None)
    fake = {"id": "o1", "client_id": "u1", "original_filename": "Call.mp3", "status": "ready_for_delivery"}
    worker.notify_client_order_ready(fake, base_url="https://kauli.test")
    assert sent[-1]["to"] == client["email"] and sent[-1]["sender_name"] == "Kauli Orders" and sent[-1]["subject"] == "Your Kauli order is ready"


def test_a_customer_is_told_when_work_on_their_order_starts_once_and_only_for_their_own_orders(env, wav_bytes, monkeypatch):
    sent = _mail(monkeypatch)
    order = _order(env, wav_bytes)
    assert lifecycle.order_received(order) is True and len(sent) == 1
    assert sent[0]["sender_name"] == "Kauli Orders" and sent[0]["subject"].startswith("We have your order") and f"/client/orders/{order['id']}" in sent[0]["html"]
    assert "Stop these emails" not in sent[0]["html"]                                          # a confirmation is not marketing
    assert lifecycle.order_received(order) is False and len(sent) == 1                         # never twice
    other = _order(env, wav_bytes)
    _deliver(env, other["id"], 1, notes="Recorded by Kauli in an online meeting.")
    assert lifecycle.order_received(env.db.get_order(other["id"])) is False                    # an order Kauli placed itself
    env.db.set_flag("lifecycle_confirm", "0")
    third = _order(env, wav_bytes)
    assert lifecycle.order_received(third) is False and len(sent) == 1                         # switched off by staff


def test_review_requests_go_by_timing_alone_to_everyone_delivered_to_and_never_twice(env, wav_bytes, monkeypatch):
    sent = _mail(monkeypatch)
    fresh, ripe, old = _order(env, wav_bytes), _order(env, wav_bytes), _order(env, wav_bytes)
    _deliver(env, fresh["id"], 0.5)
    _deliver(env, ripe["id"], 3)
    _deliver(env, old["id"], 40)
    assert lifecycle.review_candidates() == []                                                  # their latest delivery is half a day old: too soon, whatever came before
    _deliver(env, fresh["id"], 3)                                                               # now the latest is three days old
    due = lifecycle.review_candidates()
    assert [c["order"]["id"] for c in due] == [fresh["id"]]
    u = env.db.get_user("u1")
    assert lifecycle.send_review_request(u, fresh) is True
    mail = sent[-1]
    assert mail["sender_name"] == "Kauli Customer Success" and mail["subject"] == "How did we do?"
    assert "https://www.trustpilot.com/evaluate/kauli-forgemedia.com" in mail["html"] and "Review Kauli on Trustpilot" in mail["html"]
    assert "/help" in mail["html"] and "tell us first" in mail["html"] and "Stop these emails" in mail["html"]          # a private way to say what went wrong, for everyone
    assert lifecycle.send_review_request(u, ripe) is False and len(sent) == 1                   # one request per customer in 90 days
    assert lifecycle.review_candidates() == []


def test_someone_who_unsubscribed_gets_no_review_request_reminder_or_regulars_email(env, wav_bytes, monkeypatch):
    _mail(monkeypatch)
    order = _order(env, wav_bytes)
    _deliver(env, order["id"], 3, created_days_ago=60)
    assert lifecycle.review_candidates() and lifecycle.rebook_candidates()
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET marketing_consent = 0, marketing_consent_at = ? WHERE id = 'u1'", (time.time(),))
    conn.commit()
    conn.close()
    assert lifecycle.review_candidates() == [] and lifecycle.rebook_candidates() == [] and lifecycle.vip_candidates() == []
    assert lifecycle.send_review_request(env.db.get_user("u1"), order) is False


def test_the_come_back_reminder_follows_the_customers_own_rhythm(env, wav_bytes, monkeypatch):
    assert lifecycle.rebook_gap_days([0]) == 30 and lifecycle.rebook_gap_days([]) == 30                       # one order: a month
    assert lifecycle.rebook_gap_days([0, 7 * DAY, 14 * DAY]) == 14                                             # weekly customers: not sooner than two weeks
    assert lifecycle.rebook_gap_days([0, 40 * DAY, 80 * DAY, 120 * DAY]) == 40 and lifecycle.rebook_gap_days([0, 400 * DAY]) == 90
    sent = _mail(monkeypatch)
    a, b = _order(env, wav_bytes), _order(env, wav_bytes)
    _deliver(env, a["id"], 50, created_days_ago=80)
    _deliver(env, b["id"], 20, created_days_ago=30)                                                            # every 50 days; last one 30 days ago: not due yet
    assert lifecycle.rebook_candidates() == []
    _deliver(env, b["id"], 3, created_days_ago=55)                                                             # 55 days since the last order, usual gap 25: due
    due = lifecycle.rebook_candidates()
    assert len(due) == 1 and due[0]["gap"] == 25 and lifecycle.send_rebook(due[0]) is True
    assert sent[-1]["sender_name"] == "Kauli Customer Success" and sent[-1]["subject"] == "Got another recording?" and "%" not in sent[-1]["text"]
    assert lifecycle.rebook_candidates() == []                                                                  # not again for a while


def test_regulars_are_thanked_with_attention_and_never_with_a_discount(env, wav_bytes, monkeypatch):
    sent = _mail(monkeypatch)
    for i in range(5):
        _deliver(env, _order(env, wav_bytes)["id"], 10 + i, created_days_ago=11 + i * 3)
    due = lifecycle.vip_candidates()
    assert len(due) == 1 and due[0]["orders"] == 5
    assert lifecycle.send_vip(due[0]) is True
    body = sent[-1]["text"].lower()
    assert sent[-1]["subject"] == "You are one of our regulars" and "read first" in body and "first" in body
    assert not any(w in body for w in ("discount", "% off", "coupon", "promo", "voucher", "free month"))
    assert lifecycle.vip_candidates() == [] and [v["id"] for v in lifecycle.vip_members()] == ["u1"] and lifecycle.send_vip(due[0]) is False


def test_the_automations_wait_for_staff_and_then_send_each_message_once(env, wav_bytes, monkeypatch):
    sent = _mail(monkeypatch)
    order = _order(env, wav_bytes)
    _deliver(env, order["id"], 3, created_days_ago=60)
    assert lifecycle.enabled("confirm") is True and not any(lifecycle.enabled(k) for k in ("review", "rebook", "vip"))
    assert lifecycle.sweep_once() == {"review": 0, "rebook": 0, "vip": 0} and sent == []                       # nothing goes out until staff switch it on
    env.db.set_flag("lifecycle_review", "1")
    env.db.set_flag("lifecycle_rebook", "1")
    assert lifecycle.sweep_once() == {"review": 1, "rebook": 1, "vip": 0}
    assert sorted(m["subject"] for m in sent) == ["Got another recording?", "How did we do?"]
    assert lifecycle.sweep_once() == {"review": 0, "rebook": 0, "vip": 0} and len(sent) == 2


def test_staff_run_the_lifecycle_emails_from_one_page_and_the_trustpilot_link_must_be_trustpilots(env, wav_bytes, monkeypatch):
    sent = _mail(monkeypatch)
    order = _order(env, wav_bytes)
    _deliver(env, order["id"], 3, created_days_ago=60)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/lifecycle").text
    assert "Lifecycle emails" in page and "Trustpilot's rule" in page and "Kauli Customer Support" in page and "Would get a request on the next run (1)" in page
    assert 'href="/staff/lifecycle"' in page
    assert "error=" in env.client.post("/staff/lifecycle/trustpilot", data={"url": "https://evil.example.com/evaluate/kauli"}, follow_redirects=False).headers["location"]
    assert "notice=" in env.client.post("/staff/lifecycle/trustpilot", data={"url": "https://www.trustpilot.com/evaluate/example.org"}, follow_redirects=False).headers["location"]
    assert lifecycle.trustpilot_url() == "https://www.trustpilot.com/evaluate/example.org"
    env.client.post("/staff/lifecycle/test", data={"kind": "review"})
    assert sent[-1]["to"] == "staff@example.com" and "https://www.trustpilot.com/evaluate/example.org" in sent[-1]["html"]
    env.client.post("/staff/lifecycle/switch", data={"key": "review", "on": "1"})
    assert lifecycle.enabled("review") is True and "error=" in env.client.post("/staff/lifecycle/switch", data={"key": "bogus", "on": "1"}, follow_redirects=False).headers["location"]
    assert "error=" in env.client.post("/staff/lifecycle/review-selected", data={}, follow_redirects=False).headers["location"]
    r = env.client.post("/staff/lifecycle/review-selected", data={"customer": ["u1", "nobody"]}, follow_redirects=False)
    assert "Sent+1" in r.headers["location"] or "Sent%201" in r.headers["location"]
    assert sent[-1]["to"] == env.db.get_user("u1")["email"] and "/staff/lifecycle" in r.headers["location"]
    assert env.client.post("/staff/lifecycle/switch", data={"key": "review", "on": "0"}).status_code in (200, 303)
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert env.client.get("/staff/lifecycle", follow_redirects=False).status_code in (302, 303, 307)            # staff only
