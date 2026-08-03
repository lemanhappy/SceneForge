"""RenderBackend: config-driven factory for image and video generators.

Reads the ``image_generator`` and ``video_generator`` sections from a
SceneForge YAML config, instantiates the concrete classes via *class_path*,
and wires up rate limiters.

Usage::

    backend = RenderBackend.from_config(config)
    image = await backend.image_generator.generate_single_image(...)
    video = await backend.video_generator.generate_single_video(...)
"""

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict

from utils.rate_limiter import RateLimiter


_IMAGE_API_KEY_ENV_NAMES = (
    "SCENEFORGE_IMAGE_API_KEY",
    "SCENEFORGE_LLM_API_KEY",
    "SCENEFORGE_API_KEY",
)
_VIDEO_API_KEY_ENV_NAMES = (
    "SCENEFORGE_VIDEO_API_KEY",
    "SCENEFORGE_LLM_API_KEY",
    "SCENEFORGE_API_KEY",
)


@dataclass
class RenderBackend:
    """Bundles an image generator and a video generator."""

    image_generator: Any
    video_generator: Any

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "RenderBackend":
        """Build a RenderBackend from a parsed YAML config dict.

        Rate limiters are created from ``max_requests_per_minute`` /
        ``max_requests_per_day`` if present in each generator section.
        """
        img_cfg = config["image_generator"]
        vid_cfg = config["video_generator"]

        image_gen = _instantiate(img_cfg, _build_rate_limiter(img_cfg), _IMAGE_API_KEY_ENV_NAMES)
        video_gen = _instantiate(vid_cfg, _build_rate_limiter(vid_cfg), _VIDEO_API_KEY_ENV_NAMES)

        logging.info("RenderBackend: image=%s, video=%s",
                     img_cfg["class_path"], vid_cfg["class_path"])

        return cls(image_generator=image_gen, video_generator=video_gen)


def _build_rate_limiter(section: Dict[str, Any]) -> RateLimiter | None:
    rpm = section.get("max_requests_per_minute")
    rpd = section.get("max_requests_per_day")
    if rpm or rpd:
        return RateLimiter(max_requests_per_minute=rpm, max_requests_per_day=rpd)
    return None


def _resolve_api_key(init_args: Dict[str, Any], env_names: tuple[str, ...]) -> Dict[str, Any]:
    resolved = dict(init_args)
    if resolved.get("api_key"):
        return resolved
    for env_name in env_names:
        value = os.environ.get(env_name, "")
        if value:
            resolved["api_key"] = value
            break
    return resolved


def _instantiate(
    section: Dict[str, Any],
    rate_limiter: RateLimiter | None,
    api_key_env_names: tuple[str, ...] = (),
) -> Any:
    module_path, cls_name = section["class_path"].rsplit(".", 1)
    cls = getattr(importlib.import_module(module_path), cls_name)
    init_args = _resolve_api_key(section.get("init_args", {}), api_key_env_names)
    if rate_limiter is not None:
        init_args["rate_limiter"] = rate_limiter
    return cls(**init_args)
