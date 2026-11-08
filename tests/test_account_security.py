"""The account-security measures: where you are signed in, confirming it is you, and the shared form behaviour."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401
from webapp import sessions, sudo_mode  # noqa: E402

REAL_CONFIRMED_RECENTLY = sudo_mode.confirmed_recently          # the env fixture replaces it; some tests need it back
TEMPLATES = Path(__file__).parent.parent / "webapp" / "templates"
CHROME_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
SAFARI_ON_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


def fake_request(agent=CHROME_ON_WINDOWS, session=None, country="KE", ip="41.90.12.34", referer=None):
    headers = {"user-agent": agent, "cf-ipcountry": country, "x-forwarded-for": ip}
    if referer:
        headers["referer"] = referer
    return SimpleNamespace(session=session if session is not None else {}, headers=headers,
                           client=SimpleNamespace(host=ip), state=SimpleNamespace())


@pytest.fixture
def real_sudo(env):
    env.monkeypatch.setattr(sudo_mode, "confirmed_recently", REAL_CONFIRMED_RECENTLY)
    return env


# ------------------------------------------------------------------ sessions
def test_a_sign_in_is_recorded_and_listed_with_a_friendly_device_name(env):
    laptop = fake_request()
    sessions.sign_in(laptop, "u1")
    listed = sessions.list_for("u1", laptop.session["sid"])
    assert len(listed) == 1 and listed[0]["device"] == "Chrome on Windows" and listed[0]["is_current"]
    assert listed[0]["address"] == "41.90.x.x"                     # a place, not an exact address


def test_signing_out_other_devices_keeps_this_one_and_signs_the_rest_out(env):
    laptop, phone = fake_request(), fake_request(agent=SAFARI_ON_IPHONE)
    sessions.sign_in(laptop, "u1")
    sessions.sign_in(phone, "u1")
    assert sessions.revoke_others("u1", laptop.session["sid"]) == 1
    assert sessions.is_active(laptop, "u1") is True
    assert sessions.is_active(phone, "u1") is False
    assert phone.session == {}                                      # the revoked device's cookie is emptied


def test_a_person_can_only_sign_out_their_own_devices(env):
    other = fake_request()
    sessions.sign_in(other, "someone-else")
    assert sessions.revoke("u1", other.session["sid"]) is False
    assert sessions.is_active(other, "someone-else") is True


def test_a_sign_in_from_before_the_feature_is_adopted_not_signed_out(env):
    old_cookie = fake_request(session={"user_id": "u1"})            # no "sid": signed in before sessions existed
    assert sessions.is_active(old_cookie, "u1") is True
    assert "sid" in old_cookie.session and len(sessions.list_for("u1", None)) == 1


def test_a_cookie_cannot_be_used_for_a_different_account(env):
    first = fake_request()
    sessions.sign_in(first, "u1")
    borrowed = fake_request(session={"user_id": "u2", "sid": first.session["sid"]})
    assert sessions.is_active(borrowed, "u2") is False


def test_signing_out_revokes_the_record_and_empties_the_cookie(env):
    laptop = fake_request()
    sessions.sign_in(laptop, "u1")
    sid = laptop.session["sid"]
    sessions.sign_out(laptop)
    assert laptop.session == {} and sessions.list_for("u1", sid) == []


def test_signing_in_again_on_the_same_browser_starts_a_fresh_record(env):
    laptop = fake_request()
    sessions.sign_in(laptop, "u1")
    first = laptop.session["sid"]
    sessions.sign_in(laptop, "u2")
    assert laptop.session["sid"] != first and laptop.session["user_id"] == "u2"


# ----------------------------------------------------------------- sudo mode
def test_a_sensitive_route_sends_you_to_confirm_first(real_sudo):
    request = fake_request(referer="https://kauli-forgemedia.com/settings?tab=security")
    with pytest.raises(HTTPException) as caught:
        sudo_mode.required(request)
    assert caught.value.status_code == 303
    assert caught.value.headers["Location"] == "/settings/confirm?next=/settings%3Ftab%3Dsecurity"


def test_confirming_is_good_for_ten_minutes_then_asked_again(real_sudo):
    request = fake_request()
    sudo_mode.mark_confirmed(request)
    sudo_mode.required(request)                                     # no error: confirmed just now
    request.session["sudo_at"] = time.time() - 11 * 60
    with pytest.raises(HTTPException):
        sudo_mode.required(request)


@pytest.mark.parametrize("address", ["//evil.example", "/staff/customers", "https://evil.example/login", "", None])
def test_after_confirming_you_can_only_be_sent_to_your_own_account_pages(address):
    assert sudo_mode.safe_return_path(address) == sudo_mode.DEFAULT_RETURN


def test_a_return_path_inside_settings_or_account_is_kept():
    assert sudo_mode.safe_return_path("/settings/sessions") == "/settings/sessions"
    assert sudo_mode.safe_return_path("https://x.test/account/billing?a=1") == "/account/billing?a=1"
    assert sudo_mode.safe_return_path("https://evil.example/settings") == "/settings"    # only the path is ever used


def test_the_emailed_code_works_once_and_only_the_right_one_works():
    request = fake_request()
    code = sudo_mode.new_code(request)
    assert "sudo_code" in request.session and code not in str(request.session)      # only a hash is kept
    assert sudo_mode.confirm_with_code(request, "000000" if code != "000000" else "111111") is False
    assert sudo_mode.confirm_with_code(request, code) is True
    assert sudo_mode.confirmed_recently(request) and "sudo_code" not in request.session


def test_a_code_stops_working_after_five_wrong_guesses():
    request = fake_request()
    code = sudo_mode.new_code(request)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(sudo_mode.CODE_TRIES):
        assert sudo_mode.confirm_with_code(request, wrong) is False
    assert sudo_mode.confirm_with_code(request, code) is False       # even the right one, now


def test_an_expired_code_is_refused():
    request = fake_request()
    code = sudo_mode.new_code(request)
    request.session["sudo_code"]["expires"] = time.time() - 1
    assert sudo_mode.confirm_with_code(request, code) is False


def test_an_email_address_is_masked_when_shown_back():
    assert sudo_mode.mask_email("amina@example.com") == "a•••@example.com"
    assert sudo_mode.mask_email("nonsense") == "" and sudo_mode.mask_email(None) == ""


# --------------------------------------------------------------------- pages
SENSITIVE = ("/settings/api-key/generate", "/settings/api-key/revoke", "/settings/mfa/disable",
             "/settings/mfa/regenerate-backup-codes", "/settings/team/invite", "/settings/team/remove",
             "/settings/close-account", "/settings/request-deletion", "/account/billing/cancel-subscription")


@pytest.mark.parametrize("route", SENSITIVE)
def test_every_sensitive_action_asks_you_to_confirm_it_is_you(real_sudo, route):
    reply = real_sudo.client.post(route, data={}, headers={"referer": "/settings?tab=security"})
    assert reply.status_code == 303 and reply.headers["location"].startswith("/settings/confirm")


def test_the_confirm_page_offers_a_password_or_a_code_and_masks_the_email(real_sudo):
    page = real_sudo.client.get("/settings/confirm?next=/settings")
    assert page.status_code == 200 and "c•••@example.com" in page.text and "client@example.com" not in page.text
    assert 'name="password"' in page.text and "Email me a code" in page.text


def test_the_confirm_page_will_not_send_you_to_another_site(real_sudo):
    page = real_sudo.client.get("/settings/confirm?next=https://evil.example/")
    assert 'name="next" value="/settings?tab=security"' in page.text


def test_the_sessions_page_lists_devices_and_offers_sign_out_everywhere(env):
    sessions.sign_in(fake_request(), "u1")
    sessions.sign_in(fake_request(agent=SAFARI_ON_IPHONE), "u1")
    page = env.client.get("/settings/sessions")
    assert page.status_code == 200 and "Chrome on Windows" in page.text and "Safari on iPhone" in page.text
    assert "Sign out of all other devices" in page.text


def test_signing_out_other_devices_from_the_page_says_how_many(env):
    sessions.sign_in(fake_request(), "u1")
    sessions.sign_in(fake_request(agent=SAFARI_ON_IPHONE), "u1")
    reply = env.client.post("/settings/sessions/revoke-others")
    assert reply.status_code == 303 and "Signed%20out%20of%202%20other%20devices" in reply.headers["location"]


def test_the_security_tab_links_to_the_devices_page(env):
    page = env.client.get("/settings?tab=security")
    assert "/settings/sessions" in page.text and "Where you are signed in" in page.text


# ------------------------------------------------------- shared form behaviour
def test_every_page_loads_the_shared_form_script(env):
    assert "/static/app_ux.js" in env.client.get("/account/home").text


def test_customer_pages_use_the_confirm_dialog_not_the_browsers_native_prompt():
    for name in ("settings.html", "billing.html", "account_calendar.html", "account_notes.html", "order_detail.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "return confirm(" not in text, f"{name} still uses the native confirm()"


def test_destructive_buttons_are_marked_as_dangerous():
    text = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    assert 'data-confirm="Revoke your API key?' in text and "data-danger" in text
