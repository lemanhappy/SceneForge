from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit


_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class AssetModelAPI:
    def __init__(self, studio: Any):
        self.studio = studio

    async def handle(self, method: str, path: str, body: dict | None = None):
        method = method.upper()
        parts = [item for item in urlsplit(path).path.strip("/").split("/") if item]
        if parts[:2] != ["api", "assets"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    query = parse_qs(urlsplit(path).query)
                    kind = str(query.get("asset_type", [""])[0] or "") or None
                    return 200, {"assets": self.studio.list_assets(kind)}
                if method == "POST":
                    asset_id = _asset_id(body.get("asset_id"))
                    if self.studio.get(asset_id) is not None and not body.get("overwrite"):
                        return 409, {"error": f"asset_id '{asset_id}' 已存在", "exists": True}
                    return 200, self.studio.upsert(asset_id, body)
                return 405, {"error": "method not allowed"}

            asset_id = _asset_id(rest[0])
            if len(rest) == 1:
                if method == "GET":
                    found = self.studio.get(asset_id)
                    return (200, found) if found else (404, {"error": "unknown asset_id"})
                if method in {"POST", "PUT"}:
                    return 200, self.studio.upsert(asset_id, body)
                if method == "DELETE":
                    return ((200, {"removed": True}) if self.studio.remove(asset_id)
                            else (404, {"error": "unknown asset_id"}))
            if rest[1:] == ["generate"] and method == "POST":
                return 200, await self.studio.generate_reference(
                    asset_id, str(body.get("extra_prompt") or ""))
            if rest[1:] == ["image"] and method == "GET":
                target = self.studio.image_path(asset_id)
                return ((200, {"_file": target, "_content_type": "image/png"}) if target
                        else (404, {"error": "no reference image"}))
            return 404, {"error": "not found"}
        except KeyError:
            return 404, {"error": f"unknown asset_id: {rest[0] if rest else ''}"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


def _asset_id(value) -> str:
    result = str(value or "").strip()
    if not _ASSET_ID_RE.fullmatch(result):
        raise ValueError("asset_id 只能使用英文字母、数字、中划线和下划线")
    return result
