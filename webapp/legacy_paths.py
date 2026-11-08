"""Old addresses that still work: they send the visitor to the new one.

The customer area used to live under /client and the staff customer list under /staff/clients. They are now
/account and /staff/customers (see the decision log in the engineering guide). Links in emails already sent,
bookmarks, notifications stored in the database, and pages cached in a browser still point at the old addresses,
so every old address answers with a permanent redirect (308, which keeps the method and the body, so a form
posted to an old address still arrives) to the same page at the new address, with the query string kept.

Two addresses are deliberately NOT moved: the OAuth callbacks /client/youtube/callback and
/client/integrations/{provider}/callback. Google and the other providers have those exact addresses registered, so
changing them would break connecting an integration. They are declared where they are used and match before the
catch-alls here, because this module is registered last.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
RENAMED_PREFIXES = {"/client": "/account", "/staff/clients": "/staff/customers"}


def register(app_module) -> None:
    app_module.app.include_router(router)


def new_address(request: Request, old_prefix: str, rest: str) -> str:
    """The same page at the new prefix, with the query string kept."""
    path = RENAMED_PREFIXES[old_prefix] + (f"/{rest}" if rest else "")
    return f"{path}?{request.url.query}" if request.url.query else path


def redirect_for(old_prefix: str):
    """A route function that sends everything under `old_prefix` to the new prefix."""
    def go(request: Request, rest: str = ""):
        return RedirectResponse(new_address(request, old_prefix, rest), status_code=308)
    return go


for old_prefix in RENAMED_PREFIXES:
    handler = redirect_for(old_prefix)
    router.add_api_route(old_prefix, handler, methods=ALL_METHODS, include_in_schema=False)
    router.add_api_route(old_prefix + "/{rest:path}", handler, methods=ALL_METHODS, include_in_schema=False)
