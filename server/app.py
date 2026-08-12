"""Unified SceneForge web backend: mounts config / characters / production APIs on one
server, and (optionally) serves a static frontend. Stdlib http.server only.

  /api/config/*       -> ConfigAPI
  /api/characters/*   -> CharacterStudioAPI
  /api/production/*   -> ProductionAPI
  /  and  /<file>     -> static frontend (if static_dir is given)
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .http_security import (
    allowed_cors_origin,
    query_token_allowed,
    request_body_limit,
    request_origin_allowed,
    send_cors_headers as _send_cors_headers,
    send_security_headers as _send_security_headers,
)

logger = logging.getLogger(__name__)

# Web-asset MIME overrides (Windows registry maps .js -> text/plain, which makes
# browsers refuse to run ES modules from the built Vue app).
_WEB_MIME = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".json": "application/json", ".svg": "image/svg+xml", ".map": "application/json",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ico": "image/x-icon",
    ".html": "text/html",
}


def authorized(headers: dict, token, query_token: str = "") -> bool:
    """True if the request carries a valid access token (or auth is disabled).

    A falsy token disables auth for local development. The token may arrive as
    ``Authorization: Bearer <t>``, ``X-Auth-Token: <t>``, or a ``?token=<t>``
    query param (the last is for <img>/<a> media GETs, which can't set headers).
    """
    if not token:
        return True
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    provided = str(headers.get("authorization", "") or "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    if not provided:
        provided = str(headers.get("x-auth-token", "") or "")
    if not provided:
        provided = str(query_token or "")
    return bool(provided) and hmac.compare_digest(provided, str(token))


class AppAPI:
    def __init__(self, config_api: Any = None, character_api: Any = None, asset_api: Any = None,
                 production_api: Any = None, bgm_api: Any = None, voice_api: Any = None,
                 features_api: Any = None, sfx_api: Any = None, templates_api: Any = None,
                 edit_api: Any = None, skills_api: Any = None, app_settings_api: Any = None,
                 lora_api: Any = None, series_api: Any = None, static_dir: Optional[str] = None,
                 health_check: Any = None):
        self.config_api = config_api
        self.character_api = character_api
        self.asset_api = asset_api
        self.production_api = production_api
        self.bgm_api = bgm_api
        self.voice_api = voice_api
        self.features_api = features_api
        self.sfx_api = sfx_api
        self.templates_api = templates_api
        self.edit_api = edit_api
        self.skills_api = skills_api
        self.app_settings_api = app_settings_api
        self.lora_api = lora_api
        self.series_api = series_api
        self.static_dir = Path(static_dir) if static_dir else None
        self.health_check = health_check

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        clean = path.split("?")[0]
        if clean == "/api/health" and method.upper() == "GET":
            result = self.health_check() if self.health_check else {"status": "ok"}
            return (200 if result.get("status") == "ok" else 503), result
        if clean.startswith("/api/config") and self.config_api:
            return await self.config_api.handle(method, path, body)
        if clean.startswith("/api/characters") and self.character_api:
            return await self.character_api.handle(method, path, body)
        if clean.startswith("/api/assets") and self.asset_api:
            return await self.asset_api.handle(method, path, body)
        if clean.startswith("/api/production") and self.production_api:
            return await self.production_api.handle(method, path, body)
        if clean.startswith("/api/bgm") and self.bgm_api:
            return await self.bgm_api.handle(method, path, body)
        if clean.startswith("/api/voice") and self.voice_api:
            return await self.voice_api.handle(method, path, body)
        if clean.startswith("/api/features") and self.features_api:
            return await self.features_api.handle(method, path, body)
        if clean.startswith("/api/sfx") and self.sfx_api:
            return await self.sfx_api.handle(method, path, body)
        if clean.startswith("/api/templates") and self.templates_api:
            return await self.templates_api.handle(method, path, body)
        if clean.startswith("/api/edit") and self.edit_api:
            return await self.edit_api.handle(method, path, body)
        if clean.startswith("/api/skills") and self.skills_api:
            return await self.skills_api.handle(method, path, body)
        if clean.startswith("/api/app-settings") and self.app_settings_api:
            return await self.app_settings_api.handle(method, path, body)
        if clean.startswith("/api/loras") and self.lora_api:
            return await self.lora_api.handle(method, path, body)
        if clean.startswith("/api/series") and self.series_api:
            return await self.series_api.handle(method, path, body)
        if clean.startswith("/api/"):
            return 404, {"error": "not found"}
        if method.upper() in {"GET", "HEAD"} and self.static_dir is not None:
            return self._static(clean)
        return 404, {"error": "not found"}

    def _static(self, clean: str) -> Tuple[int, Any]:
        rel = "index.html" if clean in ("", "/") else clean.lstrip("/")
        target = (self.static_dir / rel).resolve()
        # prevent path traversal outside static_dir
        if self.static_dir.resolve() not in target.parents and target != self.static_dir.resolve():
            return 404, {"error": "not found"}
        if not target.is_file():
            return 404, {"error": "not found"}
        # Explicit web-asset MIME map: Windows' registry maps .js -> text/plain,
        # which makes browsers refuse to execute ES modules (Vite output). Force
        # correct types for the extensions the built frontend serves.
        ext = target.suffix.lower()
        ctype = _WEB_MIME.get(ext) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # Always revalidate the Vite entry point and hashed assets so a rebuilt
        # Vue application becomes visible immediately after deployment.
        return 200, {"_file": str(target), "_content_type": ctype, "_no_cache": True}


def _sse_stream(handler, api, status_path: str) -> None:  # pragma: no cover - socket streaming
    """Stream a background job's progress to an EventSource client.

    Server-side polls the existing ``GET <status_path>`` JSON endpoint every 0.5s
    and writes an SSE ``data:`` frame whenever the snapshot changes, a ``done``
    event when the job leaves the ``running`` state (or is unknown), and periodic
    comment pings to keep the connection alive. Bounded so a stuck job can't hold
    a handler thread forever; the client closes the EventSource on ``done`` to
    avoid the browser's automatic reconnect.
    """
    import asyncio
    import time

    def _write(text: str) -> bool:
        try:
            handler.wfile.write(text.encode("utf-8"))
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionError, OSError):
            return False  # client went away

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        _send_cors_headers(handler)
        _send_security_headers(handler)
        handler.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
        handler.end_headers()
    except (BrokenPipeError, ConnectionError, OSError):
        return

    last_len, last_state = -1, None
    start, last_ping = time.monotonic(), time.monotonic()
    while True:
        try:
            status, snap = asyncio.run(api.handle("GET", status_path, {}))
        except Exception as exc:  # never crash the handler thread
            _write("event: done\ndata: " + json.dumps({"state": "failed", "error": str(exc)}) + "\n\n")
            return
        if status != 200 or not isinstance(snap, dict):
            _write("event: done\ndata: " + json.dumps({"state": "failed", "error": "unknown job"}) + "\n\n")
            return

        state = snap.get("state")
        plen = len(snap.get("progress") or [])
        now = time.monotonic()

        if state and state != "running":
            _write("event: done\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n")
            return
        if plen != last_len or state != last_state:
            if not _write("data: " + json.dumps(snap, ensure_ascii=False) + "\n\n"):
                return
            last_len, last_state, last_ping = plen, state, now
        elif now - last_ping > 15:
            if not _write(": ping\n\n"):
                return
            last_ping = now

        if now - start > 1800:  # 30 min safety cap
            _write("event: done\ndata: " + json.dumps({"state": "timeout"}) + "\n\n")
            return
        time.sleep(0.5)


def _write_body(handler: Any, payload: bytes) -> bool:  # pragma: no cover - socket write
    """Write a response body without logging expected browser-navigation disconnects."""
    try:
        handler.wfile.write(payload)
        return True
    except (BrokenPipeError, ConnectionError, OSError):
        return False


def parse_byte_range(value: str, size: int) -> Optional[Tuple[int, int]]:
    """Parse one HTTP byte range; return inclusive start/end offsets."""
    if not value:
        return None
    if size < 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("invalid range")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0 or size == 0:
                raise ValueError
            return max(0, size - suffix), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid range") from exc
    if start < 0 or start >= size or end < start:
        raise ValueError("range not satisfiable")
    return start, min(end, size - 1)


def _serve_file(handler: Any, result: dict) -> bool:  # pragma: no cover - socket streaming
    path = Path(result["_file"])
    try:
        size = path.stat().st_size
        selected = parse_byte_range(str(handler.headers.get("Range", "") or ""), size)
    except OSError:
        return False
    except ValueError:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        _send_cors_headers(handler)
        _send_security_headers(handler)
        handler.end_headers()
        return True

    start, end = selected if selected is not None else (0, max(0, size - 1))
    length = 0 if size == 0 else end - start + 1
    handler.send_response(206 if selected is not None else 200)
    handler.send_header("Content-Type", result.get("_content_type", "application/octet-stream"))
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if selected is not None:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    _send_cors_headers(handler)
    _send_security_headers(handler)
    if result.get("_no_cache"):
        handler.send_header("Cache-Control", "no-cache, must-revalidate")
    handler.end_headers()
    if handler.command == "HEAD" or not length:
        return True
    try:
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk or not _write_body(handler, chunk):
                    break
                remaining -= len(chunk)
        return True
    except OSError:
        return True


def serve(api: Any, host: str = "127.0.0.1", port: int = 8770,
          feishu_handler: Any = None, feishu_path: str = "/feishu/events",
          auth_token: Optional[str] = None) -> None:  # pragma: no cover - socket binding
    """Serve any object exposing ``async handle(method, path, body)`` over HTTP.

    JSON dict results are returned as application/json; a result dict containing
    ``_file`` is streamed as bytes with its ``_content_type``.

    If ``feishu_handler`` is given, POSTs to ``feishu_path`` are routed to it with
    the RAW body + headers (Feishu signature verification needs both), bypassing
    the JSON ``api.handle`` path.
    """
    import asyncio
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _json_response(handler: BaseHTTPRequestHandler, status: int, result: Any) -> None:
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        _send_cors_headers(handler)
        _send_security_headers(handler)
        handler.end_headers()
        if handler.command != "HEAD":
            _write_body(handler, payload)

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        clean = handler.path.split("?")[0]
        headers = {k: v for k, v in handler.headers.items()}

        # Auth gate for /api/* (static UI + Feishu webhook are exempt — the UI must
        # load to enter the token, and Feishu has its own signature).
        query = parse_qs(urlsplit(handler.path).query)
        if clean.startswith("/api/") and not authorized(
            headers,
            auth_token,
            query.get("token", [""])[0] if query_token_allowed(handler.command, clean) else "",
        ):
            _json_response(handler, 401, {"error": "unauthorized"})
            return

        if clean.startswith("/api/") and not request_origin_allowed(headers):
            _json_response(handler, 403, {"error": "request origin is not allowed"})
            return

        if handler.headers.get("Transfer-Encoding"):
            _json_response(handler, 400, {"error": "transfer encoding is not supported"})
            return
        try:
            length = int(handler.headers.get("Content-Length", 0) or 0)
            if length < 0:
                raise ValueError
        except (TypeError, ValueError):
            _json_response(handler, 400, {"error": "invalid content length"})
            return
        limit = request_body_limit(clean)
        if length > limit:
            _json_response(handler, 413, {"error": f"request exceeds {limit // (1024 * 1024)} MB limit"})
            return
        if length and clean.startswith("/api/"):
            content_type = handler.headers.get_content_type()
            if content_type != "application/json" and not content_type.endswith("+json"):
                _json_response(handler, 415, {"error": "Content-Type must be application/json"})
                return
        try:
            handler.connection.settimeout(30)
            raw = handler.rfile.read(length) if length else b""
        except (TimeoutError, OSError):
            _json_response(handler, 408, {"error": "request body timed out"})
            return

        # Server-Sent Events: live progress for a background job. The client opens
        # GET /api/production/jobs/<id>/stream and we push the job snapshot whenever
        # it changes (server-side poll of the existing status endpoint), so the UI
        # gets near-real-time updates without client-side polling. Falls back to the
        # plain status endpoint if the client can't use EventSource.
        if handler.command == "GET" and clean.startswith("/api/production/jobs/") and clean.endswith("/stream"):
            _sse_stream(handler, api, clean[: -len("/stream")])
            return

        if feishu_handler is not None and clean == feishu_path:
            try:
                resp = asyncio.run(feishu_handler.handle_request(raw, headers=headers))
                status, result = int(resp.get("status", 200)), resp.get("body", {})
            except Exception as exc:
                status, result = 500, {"error": str(exc)}
            _json_response(handler, status, result)
            return

        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            _json_response(handler, 400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            _json_response(handler, 400, {"error": "JSON body must be an object"})
            return
        try:
            status, result = asyncio.run(api.handle(handler.command, handler.path, body))
        except Exception:
            logger.exception("Unhandled API error for %s %s", handler.command, clean)
            status, result = 500, {"error": "internal server error"}

        if isinstance(result, dict) and "_file" in result:
            if _serve_file(handler, result):
                return
            status, result = 404, {"error": "file not found"}

        _json_response(handler, status, result)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            _respond(self)

        def do_HEAD(self):
            _respond(self)

        def do_POST(self):
            _respond(self)

        def do_PUT(self):
            _respond(self)

        def do_DELETE(self):
            _respond(self)

        def do_OPTIONS(self):
            if not request_origin_allowed({k: v for k, v in self.headers.items()}):
                _json_response(self, 403, {"error": "request origin is not allowed"})
                return
            self.send_response(204)
            _send_cors_headers(self)
            _send_security_headers(self)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"SceneForge web backend on http://{host}:{port}")
    server.serve_forever()
