"""The sign-up analysis behind the Clients page's "Analyse sign-ups" button: who is stuck and why, the ready-to-send email written for each
client, and the send itself (staff only, escaped, recorded, never twice in a day)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401
from webapp import funnel_audit  # noqa: E402


def _client(env, uid, email, name, **cols):
    env.db.get_or_create_user(uid, email, "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET display_name = ?, created_at = ? WHERE id = ?", (name, time.time() - 20 * 86400, uid))
    for k, v in cols.items():
        conn.execute(f"UPDATE users SET {k} = ? WHERE id = ?", (v, uid))
    conn.commit()
    conn.close()


def _upload(env, uid):
    conn = env.db.get_conn()
    conn.execute("INSERT INTO upload_audit_log (user_id, original_filename, stored_filename, sha256, size_bytes, rejected, created_at) VALUES (?, 'a.mp3', 'x', 'h', 1000, 0, ?)",
                 (uid, time.time() - 5 * 86400))
    conn.commit()
    conn.close()


def _order(env, uid, oid, status, cost, minutes=10.0, name="talk.mp4"):
    conn = env.db.get_conn()
    conn.execute("INSERT INTO orders (id, client_id, original_filename, audio_path, source_lang, target_lang, tier, asr, mt, tts, outdir, status, created_at, updated_at, service_level, duration_minutes, cost_usd) "
                 "VALUES (?, ?, ?, 'p', 'en', 'en', 'ai', 'x', 'x', 'x', 'o', ?, ?, ?, 'transcribe', ?, ?)", (oid, uid, name, status, time.time() - 2 * 86400, time.time(), minutes, cost))
    conn.commit()
    conn.close()


def _by(result, uid):
    return next((d for d in result["stuck"] if d["id"] == uid), None)


def test_each_kind_of_stuck_client_is_told_apart_and_gets_a_draft_written_from_what_they_did(env):
    _client(env, "lost", "lost@example.com", "Amina Otieno")
    _upload(env, "lost")                                                              # uploaded, but no order was ever created
    _client(env, "pay", "pay@example.com", "Brian Kamau")
    _order(env, "pay", "o1", "pending_payment", 19.41, 191.0, "town hall.mp4")        # placed an order and stopped at the payment
    _client(env, "idle", "idle@example.com", "Chidi Eze")                             # never did anything
    r = funnel_audit.analyse("https://kauli.example")
    lost, pay, idle = _by(r, "lost"), _by(r, "pay"), _by(r, "idle")
    assert lost["code"] == "lost_upload" and lost["severity"] == "high"
    assert "Hi Amina," in lost["body"] and "can't see an order" in lost["body"] and lost["body"].rstrip().endswith("Kauli Team")
    assert pay["code"] == "waiting_to_pay" and "$19.41" in pay["body"] and "town hall.mp4" in pay["body"] and "https://kauli.example/client/orders/o1" in pay["body"]
    assert "discount" not in pay["body"].lower() and "M-Pesa" in pay["body"]
    assert idle["code"] == "never_started" and "Hi Chidi," in idle["body"] and "free" in idle["body"]
    assert {"lost_upload", "waiting_to_pay", "never_started"} <= {d["code"] for d in r["stuck"]}
    assert {"lost", "pay", "idle"} <= set(r["drafts"]) and r["drafts"]["pay"]["subject"]
    assert [s["priority"] for s in r["suggestions"]][0] == "high"                    # the most urgent things come first


def test_paying_internal_and_staff_accounts_are_not_counted_as_stuck(env):
    _client(env, "payer", "payer@example.com", "Paying Pat")
    conn = env.db.get_conn()
    conn.execute("INSERT INTO payments (id, user_id, plan, amount_usd, currency, provider, status, created_at) VALUES ('p1', 'payer', 'pro', 19, 'USD', 'bank', 'completed', ?)", (time.time(),))
    conn.commit()
    conn.close()
    _client(env, "tester", "tester@example.com", "Test Account", is_internal=1)
    r = funnel_audit.analyse()
    assert _by(r, "payer") is None and _by(r, "tester") is None
    labels = {f["key"]: f["count"] for f in r["funnel"]}
    assert labels["signed_up"] >= 2 and labels["paid"] == 1


def test_only_staff_can_open_the_analysis_and_it_changes_nothing(env):
    _client(env, "idle", "idle@example.com", "Chidi Eze")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert env.client.get("/staff/clients/analysis").status_code in (302, 303, 307)
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u1")), "role": "staff"})
    html = env.client.get("/staff/clients/analysis?partial=1").text
    assert "Sign-up analysis" in html and "Review &amp; send email" in html and 'id="fa-drafts"' in html
    full = env.client.get("/staff/clients/analysis").text
    assert "Back to Clients" in full
    assert env.client.get("/staff/clients").text.count("Analyse sign-ups") >= 1


def test_sending_the_follow_up_is_staff_only_escaped_recorded_and_never_done_twice_in_a_day(env):
    _client(env, "idle", "idle@example.com", "Chidi Eze")
    sent = []
    env.monkeypatch.setattr(env.app.mailer, "email_configured", lambda: True)
    env.monkeypatch.setattr(env.app.mailer, "send_as", lambda purpose, to, subject, html, text=None, **kw: (sent.append((to, subject, html, text)) or (True, "msg-1")))
    form = {"subject": "Your free minutes on Kauli", "body": "Hi Chidi,\n\nStart here: https://kauli.example/client <script>x</script>\n\nKauli Team"}
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u1")), "role": "client"})
    assert env.client.post("/staff/clients/idle/followup", data=form).status_code == 403          # a client cannot send it
    assert not sent
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u1")), "role": "staff", "email": "team@example.com"})
    assert env.client.post("/staff/clients/idle/followup", data={"subject": "x", "body": "short"}).status_code == 400
    ok = env.client.post("/staff/clients/idle/followup", data=form)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    to, subject, html, text = sent[0]
    assert to == "idle@example.com" and subject == "Your free minutes on Kauli"
    assert "&lt;script&gt;" in html and "<script>" not in html and 'href="https://kauli.example/client"' in html       # escaped, and the address is a link
    conn = env.db.get_conn()
    row = conn.execute("SELECT kind, status FROM onboarding_messages WHERE user_id = 'idle'").fetchone()
    note = conn.execute("SELECT staff_notes FROM users WHERE id = 'idle'").fetchone()[0]
    conn.close()
    assert tuple(row) == ("staff_followup", "sent") and "team@example.com" in note
    again = env.client.post("/staff/clients/idle/followup", data=form)
    assert again.status_code == 429 and len(sent) == 1                                              # a double click cannot send it twice
    assert _by(funnel_audit.analyse(), "idle")["followups"] == 1


def test_nothing_is_sent_when_email_is_not_set_up_or_the_provider_refuses(env):
    _client(env, "idle", "idle@example.com", "Chidi Eze")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u1")), "role": "staff", "email": "team@example.com"})
    form = {"subject": "Your free minutes on Kauli", "body": "Hi Chidi,\n\nStart here, whenever you are ready.\n\nKauli Team"}
    env.monkeypatch.setattr(env.app.mailer, "email_configured", lambda: False)
    assert env.client.post("/staff/clients/idle/followup", data=form).status_code == 503
    env.monkeypatch.setattr(env.app.mailer, "email_configured", lambda: True)
    env.monkeypatch.setattr(env.app.mailer, "send_as", lambda *a, **k: (False, "Brevo 401: nope"))
    refused = env.client.post("/staff/clients/idle/followup", data=form)
    assert refused.status_code == 502 and refused.json()["ok"] is False and "Brevo 401" in refused.json()["error"]
