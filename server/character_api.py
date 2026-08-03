"""HTTP API for the character studio (backend-only; a web frontend consumes it).

The routing core ``CharacterStudioAPI.handle`` is separated from socket binding
so it is unit-testable without a network. ``serve`` is a thin stdlib http.server
wrapper (zero extra dependencies, runs in the existing venv).

Routes (all under /api/characters):
  GET    /api/characters                      -> list characters
  POST   /api/characters                      -> create {asset_id, ...}; 409 if id exists unless {overwrite:true}
  GET    /api/characters/{id}                 -> one character
  POST   /api/characters/{id}                 -> update that character (upsert)
  DELETE /api/characters/{id}                 -> remove
  POST   /api/characters/{id}/generate        -> generate a portrait view {view, style, extra_prompt}
  GET    /api/characters/{id}/image/{view}    -> raw PNG bytes
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

from characters.studio import CharacterStudio
from .http_security import send_cors_headers as _send_cors_headers

# asset_id is used as a directory name and a registry key, so constrain it to a
# safe slug: ASCII letters/digits to start, then letters/digits/-/_ . This blocks
# path traversal (``..``, ``/``) and keeps portrait folders tidy.
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_asset_id(asset_id: Optional[str]) -> str:
    aid = str(asset_id or "").strip()
    if not aid:
        raise ValueError("asset_id is required")
    if not _ASSET_ID_RE.match(aid):
        raise ValueError("asset_id 只能用英文字母/数字/中划线/下划线，且以字母或数字开头（例如 wangyunbao）")
    return aid


class CharacterStudioAPI:
    def __init__(self, studio: CharacterStudio):
        self.studio = studio

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "characters"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}

        try:
            if not rest:
                if method == "GET":
                    return 200, {"characters": self.studio.list_characters()}
                if method == "POST":
                    # create route: reject a duplicate id rather than silently
                    # clobbering an existing character (unless overwrite is set)
                    aid = _validate_asset_id(body.get("asset_id"))
                    if self.studio.get(aid) is not None and not body.get("overwrite"):
                        return 409, {"error": f"asset_id '{aid}' 已存在。如需修改请编辑该角色，或传 overwrite:true 覆盖。",
                                     "asset_id": aid, "exists": True}
                    return 200, self._upsert(aid, body)
                return 405, {"error": "method not allowed"}

            asset_id = rest[0]

            if len(rest) == 1:
                if method == "GET":
                    found = self.studio.get(asset_id)
                    return (200, found) if found else (404, {"error": "unknown asset_id"})
                if method in ("POST", "PUT"):
                    return 200, self._upsert(asset_id, body)
                if method == "DELETE":
                    return (200, {"removed": True}) if self.studio.remove(asset_id) else (404, {"error": "unknown asset_id"})
                return 405, {"error": "method not allowed"}

            if len(rest) == 2 and rest[1] == "generate" and method == "POST":
                result = await self.studio.generate_view(
                    asset_id,
                    view=str(body.get("view", "front") or "front"),
                    style=str(body.get("style", "") or ""),
                    extra_prompt=str(body.get("extra_prompt", "") or ""),
                )
                return 200, result

            if len(rest) == 3 and rest[1] == "image" and method == "GET":
                file_path = self.studio.image_path(asset_id, rest[2])
                if not file_path:
                    return 404, {"error": "no image for that view"}
                return 200, {"_file": file_path, "_content_type": "image/png"}

            # portrait version history: list / rollback / fetch a version's bytes
            if len(rest) == 3 and rest[1] == "versions" and method == "GET":
                return 200, {"versions": self.studio.list_versions(asset_id, rest[2])}
            if len(rest) == 2 and rest[1] == "rollback" and method == "POST":
                return 200, self.studio.rollback(asset_id, str(body.get("view", "front") or "front"),
                                                 int(body.get("version")))
            if len(rest) == 4 and rest[1] == "version" and method == "GET":
                vp = self.studio.version_path(asset_id, rest[2], int(rest[3]))
                if not vp:
                    return 404, {"error": "no such version"}
                return 200, {"_file": vp, "_content_type": "image/png"}

            return 404, {"error": "not found"}
        except KeyError:
            return 404, {"error": f"unknown asset_id: {asset_id}"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # surface server errors as JSON, don't crash the socket
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _upsert(self, asset_id: Optional[str], body: dict) -> dict:
        asset_id = _validate_asset_id(asset_id)
        return self.studio.upsert(
            asset_id,
            display_name=body.get("display_name"),
            description=str(body.get("description", "") or ""),
            aliases=body.get("aliases"),
            visual_prompt=body.get("visual_prompt"),
            identity_profile=body.get("identity_profile"),
            bible=body.get("bible"),
            reference_sets=body.get("reference_sets"),
            outfit_versions=body.get("outfit_versions"),
            render_bindings=body.get("render_bindings"),
        )


def serve(api: CharacterStudioAPI, host: str = "127.0.0.1", port: int = 8770) -> None:  # pragma: no cover - socket binding
    """Serve the character studio API over HTTP using only the standard library."""
    import asyncio
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            # malformed/non-UTF-8 body -> empty dict; handlers return a clean 400
            body = {}
        try:
            status, result = asyncio.run(api.handle(handler.command, handler.path, body))
        except Exception as exc:
            status, result = 500, {"error": str(exc)}

        if isinstance(result, dict) and "_file" in result:
            try:
                with open(result["_file"], "rb") as f:
                    data = f.read()
                handler.send_response(200)
                handler.send_header("Content-Type", result.get("_content_type", "application/octet-stream"))
                handler.send_header("Content-Length", str(len(data)))
                _send_cors_headers(handler)
                handler.end_headers()
                handler.wfile.write(data)
                return
            except OSError:
                status, result = 404, {"error": "file not found"}

        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        _send_cors_headers(handler)
        handler.end_headers()
        handler.wfile.write(payload)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            _respond(self)

        def do_POST(self):
            _respond(self)

        def do_PUT(self):
            _respond(self)

        def do_DELETE(self):
            _respond(self)

        def do_OPTIONS(self):  # CORS preflight for a future browser frontend
            self.send_response(204)
            _send_cors_headers(self)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"Character studio API on http://{host}:{port}  (GET /api/characters)")
    server.serve_forever()
