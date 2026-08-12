from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit


DEFAULT_REQUEST_LIMIT = 4 * 1024 * 1024
UPLOAD_REQUEST_LIMIT = 384 * 1024 * 1024
SMALL_UPLOAD_REQUEST_LIMIT = 48 * 1024 * 1024


def _positive_env_bytes(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


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


def request_origin_allowed(headers: dict) -> bool:
    """Allow non-browser clients, loopback UIs, and exact same-origin requests."""
    normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    origin = normalized.get("origin", "").strip()
    if not origin:
        return True
    if allowed_cors_origin(normalized):
        return True
    try:
        parsed = urlsplit(origin)
        host = normalized.get("host", "").strip().lower()
        return (
            parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and bool(host)
            and parsed.netloc.lower() == host
        )
    except ValueError:
        return False


def request_body_limit(path: str) -> int:
    clean = str(path or "").split("?", 1)[0]
    if clean == "/api/edit/upload":
        return _positive_env_bytes("SCENEFORGE_MAX_VIDEO_UPLOAD_BYTES", UPLOAD_REQUEST_LIMIT)
    if clean in {"/api/bgm/upload", "/api/sfx/upload"}:
        return _positive_env_bytes("SCENEFORGE_MAX_AUDIO_UPLOAD_BYTES", SMALL_UPLOAD_REQUEST_LIMIT)
    return _positive_env_bytes("SCENEFORGE_MAX_REQUEST_BYTES", DEFAULT_REQUEST_LIMIT)


def query_token_allowed(method: str, path: str = "") -> bool:
    if str(method or "").upper() != "GET":
        return False
    clean = str(path or "").split("?", 1)[0].rstrip("/")
    if clean == "/api/edit/video":
        return True
    if clean.startswith("/api/assets/") and clean.endswith("/image"):
        return True
    if clean.startswith("/api/characters/") and ("/image/" in clean or "/version/" in clean):
        return True
    if clean.startswith("/api/production/"):
        return (
            clean.endswith("/video")
            or clean.endswith("/file")
            or clean.endswith("/stream")
            or "/artifact-versions/" in clean
        )
    return False


def send_security_headers(handler: Any) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")


def send_cors_headers(handler: Any) -> None:
    headers = getattr(handler, "headers", {}) or {}
    origin = allowed_cors_origin({k: v for k, v in headers.items()})
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
