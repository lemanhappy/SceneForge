from __future__ import annotations

from typing import Any, Optional, Tuple

from .lora_service import LoraService


class LoraAPI:
    def __init__(self, service: LoraService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [part for part in path.split("?")[0].strip("/").split("/") if part]
        if parts[:2] != ["api", "loras"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.list()
                if method == "POST":
                    item = self.service.upsert(body or {}, overwrite=bool((body or {}).get("overwrite")))
                    return 200, {"ok": True, "lora": item}
                return 405, {"error": "method not allowed"}
            if len(rest) == 1 and method in {"PUT", "POST"}:
                item = self.service.upsert(body or {}, lora_id=rest[0], overwrite=True)
                return 200, {"ok": True, "lora": item}
            if len(rest) == 1 and method == "DELETE":
                ok = self.service.delete(rest[0])
                return (200, {"ok": True, "deleted": rest[0]}) if ok else (404, {"error": "LoRA 不存在"})
            return 404, {"error": "not found"}
        except FileExistsError:
            return 409, {"error": "LoRA ID 已存在"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
