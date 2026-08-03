"""HTTP API for user-uploaded skills (风格 skill).

Routes (under /api/skills):
  GET    /api/skills            -> {skills:[...], examples:[...], market_url}
  POST   /api/skills            -> upload {filename, content} -> {ok, skill} | {ok:false, error}
  POST   /api/skills/import     -> install a bundled example {key} -> {ok, skill}
  POST   /api/skills/fork       -> copy a built-in pack to an editable skill {key} -> {ok, skill}
  DELETE /api/skills/{key}      -> remove a skill -> {ok, deleted}
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .skills_service import SkillsService


class SkillsAPI:
    def __init__(self, service: SkillsService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "skills"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.list()
                if method == "POST":
                    result = self.service.upload(str(body.get("filename", "") or ""),
                                                 str(body.get("content", "") or ""))
                    return (200 if result.get("ok") else 400), result
                return 405, {"error": "method not allowed"}
            if rest == ["import"] and method == "POST":
                result = self.service.import_example(str(body.get("key", "") or ""))
                return (200 if result.get("ok") else 400), result
            if rest == ["fork"] and method == "POST":
                result = self.service.fork_builtin(str(body.get("key", "") or ""))
                return (200 if result.get("ok") else 400), result
            if len(rest) == 1 and method == "DELETE":
                result = self.service.delete(rest[0])
                return (200 if result.get("ok") else 404), result
            return 404, {"error": "not found"}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
