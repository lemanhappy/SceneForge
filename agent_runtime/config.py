from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Fresh-install defaults: a domestic (Chinese-strong) stack — qwen-vl-max (vision
# LLM, used for planning + image selection/consistency), Doubao Seedream (image,
# strong Chinese text), Doubao Seedance (video). Users override these in 设置页.
DEFAULT_LLM_MODEL = "qwen-vl-max"
DEFAULT_LLM_MODEL_PROVIDER = "openai"
DEFAULT_LLM_BASE_URL = "https://yunwu.ai/v1"
DEFAULT_IMAGE_MODEL = "doubao-seedream-4-0-250828"
DEFAULT_IMAGE_BASE_URL = "https://yunwu.ai"
DEFAULT_VIDEO_MODEL = "doubao-seedance-1-5-pro-251215"
DEFAULT_VIDEO_BASE_URL = "https://yunwu.ai/v1"
DEFAULT_TTS_MODEL = "tts-1"
DEFAULT_TTS_VOICE = "alloy"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_MODEL_PROVIDER = "openai"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


@lru_cache(maxsize=4)
def load_agent_config(workspace_root: str | Path = ".") -> dict[str, Any]:
    path = Path(workspace_root).resolve() / "configs" / "agent.local.yaml"
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid configs/agent.local.yaml: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("configs/agent.local.yaml must be a YAML mapping")
    return payload


def config_value(section: str, key: str, env_names: list[str], default: str = "", workspace_root: str | Path = ".") -> str:
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value
    section_payload = load_agent_config(workspace_root).get(section, {})
    if isinstance(section_payload, dict):
        value = section_payload.get(key)
        if isinstance(value, str) and value:
            return value
    return default


def llm_model(workspace_root: str | Path = ".") -> str:
    return config_value("llm", "model", ["SCENEFORGE_LLM_MODEL"], DEFAULT_LLM_MODEL, workspace_root)


def llm_model_provider(workspace_root: str | Path = ".") -> str:
    return config_value("llm", "model_provider", ["SCENEFORGE_LLM_MODEL_PROVIDER"], DEFAULT_LLM_MODEL_PROVIDER, workspace_root)


def llm_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("llm", "base_url", ["SCENEFORGE_LLM_BASE_URL"], DEFAULT_LLM_BASE_URL, workspace_root)


def llm_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("llm", "api_key", ["SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY"], "", workspace_root)


def image_model(workspace_root: str | Path = ".") -> str:
    return config_value("image", "model", ["SCENEFORGE_IMAGE_MODEL"], DEFAULT_IMAGE_MODEL, workspace_root)


def image_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("image", "base_url", ["SCENEFORGE_IMAGE_BASE_URL"], DEFAULT_IMAGE_BASE_URL, workspace_root)


def image_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("image", "api_key", ["SCENEFORGE_IMAGE_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY"], llm_api_key(workspace_root), workspace_root)


def image_provider(workspace_root: str | Path = ".") -> str:
    """Explicit image backend selector (image.provider / SCENEFORGE_IMAGE_PROVIDER).

    Empty = auto-detect from the model name in the adapter builder (e.g. a
    *seedream* model selects the Doubao Seedream backend, otherwise nano-banana)."""
    return config_value("image", "provider", ["SCENEFORGE_IMAGE_PROVIDER"], "", workspace_root)



def embedding_model(workspace_root: str | Path = ".") -> str:
    return config_value("embedding", "model", ["SCENEFORGE_EMBEDDING_MODEL"], DEFAULT_EMBEDDING_MODEL, workspace_root)


def embedding_model_provider(workspace_root: str | Path = ".") -> str:
    return config_value("embedding", "model_provider", ["SCENEFORGE_EMBEDDING_MODEL_PROVIDER"], DEFAULT_EMBEDDING_MODEL_PROVIDER, workspace_root)


def embedding_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("embedding", "base_url", ["SCENEFORGE_EMBEDDING_BASE_URL"], "", workspace_root)


def embedding_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("embedding", "api_key", ["SCENEFORGE_EMBEDDING_API_KEY"], "", workspace_root)


def reranker_model(workspace_root: str | Path = ".") -> str:
    return config_value("reranker", "model", ["SCENEFORGE_RERANKER_MODEL"], DEFAULT_RERANKER_MODEL, workspace_root)


def reranker_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("reranker", "base_url", ["SCENEFORGE_RERANKER_BASE_URL"], "", workspace_root)


def reranker_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("reranker", "api_key", ["SCENEFORGE_RERANKER_API_KEY"], "", workspace_root)


def tts_model(workspace_root: str | Path = ".") -> str:
    return config_value("tts", "model", ["SCENEFORGE_TTS_MODEL"], DEFAULT_TTS_MODEL, workspace_root)


def tts_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("tts", "base_url", ["SCENEFORGE_TTS_BASE_URL", "SCENEFORGE_LLM_BASE_URL"], llm_base_url(workspace_root), workspace_root)


def tts_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("tts", "api_key", ["SCENEFORGE_TTS_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY"], llm_api_key(workspace_root), workspace_root)


def tts_voice(workspace_root: str | Path = ".") -> str:
    return config_value("tts", "voice", ["SCENEFORGE_TTS_VOICE"], DEFAULT_TTS_VOICE, workspace_root)


def video_model(workspace_root: str | Path = ".") -> str:
    return config_value("video", "model", ["SCENEFORGE_VIDEO_MODEL"], DEFAULT_VIDEO_MODEL, workspace_root)


def video_base_url(workspace_root: str | Path = ".") -> str:
    return config_value("video", "base_url", ["SCENEFORGE_VIDEO_BASE_URL"], DEFAULT_VIDEO_BASE_URL, workspace_root)


def video_api_key(workspace_root: str | Path = ".") -> str:
    return config_value("video", "api_key", ["SCENEFORGE_VIDEO_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY"], llm_api_key(workspace_root), workspace_root)


def api_provider_from_base_url(base_url: str) -> str:
    normalized = base_url.strip().lower()
    if "openrouter.ai" in normalized:
        return "openrouter"
    if "yunwu.ai" in normalized:
        return "yunwu"
    return ""


def video_provider(workspace_root: str | Path = ".") -> str:
    """Infer the video API relay/provider from video.base_url.

    This is not a model provider setting. OpenRouter/Yunwu are transport/API
    gateways here, so users should configure base_url and let the adapter pick
    the matching implementation.
    """
    return api_provider_from_base_url(video_base_url(workspace_root))


def infer_video_model_provider(model: str, provider: str = "", base_url: str = "") -> str:
    """Return the model family, distinct from the API transport/gateway."""
    explicit = str(provider or "").strip().lower()
    if explicit:
        return explicit
    normalized = str(model or "").strip().lower()
    if "seedance" in normalized:
        return "seedance"
    if "veo" in normalized:
        return "veo"
    return api_provider_from_base_url(base_url) or "video"


def video_profiles(workspace_root: str | Path = ".") -> list[dict[str, Any]]:
    """Return normalized video profiles, with the legacy ``video`` section as fallback.

    API keys may be inherited from the legacy video/LLM configuration. This is a
    runtime helper; callers exposing profiles over HTTP must mask the resolved key.
    """
    root = Path(workspace_root).resolve()
    config = load_agent_config(root)
    shared = config.get("video") if isinstance(config.get("video"), dict) else {}
    section = config.get("video_profiles") if isinstance(config.get("video_profiles"), dict) else {}
    items = section.get("items") if isinstance(section.get("items"), list) else []
    shared_key = str(shared.get("api_key") or llm_api_key(root) or "")
    profiles: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        profile_id = str(raw.get("profile_id") or raw.get("id") or f"video-{index + 1}").strip()
        model = str(raw.get("model") or "").strip()
        base_url = str(raw.get("base_url") or shared.get("base_url") or DEFAULT_VIDEO_BASE_URL).strip()
        if not profile_id or not model or not base_url:
            continue
        profiles.append({
            **raw,
            "profile_id": profile_id,
            "label": str(raw.get("label") or profile_id),
            "model": model,
            "base_url": base_url,
            "api_key": str(raw.get("api_key") or shared_key),
            "provider": infer_video_model_provider(model, str(raw.get("provider") or ""), base_url),
            "transport": str(raw.get("transport") or api_provider_from_base_url(base_url) or "").lower(),
            "enabled": bool(raw.get("enabled", True)),
        })
    if profiles:
        return profiles
    model = video_model(root)
    base_url = video_base_url(root)
    return [{
        "profile_id": "legacy",
        "label": "默认视频模型",
        "model": model,
        "base_url": base_url,
        "api_key": video_api_key(root),
        "provider": infer_video_model_provider(model, str(shared.get("provider") or ""), base_url),
        "transport": video_provider(root),
        "enabled": True,
        "quality_tier": str(shared.get("quality_tier") or "balanced"),
        "estimated_cost": shared.get("estimated_cost"),
        "supported_durations": shared.get("supported_durations"),
    }]


def video_profile(profile_id: str | None = None, workspace_root: str | Path = ".") -> dict[str, Any]:
    profiles = video_profiles(workspace_root)
    if profile_id:
        match = next((item for item in profiles if item["profile_id"] == profile_id), None)
        if match is None:
            raise ValueError(f"Unknown video profile: {profile_id}")
        return match
    config = load_agent_config(Path(workspace_root).resolve())
    section = config.get("video_profiles") if isinstance(config.get("video_profiles"), dict) else {}
    default_id = str(section.get("default") or "").strip()
    selected = next((item for item in profiles if item["profile_id"] == default_id and item["enabled"]), None)
    return selected or next((item for item in profiles if item["enabled"]), profiles[0])
