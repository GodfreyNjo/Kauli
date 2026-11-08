"""Request-level protections: forged cross-site posts, private pages that must not be cached, the closed API explorer,
the signed Calendly webhook and security.txt."""
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import web_security  # noqa: E402


def test_the_route_list_and_api_explorer_are_not_public(env):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(path).status_code == 404
    assert env.client.get("/docs/api").status_code == 200                                      # the customer-facing API guide stays


def test_a_post_from_another_site_is_refused_and_one_from_our_own_pages_is_not(env):
    form = {"email": "a@example.com", "password": "not-the-password"}
    forged = env.client.post("/login", data=form, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert forged.status_code == 403 and "another site" in forged.json()["error"]
    assert env.client.post("/login", data=form, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403          # no Origin, still cross-site
    assert env.client.post("/login", data=form, headers={"Origin": "null"}).status_code == 403
    for ok in ({"Origin": "http://testserver"}, {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "same-origin"}, {}):
        assert env.client.post("/login", data=form, headers=ok).status_code != 403, ok                                   # our own pages, and programs
    assert env.client.get("/login", headers={"Origin": "https://evil.example"}).status_code == 200                       # reading is never blocked


def test_signed_programs_are_not_held_to_the_browser_rule(env):
    evil = {"Origin": "https://evil.example"}
    assert env.client.post("/webhooks/paystack", content=b"{}", headers=evil).status_code != 403                         # its own signature decides
    assert env.client.post("/api/v1/orders", headers=evil).status_code in (400, 401, 422)                                  # its own key decides


def test_the_public_address_and_its_www_twin_are_both_ours(env, monkeypatch):
    monkeypatch.setenv("KAULI_PUBLIC_BASE_URL", "https://kauli-forgemedia.com")
    for origin in ("https://kauli-forgemedia.com", "https://www.kauli-forgemedia.com"):
        assert env.client.post("/login", data={"email": "a@example.com", "password": "x"}, headers={"Origin": origin}).status_code != 403
    assert env.client.post("/login", data={"email": "a@example.com", "password": "x"}, headers={"Origin": "https://kauli-forgemedia.com.evil.example"}).status_code == 403


def test_signed_in_pages_and_the_login_page_are_never_cached_but_public_pages_are_left_alone(env):
    assert env.client.get("/login").headers["cache-control"] == "no-store"
    assert env.client.get("/account", follow_redirects=False).headers.get("cache-control") == "no-store"
    assert "no-store" not in env.client.get("/pricing").headers.get("cache-control", "")
    assert env.client.get("/robots.txt").headers.get("cache-control") is None


def test_the_standard_security_headers_are_on_every_answer(env):
    h = env.client.get("/").headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert "payment=()" in h["permissions-policy"] and h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in env.client.get("/", headers={"x-forwarded-proto": "https"}).headers["strict-transport-security"]


def test_security_txt_says_where_to_report_a_problem(env):
    r = env.client.get("/.well-known/security.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Contact: mailto:hello@kauli-forgemedia.com" in r.text and "Expires: " in r.text and "/.well-known/security.txt" in r.text


def _calendly_headers(body: bytes, key: str, when: float | None = None) -> dict:
    stamp = str(int(when if when is not None else time.time()))
    return {"Calendly-Webhook-Signature": f"t={stamp},v1=" + hmac.new(key.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()}


def test_the_calendly_webhook_only_believes_a_signed_call(env, monkeypatch):
    monkeypatch.setattr(env.app, "_notify_internal_new_lead", lambda *a, **k: None)
    body = json.dumps({"event": "invitee.created", "payload": {"name": "Ada Booker", "email": "ada@example.com"}}).encode()
    assert env.client.post("/webhooks/calendly", content=body).status_code == 401                                         # no key set up: closed
    monkeypatch.setenv("CALENDLY_WEBHOOK_SIGNING_KEY", "signing-key")
    assert env.client.post("/webhooks/calendly", content=body).status_code == 401                                         # key set, call unsigned
    assert env.client.post("/webhooks/calendly", content=body, headers=_calendly_headers(body, "wrong-key")).status_code == 401
    assert env.client.post("/webhooks/calendly", content=body, headers=_calendly_headers(body, "signing-key", time.time() - 3600)).status_code == 401
    assert not [l for l in env.db.list_leads() if l["email"] == "ada@example.com"]
    assert env.client.post("/webhooks/calendly", content=body, headers=_calendly_headers(body, "signing-key")).status_code == 200
    assert [l for l in env.db.list_leads() if l["email"] == "ada@example.com"]
    junk = b"not json"
    assert env.client.post("/webhooks/calendly", content=junk, headers=_calendly_headers(junk, "signing-key")).status_code == 400


def test_signature_helper_rejects_malformed_headers():
    for header in ("", "garbage", "t=abc,v1=ff", "t=1,v1=", "v1=ff"):
        assert web_security.calendly_signature_ok(b"x", header, "k") is False
