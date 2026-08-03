from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit


def allowed_cors_origin(headers: dict) -> str:
    normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    origin = normalized.get("origin", "").strip()
    if not origin:
        return ""
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return ""
        is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        if not is_loopback:
            return ""
        _ = parsed.port
    except ValueError:
        return ""
    return origin


def send_cors_headers(handler: Any) -> None:
    headers = getattr(handler, "headers", {}) or {}
    origin = allowed_cors_origin({k: v for k, v in headers.items()})
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
