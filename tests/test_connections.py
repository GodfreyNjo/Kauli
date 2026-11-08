"""One-click integrations: Connect opens the provider's sign-in, the callback stores the access (encrypted), a settings dialog holds a few
switches, and a finished order is delivered according to them."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import connections  # noqa: E402


def _keys(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("KAULI_PUBLIC_BASE_URL", "https://kauli.test")


def _start(env):
    r = env.client.get("/account/integrations/gdocs/connect", follow_redirects=False)
    assert r.status_code == 303
    return urlparse(r.headers["location"]), parse_qs(urlparse(r.headers["location"]).query)


def _connect(env, monkeypatch, token="tok-secret"):
    _, q = _start(env)
    monkeypatch.setattr(connections.httpx, "post", lambda *a, **k: SimpleNamespace(
        status_code=200, json=lambda: {"access_token": token, "refresh_token": "ref-secret", "expires_in": 3600}, raise_for_status=lambda: None))
    monkeypatch.setattr(connections, "_google_email", lambda t: "me@example.com")
    return env.client.get("/client/integrations/gdocs/callback", params={"code": "c", "state": q["state"][0]})


def test_connect_sends_the_customer_to_googles_own_sign_in_with_the_narrowest_access(env, monkeypatch):
    _keys(monkeypatch)
    url, q = _start(env)
    assert url.netloc == "accounts.google.com" and q["client_id"] == ["cid"] and q["access_type"] == ["offline"]
    assert q["redirect_uri"] == ["https://kauli.test/client/integrations/gdocs/callback"]
    assert "drive.file" in q["scope"][0] and "auth/drive " not in q["scope"][0] + " "           # only files Kauli itself creates


def test_a_provider_without_keys_is_not_offered(env, monkeypatch):
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert "Not available yet" in env.client.get("/account/integrations/gdocs/connect").text
    page = env.client.get("/account/integrations").text
    assert "Google Docs" in page and 'data-connect="gdocs"' not in page and "Coming soon" in page


def test_the_callback_stores_the_access_encrypted_and_tells_the_page_it_worked(env, monkeypatch):
    _keys(monkeypatch)
    r = _connect(env, monkeypatch)
    assert "Connection Successful!" in r.text and "kauliIntegration" in r.text and 'kauliIntegration:"gdocs",ok:true' in r.text
    row = connections.get_connection("u1", "gdocs")
    assert row["account_label"] == "me@example.com" and row["token_enc"] != "tok-secret" and "secret" not in (row["token_enc"] + row["refresh_enc"])
    assert connections._dec(row["token_enc"]) == "tok-secret" and connections._dec(row["refresh_enc"]) == "ref-secret"
    page = env.client.get("/account/integrations").text
    assert "Connected" in page and 'data-settings="gdocs"' in page and 'data-disconnect="gdocs"' in page and 'data-connect="gdocs"' not in page


def test_a_callback_with_an_unknown_state_or_a_refusal_connects_nothing(env, monkeypatch):
    _keys(monkeypatch)
    assert "Connection expired" in env.client.get("/client/integrations/gdocs/callback", params={"code": "c", "state": "forged"}).text
    _, q = _start(env)
    assert "Not connected" in env.client.get("/client/integrations/gdocs/callback", params={"error": "access_denied", "state": q["state"][0]}).text
    assert "Connection expired" in env.client.get("/client/integrations/gdocs/callback", params={"code": "c", "state": q["state"][0]}).text   # a state works once
    assert connections.get_connection("u1", "gdocs") is None


def test_settings_are_saved_per_switch_and_unknown_keys_are_ignored(env, monkeypatch):
    _keys(monkeypatch)
    _connect(env, monkeypatch)
    d = env.client.get("/account/integrations/gdocs/settings").json()
    assert d["name"] == "Google Docs" and d["account"] == "me@example.com"
    assert {o["key"]: o["on"] for o in d["options"]} == {"auto": True, "summary": True, "transcript_link": True, "timestamps": True, "speakers": True}
    r = env.client.post("/account/integrations/gdocs/settings", json={"settings": {"summary": False, "timestamps": False, "evil": True}})
    assert r.json()["settings"] == {"auto": False, "summary": False, "transcript_link": False, "timestamps": False, "speakers": False}   # unsent switches are off
    assert "evil" not in json.loads(connections.get_connection("u1", "gdocs")["settings_json"])
    assert env.client.get("/account/integrations/slack/settings").status_code == 404


def test_disconnect_removes_the_access(env, monkeypatch):
    _keys(monkeypatch)
    _connect(env, monkeypatch)
    revoked = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **k: revoked.append(url))
    assert env.client.post("/account/integrations/gdocs/disconnect").json() == {"ok": True}
    assert connections.get_connection("u1", "gdocs") is None and revoked == ["https://oauth2.googleapis.com/revoke"]


def test_a_finished_order_becomes_a_google_doc_that_follows_the_switches(env, wav_bytes, monkeypatch):
    _keys(monkeypatch)
    _connect(env, monkeypatch)
    order = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET ai_summary_text = ? WHERE id = ?", ("A short summary of the call.", order["id"]))
    conn.commit()
    conn.close()
    order = env.db.get_order(order["id"])
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: None)
    made = []
    monkeypatch.setattr(connections, "_make_google_doc", lambda row, title, body: made.append((title, body)) or "https://docs.google.com/x")
    connections.deliver_order_ready(order, "https://kauli.test")
    assert len(made) == 1 and "A short summary of the call." in made[0][1] and f"https://kauli.test/account/orders/{order['id']}" in made[0][1]
    env.client.post("/account/integrations/gdocs/settings", json={"settings": {"auto": True, "summary": False, "transcript_link": False}})
    connections.deliver_order_ready(order, "https://kauli.test")
    assert "A short summary" not in made[1][1] and "Open this transcript" not in made[1][1]
    env.client.post("/account/integrations/gdocs/settings", json={"settings": {"auto": False}})
    connections.deliver_order_ready(order, "https://kauli.test")
    assert len(made) == 2                                                                           # automatic sending is off


def test_a_failed_delivery_is_logged_and_never_raises(env, wav_bytes, monkeypatch):
    _keys(monkeypatch)
    _connect(env, monkeypatch)
    order = env.order_from(env.place(wav_bytes))
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: None)
    monkeypatch.setattr(connections, "_make_google_doc", lambda *a: (_ for _ in ()).throw(RuntimeError("Google says no")))
    connections.deliver_order_ready(order, "https://kauli.test")
    conn = env.db.get_conn()
    row = conn.execute("SELECT * FROM webhook_deliveries WHERE order_id = ?", (order["id"],)).fetchone()
    conn.close()
    assert row is not None and row["event"] == "gdocs.order.ready" and "Google says no" in (row["error"] or "")
