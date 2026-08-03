import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class OpenAITTSProvider:
    """Text-to-speech over an OpenAI-compatible ``/audio/speech`` endpoint.

    Works through the same yunwu.ai gateway (and key) the project already uses
    for chat/image/video, so no new account is required. Synthesis is a plain
    synchronous ``requests`` POST — the pipeline calls it from a blocking helper
    (mirroring the subtitle burn-in step), so there is no event-loop binding to
    worry about.
    """

    def __init__(self, api_key: str, model: str, base_url: str,
                 voice: str = "alloy", response_format: str = "mp3",
                 speed: float = 1.0, timeout: float = 120.0, attempts: int = 3):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.voice = voice
        self.response_format = response_format
        self.speed = speed
        self.timeout = timeout
        self.attempts = max(1, int(attempts or 1))

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def synthesize(self, text: str, out_path: str, voice: Optional[str] = None) -> Optional[str]:
        """Render ``text`` to ``out_path``. Returns the path on success, or
        ``None`` on any failure (no key, network error, empty text) so the caller
        degrades to a silent / voiceover-less video instead of crashing a run."""
        text = (text or "").strip()
        if not text or not self.available:
            return None

        import requests  # lazy: keep the module importable without network deps

        url = f"{self.base_url}/audio/speech"
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice or self.voice,
            "response_format": self.response_format,
        }
        if self.speed and self.speed != 1.0:
            payload["speed"] = self.speed
        content = None
        for attempt in range(1, self.attempts + 1):
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=(10, self.timeout),
                )
                resp.raise_for_status()
                candidate = bytes(resp.content or b"")
                content_type = str(resp.headers.get("Content-Type", "") or "").lower()
                if not _looks_like_audio(candidate, self.response_format, content_type):
                    raise ValueError(
                        f"TTS returned non-audio content ({content_type or 'unknown'}, {len(candidate)} bytes)"
                    )
                content = candidate
                break
            except Exception as exc:  # pragma: no cover - environment/network dependent
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or int(status) >= 500
                if attempt >= self.attempts or not retryable:
                    logger.warning("TTS request failed: %s", exc)
                    return None
                logger.warning(
                    "Retrying TTS after invalid/transient response (attempt %d/%d): %s",
                    attempt,
                    self.attempts,
                    exc,
                )
                time.sleep(min(8.0, float(2 ** (attempt - 1))))

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            Path(out_path).write_bytes(content or b"")
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to write TTS audio %s: %s", out_path, exc)
            return None
        if os.path.getsize(out_path) == 0:
            logger.warning("TTS returned empty audio for: %.40s", text)
            return None
        return out_path


def _looks_like_audio(data: bytes, audio_format: str, content_type: str = "") -> bool:
    """Reject HTML/JSON gateway bodies that occasionally arrive with HTTP 200."""
    if not data or "text/html" in content_type or "application/json" in content_type:
        return False
    fmt = str(audio_format or "mp3").strip().lower()
    if fmt in {"mp3", "mpeg"}:
        return data.startswith(b"ID3") or (
            len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
        )
    if fmt == "wav":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    if fmt == "flac":
        return data.startswith(b"fLaC")
    if fmt in {"opus", "ogg"}:
        return data.startswith(b"OggS")
    if fmt in {"aac", "m4a"}:
        return data.startswith(b"ftyp", 4) or (
            len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0
        )
    return len(data) > 16 and not data.lstrip().startswith((b"<", b"{", b"["))


class MiniMaxTTSProvider:
    """MiniMax T2A v2 voiceover (much stronger Chinese / emotional speech than
    OpenAI tts-1). Uses MiniMax's task-style endpoint, exposed behind the gateway
    at ``<host>/minimax/v1/t2a_v2``; the response carries hex-encoded audio in
    ``data.audio`` which we decode to bytes.
    """

    def __init__(self, api_key: str, base_url: str, model: str = "speech-2.6-hd",
                 voice_id: str = "male-qn-jingying", speed: float = 1.0, vol: float = 1.0,
                 pitch: int = 0, sample_rate: int = 32000, audio_format: str = "mp3",
                 endpoint: Optional[str] = None, timeout: float = 120.0):
        self.api_key = api_key
        host = (base_url or "").rstrip("/")
        if host.endswith("/v1"):
            host = host[:-3]
        self.endpoint = endpoint or f"{host}/minimax/v1/t2a_v2"
        self.model = model
        self.voice_id = voice_id
        self.speed = speed
        self.vol = vol
        self.pitch = pitch
        self.sample_rate = sample_rate
        self.audio_format = audio_format
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.endpoint and self.model)

    def synthesize(self, text: str, out_path: str, voice: Optional[str] = None) -> Optional[str]:
        text = (text or "").strip()
        if not text or not self.available:
            return None

        import requests

        payload = {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice or self.voice_id,
                "speed": self.speed,
                "vol": self.vol,
                "pitch": self.pitch,
            },
            "audio_setting": {
                "format": self.audio_format,
                "sample_rate": self.sample_rate,
                "bitrate": 128000,
                "channel": 1,
            },
        }
        try:
            resp = requests.post(self.endpoint, headers={"Authorization": f"Bearer {self.api_key}"},
                                 json=payload, timeout=(10, self.timeout))
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # pragma: no cover - environment/network dependent
            logger.warning("MiniMax TTS request failed: %s", exc)
            return None

        hex_audio = (body.get("data") or {}).get("audio") if isinstance(body, dict) else None
        if not hex_audio:
            base_resp = body.get("base_resp") if isinstance(body, dict) else None
            logger.warning("MiniMax TTS returned no audio (base_resp=%s)", base_resp)
            return None
        try:
            raw = bytes.fromhex(hex_audio)
        except ValueError:
            logger.warning("MiniMax TTS audio payload was not hex-encoded")
            return None

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            Path(out_path).write_bytes(raw)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to write MiniMax audio %s: %s", out_path, exc)
            return None
        if os.path.getsize(out_path) == 0:
            logger.warning("MiniMax TTS produced empty audio for: %.40s", text)
            return None
        return out_path
