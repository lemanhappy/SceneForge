"""HTTP API for preference/template memory.

Routes (under /api/templates):
  GET    /api/templates            -> {last_used, templates[]}
  POST   /api/templates            -> save a template {name, style, user_requirement, note}
  POST   /api/templates/remember   -> update last-used {style, user_requirement}
  DELETE /api/templates/{name}     -> delete a template
"""

from __future__ import annotations

from typing import Any, Optional, Tuple
from urllib.parse import unquote

from .preference_service import PreferenceService


class TemplatesAPI:
    def __init__(self, service: PreferenceService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "templates"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.get()
                if method == "POST":
                    return 200, self.service.save_template(
                        str(body.get("name") or ""), str(body.get("style") or ""),
                        str(body.get("user_requirement") or ""), str(body.get("note") or ""))
                return 405, {"error": "method not allowed"}
            if rest == ["remember"] and method == "POST":
                return 200, self.service.remember(str(body.get("style") or ""), str(body.get("user_requirement") or ""))
            if len(rest) == 1 and method == "DELETE":
                return 200, self.service.delete_template(unquote(rest[0]))
            return 404, {"error": "not found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
