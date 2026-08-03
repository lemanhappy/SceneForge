"""HTTP API for TTS voice selection.

Routes (under /api/voice):
  GET  /api/voice          -> {enabled, provider, model, voice, catalog}
  PUT  /api/voice          -> set {enabled, provider, model, voice}
  POST /api/voice/preview  -> {provider, model, voice, text?} -> {audio_b64, format}
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .voice_service import VoiceService


class VoiceAPI:
    def __init__(self, service: VoiceService):
        self.service = service

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "voice"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, self.service.get()
                if method in ("PUT", "POST"):
                    return 200, self.service.update(
                        enabled=bool(body.get("enabled", False)),
                        provider=str(body.get("provider") or "openai"),
                        model=str(body.get("model") or ""),
                        voice=str(body.get("voice") or ""),
                    )
                return 405, {"error": "method not allowed"}
            if rest == ["preview"] and method == "POST":
                return 200, self.service.preview(
                    provider=str(body.get("provider") or "openai"),
                    model=str(body.get("model") or ""),
                    voice=str(body.get("voice") or ""),
                    text=str(body.get("text") or ""),
                )
            return 404, {"error": "not found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
