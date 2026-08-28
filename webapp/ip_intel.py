"""Real, free datacenter/hosting-IP detection - a legitimate, honest
signal that a signup or login is coming from a cloud server rather than
a residential connection, without a paid IP-reputation API/Redis cache
this project has no account or need for (see rate_limit.py's own header
comment on why an in-process cache is the right call for a single-
instance app, not a distributed one).

Uses AWS's and Google Cloud's own published IP-range feeds - real,
official, no signup needed, refreshed at most twice a day since real
ranges change rarely. Deliberately narrow about what this actually
proves: it catches traffic FROM those two clouds (a real, common pattern
for automated/bot signups, and for some commercial VPN providers that
rent cloud capacity), not residential proxies or most consumer VPN
services, which aren't cloud-hosted the same way. That's a real, honest
gap, not a claim this is comprehensive VPN detection - see app.py/
staff_clients.html for how the result is actually used: a visible flag
for a human to weigh, never an automatic block. A real, legitimate
diaspora client on a VPN or a corporate network must never be silently
turned away by this on its own.
"""
from __future__ import annotations

import ipaddress
import threading
import time

import httpx

_RANGE_SOURCES = {
    "aws": "https://ip-ranges.amazonaws.com/ip-ranges.json",
    "gcp": "https://www.gstatic.com/ipranges/cloud.json",
}
_REFRESH_INTERVAL_S = 12 * 3600

_lock = threading.Lock()
_networks: list = []
_last_refreshed = 0.0


def _fetch_ranges() -> list:
    networks: list = []
    try:
        aws = httpx.get(_RANGE_SOURCES["aws"], timeout=10).json()
        for prefix in aws.get("prefixes", []):
            try:
                networks.append(ipaddress.ip_network(prefix["ip_prefix"]))
            except (ValueError, KeyError):
                continue
    except Exception:  # noqa: BLE001 - one source failing must never block the other
        pass
    try:
        gcp = httpx.get(_RANGE_SOURCES["gcp"], timeout=10).json()
        for entry in gcp.get("prefixes", []):
            cidr = entry.get("ipv4Prefix") or entry.get("ipv6Prefix")
            if not cidr:
                continue
            try:
                networks.append(ipaddress.ip_network(cidr))
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return networks


def _ensure_fresh() -> None:
    global _networks, _last_refreshed
    now = time.time()
    with _lock:
        already_fresh = _networks and (now - _last_refreshed < _REFRESH_INTERVAL_S)
    if already_fresh:
        return
    fresh = _fetch_ranges()  # real network calls - never holds _lock while this runs
    if fresh:
        with _lock:
            _networks = fresh
            _last_refreshed = now
    # A failed fetch with no prior data just leaves _networks empty - the
    # next real call (is_datacenter_ip, or the next request that reaches
    # here) tries again rather than caching a permanent failure.


def is_datacenter_ip(ip: str) -> bool | None:
    """True/False when real range data is available, None when it isn't
    (network hiccup, first call not yet resolved) - an unknown IP is
    never treated as True just because this feature is degraded. Meant
    to be called from a background thread (see app.py's callers) - the
    first-ever call in a process does a real, synchronous network fetch,
    which must never block an actual signup/login response."""
    _ensure_fresh()
    with _lock:
        ranges = list(_networks)
    if not ranges:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return any(addr in net for net in ranges)
