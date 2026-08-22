"""Thin wrapper around supabase-py for email/password auth.

Deliberately thin: Supabase's GoTrue service does the actual security work
(password hashing, token issuance, rate limiting) - this module just calls
it and translates results into what app.py needs. Never write password
handling code yourself; that's the whole point of using this over the old
mocked login.
"""
from __future__ import annotations

import os
import re

from supabase import create_client, Client

_client: Client | None = None

# Comma-separated allowlist of emails that get the 'staff' role on first
# login. Everyone else defaults to 'client'. Set this in .env to your own
# email(s) before testing the staff side.
STAFF_EMAILS = {
    e.strip().lower() for e in os.environ.get("KAULI_STAFF_EMAILS", "").split(",") if e.strip()
}

# ---------------------------------------------------------- password policy ----
# Only enforced at signup - existing accounts keep whatever password they
# already have (a real Supabase project applies its own minimum retroactively
# would just lock people out; there's no "change password" flow yet to send
# them through, so this only ever tightens things going forward).
PASSWORD_MIN_LENGTH = 10
PASSWORD_POLICY_HINT = (
    f"At least {PASSWORD_MIN_LENGTH} characters, with an uppercase letter, "
    "a lowercase letter, a number and a symbol."
)

# Not exhaustive - a real breached-password check (e.g. Have I Been Pwned's
# k-anonymity API) is future work. Matched against the password with any
# trailing digits/symbols stripped, not exact-matched - "Password123!"
# passes every length/complexity rule above but is still one of the first
# guesses in any real attack; a literal blocklist would miss it entirely
# since "password123!" itself was never typed into the set.
_COMMON_PASSWORD_BASES = {
    "password", "passw0rd", "qwerty", "qwertyuiop", "letmein", "welcome",
    "admin", "changeme", "iloveyou", "trustno1", "dragon", "monkey",
    "football", "baseball", "master", "sunshine", "princess", "1qaz2wsx",
}


def password_policy_errors(password: str, email: str | None = None) -> list[str]:
    """Server-side enforcement - the HTML `minlength` on the signup form is
    just a UX hint, never trusted by itself; anyone can bypass client-side
    validation. Returns every rule the password fails at once (empty list =
    it passes), so someone fixing it isn't stuck fixing one rule at a time."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if not re.search(r"[a-z]", password):
        errors.append("a lowercase letter")
    if not re.search(r"[A-Z]", password):
        errors.append("an uppercase letter")
    if not re.search(r"\d", password):
        errors.append("a number")
    if not re.search(r"[^\w\s]", password):
        errors.append("a symbol (e.g. ! @ # $ %)")
    lowered = password.lower()
    stripped = re.sub(r"[\d\W_]+$", "", lowered)  # trailing digits/symbols peeled off
    if lowered in _COMMON_PASSWORD_BASES or stripped in _COMMON_PASSWORD_BASES:
        errors.append("not be a commonly used password (even with numbers/symbols appended)")
    if email:
        local_part = email.split("@")[0].strip().lower()
        if len(local_part) >= 3 and local_part in password.lower():
            errors.append("not contain your email address")
    return errors


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_ANON_KEY not set in .env - "
                "real auth can't work without them."
            )
        _client = create_client(url, key)
    return _client


def role_for_email(email: str) -> str:
    return "staff" if email.strip().lower() in STAFF_EMAILS else "client"


def sign_up(email: str, password: str):
    """Returns (session_or_None, error_message_or_None).
    session is None (with no error) when Supabase's "Confirm email" setting
    is on and the account needs an email click before it can log in - that
    is normal Supabase behaviour, not a bug."""
    try:
        res = get_client().auth.sign_up({"email": email, "password": password})
        return res.session, None
    except Exception as exc:  # noqa: BLE001 - surface Supabase's own message
        return None, str(exc)


def sign_in(email: str, password: str):
    """Returns (session_or_None, error_message_or_None)."""
    try:
        res = get_client().auth.sign_in_with_password({"email": email, "password": password})
        return res.session, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def request_password_reset(email: str, redirect_to: str) -> None:
    """Fire-and-forget - deliberately swallows every error. Surfacing
    "no account with that email" would let anyone enumerate which emails
    have a Kauli account; the /forgot-password route shows the same
    generic "if an account exists..." message regardless of what actually
    happened here. Supabase itself sends the email (we never see the
    reset link or token), addressed via Supabase's own "Reset Password"
    template - `redirect_to` is where Supabase sends the browser back to
    once the link is clicked, must be on Supabase's project Redirect URLs
    allowlist or the click fails."""
    try:
        get_client().auth.reset_password_for_email(email, {"redirect_to": redirect_to})
    except Exception:  # noqa: BLE001
        pass


def set_new_password(access_token: str, refresh_token: str, new_password: str):
    """Consumes the (access_token, refresh_token) pair a recovery link
    handed back to the browser (see reset_password.html - Supabase returns
    these in the URL fragment, which never reaches our server on its own,
    so the page's own JS forwards them here as normal form fields).
    Returns (ok, error_message_or_None)."""
    try:
        get_client().auth.set_session(access_token, refresh_token)
        get_client().auth.update_user({"password": new_password})
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
