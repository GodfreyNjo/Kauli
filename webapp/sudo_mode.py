"""Confirm it is you before something sensitive: re-enter your password, or use a code we email you.

Being signed in is not enough for actions that are hard to undo or that give access to others: creating or revoking
an API key, adding or removing a team member, cancelling a subscription, closing the account. For those, the person
confirms again, and the confirmation is good for SUDO_MINUTES. This is the "sudo mode" other sites use.

  required         a FastAPI dependency: add `dependencies=[Depends(sudo_mode.required)]` to a route to protect it
  confirm_with_password / send_code / confirm_with_code   the two ways to confirm
  mask_email       a@example.com -> a•••@example.com, for showing an address without publishing it all

A person who signed in with Google or an emailed link may have no password at all, so a one-time code sent to the
account's email is always offered too. The code is stored only as a hash, lasts 10 minutes, and allows 5 tries.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request

SUDO_MINUTES = 10
CODE_MINUTES = 10
CODE_TRIES = 5
DEFAULT_RETURN = "/settings?tab=security"


def confirmed_recently(request: Request) -> bool:
    return time.time() - request.session.get("sudo_at", 0) < SUDO_MINUTES * 60


def mark_confirmed(request: Request) -> None:
    request.session["sudo_at"] = time.time()
    request.session.pop("sudo_code", None)


def safe_return_path(address: str | None) -> str:
    """Where to go after confirming: a path and query on this site, never another site; else the security tab."""
    parts = urlparse(address or "")
    path = parts.path
    if path.startswith("/") and not path.startswith("//") and path.startswith(("/settings", "/account")):
        return f"{path}?{parts.query}" if parts.query else path
    return DEFAULT_RETURN


def required(request: Request) -> None:
    """Dependency for sensitive routes: send the person to confirm first unless they did so in the last few minutes."""
    if confirmed_recently(request):
        return
    back = safe_return_path(request.headers.get("referer"))
    raise HTTPException(status_code=303, headers={"Location": f"/settings/confirm?next={quote(back)}"})


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def new_code(request: Request) -> str:
    """Make a six-digit code, keep only its hash in the session, and return the code to be emailed."""
    code = f"{secrets.randbelow(10**6):06d}"
    salt = secrets.token_hex(8)
    request.session["sudo_code"] = {"salt": salt, "hash": _hash(code, salt), "expires": time.time() + CODE_MINUTES * 60,
                                    "tries": 0}
    return code


def confirm_with_code(request: Request, code: str) -> bool:
    """True, and the person is confirmed, when the code matches and has not expired or been guessed at too often."""
    saved = request.session.get("sudo_code")
    if not saved or time.time() > saved["expires"] or saved["tries"] >= CODE_TRIES:
        return False
    saved["tries"] += 1
    request.session["sudo_code"] = saved
    if hmac.compare_digest(_hash(code.strip(), saved["salt"]), saved["hash"]):
        mark_confirmed(request)
        return True
    return False


def mask_email(address: str | None) -> str:
    """a@example.com -> a•••@example.com, so an address can be shown back to its owner without publishing it all."""
    if not address or "@" not in address:
        return ""
    name, domain = address.split("@", 1)
    return f"{name[:1]}•••@{domain}"
