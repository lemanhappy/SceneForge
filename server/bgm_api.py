"""HTTP API for background music (library + selection).

Routes (under /api/bgm):
  GET  /api/bgm           -> {enabled, volume, track, ducking_*, tracks[], library_dir}
  PUT  /api/bgm           -> set {enabled, track, volume, ducking_*}
  POST /api/bgm/upload    -> add a custom track {filename, data_b64}
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .bgm_service import BgmService


class BgmAPI:
    def __init__(self, service: BgmService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "bgm"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.get()
                if method in ("PUT", "POST"):
                    return 200, self.service.update(
                        enabled=bool(body.get("enabled", False)),
                        track=body.get("track") or None,
                        volume=float(body.get("volume", 0.2)),
                        ducking_enabled=(bool(body.get("ducking_enabled"))
                                         if "ducking_enabled" in body else None),
                        ducking_strength=(str(body.get("ducking_strength") or "") or None),
                    )
                return 405, {"error": "method not allowed"}
            if rest == ["upload"] and method == "POST":
                return 200, self.service.upload(
                    filename=str(body.get("filename") or "track.mp3"),
                    data_b64=str(body.get("data_b64") or ""),
                )
            return 404, {"error": "not found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
