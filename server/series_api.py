from __future__ import annotations

from typing import Any, Optional, Tuple

from .series_service import SeriesService


class SeriesAPI:
    def __init__(self, service: SeriesService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [part for part in path.split("?")[0].strip("/").split("/") if part]
        if parts[:2] != ["api", "series"]:
            return 404, {"error": "not found"}
        try:
            if len(parts) == 2:
                if method == "GET":
                    return 200, {"series": self.service.list()}
                if method == "POST":
                    return 201, self.service.create(body or {})
                return 405, {"error": "method not allowed"}
            if len(parts) != 3:
                return 404, {"error": "not found"}
            series_id = parts[2]
            if method == "GET":
                return 200, self.service.get(series_id)
            if method in {"PUT", "PATCH"}:
                return 200, self.service.update(series_id, body or {})
            if method == "DELETE":
                return 200, {"ok": self.service.delete(series_id)}
            return 405, {"error": "method not allowed"}
        except KeyError as exc:
            return 404, {"error": str(exc).strip("'")}
        except (TypeError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
