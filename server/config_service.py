"""Config backend: read/update model settings in configs/agent.local.yaml.

Backs the "配置后台" page. API keys are masked on read (only a set flag + last-4
hint) and only overwritten on update when a non-empty value is supplied, so the
page never leaks or accidentally wipes a key.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict

import yaml

# editable fields per service section in agent.local.yaml
SECTIONS: Dict[str, list] = {
    "llm": ["model", "model_provider", "base_url", "api_key"],
    "image": ["model", "provider", "base_url", "api_key"],
    "video": ["model", "base_url", "api_key"],
    "embedding": ["model", "model_provider", "base_url", "api_key"],
    "reranker": ["model", "base_url", "api_key"],
}
_SECRET_FIELDS = {"api_key"}
_VIDEO_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VIDEO_PROFILE_FIELDS = {
    "label", "enabled", "provider", "transport", "model", "base_url", "api_key",
    "quality_tier", "estimated_cost", "supported_aspect_ratios", "supported_durations",
    "max_reference_count", "remote_cancel", "capabilities",
}

# Flat dropdown presets (base_url / model_provider). The UI renders these as a
# <select> + a "自定义…" escape hatch.
#
# NOTE on model_provider: this is langchain's *access protocol*, not the model's
# brand. Through an OpenAI-compatible gateway (yunwu/openrouter) it is "openai"
# for every model; only native vendor APIs use "anthropic"/"google_genai".
_OPTIONS: Dict[str, Dict[str, list]] = {
    "llm": {
        "model_provider": ["openai", "anthropic", "google_genai"],
        "base_url": ["https://yunwu.ai/v1", "https://openrouter.ai/api/v1", "https://api.openai.com/v1"],
    },
    "image": {
        "provider": ["", "nanobanana", "seedream"],
        "base_url": ["https://yunwu.ai", "https://yunwu.ai/v1"],
    },
    "video": {
        "base_url": ["https://yunwu.ai/v1", "https://openrouter.ai/api/v1"],
    },
    "embedding": {
        "model_provider": ["openai"],
        "base_url": ["https://yunwu.ai/v1", "https://api.openai.com/v1"],
    },
    "reranker": {
        "base_url": ["https://api.siliconflow.cn/v1", "https://yunwu.ai/v1"],
    },
}

# Model presets grouped by 厂商 (brand) — drives a vendor → model cascade in the
# UI. Brand is purely for filtering the model list; only the chosen model string
# is persisted.
_MODEL_CATALOG: Dict[str, Dict[str, list]] = {
    "llm": {
        "Google": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "OpenAI": ["gpt-4o", "gpt-4o-mini"],
        "Anthropic": ["claude-sonnet-4-6", "claude-opus-4-1"],
        "DeepSeek": ["deepseek-v3", "deepseek-r1"],
        "阿里通义": ["qwen-max", "qwen-plus"],
    },
    "image": {
        "字节跳动（即梦Seedream，中文文字强）": ["doubao-seedream-4-0-250828"],
        "Google": ["gemini-2.5-flash-image"],
        "OpenAI": ["gpt-image-1", "dall-e-3"],
    },
    "video": {
        "字节跳动": ["doubao-seedance-1-5-pro-251215", "doubao-seedance-1-0-pro-250528"],
        "Google": ["veo3.1-fast", "veo3.1"],
    },
    "embedding": {
        "OpenAI": ["text-embedding-3-small", "text-embedding-3-large"],
        "BAAI": ["BAAI/bge-m3"],
    },
    "reranker": {
        "BAAI": ["BAAI/bge-reranker-v2-m3"],
    },
}


def _mask(value: str) -> dict:
    value = str(value or "")
    return {"set": bool(value), "hint": (("…" + value[-4:]) if len(value) >= 4 else ("set" if value else ""))}


class ConfigService:
    def __init__(self, config_path: str = "configs/agent.local.yaml"):
        self.config_path = Path(config_path)

    def _load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        from utils.atomic import atomic_write_text
        atomic_write_text(self.config_path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        # the agent config loader is lru_cached; drop it so changes take effect now
        try:
            from agent_runtime.config import load_agent_config
            load_agent_config.cache_clear()
        except Exception:
            pass

    def get(self) -> dict:
        data = self._load()
        out: Dict[str, Any] = {}
        for section, fields in SECTIONS.items():
            payload = data.get(section) or {}
            section_view: Dict[str, Any] = {}
            for field in fields:
                if field in _SECRET_FIELDS:
                    section_view[field] = _mask(payload.get(field, ""))
                else:
                    section_view[field] = payload.get(field, "")
            section_view["_options"] = _OPTIONS.get(section, {})
            section_view["_model_catalog"] = _MODEL_CATALOG.get(section, {})
            out[section] = section_view
        # video provider is derived from base_url, surfaced read-only for the UI
        try:
            from agent_runtime.config import api_provider_from_base_url
            out["video"]["provider_derived"] = api_provider_from_base_url(str((data.get("video") or {}).get("base_url", "")))
        except Exception:
            pass
        out["video_profiles"] = self.get_video_profiles(data=data)
        return out

    def get_video_profiles(self, *, data: Dict[str, Any] | None = None) -> dict:
        data = data if data is not None else self._load()
        section = data.get("video_profiles") if isinstance(data.get("video_profiles"), dict) else {}
        items = section.get("items") if isinstance(section.get("items"), list) else []
        synthetic = False
        if not items:
            synthetic = True
            legacy = data.get("video") if isinstance(data.get("video"), dict) else {}
            items = [{
                "profile_id": "legacy",
                "label": "默认视频模型",
                "enabled": True,
                "model": legacy.get("model", ""),
                "base_url": legacy.get("base_url", ""),
                "api_key": legacy.get("api_key", ""),
                "quality_tier": legacy.get("quality_tier", "balanced"),
                "estimated_cost": legacy.get("estimated_cost"),
                "supported_durations": legacy.get("supported_durations") or [],
            }]
        shared = data.get("video") if isinstance(data.get("video"), dict) else {}
        profiles = []
        from agent_runtime.config import api_provider_from_base_url, infer_video_model_provider
        for raw in items:
            if not isinstance(raw, dict):
                continue
            item = {key: value for key, value in raw.items() if key != "api_key"}
            profile_id = str(raw.get("profile_id") or raw.get("id") or "").strip()
            model = str(raw.get("model") or "").strip()
            base_url = str(raw.get("base_url") or shared.get("base_url") or "").strip()
            own_key = str(raw.get("api_key") or "")
            effective_key = own_key or str(shared.get("api_key") or "")
            item.update({
                "profile_id": profile_id,
                "label": str(raw.get("label") or profile_id),
                "enabled": bool(raw.get("enabled", True)),
                "model": model,
                "base_url": base_url,
                "provider": infer_video_model_provider(model, str(raw.get("provider") or ""), base_url),
                "transport": str(raw.get("transport") or api_provider_from_base_url(base_url) or ""),
                "quality_tier": str(raw.get("quality_tier") or "balanced"),
                "estimated_cost": raw.get("estimated_cost"),
                "supported_aspect_ratios": list(raw.get("supported_aspect_ratios") or
                                                  ["landscape", "portrait", "square"]),
                "supported_durations": list(raw.get("supported_durations") or []),
                "max_reference_count": int(raw.get("max_reference_count", 2) or 0),
                "remote_cancel": bool(raw.get("remote_cancel", False)),
                "capabilities": dict(raw.get("capabilities") or {
                    "text_to_video": True,
                    "image_to_video": True,
                    "first_last_frame": True,
                    "multi_reference": True,
                }),
                "api_key": _mask(effective_key),
                "api_key_inherited": bool(effective_key and not own_key),
            })
            profiles.append(item)
        default_id = str(section.get("default") or "").strip()
        if not default_id or not any(item["profile_id"] == default_id for item in profiles):
            default_id = profiles[0]["profile_id"] if profiles else ""
        return {
            "default_profile_id": default_id,
            "profiles": profiles,
            "synthetic_legacy": synthetic,
            "model_catalog": _MODEL_CATALOG["video"],
            "base_url_options": _OPTIONS["video"]["base_url"],
        }

    def upsert_video_profile(self, profile_id: str, fields: dict) -> dict:
        profile_id = self._validate_profile_id(profile_id)
        from utils.atomic import file_lock
        with file_lock(self.config_path):
            data = self._load()
            section = self._materialize_video_profiles(data)
            items = section["items"]
            target = next((item for item in items if item.get("profile_id") == profile_id), None)
            if target is None:
                target = {"profile_id": profile_id, "enabled": True}
                items.append(target)
            self._update_video_profile_fields(target, fields)
            if not str(target.get("model") or "").strip():
                raise ValueError("video profile model is required")
            if not str(target.get("base_url") or "").strip():
                raise ValueError("video profile base_url is required")
            if not section.get("default"):
                section["default"] = profile_id
            self._save(data)
        return self.get_video_profiles()

    def delete_video_profile(self, profile_id: str) -> dict:
        profile_id = self._validate_profile_id(profile_id)
        from utils.atomic import file_lock
        with file_lock(self.config_path):
            data = self._load()
            section = self._materialize_video_profiles(data)
            remaining = [item for item in section["items"] if item.get("profile_id") != profile_id]
            if len(remaining) == len(section["items"]):
                raise ValueError(f"unknown video profile: {profile_id}")
            if not remaining:
                raise ValueError("at least one video profile is required")
            section["items"] = remaining
            if section.get("default") == profile_id:
                section["default"] = str(remaining[0].get("profile_id") or "")
            self._save(data)
        return self.get_video_profiles()

    def activate_video_profile(self, profile_id: str) -> dict:
        profile_id = self._validate_profile_id(profile_id)
        from utils.atomic import file_lock
        with file_lock(self.config_path):
            data = self._load()
            section = self._materialize_video_profiles(data)
            target = next((item for item in section["items"] if item.get("profile_id") == profile_id), None)
            if target is None:
                raise ValueError(f"unknown video profile: {profile_id}")
            if not bool(target.get("enabled", True)):
                raise ValueError("disabled video profile cannot be the default")
            section["default"] = profile_id
            self._save(data)
        return self.get_video_profiles()

    @staticmethod
    def _validate_profile_id(profile_id: str) -> str:
        value = str(profile_id or "").strip()
        if not _VIDEO_PROFILE_ID.fullmatch(value):
            raise ValueError("profile_id must use 1-64 letters, numbers, '_' or '-'")
        return value

    @staticmethod
    def _materialize_video_profiles(data: dict) -> dict:
        section = data.get("video_profiles")
        if not isinstance(section, dict):
            section = {}
            data["video_profiles"] = section
        items = section.get("items")
        if not isinstance(items, list) or not items:
            legacy = data.get("video") if isinstance(data.get("video"), dict) else {}
            items = []
            if legacy.get("model") and legacy.get("base_url"):
                items.append({
                    "profile_id": "legacy",
                    "label": "默认视频模型",
                    "enabled": True,
                    **{key: legacy[key] for key in (
                        "model", "base_url", "api_key", "quality_tier", "estimated_cost", "supported_durations"
                    ) if key in legacy},
                })
            section["items"] = items
            if items:
                section.setdefault("default", "legacy")
        return section

    @staticmethod
    def _update_video_profile_fields(target: dict, fields: dict) -> None:
        for key, value in (fields or {}).items():
            if key not in _VIDEO_PROFILE_FIELDS:
                continue
            if key == "api_key":
                if value:
                    target[key] = str(value)
                continue
            if key in {"supported_aspect_ratios", "supported_durations"}:
                values = value if isinstance(value, list) else str(value or "").split(",")
                if key == "supported_durations":
                    try:
                        target[key] = sorted({int(item) for item in values if int(item) > 0})
                    except (TypeError, ValueError) as exc:
                        raise ValueError("supported_durations must contain positive integers") from exc
                else:
                    target[key] = [str(item).strip() for item in values if str(item).strip()]
                continue
            if key == "quality_tier":
                if value not in {"economy", "balanced", "quality"}:
                    raise ValueError("quality_tier must be economy, balanced, or quality")
                target[key] = value
                continue
            if key == "estimated_cost":
                target[key] = None if value in (None, "") else max(0.0, float(value))
                continue
            if key == "max_reference_count":
                target[key] = max(0, int(value))
                continue
            if key in {"enabled", "remote_cancel"}:
                target[key] = bool(value)
                continue
            if key == "capabilities":
                target[key] = {name: bool(enabled) for name, enabled in dict(value or {}).items()}
                continue
            target[key] = str(value or "").strip()

    def update(self, section: str, fields: dict) -> dict:
        if section not in SECTIONS:
            raise ValueError(f"unknown section: {section} (allowed: {', '.join(SECTIONS)})")
        from utils.atomic import file_lock
        with file_lock(self.config_path):
            return self._update_locked(section, fields)

    def _update_locked(self, section: str, fields: dict) -> dict:
        allowed = SECTIONS[section]
        data = self._load()
        target = data.setdefault(section, {})
        if not isinstance(target, dict):
            target = {}
            data[section] = target
        for key, value in (fields or {}).items():
            if key not in allowed:
                continue
            if key in _SECRET_FIELDS:
                # only overwrite a secret when a non-empty value is provided
                if value:
                    target[key] = value
            else:
                target[key] = value
        self._save(data)
        return self.get()[section]
