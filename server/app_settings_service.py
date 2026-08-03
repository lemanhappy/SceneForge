"""Desktop application preferences that are not pipeline model settings."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from utils.atomic import atomic_write_text, file_lock
from project_identity import state_directory


class AppSettingsService:
    def __init__(self, workspace_root: str | Path, session_index: Any = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.path = state_directory(self.workspace_root) / "app_settings.json"
        self.default_media_root = (self.workspace_root / ".working_dir").resolve()
        self.session_index = session_index
        self._apply_media_root(self._load().get("media_root"))

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def _resolve_media_root(self, value: Any) -> Path:
        raw = str(value or "").strip()
        if not raw:
            return self.default_media_root
        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            raise ValueError("存储目录必须填写绝对路径")
        return path.resolve()

    @staticmethod
    def _ensure_writable(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".sceneforge-write-test-{uuid.uuid4().hex}"
        try:
            probe.write_text("ok", encoding="ascii")
        except OSError as exc:
            raise ValueError(f"存储目录不可写：{exc}") from exc
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def _apply_media_root(self, value: Any) -> Path:
        path = self._resolve_media_root(value)
        self._ensure_writable(path)
        if self.session_index is not None:
            self.session_index.set_working_root(path)
        return path

    def get(self) -> dict:
        data = self._load()
        theme = str(data.get("theme") or "light")
        if theme not in {"light", "dark"}:
            theme = "light"
        media_root = self._resolve_media_root(data.get("media_root"))
        return {
            "theme": theme,
            "media_root": str(media_root),
            "default_media_root": str(self.default_media_root),
            "applies_to": "新建项目的图片、视频、音频和中间文件",
        }

    def update(self, values: dict) -> dict:
        values = values or {}
        with file_lock(self.path):
            data = self._load()
            if "theme" in values:
                theme = str(values.get("theme") or "light")
                if theme not in {"light", "dark"}:
                    raise ValueError("theme must be light or dark")
                data["theme"] = theme
            if "media_root" in values:
                media_root = self._apply_media_root(values.get("media_root"))
                data["media_root"] = str(media_root)
            self._write(data)
        return self.get()
