from __future__ import annotations

from typing import Any, Optional, Tuple

from .app_settings_service import AppSettingsService


class AppSettingsAPI:
    def __init__(self, service: AppSettingsService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [part for part in path.split("?")[0].strip("/").split("/") if part]
        if parts != ["api", "app-settings"]:
            return 404, {"error": "not found"}
        try:
            if method == "GET":
                return 200, self.service.get()
            if method in {"PUT", "POST"}:
                return 200, self.service.update(body or {})
            return 405, {"error": "method not allowed"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
