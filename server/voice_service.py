"""Voice (TTS) selection backend: list the voice catalog, persist the chosen
provider/model/voice into the pipeline configs' ``audio.tts`` section, and
generate a short "试听" preview clip.

Mirrors BgmService: selection is written to every pipeline yaml so the
generation path picks it up. The API key is NOT written here — it stays in
agent.local.yaml and is resolved at generation/preview time via config getters.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from audio.voice_catalog import PREVIEW_TEXT, catalog
from project_identity import state_directory


class VoiceService:
    def __init__(self, config_paths: Optional[List[str]] = None, workspace_root: str = "."):
        self.config_paths = [Path(p) for p in (config_paths or
                             ["configs/idea2video.yaml", "configs/script2video.yaml"])]
        self.workspace_root = workspace_root

    def _load(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}

    def _current_tts(self) -> Dict[str, Any]:
        for path in self.config_paths:
            tts = ((self._load(path).get("audio") or {}).get("tts")) or {}
            if tts:
                return tts
        return {}

    def get(self) -> dict:
        tts = self._current_tts()
        provider = (tts.get("provider") or "openai").lower()
        # voice is stored as voice_id for minimax, voice for openai
        voice = tts.get("voice_id") if provider == "minimax" else tts.get("voice")
        return {
            "enabled": bool(tts.get("enabled", False)),
            "provider": provider,
            "model": tts.get("model") or "",
            "voice": voice or "",
            "catalog": catalog(),
        }

    def update(self, enabled: bool, provider: str, model: str, voice: str) -> dict:
        from utils.atomic import atomic_write_text, file_lock
        provider = (provider or "openai").lower()
        for path in self.config_paths:
            if not path.exists():
                continue
            with file_lock(path):
                data = self._load(path)
                audio = data.setdefault("audio", {})
                if not isinstance(audio, dict):
                    audio = {}
                    data["audio"] = audio
                tts = audio.setdefault("tts", {})
                if not isinstance(tts, dict):
                    tts = {}
                    audio["tts"] = tts
                tts["enabled"] = bool(enabled)
                tts["provider"] = provider
                if model:
                    tts["model"] = model
                if provider == "minimax":
                    tts["voice_id"] = voice
                else:
                    tts["voice"] = voice
                atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return self.get()

    def preview(self, provider: str, model: str, voice: str, text: str = "") -> dict:
        """Synthesize a short sample with the given voice and return base64 audio
        for in-browser playback. Raises ValueError when no key is configured.

        The default sample line is cached on disk per (provider, model, voice): the
        first 试听 of a voice synthesizes + caches; later ones replay the cache —
        instant, free and offline (effectively built-in after first listen). Custom
        ``text`` is never cached (always synthesized fresh)."""
        import os
        import re

        provider = (provider or "openai").lower()
        use_cache = not text  # only the fixed sample line is cacheable
        cache_path = None
        if use_cache:
            key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{provider}__{model}__{voice}")
            cache_dir = state_directory(self.workspace_root) / "voice_previews"
            cache_path = cache_dir / f"{key}.mp3"
            if cache_path.is_file():
                data = cache_path.read_bytes()
                return {"audio_b64": base64.b64encode(data).decode("ascii"), "format": "mp3", "cached": True}

        from audio.service import make_provider
        from agent_runtime.config import tts_api_key, tts_base_url
        api_key = tts_api_key(self.workspace_root)
        if not api_key:
            raise ValueError("未配置 TTS/LLM api_key（请在模型配置里设置）")
        section = {"provider": provider, "model": model,
                   ("voice_id" if provider == "minimax" else "voice"): voice}
        tts = make_provider(api_key, tts_base_url(self.workspace_root), section)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "preview.mp3")
            rendered = tts.synthesize(text or PREVIEW_TEXT, out)
            if not rendered or not os.path.exists(rendered):
                raise ValueError("试听失败：该音色/模型未返回音频")
            data = open(rendered, "rb").read()
        if use_cache and cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(data)
            except OSError:
                pass
        return {"audio_b64": base64.b64encode(data).decode("ascii"), "format": "mp3"}
