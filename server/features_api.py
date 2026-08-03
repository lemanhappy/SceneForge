"""HTTP API for production feature toggles.

Routes (under /api/features):
  GET  /api/features   -> {groups:[{title, fields:[{path,label,type,options,value}]}]}
  PUT  /api/features   -> apply {path: value, ...} -> updated state
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .features_service import FeaturesService


class FeaturesAPI:
    def __init__(self, service: FeaturesService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "features"]:
            return 404, {"error": "not found"}
        if parts[2:]:
            return 404, {"error": "not found"}
        try:
            if method == "GET":
                return 200, self.service.get()
            if method in ("PUT", "POST"):
                return 200, self.service.update(body or {})
            return 405, {"error": "method not allowed"}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
