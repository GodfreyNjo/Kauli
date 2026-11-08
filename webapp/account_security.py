"""The account-security pages: where you are signed in, and confirming it is you before something sensitive.

/settings/sessions   the list of devices signed in, with Sign out for each and for all the others
/settings/confirm    re-enter your password, or use a code emailed to you (see sudo_mode.py)

`register(app_module)` is called once from app.py. The logic is in sessions.py and sudo_mode.py; this file is
only the pages and the forms.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import mailer, rate_limit, sessions, sudo_mode, supabase_auth

router = APIRouter()
A = None          # the app module, set by register(): gives access to current_user and templates


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def signed_in_user(request: Request):
    """The person signed in, or None (the caller then sends them to the sign-in page)."""
    user = A.current_user(request)
    return user if user and user["role"] != "api" else None


@router.get("/settings/sessions", response_class=HTMLResponse)
def sessions_page(request: Request, notice: str = ""):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    current = request.session.get("sid")
    context = {"user": user, "notice": notice, "devices": sessions.list_for(user["id"], current)}
    return A.templates.TemplateResponse(request, "account_sessions.html", context)


@router.post("/settings/sessions/revoke")
def sessions_revoke(request: Request, sid: str = Form("")):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    done = sessions.revoke(user["id"], sid)
    message = "That device has been signed out." if done else "That sign-in was already gone."
    return RedirectResponse(f"/settings/sessions?notice={quote(message)}", status_code=303)


@router.post("/settings/sessions/revoke-others")
def sessions_revoke_others(request: Request):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    count = sessions.revoke_others(user["id"], request.session.get("sid"))
    message = f"Signed out of {count} other device{'' if count == 1 else 's'}."
    return RedirectResponse(f"/settings/sessions?notice={quote(message)}", status_code=303)


@router.get("/settings/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, next: str = "", notice: str = "", error: str = ""):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    context = {"user": user, "next": sudo_mode.safe_return_path(next), "notice": notice, "error": error,
               "masked_email": sudo_mode.mask_email(user["email"]), "code_sent": bool(request.session.get("sudo_code"))}
    return A.templates.TemplateResponse(request, "account_confirm.html", context)


def back_to(path: str, key: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/settings/confirm?next={quote(path)}&{key}={quote(message)}", status_code=303)


@router.post("/settings/confirm")
def confirm_submit(request: Request, next: str = Form(""), password: str = Form(""), code: str = Form("")):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    destination = sudo_mode.safe_return_path(next)
    allowed, _wait = rate_limit.check(f"sudo:{user['id']}", limit=8, window_s=600)
    if not allowed:
        return back_to(destination, "error", "Too many tries. Please wait a few minutes.")
    if code.strip():
        ok = sudo_mode.confirm_with_code(request, code)
    else:
        session, error = supabase_auth.sign_in(user["email"], password) if password else (None, "no password")
        ok = bool(session) and not error
        if ok:
            sudo_mode.mark_confirmed(request)
    if not ok:
        return back_to(destination, "error", "That did not match. Try again, or ask for a code.")
    return RedirectResponse(destination, status_code=303)


@router.post("/settings/confirm/send-code")
def confirm_send_code(request: Request, next: str = Form("")):
    user = signed_in_user(request)
    if not user:
        return RedirectResponse("/login")
    destination = sudo_mode.safe_return_path(next)
    allowed, _wait = rate_limit.check(f"sudo-mail:{user['id']}", limit=3, window_s=600)
    if not allowed:
        return back_to(destination, "error", "A code was sent a moment ago. Please check your email.")
    code = sudo_mode.new_code(request)
    body = (f"Hi,\n\nYour Kauli confirmation code is {code}. It works for {sudo_mode.CODE_MINUTES} minutes.\n\n"
            "If you did not ask for it, you can ignore this email. If you are worried about your account, "
            "change your password.\n\nKauli Team")
    mailer.send_as("support", user["email"], "Your Kauli confirmation code", mailer.text_to_html_paragraphs(body), body)
    return back_to(destination, "notice", f"We sent a 6-digit code to {sudo_mode.mask_email(user['email'])}.")
