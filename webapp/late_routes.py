"""Route modules that must be registered last, in this order, so that app.py does not need a line for each.

Order matters because FastAPI uses the first route that matches:
  1. diagnostics_routes  the error handler and the staff pages for it
  2. legacy_paths        the catch-alls that send old /client addresses to /account. They are last so the
                         specific routes (such as the OAuth callbacks, which keep their old address because the
                         providers have it on file) are matched before them.
  3. account_security    the sign-in list and the "confirm it is you" pages
"""
from __future__ import annotations

from . import account_security, diagnostics_routes, legacy_paths

IN_ORDER = (diagnostics_routes, legacy_paths, account_security)


def register(app_module) -> None:
    for module in IN_ORDER:
        module.register(app_module)
