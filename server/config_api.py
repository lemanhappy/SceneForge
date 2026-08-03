"""HTTP API for the config backend (model keys/settings).

Routes (under /api/config):
  GET  /api/config            -> all sections (api_key masked)
  PUT  /api/config/{section}  -> update fields of a section {model, base_url, api_key, ...}
  GET  /api/config/video-profiles
  POST /api/config/video-profiles
  PUT/DELETE /api/config/video-profiles/{profile_id}
  POST /api/config/video-profiles/{profile_id}/activate
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .config_service import ConfigService


class ConfigAPI:
    def __init__(self, service: ConfigService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "config"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.get()
                return 405, {"error": "method not allowed"}
            if rest[0] == "video-profiles":
                if len(rest) == 1 and method == "GET":
                    return 200, self.service.get_video_profiles()
                if len(rest) == 1 and method == "POST":
                    return 200, self.service.upsert_video_profile(str(body.get("profile_id") or ""), body)
                if len(rest) == 2 and method == "PUT":
                    return 200, self.service.upsert_video_profile(rest[1], body)
                if len(rest) == 2 and method == "DELETE":
                    return 200, self.service.delete_video_profile(rest[1])
                if len(rest) == 3 and rest[2] == "activate" and method == "POST":
                    return 200, self.service.activate_video_profile(rest[1])
                return 405, {"error": "method not allowed"}
            if len(rest) == 1 and method in ("PUT", "POST"):
                return 200, {"section": rest[0], "config": self.service.update(rest[0], body)}
            return 404, {"error": "not found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
