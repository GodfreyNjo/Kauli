"""One-click integrations: Connect, sign in on the provider's own page, "Connection successful", then a few switches.

The customer never pastes keys or reads setup instructions. They press Connect, a small window opens on the provider's own sign-in and
consent page, and when they come back the card shows Connected with a Settings dialog. Kauli keeps only the access it was given (stored
encrypted), and Disconnect removes it.

Each provider is inert until Kauli's own app credentials for it are in .env (these are set once by us, never by customers):
    gdocs  (Google Docs)   GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET   redirect: <site>/client/integrations/gdocs/callback
    slack                  SLACK_CLIENT_ID + SLACK_CLIENT_SECRET                  redirect: <site>/client/integrations/slack/callback
A provider that is not set up shows "Coming soon" instead of a Connect button that cannot work.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import db

router = APIRouter()
A = None
STATE_SECONDS = 600

PROVIDERS = {
    "gdocs": {
        "name": "Google Docs",
        "id_env": "GOOGLE_OAUTH_CLIENT_ID", "secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth", "token_url": "https://oauth2.googleapis.com/token",
        # drive.file: Kauli can only touch the documents it creates itself, never the rest of the customer's Drive
        "scopes": "https://www.googleapis.com/auth/drive.file openid email",
        "extra": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "blurb": "Save transcripts directly to Google Docs for easy editing, sharing, and collaboration.",
        "settings": [("auto", "Save every finished order automatically", True), ("summary", "Include summary", True),
                     ("transcript_link", "Include transcript link", True), ("timestamps", "Include timestamps", True),
                     ("speakers", "Include speaker names", True)],
    },
    "slack": {
        "name": "Slack",
        "id_env": "SLACK_CLIENT_ID", "secret_env": "SLACK_CLIENT_SECRET",
        "auth_url": "https://slack.com/oauth/v2/authorize", "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": "incoming-webhook", "scope_param": "scope", "extra": {},
        "blurb": "Get a message in your channel the moment a transcript is ready.",
        "settings": [("auto", "Post when an order is ready", True), ("summary", "Include summary", True),
                     ("transcript_link", "Include transcript link", True)],
    },
}


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------ small helpers
def configured(provider: str) -> bool:
    p = PROVIDERS.get(provider)
    return bool(p and os.environ.get(p["id_env"]) and os.environ.get(p["secret_env"]))


def _enc(text: str | None) -> str | None:
    return A._notion_encrypt(text) if text else None


def _dec(text: str | None) -> str | None:
    return A._notion_decrypt(text) if text else None


def _owner_id(request: Request):
    """The signed-in customer's account id, or None (staff and signed-out visitors cannot connect anything)."""
    user = A.current_user(request)
    return (user, user["client_scope_id"]) if user and user["role"] == "client" else (None, None)


def _base(request: Request) -> str:
    return A._public_base_url(request)


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_base(request)}/client/integrations/{provider}/callback"


def get_connection(client_id: str, provider: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM integration_connections WHERE client_id = ? AND provider = ?", (client_id, provider)).fetchone()
    conn.close()
    return row


def connections_for(client_id: str) -> dict:
    """{provider: {"label", "settings"}} for the integrations page."""
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM integration_connections WHERE client_id = ?", (client_id,)).fetchall()
    conn.close()
    return {r["provider"]: {"label": r["account_label"] or "", "settings": settings_of(r)} for r in rows if r["provider"] in PROVIDERS}


def settings_of(row) -> dict:
    spec = PROVIDERS[row["provider"]]["settings"]
    try:
        saved = json.loads(row["settings_json"] or "{}")
    except ValueError:
        saved = {}
    return {key: bool(saved.get(key, default)) for key, _label, default in spec}


def _popup(title: str, message: str, ok: bool, provider: str) -> HTMLResponse:
    """The small window's last page: tells the page that opened it, then closes itself."""
    safe_t, safe_m = html.escape(title), html.escape(message)
    tone, mark = ("#0c6b3a", "&#10003;") if ok else ("#a02323", "!")
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_t}</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;background:#f4f8ff;color:#27324a}}
.c{{background:#fff;border-radius:16px;box-shadow:0 8px 30px rgba(8,20,45,.12);padding:36px 40px;text-align:center;max-width:420px}}
.m{{width:72px;height:72px;border-radius:50%;margin:0 auto 16px;display:grid;place-items:center;font-size:36px;color:{tone};background:{'#dcfce7' if ok else '#fde8e8'}}}
h1{{margin:0 0 8px;color:{tone};font-size:1.4rem}}p{{margin:6px 0}}.s{{color:#7b8499;font-size:.9rem}}</style></head>
<body><div class="c"><div class="m">{mark}</div><h1>{safe_t}</h1><p>{safe_m}</p><p class="s">This window will close automatically in a moment...</p></div>
<script>try{{if(window.opener)window.opener.postMessage({{kauliIntegration:{json.dumps(provider)},ok:{json.dumps(ok)}}},location.origin)}}catch(e){{}}
setTimeout(function(){{window.close()}},1400)</script></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ------------------------------------------------------------------------------------------------ connect
@router.get("/client/integrations/{provider}/connect")
def connect(request: Request, provider: str):
    user, client_id = _owner_id(request)
    if not user:
        return _popup("Please sign in first", "Sign in to Kauli, then press Connect again.", False, provider)
    if user["client_scope_id"] != user["id"]:
        return _popup("Ask the account owner", "Only the account owner can connect integrations.", False, provider)
    if provider not in PROVIDERS or not configured(provider):
        return _popup("Not available yet", "This connection is not switched on yet.", False, provider)
    spec = PROVIDERS[provider]
    state = secrets.token_urlsafe(24)
    conn = db.get_conn()
    conn.execute("DELETE FROM integration_oauth_states WHERE created_at < ?", (time.time() - STATE_SECONDS,))
    conn.execute("INSERT INTO integration_oauth_states (state, client_id, provider, created_at) VALUES (?, ?, ?, ?)",
                 (state, client_id, provider, time.time()))
    conn.commit()
    conn.close()
    params = {"client_id": os.environ[spec["id_env"]], "redirect_uri": _redirect_uri(request, provider), "response_type": "code",
              spec.get("scope_param", "scope"): spec["scopes"], "state": state, **spec["extra"]}
    from fastapi.responses import RedirectResponse
    return RedirectResponse(spec["auth_url"] + "?" + urlencode(params), status_code=303)


@router.get("/client/integrations/{provider}/callback")
def callback(request: Request, provider: str, code: str = "", state: str = "", error: str = ""):
    user, client_id = _owner_id(request)
    if provider not in PROVIDERS:
        return _popup("Not available", "Unknown connection.", False, provider)
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM integration_oauth_states WHERE state = ?", (state,)).fetchone()
    conn.execute("DELETE FROM integration_oauth_states WHERE state = ?", (state,))
    conn.commit()
    conn.close()
    if not user or not row or row["client_id"] != client_id or row["provider"] != provider or time.time() - row["created_at"] > STATE_SECONDS:
        return _popup("Connection expired", "That sign-in took too long or came from somewhere else. Press Connect again.", False, provider)
    if error or not code:
        return _popup("Not connected", "You chose not to allow access, so nothing was connected.", False, provider)
    spec = PROVIDERS[provider]
    try:
        r = httpx.post(spec["token_url"], data={"code": code, "client_id": os.environ[spec["id_env"]], "client_secret": os.environ[spec["secret_env"]],
                                                 "redirect_uri": _redirect_uri(request, provider), "grant_type": "authorization_code"}, timeout=20)
        data = r.json()
        if provider == "slack":
            if not data.get("ok"):
                raise ValueError(data.get("error", "slack refused"))
            hook = (data.get("incoming_webhook") or {})
            token, refresh, expires = None, None, None
            target = hook.get("url")
            label = (hook.get("channel") or "") + (f" in {data['team']['name']}" if data.get("team") else "")
        else:
            r.raise_for_status()
            token, refresh = data["access_token"], data.get("refresh_token")
            expires, target = time.time() + int(data.get("expires_in", 3600)) - 60, None
            label = _google_email(token)
    except Exception:  # noqa: BLE001 - any provider trouble ends as a clear message in the window, never an error page
        return _popup("Could not connect", "The service did not accept the connection. Please try again.", False, provider)
    conn = db.get_conn()
    prior = conn.execute("SELECT settings_json, refresh_enc FROM integration_connections WHERE client_id = ? AND provider = ?", (client_id, provider)).fetchone()
    conn.execute("""INSERT OR REPLACE INTO integration_connections
        (client_id, provider, token_enc, refresh_enc, expires_at, account_label, target_enc, settings_json, connected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (client_id, provider, _enc(token), _enc(refresh) or (prior["refresh_enc"] if prior else None), expires, label, _enc(target),
                  prior["settings_json"] if prior else "{}", time.time()))
    conn.commit()
    conn.close()
    return _popup("Connection Successful!", f"{spec['name']} is connected. You can close this window.", True, provider)


def _google_email(token: str) -> str:
    try:
        return httpx.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {token}"}, timeout=10).json().get("email", "")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------------------------------------ settings and disconnect
@router.get("/client/integrations/{provider}/settings")
def get_settings(request: Request, provider: str):
    user, client_id = _owner_id(request)
    row = get_connection(client_id, provider) if user and provider in PROVIDERS else None
    if not row:
        return JSONResponse({"error": "not connected"}, status_code=404)
    spec = PROVIDERS[provider]
    current = settings_of(row)
    return JSONResponse({"name": spec["name"], "account": row["account_label"] or "",
                         "options": [{"key": k, "label": label, "on": current[k]} for k, label, _d in spec["settings"]]})


@router.post("/client/integrations/{provider}/settings")
async def save_settings(request: Request, provider: str):
    user, client_id = _owner_id(request)
    row = get_connection(client_id, provider) if user and provider in PROVIDERS else None
    if not row:
        return JSONResponse({"error": "not connected"}, status_code=404)
    try:
        sent = (await request.json()).get("settings", {})
    except Exception:  # noqa: BLE001
        sent = {}
    keys = [k for k, _l, _d in PROVIDERS[provider]["settings"]]
    clean = {k: bool(sent.get(k)) for k in keys}
    conn = db.get_conn()
    conn.execute("UPDATE integration_connections SET settings_json = ? WHERE client_id = ? AND provider = ?", (json.dumps(clean), client_id, provider))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "settings": clean})


@router.post("/client/integrations/{provider}/disconnect")
def disconnect(request: Request, provider: str):
    user, client_id = _owner_id(request)
    if not user or provider not in PROVIDERS:
        return JSONResponse({"error": "not allowed"}, status_code=403)
    row = get_connection(client_id, provider)
    if row and provider == "gdocs":                                  # tell Google too, so the access really ends
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": _dec(row["refresh_enc"]) or _dec(row["token_enc"]) or ""}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    conn = db.get_conn()
    conn.execute("DELETE FROM integration_connections WHERE client_id = ? AND provider = ?", (client_id, provider))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------------------------------------ delivering a finished order
def _google_token(row) -> str | None:
    """A working access token, refreshed when the saved one has run out."""
    if row["expires_at"] and row["expires_at"] > time.time() and _dec(row["token_enc"]):
        return _dec(row["token_enc"])
    refresh = _dec(row["refresh_enc"])
    if not refresh:
        return None
    spec = PROVIDERS["gdocs"]
    r = httpx.post(spec["token_url"], data={"client_id": os.environ[spec["id_env"]], "client_secret": os.environ[spec["secret_env"]],
                                            "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    conn = db.get_conn()
    conn.execute("UPDATE integration_connections SET token_enc = ?, expires_at = ? WHERE client_id = ? AND provider = 'gdocs'",
                 (_enc(data["access_token"]), time.time() + int(data.get("expires_in", 3600)) - 60, row["client_id"]))
    conn.commit()
    conn.close()
    return data["access_token"]


def _document(order, settings: dict, link: str) -> tuple[str, str]:
    """(title, html) for the Google Doc: what the customer's switches asked for."""
    from . import transcript_export
    title = order["original_filename"] or "Kauli transcript"
    parts = [f"<h1>{html.escape(title)}</h1>"]
    if settings.get("transcript_link") and link:
        parts.append(f'<p><a href="{html.escape(link)}">Open this transcript in Kauli</a></p>')
    if settings.get("summary") and order["ai_summary_text"]:
        parts.append("<h2>Summary</h2>" + "".join(f"<p>{html.escape(p)}</p>" for p in order["ai_summary_text"].split("\n") if p.strip()))
    job = A._load_job(order)
    if job:
        parts.append("<h2>Transcript</h2>")
        for line in transcript_export.paragraphs(job, timestamps=settings.get("timestamps", True), speakers=settings.get("speakers", True)):
            parts.append(f"<p>{html.escape(line)}</p>")
    return title, "".join(parts)


def _make_google_doc(row, title: str, body_html: str) -> str:
    token = _google_token(row)
    if not token:
        raise RuntimeError("Google access expired: reconnect Google Docs")
    boundary = "kauli" + secrets.token_hex(8)
    meta = json.dumps({"name": title[:200], "mimeType": "application/vnd.google-apps.document"})
    payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
               f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n<html><body>{body_html}</body></html>\r\n--{boundary}--").encode()
    r = httpx.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink", content=payload,
                   headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"}, timeout=60)
    r.raise_for_status()
    return r.json().get("webViewLink", "")


def _post_slack(row, order, settings: dict, link: str) -> None:
    url = _dec(row["target_enc"])
    if not url:
        raise RuntimeError("Slack channel missing: reconnect Slack")
    text = f":white_check_mark: *{order['original_filename']}* is ready."
    if settings.get("summary") and order["ai_summary_text"]:
        text += "\n" + order["ai_summary_text"][:600]
    if settings.get("transcript_link") and link:
        text += f"\n<{link}|Open the transcript in Kauli>"
    httpx.post(url, json={"text": text}, timeout=15).raise_for_status()


def deliver_order_ready(order, base_url: str) -> None:
    """Send a finished order to every connected service that is set to receive it. A failure is logged, never raised: it must not
    disturb the approval it hangs off."""
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM integration_connections WHERE client_id = ?", (order["client_id"],)).fetchall()
    conn.close()
    link = f"{base_url.rstrip('/')}/client/orders/{order['id']}"
    for row in rows:
        if row["provider"] not in PROVIDERS:
            continue
        settings = settings_of(row)
        if not settings.get("auto"):
            continue
        try:
            if row["provider"] == "gdocs":
                title, body = _document(order, settings, link)
                _make_google_doc(row, title, body)
            else:
                _post_slack(row, order, settings, link)
            db.log_webhook_delivery(order["client_id"], order["id"], f"{row['provider']}.order.ready", True, 200)
        except Exception as exc:  # noqa: BLE001
            db.log_webhook_delivery(order["client_id"], order["id"], f"{row['provider']}.order.ready", False, None, str(exc)[:300])


def deliver_in_background(order, base_url: str) -> None:
    threading.Thread(target=deliver_order_ready, args=(order, base_url), daemon=True).start()
