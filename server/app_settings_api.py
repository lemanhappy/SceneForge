from __future__ import annotations

import asyncio
from typing import Any, Optional, Tuple

from .app_settings_service import AppSettingsService


class AppSettingsAPI:
    def __init__(self, service: AppSettingsService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [part for part in path.split("?")[0].strip("/").split("/") if part]
        try:
            if parts == ["api", "app-settings", "directory-picker"]:
                if method != "POST":
                    return 405, {"error": "method not allowed"}
                initial = (body or {}).get("initial_directory")
                selected = await asyncio.to_thread(self.service.select_directory, initial)
                return 200, {"selected": bool(selected), "path": selected or ""}
            if parts == ["api", "app-settings", "readiness"]:
                if method != "GET":
                    return 405, {"error": "method not allowed"}
                return 200, await asyncio.to_thread(self.service.readiness)
            if parts != ["api", "app-settings"]:
                return 404, {"error": "not found"}
            if method == "GET":
                return 200, self.service.get()
            if method in {"PUT", "POST"}:
                return 200, self.service.update(body or {})
            return 405, {"error": "method not allowed"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except RuntimeError as exc:
            return 503, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
