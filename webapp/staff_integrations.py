"""Integrations for staff accounts: each staff member can have their alerts sent to their own Slack channel, Microsoft Teams channel or webhook.

Staff alerts already reach every staff account in the app's bell (db.notify_all_staff). This sends the same alert on to whatever a member
connected, for the event kinds they ticked: a job ready for review, a job that failed, a provider that was switched, a help request, a bank
transfer, a stuck first order, a closed account, an integration request. A webhook is signed (X-Kauli-Signature: sha256=<HMAC of the body>) so the
receiver can check it came from Kauli. A failing integration never stops an alert: the result is kept ("Delivered", "HTTP 400...") and shown on the card.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db

router = APIRouter()
A = None

EVENTS = {
    "needs_review": "A job is ready for review",
    "dead_letter": "A job failed and needs attention",
    "provider_switched": "A provider failed and the order was switched",
    "support": "A customer asked for help",
    "payment_received": "A bank transfer was requested",
    "first_order_stuck": "A new customer's first order is stuck",
    "account_closed": "A customer closed their account",
    "integration_request": "A customer asked for an integration",
}
PROVIDERS = {
    "slack": {"name": "Slack", "blurb": "A message in your channel for each alert.", "placeholder": "https://hooks.slack.com/services/T000/B000/XXXX",
              "help": "In Slack: Apps, Incoming Webhooks, Add to a channel, then copy the Webhook URL."},
    "teams": {"name": "Microsoft Teams", "blurb": "A card in your Teams channel for each alert.", "placeholder": "https://prod-00.westeurope.logic.azure.com/workflows/...",
              "help": "In Teams: the channel's menu, Workflows, \"Post to a channel when a webhook request is received\", then copy the URL."},
    "webhook": {"name": "Webhook", "blurb": "A signed JSON message to any URL: Zapier, Make, n8n or your own system.", "placeholder": "https://hooks.example.com/kauli",
                "help": "Each request carries X-Kauli-Signature: sha256=<HMAC-SHA256 of the body, keyed with your signing secret>."},
}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


def _base() -> str:
    return (os.environ.get("KAULI_PUBLIC_BASE_URL") or "https://kauli-forgemedia.com").rstrip("/")


def _valid_url(provider: str, url: str) -> str | None:
    """None when the URL is fine for this provider, otherwise what is wrong with it."""
    if provider == "slack":
        return None if A._is_slack_webhook_url(url) else "That is not a Slack Incoming Webhook URL (it starts https://hooks.slack.com/services/)."
    if provider == "teams":
        return None if A._is_teams_webhook_url(url) else "That is not a Microsoft Teams Workflows webhook URL."
    if provider == "webhook":
        return None if A._is_safe_webhook_url(url) else "Use a public https address (not one on a private network)."
    return "Unknown integration."


def mask(url: str) -> str:
    return (url[:28] + "..." + url[-4:]) if url and len(url) > 40 else (url or "")


def _message(provider: str, kind: str, title: str, link: str | None) -> dict | None:
    full = (_base() + link) if link and link.startswith("/") else (link or "")
    if provider == "slack":
        text = f"*{A._slack_escape(title)}*" + (f"\n<{full}|Open in Kauli>" if full else "")
        return {"text": text}
    if provider == "teams":
        body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True}, {"type": "TextBlock", "text": EVENTS.get(kind, kind), "isSubtle": True, "wrap": True}]
        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4", "body": body}
        if full:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Kauli", "url": full}]
        return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    return None


def _post(row, kind: str, title: str, link: str | None) -> tuple[bool, str]:
    """One real POST to one integration. Never follows redirects, never raises."""
    provider = row["provider"]
    try:
        if provider == "webhook":
            body = json.dumps({"event": kind, "label": EVENTS.get(kind, kind), "title": title,
                               "link": (_base() + link) if link and link.startswith("/") else link, "at": int(time.time())}, ensure_ascii=False).encode("utf-8")
            sig = hmac.new((row["secret"] or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
            resp = httpx.post(row["url"], content=body, timeout=8.0, follow_redirects=False,
                              headers={"Content-Type": "application/json", "X-Kauli-Event": kind, "X-Kauli-Signature": "sha256=" + sig})
        else:
            resp = httpx.post(row["url"], json=_message(provider, kind, title, link), timeout=8.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        return False, str(exc)[:160]
    ok = 200 <= resp.status_code < 300
    return ok, "Delivered" if ok else f"HTTP {resp.status_code}: {(resp.text or '')[:120]}"


def deliver(kind: str, title: str, link: str | None = None) -> int:
    """Send one alert to every staff integration that asked for this kind. How many were sent."""
    sent = 0
    for row in db.all_staff_integrations():
        try:
            wanted = json.loads(row["events"] or "[]")
        except (TypeError, ValueError):
            wanted = []
        if kind not in wanted:
            continue
        ok, detail = _post(row, kind, title, link)
        db.set_staff_integration_result(row["user_id"], row["provider"], detail if not ok else "Delivered")
        sent += 1 if ok else 0
    return sent


def deliver_in_background(kind: str, title: str, link: str | None = None) -> None:
    threading.Thread(target=deliver, args=(kind, title, link), daemon=True, name="staff-integrations").start()


# ------------------------------------------------------------------------------------------------ the page
@router.get("/staff/integrations", response_class=HTMLResponse)
def page(request: Request):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    mine = db.get_staff_integrations(user["id"])
    cards = []
    for key, meta in PROVIDERS.items():
        row = mine.get(key)
        try:
            chosen = set(json.loads(row["events"] or "[]")) if row else set(EVENTS)
        except (TypeError, ValueError):
            chosen = set(EVENTS)
        cards.append({"key": key, **meta, "connected": bool(row), "url": mask(row["url"]) if row else "", "enabled": bool(row["enabled"]) if row else True,
                      "secret": (row["secret"] if row and key == "webhook" else ""), "events": chosen,
                      "last_status": row["last_status"] if row else None, "last_at": row["last_at"] if row else None})
    from . import connections
    return A.templates.TemplateResponse(request, "staff_integrations.html", {
        "user": user, "theme": "dark", "cards": cards, "events": EVENTS, "apps": connections.setup_status(A._public_base_url(request)), "notice": request.query_params.get("notice"), "error": request.query_params.get("error")})


@router.post("/staff/integrations/save")
async def save(request: Request):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    provider = str(form.get("provider") or "")
    if provider not in PROVIDERS:
        return RedirectResponse("/staff/integrations?error=" + quote("Unknown integration."), status_code=303)
    existing = db.get_staff_integrations(user["id"]).get(provider)
    url = str(form.get("url") or "").strip() or (existing["url"] if existing else "")
    if not url:
        return RedirectResponse("/staff/integrations?error=" + quote("Paste the URL first."), status_code=303)
    if not existing or url != existing["url"]:
        problem = _valid_url(provider, url)
        if problem:
            return RedirectResponse("/staff/integrations?error=" + quote(problem), status_code=303)
    events = [e for e in form.getlist("events") if e in EVENTS]
    secret = None if (existing and existing["secret"]) or provider != "webhook" else secrets.token_hex(24)
    db.set_staff_integration(user["id"], provider, url, events, secret=secret, enabled=bool(form.get("enabled")))
    db.log_security_event("staff_integration_saved", user_id=user["id"], email=user["email"], detail=f"{provider} ({len(events)} events)")
    return RedirectResponse("/staff/integrations?notice=" + quote(f"{PROVIDERS[provider]['name']} saved."), status_code=303)


@router.post("/staff/integrations/test")
def test(request: Request, provider: str = Form("")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    row = db.get_staff_integrations(user["id"]).get(provider)
    if not row:
        return RedirectResponse("/staff/integrations?error=" + quote("Save it first, then send a test."), status_code=303)
    ok, detail = _post(row, "needs_review", "Test alert from Kauli: your integration works", "/staff")
    db.set_staff_integration_result(user["id"], provider, detail)
    target = "/staff/integrations?notice=" if ok else "/staff/integrations?error="
    return RedirectResponse(target + quote(f"{PROVIDERS[provider]['name']}: {detail}"), status_code=303)


@router.post("/staff/integrations/remove")
def remove(request: Request, provider: str = Form("")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login")
    if provider in PROVIDERS:
        db.delete_staff_integration(user["id"], provider)
        db.log_security_event("staff_integration_removed", user_id=user["id"], email=user["email"], detail=provider)
    return RedirectResponse("/staff/integrations?notice=" + quote("Removed."), status_code=303)
