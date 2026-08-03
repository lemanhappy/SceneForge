"""HTTP API for imported-video post-production.

Routes (under /api/edit):
  GET  /api/edit              -> {videos[], imports_dir}
  POST /api/edit/upload       -> add a video {filename, data_b64} (small clips)
  POST /api/edit/transcribe   -> {path} -> {segments:[{start,end,text}]}
  POST /api/edit/apply        -> {path, subtitles[], voiceover[], bgm, aigc_label, voice, replace_audio} -> {output}
  GET  /api/edit/video?path=  -> stream an edited result file
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Optional, Tuple

from editing.video_editor import VideoEditService


class EditAPI:
    def __init__(self, service: VideoEditService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "edit"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest and method == "GET":
                return 200, {"videos": self.service.list_videos(), "imports_dir": str(self.service.imports_dir)}
            if rest == ["upload"] and method == "POST":
                return 200, self.service.upload(str(body.get("filename") or "video.mp4"), str(body.get("data_b64") or ""))
            if rest == ["transcribe"] and method == "POST":
                return 200, await self.service.transcribe(str(body.get("path") or ""))
            if rest == ["apply"] and method == "POST":
                return 200, await self.service.apply(str(body.get("path") or ""), body)
            if rest == ["video"] and method == "GET":
                rel = parse_qs(urlsplit(path).query).get("path", [""])[0]
                target = self._safe_output(rel)
                if not target:
                    return 404, {"error": "file not found"}
                return 200, {"_file": target, "_content_type": mimetypes.guess_type(target)[0] or "video/mp4"}
            return 404, {"error": "not found"}
        except FileNotFoundError as exc:
            return 404, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _safe_output(self, rel: str) -> Optional[str]:
        """Only serve files inside the imports dir (guards path traversal)."""
        if not rel:
            return None
        root = self.service.imports_dir.resolve()
        target = Path(rel).resolve()
        if root != target and root not in target.parents:
            return None
        return str(target) if target.is_file() else None
