"""SSRF guard for presigned document URLs (V13). Only allow-listed ``host:port`` over http(s); never follow
redirects; refuse IP literals (v4/v6/decimal/hex/octal forms), ``localhost`` unless explicitly allow-listed,
credentials in the URL and non-http schemes."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


class UrlNotAllowed(ValueError):
    pass


_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$", re.I)


def parse_allow_list(raw: str) -> set[str]:
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_ip_like(host: str) -> bool:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return bool(_NUMERIC_HOST.match(h)) or ":" in h


def assert_allowed_host(url: str, allow: set[str]) -> None:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlNotAllowed("malformed url") from exc
    if parts.scheme not in ("http", "https"):
        raise UrlNotAllowed(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise UrlNotAllowed("credentials in url not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlNotAllowed("missing host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlNotAllowed("invalid port") from exc
    hostport = f"{host}:{port}" if port else host
    allowed = hostport in allow or (port is None and host in allow)
    if not allowed:
        raise UrlNotAllowed(f"host {hostport!r} not in allow-list")
    if _is_ip_like(host) and host not in {h.split(":")[0] for h in allow}:
        raise UrlNotAllowed("ip literals are not allowed")
