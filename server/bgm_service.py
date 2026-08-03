"""BGM settings backend: lists library tracks, accepts custom uploads, and
persists the selection (on/off, track, volume) into the pipeline configs'
``audio.bgm`` section so the generation path picks it up unchanged.

Selection is written to every configured pipeline yaml (idea2video / script2video)
as ``audio.bgm: {enabled, volume, path, ducking}`` — ``path`` resolves to the chosen
library track (or null when none is selected, so "enabled but unset" plays no
music rather than silently grabbing a random track).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from audio.bgm_library import BgmLibrary


class BgmService:
    def __init__(self, library_dir: str = "assets/bgm",
                 config_paths: Optional[List[str]] = None):
        self.library = BgmLibrary(library_dir)
        self.library_dir = str(library_dir)
        self.config_paths = [Path(p) for p in (config_paths or
                             ["configs/idea2video.yaml", "configs/script2video.yaml"])]

    def _load(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}

    def _current_bgm(self) -> Dict[str, Any]:
        for path in self.config_paths:
            data = self._load(path)
            bgm = ((data.get("audio") or {}).get("bgm")) or {}
            if bgm:
                return bgm
        return {}

    def _track_from_path(self, path_value: str) -> Optional[str]:
        if not path_value:
            return None
        name = Path(path_value).name
        # only report it as a known selection if it's still in the library
        return name if self.library.track_path(name) else name

    def get(self) -> dict:
        bgm = self._current_bgm()
        path_value = str(bgm.get("path") or "")
        ducking = bgm.get("ducking") or {}
        if not isinstance(ducking, dict):
            ducking = {"enabled": bool(ducking)}
        return {
            "enabled": bool(bgm.get("enabled", False)),
            "volume": float(bgm.get("volume", 0.2)),
            "track": self._track_from_path(path_value),
            "ducking_enabled": bool(ducking.get("enabled", False)),
            "ducking_strength": str(ducking.get("strength") or "balanced"),
            "tracks": self.library.list_tracks(),
            "library_dir": self.library_dir,
        }

    def update(self, enabled: bool, track: Optional[str], volume: float,
               ducking_enabled: Optional[bool] = None,
               ducking_strength: Optional[str] = None) -> dict:
        from utils.atomic import atomic_write_text, file_lock
        resolved = self.library.track_path(track) if track else None
        for path in self.config_paths:
            if not path.exists():
                continue
            with file_lock(path):
                data = self._load(path)
                audio = data.setdefault("audio", {})
                if not isinstance(audio, dict):
                    audio = {}
                    data["audio"] = audio
                bgm = audio.setdefault("bgm", {})
                if not isinstance(bgm, dict):
                    bgm = {}
                    audio["bgm"] = bgm
                bgm["enabled"] = bool(enabled)
                bgm["volume"] = float(volume)
                bgm["path"] = resolved  # null when no track selected
                ducking = bgm.setdefault("ducking", {})
                if not isinstance(ducking, dict):
                    ducking = {}
                    bgm["ducking"] = ducking
                if ducking_enabled is not None:
                    ducking["enabled"] = bool(ducking_enabled)
                strength = str(ducking_strength or ducking.get("strength") or "balanced")
                ducking["strength"] = strength if strength in {"gentle", "balanced", "strong"} else "balanced"
                ducking.setdefault("attack_ms", 20)
                ducking.setdefault("release_ms", 350)
                atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return self.get()

    def upload(self, filename: str, data_b64: str) -> dict:
        raw = base64.b64decode(data_b64 or "", validate=False)
        if not raw:
            raise ValueError("empty upload")
        self.library.add_track(filename, raw)
        return self.get()
