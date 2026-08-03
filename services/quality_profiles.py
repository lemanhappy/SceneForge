from __future__ import annotations

import copy
from dataclasses import asdict, dataclass

from domain.providers import QualityTier


@dataclass(frozen=True, slots=True)
class QualityProfile:
    tier: QualityTier
    label: str
    cost_multiplier: float
    render_retries: int
    consistency_retries: int
    video_sampling: bool
    image_candidates: int
    video_candidates: int


QUALITY_PROFILES = {
    QualityTier.ECONOMY: QualityProfile(
        tier=QualityTier.ECONOMY,
        label="省钱",
        cost_multiplier=0.7,
        render_retries=1,
        consistency_retries=0,
        video_sampling=False,
        image_candidates=1,
        video_candidates=1,
    ),
    QualityTier.BALANCED: QualityProfile(
        tier=QualityTier.BALANCED,
        label="均衡",
        cost_multiplier=1.0,
        render_retries=2,
        consistency_retries=1,
        video_sampling=True,
        image_candidates=2,
        video_candidates=1,
    ),
    QualityTier.QUALITY: QualityProfile(
        tier=QualityTier.QUALITY,
        label="高质量",
        cost_multiplier=1.6,
        render_retries=3,
        consistency_retries=2,
        video_sampling=True,
        image_candidates=3,
        video_candidates=2,
    ),
}

_PROFILE_COPY = {
    QualityTier.ECONOMY: {
        "description": "单张关键帧、单个视频候选，速度最快",
        "speed_label": "最快",
        "recommended": False,
    },
    QualityTier.BALANCED: {
        "description": "每帧生成 2 张并自动选优，兼顾效果与成本",
        "speed_label": "适中",
        "recommended": True,
    },
    QualityTier.QUALITY: {
        "description": "每帧生成 3 张选优，每镜生成 2 个视频候选",
        "speed_label": "较慢",
        "recommended": False,
    },
}


def get_quality_profile(tier: QualityTier | str | None) -> QualityProfile:
    try:
        normalized = QualityTier(str(tier or QualityTier.BALANCED.value))
    except ValueError:
        normalized = QualityTier.BALANCED
    return QUALITY_PROFILES[normalized]


def apply_quality_profile(config: dict, tier: QualityTier | str | None) -> dict:
    profile = get_quality_profile(tier)
    result = copy.deepcopy(config or {})
    result.setdefault("generation", {})["quality_tier"] = profile.tier.value
    result["generation"]["render_retries"] = profile.render_retries
    result["generation"]["image_candidates"] = profile.image_candidates
    result["generation"]["video_candidates"] = profile.video_candidates
    consistency = result.setdefault("quality", {}).setdefault("consistency", {})
    # Explicit Settings values are authoritative. A quality tier supplies
    # defaults only when the user has not configured the corresponding control.
    consistency.setdefault("max_retries", profile.consistency_retries)
    consistency["video_sampling_enabled"] = profile.video_sampling
    if profile.tier is QualityTier.ECONOMY:
        consistency.setdefault("aesthetic_threshold", 0.0)
        consistency.setdefault("temporal_threshold", 0.0)
    return result


def public_quality_profiles() -> list[dict]:
    output = []
    for profile in QUALITY_PROFILES.values():
        item = asdict(profile)
        item["tier"] = profile.tier.value
        item.update(_PROFILE_COPY[profile.tier])
        output.append(item)
    return output
