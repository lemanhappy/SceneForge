"""Provider-neutral LoRA binding adapter for image generation."""

from __future__ import annotations

import re
from typing import Any


class LoraAwareImageGenerator:
    def __init__(self, inner: Any, bindings: list[dict]):
        self._inner = inner
        self.bindings = [dict(item) for item in bindings if item.get("enabled", True)]

    async def generate_single_image(self, prompt: str, reference_image_paths=None, **kwargs):
        native = [item for item in self.bindings if item.get("application_mode", "native") == "native"]
        if native and not bool(getattr(self._inner, "supports_lora", False)):
            names = "、".join(str(item.get("display_name") or item.get("lora_id")) for item in native)
            raise RuntimeError(f"当前图像模型不支持原生 LoRA：{names}。请更换支持 LoRA 的提供商，或将该条目改为“仅使用触发词”。")
        words = [word for item in self.bindings for word in _trigger_words(item.get("trigger_words"))]
        if words:
            prompt = f"{prompt}\nLoRA 触发词：{', '.join(dict.fromkeys(words))}。"
        kwargs.setdefault("lora_bindings", [
            {
                "id": item.get("lora_id"),
                "provider": item.get("provider"),
                "base_model": item.get("base_model"),
                "model_ref": item.get("model_ref"),
                "weight": item.get("default_weight", 0.8),
                "application_mode": item.get("application_mode", "native"),
            }
            for item in self.bindings
        ])
        return await self._inner.generate_single_image(
            prompt=prompt,
            reference_image_paths=reference_image_paths or [],
            **kwargs,
        )


def with_project_loras(image_generator: Any, session: dict) -> Any:
    bindings = list((session or {}).get("lora_bindings") or [])
    return LoraAwareImageGenerator(image_generator, bindings) if bindings else image_generator


def _trigger_words(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,，;；\n]", str(value or "")) if item.strip()]
