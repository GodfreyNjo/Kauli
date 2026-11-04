"""One-click integrations: Connect, sign in on the provider's own page, "Connection successful", then a few switches.

The customer never pastes keys or reads setup instructions. They press Connect, a small window opens on the provider's own sign-in and
consent page, and when they come back the card shows Connected with a Settings dialog. Kauli keeps only the access it was given (stored
encrypted), and Disconnect removes it.

Each provider is inert until Kauli's own app credentials for it are in .env (these are set once by us, never by customers; the Staff >
Integrations page lists exactly what to register and which address to give each provider). A provider that is not set up shows
"Coming soon" instead of a Connect button that cannot work.

Adding a provider is one entry in PROVIDERS (its sign-in addresses, scopes and switches), one small "what the sign-in gave us" reader in
_FINISH and one sender in _SEND.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db

router = APIRouter()
A = None
STATE_SECONDS = 600

_GOOGLE = {
    "id_env": "GOOGLE_OAUTH_CLIENT_ID", "secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth", "token_url": "https://oauth2.googleapis.com/token",
    # drive.file: Kauli can only touch the files it creates itself, never the rest of the customer's Drive
    "scopes": "https://www.googleapis.com/auth/drive.file openid email",
    "extra": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
    "console": "https://console.cloud.google.com/apis/credentials", "console_name": "Google Cloud",
    "family": "google",
}
_SHARE = [("auto", "Save every finished order automatically", True), ("summary", "Include summary", True),
          ("transcript_link", "Include transcript link", True), ("timestamps", "Include timestamps", True),
          ("speakers", "Include speaker names", True)]
_FILE = [("auto", "Save every finished order automatically", True), ("summary", "Include summary", True),
         ("timestamps", "Include timestamps", True), ("speakers", "Include speaker names", True)]
_POST = [("auto", "Post when an order is ready", True), ("summary", "Include summary", True), ("transcript_link", "Include transcript link", True)]

PROVIDERS = {
    "gdocs": {**_GOOGLE, "name": "Google Docs", "blurb": "Save transcripts directly to Google Docs for easy editing, sharing, and collaboration.",
              "settings": _SHARE},
    "gsheets": {**_GOOGLE, "name": "Google Sheets", "blurb": "Add a row to a Google Sheet for every finished transcript.",
                "settings": [("auto", "Add a row for every finished order", True), ("summary", "Include summary", True),
                             ("transcript_link", "Include transcript link", True), ("transcript", "Include the full transcript", False)]},
    "gdrive": {**_GOOGLE, "name": "Google Drive", "blurb": "Save every transcript to a Kauli folder in your Google Drive.", "settings": _FILE},
    "slack": {
        "name": "Slack", "id_env": "SLACK_CLIENT_ID", "secret_env": "SLACK_CLIENT_SECRET",
        "auth_url": "https://slack.com/oauth/v2/authorize", "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": "incoming-webhook", "extra": {}, "console": "https://api.slack.com/apps", "console_name": "Slack API",
        "blurb": "Get a message in your channel the moment a transcript is ready.", "settings": _POST,
    },
    "discord": {
        "name": "Discord", "id_env": "DISCORD_CLIENT_ID", "secret_env": "DISCORD_CLIENT_SECRET",
        "auth_url": "https://discord.com/oauth2/authorize", "token_url": "https://discord.com/api/oauth2/token",
        "scopes": "webhook.incoming", "extra": {}, "console": "https://discord.com/developers/applications", "console_name": "Discord Developer Portal",
        "blurb": "Post a message in your Discord channel when a transcript is ready.", "settings": _POST,
    },
    "notion": {
        "name": "Notion", "id_env": "NOTION_CLIENT_ID", "secret_env": "NOTION_CLIENT_SECRET",
        "auth_url": "https://api.notion.com/v1/oauth/authorize", "token_url": "https://api.notion.com/v1/oauth/token",
        "scopes": "", "extra": {"owner": "user"}, "basic_auth": True,
        "console": "https://www.notion.so/profile/integrations", "console_name": "Notion integrations (a Public integration)",
        "blurb": "Send each transcript to Notion as a new page.", "settings": _SHARE,
    },
    "dropbox": {
        "name": "Dropbox", "id_env": "DROPBOX_APP_KEY", "secret_env": "DROPBOX_APP_SECRET",
        "auth_url": "https://www.dropbox.com/oauth2/authorize", "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scopes": "files.content.write account_info.read", "extra": {"token_access_type": "offline"},
        "console": "https://www.dropbox.com/developers/apps", "console_name": "Dropbox App Console (an App folder app)",
        "blurb": "Auto-save every transcript to a Kauli folder in your Dropbox.", "settings": _FILE,
    },
    "onedrive": {
        "name": "OneDrive", "id_env": "ONEDRIVE_CLIENT_ID", "secret_env": "ONEDRIVE_CLIENT_SECRET",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": "Files.ReadWrite.AppFolder offline_access User.Read", "extra": {"response_mode": "query", "prompt": "select_account"},
        "console": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade", "console_name": "Microsoft Entra app registrations",
        "blurb": "Auto-save every transcript to a Kauli folder in your OneDrive.", "settings": _FILE,
    },
}
_CAL = [("auto", "Send Kauli to my meetings automatically", True), ("only_mine", "Only meetings I organize", False),
        ("tell_attendees", "Email the notes to everyone invited", False)]
PROVIDERS["gcal"] = {**_GOOGLE, "name": "Google Calendar", "scopes": "https://www.googleapis.com/auth/calendar.events.readonly openid email",
                     "blurb": "Kauli joins your Zoom, Meet and Teams calls on its own and sends you the notes.", "settings": _CAL, "calendar": True}
PROVIDERS["outlook"] = {**PROVIDERS["onedrive"], "name": "Outlook Calendar", "scopes": "Calendars.Read offline_access User.Read",
                        "extra": {"response_mode": "query", "prompt": "select_account"},
                        "blurb": "Kauli joins your Zoom, Meet and Teams calls on its own and sends you the notes.", "settings": _CAL, "calendar": True}
GOOGLE_PROVIDERS = tuple(k for k, v in PROVIDERS.items() if v.get("family") == "google")


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


def setup_status(base: str) -> list[dict]:
    """For Staff > Integrations: one row per developer account Kauli has to register (Google's one account serves three cards), with
    the exact address each provider asks for and whether the keys are in place."""
    groups: dict[str, dict] = {}
    for key, spec in PROVIDERS.items():
        g = groups.setdefault(spec["id_env"], {"id_env": spec["id_env"], "secret_env": spec["secret_env"], "console": spec["console"],
                                               "console_name": spec["console_name"], "names": [], "redirects": [], "scopes": [],
                                               "ready": configured(key)})
        g["names"].append(spec["name"])
        g["redirects"].append(f"{base}/client/integrations/{key}/callback")
        if spec["scopes"]:
            g["scopes"].append(spec["scopes"])
    return [{**g, "title": " / ".join(g["names"]), "scopes": " ".join(dict.fromkeys(" ".join(g["scopes"]).split()))} for g in groups.values()]


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
              "state": state, **spec["extra"]}
    if spec["scopes"]:
        params["scope"] = spec["scopes"]
    return RedirectResponse(spec["auth_url"] + "?" + urlencode(params), status_code=303)


def _token_request(provider: str, form: dict):
    """The provider's token address, asked the way that provider wants it asked."""
    spec = PROVIDERS[provider]
    cid, secret = os.environ[spec["id_env"]], os.environ[spec["secret_env"]]
    if spec.get("basic_auth"):
        return httpx.post(spec["token_url"], json=form, auth=(cid, secret), headers={"Accept": "application/json"}, timeout=20)
    return httpx.post(spec["token_url"], data={**form, "client_id": cid, "client_secret": secret}, timeout=20)


def _exchange(provider: str, request: Request, code: str) -> dict:
    """Trade the one-off code for access. Returns {token, refresh, expires, target, label}."""
    r = _token_request(provider, {"code": code, "redirect_uri": _redirect_uri(request, provider), "grant_type": "authorization_code"})
    data = r.json()
    out = {"token": None, "refresh": None, "expires": None, "target": None, "label": ""}
    if provider == "slack":
        if not data.get("ok"):
            raise ValueError(data.get("error", "slack refused"))
        hook = data.get("incoming_webhook") or {}
        out.update(target=hook.get("url"), label=(hook.get("channel") or "") + (f" in {data['team']['name']}" if data.get("team") else ""))
        return out
    r.raise_for_status()
    if provider == "discord":
        hook = data.get("webhook") or {}
        if not hook.get("url"):
            raise ValueError("discord gave no channel")
        out.update(target=hook["url"], label=hook.get("name") or "Discord channel")
        return out
    token = data["access_token"]
    out.update(token=token, refresh=data.get("refresh_token"))
    if data.get("expires_in"):
        out["expires"] = time.time() + int(data["expires_in"]) - 60
    if provider in GOOGLE_PROVIDERS:
        out["label"] = _google_email(token)
    elif provider == "notion":
        out.update(label=data.get("workspace_name") or "", target=_notion_first_page(token))
    elif provider == "dropbox":
        out["label"] = _dropbox_label(token)
    elif provider in ("onedrive", "outlook"):
        out["label"] = _graph_label(token)
    return out


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
        got = _exchange(provider, request, code)
    except Exception:  # noqa: BLE001 - any provider trouble ends as a clear message in the window, never an error page
        return _popup("Could not connect", "The service did not accept the connection. Please try again.", False, provider)
    conn = db.get_conn()
    prior = conn.execute("SELECT settings_json, refresh_enc FROM integration_connections WHERE client_id = ? AND provider = ?", (client_id, provider)).fetchone()
    conn.execute("""INSERT OR REPLACE INTO integration_connections
        (client_id, provider, token_enc, refresh_enc, expires_at, account_label, target_enc, settings_json, connected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (client_id, provider, _enc(got["token"]), _enc(got["refresh"]) or (prior["refresh_enc"] if prior else None), got["expires"], got["label"],
                  _enc(got["target"]), prior["settings_json"] if prior else "{}", time.time()))
    conn.commit()
    conn.close()
    return _popup("Connection Successful!", f"{spec['name']} is connected. You can close this window.", True, provider)


def _google_email(token: str) -> str:
    try:
        return httpx.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {token}"}, timeout=10).json().get("email", "")
    except Exception:  # noqa: BLE001
        return ""


def _dropbox_label(token: str) -> str:
    try:
        return httpx.post("https://api.dropboxapi.com/2/users/get_current_account", headers={"Authorization": f"Bearer {token}"}, timeout=10).json().get("email", "")
    except Exception:  # noqa: BLE001
        return ""


def _graph_label(token: str) -> str:
    try:
        me = httpx.get("https://graph.microsoft.com/v1.0/me", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        return me.get("mail") or me.get("userPrincipalName") or ""
    except Exception:  # noqa: BLE001
        return ""


def _notion_first_page(token: str) -> str | None:
    """Notion lets the customer tick the pages they share on its own consent screen: new transcripts go inside the first of them."""
    from . import notion_export
    try:
        r = httpx.post("https://api.notion.com/v1/search", headers={"Authorization": f"Bearer {token}", "Notion-Version": notion_export.NOTION_VERSION},
                       json={"filter": {"property": "object", "value": "page"}, "page_size": 1}, timeout=15)
        results = r.json().get("results") or []
        return results[0]["id"] if results else None
    except Exception:  # noqa: BLE001
        return None


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
    if row and provider in GOOGLE_PROVIDERS:                         # tell Google too, so the access really ends
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
def _access_token(row) -> str | None:
    """A working access token, refreshed when the saved one has run out. Tokens that never expire (Notion) are used as they are."""
    token = _dec(row["token_enc"])
    if token and (not row["expires_at"] or row["expires_at"] > time.time()):
        return token
    refresh = _dec(row["refresh_enc"])
    if not refresh:
        return None
    provider = row["provider"]
    r = _token_request(provider, {"refresh_token": refresh, "grant_type": "refresh_token"})
    r.raise_for_status()
    data = r.json()
    conn = db.get_conn()
    conn.execute("UPDATE integration_connections SET token_enc = ?, expires_at = ?, refresh_enc = COALESCE(?, refresh_enc) WHERE client_id = ? AND provider = ?",
                 (_enc(data["access_token"]), time.time() + int(data.get("expires_in", 3600)) - 60, _enc(data.get("refresh_token")), row["client_id"], provider))
    conn.commit()
    conn.close()
    return data["access_token"]


_google_token = _access_token                                           # the name earlier code and tests know


def _transcript_lines(order, settings: dict) -> list[str]:
    from . import transcript_export
    job = A._load_job(order)
    if not job:
        return []
    return transcript_export.paragraphs(job, timestamps=settings.get("timestamps", True), speakers=settings.get("speakers", True))


def _document(order, settings: dict, link: str) -> tuple[str, str]:
    """(title, html) for the Google Doc: what the customer's switches asked for."""
    title = order["original_filename"] or "Kauli transcript"
    parts = [f"<h1>{html.escape(title)}</h1>"]
    if settings.get("transcript_link") and link:
        parts.append(f'<p><a href="{html.escape(link)}">Open this transcript in Kauli</a></p>')
    if settings.get("summary") and order["ai_summary_text"]:
        parts.append("<h2>Summary</h2>" + "".join(f"<p>{html.escape(p)}</p>" for p in order["ai_summary_text"].split("\n") if p.strip()))
    lines = _transcript_lines(order, settings)
    if lines:
        parts.append("<h2>Transcript</h2>")
        parts.extend(f"<p>{html.escape(line)}</p>" for line in lines)
    return title, "".join(parts)


def _plain_text(order, settings: dict, link: str) -> tuple[str, str]:
    """(file name, text) for the services that keep files (Drive, Dropbox, OneDrive)."""
    title = order["original_filename"] or "Kauli transcript"
    parts = [title]
    if settings.get("transcript_link") and link:
        parts.append(f"Open in Kauli: {link}")
    if settings.get("summary") and order["ai_summary_text"]:
        parts.append("Summary\n" + order["ai_summary_text"].strip())
    lines = _transcript_lines(order, settings)
    if lines:
        parts.append("Transcript\n" + "\n\n".join(lines))
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "-", title).strip(" .-")[:80] or "transcript"
    return f"{safe} (Kauli).txt", "\n\n".join(parts)


def _make_google_doc(row, title: str, body_html: str) -> str:
    token = _access_token(row)
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


def _save_target(row, value: str) -> None:
    conn = db.get_conn()
    conn.execute("UPDATE integration_connections SET target_enc = ? WHERE client_id = ? AND provider = ?", (_enc(value), row["client_id"], row["provider"]))
    conn.commit()
    conn.close()


def _drive_create(token: str, meta: dict) -> str:
    r = httpx.post("https://www.googleapis.com/drive/v3/files?fields=id", json=meta, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def _send_gdocs(row, order, settings, link):
    title, body = _document(order, settings, link)
    _make_google_doc(row, title, body)


def _send_gdrive(row, order, settings, link):
    token = _access_token(row)
    if not token:
        raise RuntimeError("Google access expired: reconnect Google Drive")
    folder = _dec(row["target_enc"])
    if not folder:
        folder = _drive_create(token, {"name": "Kauli", "mimeType": "application/vnd.google-apps.folder"})
        _save_target(row, folder)
    name, text = _plain_text(order, settings, link)
    boundary = "kauli" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "parents": [folder]})
    payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
               f"--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n{text}\r\n--{boundary}--").encode("utf-8")
    httpx.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart", content=payload,
               headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"}, timeout=60).raise_for_status()


def _send_gsheets(row, order, settings, link):
    token = _access_token(row)
    if not token:
        raise RuntimeError("Google access expired: reconnect Google Sheets")
    cells = ["Date", "Title", "Link", "Summary", "Transcript"]
    fresh = False
    sheet = _dec(row["target_enc"])
    if not sheet:
        sheet = _drive_create(token, {"name": "Kauli transcripts", "mimeType": "application/vnd.google-apps.spreadsheet"})
        _save_target(row, sheet)
        fresh = True
    lines = _transcript_lines(order, settings) if settings.get("transcript") else []
    values = [time.strftime("%Y-%m-%d %H:%M", time.gmtime()), order["original_filename"] or "", link if settings.get("transcript_link") else "",
              (order["ai_summary_text"] or "")[:5000] if settings.get("summary") else "", "\n".join(lines)[:45000]]
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet}/values/A1:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    r = httpx.post(url, json={"values": ([cells] if fresh else []) + [values]}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 404 and not fresh:                              # they deleted the sheet: start a new one
        _save_target(row, "")
        row = get_connection(row["client_id"], "gsheets")
        return _send_gsheets(row, order, settings, link)
    r.raise_for_status()


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


def _send_slack(row, order, settings, link):
    _post_slack(row, order, settings, link)


def _send_discord(row, order, settings, link):
    url = _dec(row["target_enc"])
    if not url:
        raise RuntimeError("Discord channel missing: reconnect Discord")
    text = f"✅ **{order['original_filename']}** is ready."
    if settings.get("summary") and order["ai_summary_text"]:
        text += "\n" + order["ai_summary_text"][:1200]
    if settings.get("transcript_link") and link:
        text += f"\n{link}"
    httpx.post(url, json={"content": text[:1990]}, timeout=15).raise_for_status()


def _send_notion(row, order, settings, link):
    from . import notion_export
    token = _access_token(row)
    parent = _dec(row["target_enc"]) or (_notion_first_page(token) if token else None)
    if not token or not parent:
        raise RuntimeError("Notion has no page to put transcripts in: reconnect Notion and tick a page to share")
    _save_target(row, parent)
    summary = order["ai_summary_text"] if settings.get("summary") and order["ai_summary_text"] else ""
    blocks = notion_export.build_blocks(summary, link if settings.get("transcript_link") else "https://kauli-forgemedia.com", _transcript_lines(order, settings), False)
    notion_export.create_page(token, parent, "page", "title", order["original_filename"] or "Kauli transcript", blocks)


def _send_dropbox(row, order, settings, link):
    token = _access_token(row)
    if not token:
        raise RuntimeError("Dropbox access expired: reconnect Dropbox")
    name, text = _plain_text(order, settings, link)
    arg = json.dumps({"path": "/" + name, "mode": "add", "autorename": True, "mute": True})
    httpx.post("https://content.dropboxapi.com/2/files/upload", content=text.encode("utf-8"),
               headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": arg.encode("ascii", "backslashreplace").decode(),
                        "Content-Type": "application/octet-stream"}, timeout=60).raise_for_status()


def _send_onedrive(row, order, settings, link):
    token = _access_token(row)
    if not token:
        raise RuntimeError("OneDrive access expired: reconnect OneDrive")
    name, text = _plain_text(order, settings, link)
    httpx.put(f"https://graph.microsoft.com/v1.0/me/drive/special/approot:/{quote(name)}:/content", content=text.encode("utf-8"),
              headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain; charset=utf-8"}, timeout=60).raise_for_status()


_SEND = {"gdocs": _send_gdocs, "gsheets": _send_gsheets, "gdrive": _send_gdrive, "slack": _send_slack, "discord": _send_discord,
         "notion": _send_notion, "dropbox": _send_dropbox, "onedrive": _send_onedrive}


def deliver_order_ready(order, base_url: str) -> None:
    """Send a finished order to every connected service that is set to receive it. A failure is logged, never raised: it must not
    disturb the approval it hangs off."""
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM integration_connections WHERE client_id = ?", (order["client_id"],)).fetchall()
    conn.close()
    link = f"{base_url.rstrip('/')}/client/orders/{order['id']}"
    for row in rows:
        if row["provider"] not in _SEND:                                 # calendars send Kauli to meetings; they receive nothing
            continue
        settings = settings_of(row)
        if not settings.get("auto"):
            continue
        try:
            _SEND[row["provider"]](row, order, settings, link)
            db.log_webhook_delivery(order["client_id"], order["id"], f"{row['provider']}.order.ready", True, 200)
        except Exception as exc:  # noqa: BLE001
            db.log_webhook_delivery(order["client_id"], order["id"], f"{row['provider']}.order.ready", False, None, str(exc)[:300])


def deliver_in_background(order, base_url: str) -> None:
    threading.Thread(target=deliver_order_ready, args=(order, base_url), daemon=True).start()
