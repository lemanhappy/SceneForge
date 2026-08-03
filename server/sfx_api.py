"""HTTP API for the sound-effect library (mirrors BgmAPI).

Routes (under /api/sfx):
  GET  /api/sfx          -> {enabled, volume, library_dir, files[]}
  PUT  /api/sfx          -> set {enabled, volume}
  POST /api/sfx/upload   -> add a sfx file {filename, data_b64}
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .sfx_service import SfxService


class SfxAPI:
    def __init__(self, service: SfxService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "sfx"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.get()
                if method in ("PUT", "POST"):
                    return 200, self.service.update(enabled=bool(body.get("enabled", False)),
                                                    volume=float(body.get("volume", 0.8)))
                return 405, {"error": "method not allowed"}
            if rest == ["upload"] and method == "POST":
                return 200, self.service.upload(filename=str(body.get("filename") or "sfx.mp3"),
                                                data_b64=str(body.get("data_b64") or ""))
            return 404, {"error": "not found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
