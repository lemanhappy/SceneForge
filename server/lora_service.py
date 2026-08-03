"""Persistent LoRA catalog used by the market UI and project snapshots."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from utils.atomic import atomic_write_text, file_lock


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


class LoraService:
    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).resolve()
        self.path = self.workspace_root / "assets" / "loras" / "library.json"

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(items, ensure_ascii=False, indent=2))

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if isinstance(value, list):
            source: Iterable[Any] = value
        else:
            source = re.split(r"[,，;；\n]", str(value or ""))
        return list(dict.fromkeys(str(item).strip() for item in source if str(item).strip()))

    def _normalize(self, payload: dict, *, lora_id: str | None = None) -> dict:
        identifier = str(lora_id or payload.get("lora_id") or "").strip()
        if not _ID_RE.fullmatch(identifier):
            raise ValueError("LoRA ID 只能使用英文字母、数字、- 和 _，且不超过 96 个字符")
        mode = str(payload.get("application_mode") or "native").strip().lower()
        if mode not in {"native", "trigger"}:
            raise ValueError("application_mode must be native or trigger")
        source_type = str(payload.get("source_type") or "cloud").strip().lower()
        if source_type not in {"cloud", "local"}:
            raise ValueError("source_type must be cloud or local")
        try:
            weight = float(payload.get("default_weight", 0.8))
        except (TypeError, ValueError) as exc:
            raise ValueError("LoRA 权重必须是数字") from exc
        if not 0 <= weight <= 2:
            raise ValueError("LoRA 权重必须在 0 到 2 之间")
        model_ref = str(payload.get("model_ref") or "").strip()
        if mode == "native" and not model_ref:
            raise ValueError("原生 LoRA 必须填写本地模型路径或云端模型 ID")
        return {
            "lora_id": identifier,
            "display_name": str(payload.get("display_name") or identifier).strip() or identifier,
            "provider": str(payload.get("provider") or "").strip(),
            "base_model": str(payload.get("base_model") or "").strip(),
            "source_type": source_type,
            "model_ref": model_ref,
            "trigger_words": self._strings(payload.get("trigger_words")),
            "default_weight": weight,
            "application_mode": mode,
            "tags": self._strings(payload.get("tags")),
            "notes": str(payload.get("notes") or "").strip(),
            "enabled": bool(payload.get("enabled", True)),
        }

    def list(self) -> dict:
        items = sorted(self._load(), key=lambda item: str(item.get("display_name") or item.get("lora_id")))
        return {"loras": items}

    def get(self, lora_id: str) -> dict | None:
        return next((item for item in self._load() if item.get("lora_id") == lora_id), None)

    def upsert(self, payload: dict, *, lora_id: str | None = None, overwrite: bool = False) -> dict:
        item = self._normalize(payload or {}, lora_id=lora_id)
        with file_lock(self.path):
            items = self._load()
            index = next((idx for idx, current in enumerate(items) if current.get("lora_id") == item["lora_id"]), None)
            if index is not None and not overwrite and lora_id is None:
                raise FileExistsError(item["lora_id"])
            if index is None:
                items.append(item)
            else:
                items[index] = item
            self._write(items)
        return item

    def delete(self, lora_id: str) -> bool:
        with file_lock(self.path):
            items = self._load()
            remaining = [item for item in items if item.get("lora_id") != lora_id]
            if len(remaining) == len(items):
                return False
            self._write(remaining)
        return True

    def resolve(self, lora_ids: Iterable[str]) -> list[dict]:
        requested = list(dict.fromkeys(str(item).strip() for item in lora_ids if str(item).strip()))
        by_id = {item.get("lora_id"): item for item in self._load() if item.get("enabled", True)}
        missing = [identifier for identifier in requested if identifier not in by_id]
        if missing:
            raise ValueError("LoRA 不存在或已停用：" + "、".join(missing))
        return [dict(by_id[identifier]) for identifier in requested]
