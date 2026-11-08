"""Import a recording straight from Google Drive, OneDrive or Dropbox.

The customer picks the file in that service's own file picker (in the browser, so Kauli never sees their account password and never gets
access to anything but the file they chose). The browser then hands Kauli a one-off download address (or, for Google, a one-off token
for that one file), the server downloads it into a private staging folder, and the normal upload checks and order flow take over from
there (see app._resolve_uploaded_audio, which accepts the "import:" keys made here).

Each provider is inert until its keys are in .env:
    DROPBOX_APP_KEY                      Dropbox App Console > the app's App key (Chooser, with kauli-forgemedia.com in its domains)
    GOOGLE_PICKER_API_KEY                Google Cloud > Credentials > API key (Picker API enabled)
    GOOGLE_OAUTH_CLIENT_ID               a Web OAuth client with kauli-forgemedia.com as an authorised JavaScript origin
    GOOGLE_PICKER_APP_ID                 the Google Cloud project NUMBER
    ONEDRIVE_CLIENT_ID                   an Azure app registration (Application/client ID) with the site as a redirect URI
Setup steps are in marketing/cloud-import-setup.md.

Without any keys, a customer can still paste a share link ("Anyone with the link") from any of the three: see
cloud_links.py for how the link becomes a download address, and import_link below.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import cloud_links, db, rate_limit, upload_security

router = APIRouter()
A = None
MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".m4v", ".3gp"}
# where a download address may point: the service's own file hosts and nothing else, so the server can never be made to fetch an internal address
ALLOWED_HOSTS = {
    "dropbox": (".dropbox.com", ".dropboxusercontent.com"),
    "onedrive": (".sharepoint.com", ".1drv.com", ".1drv.ms", ".live.com", ".microsoftpersonalcontent.com", ".onedrive.com"),
    "gdrive": (".drive.google.com", ".usercontent.google.com", ".googleusercontent.com"),      # a public Drive link
}
KEEP_SECONDS = 24 * 3600


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


def config() -> dict:
    """What the browser needs to show the pickers. Only providers that are fully set up appear."""
    out: dict = {}
    if os.environ.get("DROPBOX_APP_KEY"):
        out["dropbox"] = {"app_key": os.environ["DROPBOX_APP_KEY"]}
    g = {"api_key": os.environ.get("GOOGLE_PICKER_API_KEY", ""), "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "") or os.environ.get("GOOGLE_ONE_TAP_CLIENT_ID", ""),
         "app_id": os.environ.get("GOOGLE_PICKER_APP_ID", "")}
    if all(g.values()):
        out["google"] = g
    if os.environ.get("ONEDRIVE_CLIENT_ID"):
        out["onedrive"] = {"client_id": os.environ["ONEDRIVE_CLIENT_ID"]}
    return out


def imports_dir(client_id: str) -> Path:
    d = db.DB_PATH.parent / "imports" / re.sub(r"[^A-Za-z0-9_-]", "", client_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _purge(d: Path) -> None:
    cutoff = time.time() - KEEP_SECONDS
    for f in d.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "recording").strip())[:120].strip(" .") or "recording"
    return name


def host_allowed(provider: str, url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == s[1:] or host.endswith(s) for s in ALLOWED_HOSTS.get(provider, ()))


def staged_file(upload_key: str, client_id: str) -> Path | None:
    """The file behind an "import:" key, if it belongs to this client and is still there."""
    m = re.fullmatch(r"import:([0-9a-f]{32}):(.+)", upload_key or "")
    if not m:
        return None
    matches = list(imports_dir(client_id).glob(f"{m.group(1)}.*"))
    return matches[0] if len(matches) == 1 and matches[0].is_file() else None


def display_name(upload_key: str) -> str:
    return upload_key.split(":", 2)[2] if upload_key.count(":") >= 2 else "recording"


def _download(url: str, dest: Path, headers: dict | None = None, provider: str | None = None, limit: int | None = None,
              meta: dict | None = None) -> int:
    """Streams a file to disk with a size ceiling. Redirects are followed by hand and must stay on the provider's own hosts.

    When `meta` is given it is filled with the content type and the announced file name of the final response."""
    limit = limit or upload_security.MAX_UPLOAD_BYTES
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, read=120.0)) as client:
        for _ in range(4):
            with client.stream("GET", url, headers=headers or {}) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = str(httpx.URL(url).join(r.headers.get("location", "")))
                    if provider and not host_allowed(provider, url):
                        raise ValueError("The file's address leads somewhere that is not allowed.")
                    continue
                if r.status_code != 200:
                    raise ValueError(f"The provider refused the download ({r.status_code}). Pick the file again.")
                if meta is not None:
                    meta["type"] = r.headers.get("content-type", "")
                    meta["disposition"] = r.headers.get("content-disposition", "")
                size = int(r.headers.get("content-length") or 0)
                if size and size > limit:
                    raise ValueError(f"That file is larger than the {limit // (1024 * 1024 * 1024)} GB limit.")
                total = 0
                with open(dest, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        total += len(chunk)
                        if total > limit:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise ValueError(f"That file is larger than the {limit // (1024 * 1024 * 1024)} GB limit.")
                        fh.write(chunk)
                return total
    raise ValueError("Too many redirects.")


def _google_info(file_id: str, token: str) -> dict:
    r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", params={"fields": "name,size,mimeType"},
                  headers={"Authorization": f"Bearer {token}"}, timeout=20, follow_redirects=False)
    if r.status_code != 200:
        raise ValueError("Google Drive would not share that file. Pick it again and allow access.")
    return r.json()


@router.get("/account/import/config")
def import_config(request: Request):
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    return JSONResponse(config())


@router.post("/account/import/fetch")
async def import_fetch(request: Request):
    """Body: {"provider": "dropbox"|"onedrive", "url", "name"} or {"provider": "google", "file_id", "token", "name"}. Returns {upload_key, filename, size}."""
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    provider = str(body.get("provider") or "")
    if provider not in config():
        return JSONResponse({"error": "That import is not switched on yet."}, status_code=400)
    allowed, retry = rate_limit.check(f"import:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return JSONResponse({"error": f"Too many imports. Try again in {retry}s."}, status_code=429)
    cid = user["client_scope_id"]
    headers = None
    try:
        if provider == "google":
            file_id, token = str(body.get("file_id") or ""), str(body.get("token") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", file_id) or not token:
                return JSONResponse({"error": "Pick the file again."}, status_code=400)
            info = _google_info(file_id, token)
            if int(info.get("size") or 0) > upload_security.MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "That file is larger than the 2 GB limit."}, status_code=400)
            name = safe_name(info.get("name") or body.get("name"))
            url, headers = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", {"Authorization": f"Bearer {token}"}
        else:
            url, name = str(body.get("url") or ""), safe_name(body.get("name"))
            if not host_allowed(provider, url):
                return JSONResponse({"error": "That file's address is not one we can import from."}, status_code=400)
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA_SUFFIXES:
            return JSONResponse({"error": "That is not an audio or video file we can transcribe."}, status_code=400)
        d = imports_dir(cid)
        _purge(d)
        token_id = uuid.uuid4().hex
        dest = d / f"{token_id}{suffix}"
        size = _download(url, dest, headers, None if provider == "google" else provider)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except httpx.HTTPError:
        return JSONResponse({"error": "Could not download that file. Check your connection and try again."}, status_code=502)
    A.account_hub.track_feature(request, "cloud_import", provider=provider)
    return JSONResponse({"ok": True, "upload_key": f"import:{token_id}:{name}", "filename": name, "size": size})


DOWNLOAD_FAILED = "Could not download that file. Check your connection and try again."
LINK_HELP = 'That link did not work. Set sharing to "Anyone with the link", copy the link again and paste it here.'


def _link_file_name(hint: str, meta: dict) -> str:
    """The name to keep the file under: the server's header or the link, and the content type if it has no suffix."""
    kind = (meta.get("type") or "").lower()
    if kind.startswith(("text/", "application/json")):
        raise ValueError(LINK_HELP)                                     # a sign-in or error page, not the file
    name = safe_name(cloud_links.name_from_headers(meta.get("disposition"), kind) or hint)
    if Path(name).suffix.lower() not in MEDIA_SUFFIXES:
        suffix = cloud_links.suffix_from_type(kind)
        if not suffix:
            raise ValueError("That is not an audio or video file we can transcribe.")
        name = Path(name).stem + suffix
    return name


def _stage_link(client_id: str, found: cloud_links.Resolved) -> dict:
    """Download the file behind a share link into the private staging folder. ValueError carries a message to show."""
    d = imports_dir(client_id)
    _purge(d)
    token_id = uuid.uuid4().hex
    part, meta = d / f"{token_id}.part", {}
    try:
        size = _download(found.url, part, None, found.provider, meta=meta)
        name = _link_file_name(found.name, meta)
        part.rename(d / f"{token_id}{Path(name).suffix.lower()}")
    except ValueError as exc:
        part.unlink(missing_ok=True)
        refused = str(exc).startswith(("The provider refused", "The file's address leads"))
        raise ValueError(LINK_HELP if refused else str(exc)) from exc
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return {"ok": True, "upload_key": f"import:{token_id}:{name}", "filename": name, "size": size}


@router.post("/account/import/link")
async def import_link(request: Request):
    """Body: {"url": a Google Drive, OneDrive or Dropbox share link}. Returns {upload_key, filename, size}. No keys."""
    user = A.current_user(request)
    if not user or user["role"] != "client":
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    found = cloud_links.resolve(str(body.get("url") or ""))
    if not found:
        return JSONResponse({"error": "Paste a share link from Google Drive, OneDrive or Dropbox."}, status_code=400)
    allowed, retry = rate_limit.check(f"import:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return JSONResponse({"error": f"Too many imports. Try again in {retry}s."}, status_code=429)
    try:
        staged = _stage_link(user["client_scope_id"], found)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except httpx.HTTPError:
        return JSONResponse({"error": DOWNLOAD_FAILED}, status_code=502)
    A.account_hub.track_feature(request, "cloud_import", provider="link")
    return JSONResponse(staged)
