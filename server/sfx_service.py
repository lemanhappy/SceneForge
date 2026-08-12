"""Sound-effect library settings backend (mirrors BgmService).

SFX differ from BGM: there's no single "selected" track — every file in the
library is a candidate, matched to a shot's ``[Sound Effect]`` description by
keyword (see audio/sfx.py). So the UI manages: enable toggle, volume, and the
library files (list / upload). Settings persist to the pipeline configs'
``audio.sfx`` section.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from audio.bgm_library import BgmLibrary  # generic audio-file library (list/add/path)
from utils.atomic import atomic_write_text, file_lock


class SfxService:
    def __init__(self, library_dir: str = "assets/sfx",
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

    def _current(self) -> Dict[str, Any]:
        for path in self.config_paths:
            sfx = ((self._load(path).get("audio") or {}).get("sfx")) or {}
            if sfx:
                return sfx
        return {}

    def get(self) -> dict:
        sfx = self._current()
        return {
            "enabled": bool(sfx.get("enabled", False)),
            "volume": float(sfx.get("volume", 0.8)),
            "library_dir": self.library_dir,
            "files": self.library.list_tracks(),
        }

    def update(self, enabled: bool, volume: float) -> dict:
        from utils.atomic import atomic_write_text, file_lock
        for path in self.config_paths:
            if not path.exists():
                continue
            with file_lock(path):
                data = self._load(path)
                audio = data.setdefault("audio", {})
                if not isinstance(audio, dict):
                    audio = {}; data["audio"] = audio
                sfx = audio.setdefault("sfx", {})
                if not isinstance(sfx, dict):
                    sfx = {}; audio["sfx"] = sfx
                sfx["enabled"] = bool(enabled)
                sfx["volume"] = float(volume)
                sfx["library"] = self.library_dir
                atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return self.get()

    def upload(self, filename: str, data_b64: str) -> dict:
        from utils.uploads import DEFAULT_AUDIO_UPLOAD_BYTES, decode_base64_upload, upload_limit

        raw = decode_base64_upload(
            data_b64,
            max_bytes=upload_limit("SCENEFORGE_MAX_AUDIO_BYTES", DEFAULT_AUDIO_UPLOAD_BYTES),
        )
        self.library.add_track(filename, raw)
        return self.get()
