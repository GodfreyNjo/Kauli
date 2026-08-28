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
import time

import httpx
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

_client: Client | None = None

# The installed supabase-py's own default timeouts only cover the
# Postgrest/Storage/Functions sub-clients (ClientOptions.postgrest_client_timeout
# etc.) - the Auth/GoTrue sub-client has no timeout field of its own. Passing
# a custom httpx.Client via ClientOptions.httpx_client is the real, supported
# way to give it one (confirmed by reading the installed package's own source
# - Client._init_supabase_auth_client forwards client_options.httpx_client
# straight into the GoTrue client as its http_client).
#
# Live testing against the real Supabase project traced the actual failure:
# real requests normally come back in well under a second (a deliberately
# throttled invalid-password response took ~3s), but the client's pooled
# connection occasionally goes stale and one request hangs until it's read
# timeout - then every request after that, on a fresh connection, is fast
# again. So the fix is two parts: a timeout long enough to never cut off a
# genuinely slow-but-real response, and a single automatic retry (see
# _with_timeout_retry below) so a stale connection doesn't force the user to
# manually resubmit the form, which is what "succeeds after several
# attempts" was actually them doing by hand.
_AUTH_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

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
        options = SyncClientOptions(httpx_client=httpx.Client(timeout=_AUTH_TIMEOUT))
        _client = create_client(url, key, options=options)
    return _client


def _is_timeout(exc: Exception) -> bool:
    """True for a real network timeout (worth a silent retry and a friendly
    message), False for anything else (e.g. "Invalid login credentials",
    which is meaningful to the user and should keep surfacing as-is)."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    return "timed out" in str(exc).lower()


_TIMEOUT_MESSAGE = (
    "We couldn't reach the login service in time. This is usually a brief "
    "network blip - please try again."
)


def _with_timeout_retry(call):
    """Runs `call` once, and once more if the first attempt was a genuine
    timeout - matching what people were already doing by hand (the user's
    own report: "log in succeeds after several attempts"). A second, real
    timeout is reported with a friendly message instead of the raw
    exception text; any other error is returned untouched."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if not _is_timeout(exc):
            raise
        time.sleep(0.5)
        try:
            return call()
        except Exception as exc2:  # noqa: BLE001
            if _is_timeout(exc2):
                raise TimeoutError(_TIMEOUT_MESSAGE) from exc2
            raise


def role_for_email(email: str) -> str:
    return "staff" if email.strip().lower() in STAFF_EMAILS else "client"


def sign_up(email: str, password: str):
    """Returns (session_or_None, error_message_or_None).
    session is None (with no error) when Supabase's "Confirm email" setting
    is on and the account needs an email click before it can log in - that
    is normal Supabase behaviour, not a bug."""
    try:
        res = _with_timeout_retry(
            lambda: get_client().auth.sign_up({"email": email, "password": password})
        )
        return res.session, None
    except Exception as exc:  # noqa: BLE001 - surfaces Supabase's own message,
        return None, str(exc)  # or the friendly one _with_timeout_retry raised


def sign_in(email: str, password: str):
    """Returns (session_or_None, error_message_or_None)."""
    try:
        res = _with_timeout_retry(
            lambda: get_client().auth.sign_in_with_password({"email": email, "password": password})
        )
        return res.session, None
    except Exception as exc:  # noqa: BLE001 - surfaces Supabase's own message,
        return None, str(exc)  # or the friendly one _with_timeout_retry raised


def change_password(email: str, current_password: str, new_password: str):
    """For a logged-in user changing their password from Settings -
    different path from set_new_password below (that one consumes a
    recovery-link token; this one re-authenticates with the CURRENT
    password instead). Needed either way: this app's own session cookie
    only ever stores our local user id (see app.py's /login), never a
    Supabase access token, so there's nothing to reuse here - and even if
    there were, requiring the current password again is the right call
    for changing it, not a workaround. Returns (ok, error_message_or_None)."""
    session, error = sign_in(email, current_password)
    if error or not session:
        return False, "Your current password is incorrect."
    try:
        get_client().auth.set_session(session.access_token, session.refresh_token)
        get_client().auth.update_user({"password": new_password})
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


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
