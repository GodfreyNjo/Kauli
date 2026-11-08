"""Where you are signed in: a list of devices, and the power to sign any of them out.

The sign-in cookie is signed but carries no state of its own, so on its own it cannot be cancelled from the server.
So every sign-in is also a row in `user_sessions`, and every request checks that its row has not been revoked.
That is what makes "sign out of all other devices" real: the next request from any other device finds its row
revoked and is signed out.

  sign_in / sign_out   what login and logout call: set or empty the cookie and keep the record in step
  start            record a new sign-in (sign_in calls it)
  is_active        True while this request's sign-in has not been revoked; also adopts sign-ins that began
                   before this feature existed, so nobody is signed out by its arrival
  end              revoke the sign-in of this request (called when a person signs out)
  list_for         a person's sign-ins, newest first, for the "where you are signed in" page
  revoke / revoke_others   sign one device, or every device but the current one, out

Only what a person would recognise is kept: the device (browser and system), the country and a shortened address.
"""
from __future__ import annotations

import re
import secrets
import time

from fastapi import Request

from . import db

TOUCH_EVERY_SECONDS = 300
KEEP_DAYS = 90

_TABLE = """
CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,           -- also stored in the person's cookie as "sid"
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ip TEXT,
    country TEXT,
    user_agent TEXT,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions (user_id, revoked_at);
"""

BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("Firefox", "Firefox"), ("Chrome", "Chrome"), ("Safari", "Safari"))
SYSTEMS = (("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
           ("Mac OS X", "Mac"), ("Linux", "Linux"))


def _connection():
    conn = db.get_conn()
    conn.executescript(_TABLE)
    return conn


def describe_device(user_agent: str | None) -> str:
    """A person-friendly name for a browser: 'Chrome on Windows'."""
    agent = user_agent or ""
    browser = next((name for marker, name in BROWSERS if marker in agent), "A browser")
    system = next((name for marker, name in SYSTEMS if marker in agent), "")
    return f"{browser} on {system}" if system else browser


def shorten_ip(ip: str | None) -> str:
    """41.90.12.34 -> 41.90.x.x, so the page recognises a place without publishing an exact address."""
    if not ip:
        return ""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
        return ".".join(ip.split(".")[:2]) + ".x.x"
    return ip.split(":")[0] + ":x"


def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else ""))


def start(request: Request, user_id: str) -> str:
    """Record a new sign-in and remember its id in the cookie. Returns the id."""
    sid = secrets.token_urlsafe(16)
    now = time.time()
    conn = _connection()
    conn.execute("INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, ip, country, user_agent) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (sid, user_id, now, now, _client_address(request), request.headers.get("cf-ipcountry"),
                  (request.headers.get("user-agent") or "")[:300]))
    conn.execute("DELETE FROM user_sessions WHERE last_seen_at < ?", (now - KEEP_DAYS * 86400,))
    conn.commit()
    conn.close()
    request.session["sid"] = sid
    return sid


def sign_in(request: Request, user_id: str) -> None:
    """Sign the person in on this browser: the cookie names them, and the sign-in is recorded."""
    request.session.pop("sid", None)
    request.session["user_id"] = user_id
    start(request, user_id)


def sign_out(request: Request) -> None:
    """Revoke this sign-in and empty the cookie."""
    end(request)
    request.session.clear()


def is_active(request: Request, user_id: str) -> bool:
    """False when this request's sign-in was revoked or belongs to someone else. Checked once per request."""
    cached = getattr(request.state, "session_active", None)
    if cached is not None:
        return cached
    conn = _connection()
    row = conn.execute("SELECT * FROM user_sessions WHERE id = ?", (request.session.get("sid", ""),)).fetchone()
    conn.close()
    if row is None:
        start(request, user_id)                      # a sign-in from before this feature: adopt it
        active = True
    else:
        active = row["user_id"] == user_id and row["revoked_at"] is None
        if active and time.time() - row["last_seen_at"] > TOUCH_EVERY_SECONDS:
            _touch(row["id"], request)
    request.state.session_active = active
    if not active:
        request.session.clear()
    return active


def _touch(sid: str, request: Request) -> None:
    conn = _connection()
    conn.execute("UPDATE user_sessions SET last_seen_at = ?, ip = ?, country = COALESCE(?, country) WHERE id = ?",
                 (time.time(), _client_address(request), request.headers.get("cf-ipcountry"), sid))
    conn.commit()
    conn.close()


def end(request: Request) -> None:
    """Revoke the sign-in of this request (a person pressed Sign out)."""
    sid = request.session.get("sid")
    if sid:
        conn = _connection()
        conn.execute("UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), sid))
        conn.commit()
        conn.close()


def list_for(user_id: str, current_sid: str | None) -> list[dict]:
    """The person's active sign-ins, newest activity first, each marked if it is this device."""
    conn = _connection()
    rows = conn.execute("SELECT * FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL "
                        "ORDER BY last_seen_at DESC", (user_id,)).fetchall()
    conn.close()
    return [{"id": r["id"], "device": describe_device(r["user_agent"]), "country": r["country"] or "",
             "address": shorten_ip(r["ip"]), "signed_in_at": r["created_at"], "last_seen_at": r["last_seen_at"],
             "is_current": r["id"] == current_sid} for r in rows]


def revoke(user_id: str, sid: str) -> bool:
    """Sign one device out. Only the owner's own sign-ins can be revoked."""
    conn = _connection()
    cursor = conn.execute("UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                          (time.time(), sid, user_id))
    conn.commit()
    conn.close()
    return cursor.rowcount == 1


def revoke_others(user_id: str, current_sid: str | None) -> int:
    """Sign out every device except this one. Returns how many were signed out."""
    conn = _connection()
    cursor = conn.execute(
        "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND id != ?",
        (time.time(), user_id, current_sid or ""))
    conn.commit()
    conn.close()
    return cursor.rowcount
