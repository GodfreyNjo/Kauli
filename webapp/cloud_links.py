"""Turn a share link from Google Drive, OneDrive or Dropbox into an address the server can download. No API keys needed.

The customer sets the file to "Anyone with the link", copies the link and pastes it. This module only decides what the
download address is, and refuses everything that is not a link on those services' own hosts, so the server can never be
made to fetch some other address. The download itself, with its size limit and its check on every redirect, is in
cloud_import.py.

  resolve(link)                      -> Resolved(provider, url, name) or None when the link is not one we accept
  name_from_headers / suffix_from_type   what to call the file when the link itself does not say
"""
from __future__ import annotations

import base64
import re
from typing import NamedTuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

GOOGLE_ID = re.compile(r"[A-Za-z0-9_-]{10,100}")
ONEDRIVE_HOSTS = ("1drv.ms", "onedrive.live.com", "1drv.com")
TYPE_SUFFIX = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/flac": ".flac", "audio/ogg": ".ogg", "audio/aac": ".aac", "audio/webm": ".webm",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/mpeg": ".mpeg", "video/x-msvideo": ".avi",
}


class Resolved(NamedTuple):
    provider: str          # "dropbox", "gdrive" or "onedrive": the key of the allowed hosts in cloud_import.py
    url: str               # the address to download from
    name: str              # a file name taken from the link, or "" when the link does not say


def _last_segment(path: str) -> str:
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return name if "." in name else ""


def _dropbox(parts) -> Resolved:
    query = dict(parse_qsl(parts.query))
    query.pop("raw", None)
    query["dl"] = "1"
    url = urlunparse(("https", parts.hostname, parts.path, "", urlencode(query), ""))
    return Resolved("dropbox", url, _last_segment(parts.path))


def _google_drive(parts) -> Resolved | None:
    found = re.search(r"/file/d/([^/]+)", parts.path)
    file_id = found.group(1) if found else dict(parse_qsl(parts.query)).get("id", "")
    if not GOOGLE_ID.fullmatch(file_id):
        return None
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    return Resolved("gdrive", url, "")


def _onedrive(link: str, parts) -> Resolved:
    if parts.hostname.endswith(".sharepoint.com"):
        query = dict(parse_qsl(parts.query))
        query["download"] = "1"
        url = urlunparse(("https", parts.hostname, parts.path, "", urlencode(query), ""))
        return Resolved("onedrive", url, _last_segment(parts.path))
    token = base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
    return Resolved("onedrive", f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content", "")


def resolve(link: str) -> Resolved | None:
    """The download address for a share link, or None when it is not a link on Google Drive, OneDrive or Dropbox."""
    link = (link or "").strip()
    parts = urlparse(link)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or len(link) > 2000:
        return None
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        return _dropbox(parts)
    if host == "drive.google.com":
        return _google_drive(parts)
    if host in ONEDRIVE_HOSTS or host.endswith(".sharepoint.com"):
        return _onedrive(link, parts)
    return None


def name_from_headers(disposition: str | None, content_type: str | None = None) -> str:
    """The file name a server announces in Content-Disposition, or "" when it does not announce one."""
    text = disposition or ""
    star = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", text)
    if star:
        return unquote(star.group(1)).strip()
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', text)
    return plain.group(1).strip() if plain else ""


def suffix_from_type(content_type: str | None) -> str:
    """".mp3" for audio/mpeg and so on, or "" when the type is not audio or video we know."""
    return TYPE_SUFFIX.get((content_type or "").split(";")[0].strip().lower(), "")
