"""The routes behind diagnostics and the engineering guide.

That is: the global error handler, /staff/diagnostics and /staff/engineering. They are kept out of app.py on
purpose (one topic, one module). `register(app_module)` is called once from app.py and attaches everything here.
Recording and reading failures is in diagnostics.py; the Markdown pages are rendered by engineering_docs.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import diagnostics, engineering_docs

router = APIRouter()
A = None          # the app module, set by register(): gives access to current_user, templates, api_log


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)
    app_module.app.add_exception_handler(Exception, unexpected_error)
    diagnostics.init_sentry()      # optional: also report failures to Sentry when SENTRY_DSN is set


def wants_json(request: Request) -> bool:
    """True for callers that expect JSON back (the API, and pages that call the server with fetch)."""
    return (request.url.path.startswith("/api/")
            or "application/json" in request.headers.get("accept", "")
            or request.headers.get("x-requested-with") == "XMLHttpRequest")


def error_page_html(reference: str) -> str:
    """The page a visitor sees when something fails on our side.

    Plain HTML on purpose: it must still work when the templates are what broke.
    """
    contact = A.CONTACT_EMAIL
    body_style = "font-family:system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.25rem;line-height:1.55"
    code_style = "font-size:1.1rem;background:#eef3fc;padding:.2rem .5rem;border-radius:.4rem"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Something went wrong - Kauli</title></head>"
        f"<body style=\"{body_style};color:#0b2447\">"
        "<h1 style='font-size:1.6rem'>Something went wrong on our side</h1>"
        "<p>Your work is safe, and we have recorded what happened. Please try again in a minute.</p>"
        f"<p>If it keeps happening, email <a href='mailto:{contact}'>{contact}</a> and quote this reference "
        "so we can find it straight away:</p>"
        f"<p><code style='{code_style}'>{reference}</code></p>"
        "<p><a href='/'>Back to Kauli</a></p></body></html>")


async def unexpected_error(request: Request, error: Exception):
    """Every failure nobody planned for ends here.

    It is recorded with a reference number (the trace id) and logged with where it happened. The visitor gets a
    short page, or a JSON message, that quotes the reference.
    """
    reference = getattr(request.state, "trace_id", None) or uuid.uuid4().hex[:16]
    user_id = (request.scope.get("session") or {}).get("user_id")
    diagnostics.record_error(reference, request.method, request.url.path, user_id, error)
    A.api_log.error("unhandled_exception", exc_info=error, extra={
        "trace_id": reference, "method": request.method, "path": request.url.path,
        "error_type": type(error).__name__, "where": diagnostics.where_raised(error)})
    headers = {"X-Trace-Id": reference}
    if wants_json(request):
        message = {"error": "Something went wrong on our side.", "reference": reference}
        return JSONResponse(message, status_code=500, headers=headers)
    return HTMLResponse(error_page_html(reference), status_code=500, headers=headers)


@router.get("/staff/engineering", response_class=HTMLResponse)
@router.get("/staff/engineering/{slug}", response_class=HTMLResponse)
def staff_engineering(request: Request, slug: str = "00-start-here"):
    """The engineering guide: how the system is built, the standards, how to work on it, diagnose it and deploy it.

    Every staff member can read it.
    """
    user = A.current_user(request)
    if not user or user["role"] != "staff":
        return RedirectResponse("/login")
    page = engineering_docs.render(slug)
    if page is None:
        return HTMLResponse("Page not found.", status_code=404)
    context = {"user": user, "theme": "dark", "pages": engineering_docs.available(), "page": page}
    return A.templates.TemplateResponse(request, "staff_engineering.html", context)


@router.get("/staff/diagnostics", response_class=HTMLResponse)
def staff_diagnostics(request: Request, ref: str = ""):
    """Recent failures grouped by bug, a lookup by reference number, and which outside services are on.

    Admins only: stack traces can mention internal details.
    """
    user = A.current_user(request)
    if not user or user["role"] != "staff" or not user["is_admin"]:
        return RedirectResponse("/login")
    reference = ref.strip()
    context = {"user": user, "theme": "dark", "groups": diagnostics.recent_groups(),
               "checks": diagnostics.configuration_checks(), "ref": reference,
               "found": diagnostics.find_error(reference) if reference else None}
    return A.templates.TemplateResponse(request, "staff_diagnostics.html", context)
