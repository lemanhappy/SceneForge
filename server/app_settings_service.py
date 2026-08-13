"""Desktop application preferences that are not pipeline model settings."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from utils.atomic import atomic_write_text, file_lock
from project_identity import state_directory


def _open_directory_picker(initial_directory: Optional[str] = None) -> Optional[str]:
    """Open the host operating system's directory picker.

    The web UI cannot read an absolute directory path from a browser file input,
    so the local SceneForge server owns this small piece of native UI.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - depends on the host Python build
        raise RuntimeError("当前 Python 环境不支持系统文件夹选择器，请手动输入绝对路径") from exc

    initial = Path(str(initial_directory or "").strip()).expanduser()
    if not initial.is_dir():
        initial = Path.home()

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            parent=root,
            title="选择 SceneForge 媒体存储目录",
            initialdir=str(initial),
            mustexist=True,
        )
    except tk.TclError as exc:  # pragma: no cover - only occurs on headless hosts
        raise RuntimeError("当前运行环境无法打开系统文件夹选择器，请手动输入绝对路径") from exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass

    if not selected:
        return None
    return str(Path(selected).resolve())


class AppSettingsService:
    def __init__(self, workspace_root: str | Path, session_index: Any = None,
                 directory_picker: Optional[Callable[[Optional[str]], Optional[str]]] = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.path = state_directory(self.workspace_root) / "app_settings.json"
        self.default_media_root = (self.workspace_root / ".working_dir").resolve()
        self.session_index = session_index
        self.directory_picker = directory_picker or _open_directory_picker
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

    def select_directory(self, initial_directory: Any = None) -> Optional[str]:
        initial = str(initial_directory or "").strip()
        if not initial or not Path(os.path.expandvars(initial)).expanduser().is_dir():
            initial = self.get()["media_root"]
        selected = self.directory_picker(initial)
        if not selected:
            return None
        path = Path(selected).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise RuntimeError("文件夹选择器返回了无效目录，请手动输入绝对路径")
        return str(path.resolve())

    def readiness(self) -> dict:
        """Return a secret-free preflight for starting a normal video project."""
        from agent_runtime.config import (
            image_api_key, image_base_url, image_model, llm_api_key, llm_base_url,
            llm_model, tts_api_key, video_profiles,
        )
        from scripts.doctor import run_checks

        checks = []

        def add(key: str, label: str, ok: bool, detail: str, *, required: bool = True) -> None:
            checks.append({
                "key": key,
                "label": label,
                "status": "ok" if ok else ("error" if required else "warning"),
                "detail": detail,
                "required": required,
            })

        for item in run_checks(self.workspace_root, check_web=True):
            if item["name"] == "Model configuration":
                continue
            add(
                "local_" + item["name"].lower().replace(" ", "_"),
                item["name"],
                item["status"] == "ok",
                item["detail"],
                required=item["status"] != "warning",
            )

        llm_ok = bool(llm_api_key(self.workspace_root) and llm_model(self.workspace_root) and llm_base_url(self.workspace_root))
        add("llm", "文本与视觉模型", llm_ok, "已配置" if llm_ok else "缺少 API Key、模型名或 API 地址")

        image_ok = bool(image_api_key(self.workspace_root) and image_model(self.workspace_root) and image_base_url(self.workspace_root))
        add("image", "图像模型", image_ok, "已配置" if image_ok else "缺少 API Key、模型名或 API 地址")

        profiles = [item for item in video_profiles(self.workspace_root) if item.get("enabled")]
        video_ok = any(item.get("api_key") and item.get("model") and item.get("base_url") for item in profiles)
        add("video", "视频模型", video_ok, f"{len(profiles)} 个启用配置" if video_ok else "没有完整可用的视频模型配置")

        tts_ok = bool(tts_api_key(self.workspace_root))
        add("tts", "配音服务", tts_ok, "已配置" if tts_ok else "未配置；可关闭本片配音后继续", required=False)

        required_errors = [item for item in checks if item["required"] and item["status"] == "error"]
        warnings = [item for item in checks if item["status"] == "warning"]
        if required_errors:
            summary = "、".join(item["label"] for item in required_errors) + "需要处理"
        elif warnings:
            summary = "核心创作能力已就绪，" + "、".join(item["label"] for item in warnings) + "可选配置未完成"
        else:
            summary = "本地环境和核心模型均已就绪"
        return {"ready": not required_errors, "summary": summary, "checks": checks}

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
