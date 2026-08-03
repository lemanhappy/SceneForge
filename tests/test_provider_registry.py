import tempfile
from pathlib import Path

import yaml

from domain.providers import MediaType, ModelRequirement, ProviderCapability, QualityTier
from services.provider_registry import NoCompatibleProviderError, ProviderRegistry
from services.quality_profiles import apply_quality_profile, get_quality_profile, public_quality_profiles


def _video(model, tier, cost, **overrides):
    values = dict(
        provider_id="cloud",
        model_id=model,
        media_type=MediaType.VIDEO,
        image_to_video=True,
        first_last_frame=True,
        multi_reference=True,
        supported_aspect_ratios=("landscape", "portrait"),
        supported_durations=(5, 10),
        max_reference_count=2,
        estimated_cost=cost,
        quality_tier=tier,
    )
    values.update(overrides)
    return ProviderCapability(**values)


def test_router_filters_hard_requirements_before_quality_ranking():
    registry = ProviderRegistry([
        _video("cheap", QualityTier.ECONOMY, 1.0, first_last_frame=False),
        _video("balanced", QualityTier.BALANCED, 2.0),
        _video("quality", QualityTier.QUALITY, 4.0),
    ])
    requirement = ModelRequirement(
        media_type=MediaType.VIDEO,
        image_to_video=True,
        first_last_frame=True,
        multi_reference=True,
        aspect_ratio="portrait",
        duration=5,
        reference_count=2,
    )

    economy = registry.route(requirement, quality_tier="economy")
    quality = registry.route(requirement, quality_tier="quality")

    assert economy.capability.model_id == "balanced"
    assert economy.reason == "fallback_to_balanced"
    assert quality.capability.model_id == "quality"


def test_router_rejects_unsupported_request_and_budget():
    registry = ProviderRegistry([_video("balanced", QualityTier.BALANCED, 2.0)])
    with __import__("pytest").raises(NoCompatibleProviderError):
        registry.route(ModelRequirement(media_type=MediaType.VIDEO, lora=True))
    with __import__("pytest").raises(NoCompatibleProviderError):
        registry.route(
            ModelRequirement(media_type=MediaType.VIDEO, image_to_video=True),
            max_cost=1.0,
        )


def test_registry_loads_declarative_capabilities():
    registry = ProviderRegistry.from_config({"provider_registry": {"models": [{
        "provider_id": "p",
        "model_id": "image-v1",
        "media_type": "image",
        "text_to_image": True,
        "supported_aspect_ratios": ["landscape"],
        "max_reference_count": 1,
        "execution_mode": "sync",
        "quality_tier": "economy",
    }]}})
    decision = registry.route(
        ModelRequirement(media_type=MediaType.IMAGE, text_to_image=True, aspect_ratio="landscape"),
        quality_tier="economy",
    )
    assert decision.capability.model_id == "image-v1"


def test_workspace_registry_does_not_expose_keys():
    from agent_runtime.config import load_agent_config

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "configs").mkdir()
        (root / "configs" / "agent.local.yaml").write_text(yaml.safe_dump({
            "image": {"provider": "seedream", "model": "image-v1", "base_url": "https://yunwu.ai", "api_key": "secret"},
            "video": {"model": "seedance-v1", "base_url": "https://yunwu.ai", "api_key": "secret"},
        }), encoding="utf-8")
        load_agent_config.cache_clear()
        public = ProviderRegistry.from_workspace(root).public_catalog()
        load_agent_config.cache_clear()

    assert len(public) == 2
    assert "api_key" not in str(public)
    assert all(item["enabled"] for item in public)


def test_workspace_registry_routes_across_video_profiles():
    from agent_runtime.config import load_agent_config

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "configs").mkdir()
        (root / "configs" / "agent.local.yaml").write_text(yaml.safe_dump({
            "video": {"api_key": "shared-secret", "base_url": "https://yunwu.ai/v1"},
            "video_profiles": {
                "default": "fast",
                "items": [
                    {"profile_id": "fast", "provider": "seedance", "model": "seedance-fast",
                     "base_url": "https://yunwu.ai/v1", "quality_tier": "economy",
                     "estimated_cost": 1.0, "supported_durations": [5, 10]},
                    {"profile_id": "cinema", "provider": "veo", "model": "veo-quality",
                     "base_url": "https://yunwu.ai/v1", "quality_tier": "quality",
                     "estimated_cost": 4.0, "supported_durations": [8]},
                ],
            },
        }), encoding="utf-8")
        load_agent_config.cache_clear()
        registry = ProviderRegistry.from_workspace(root)
        requirement = ModelRequirement(
            media_type=MediaType.VIDEO,
            image_to_video=True,
            first_last_frame=True,
            multi_reference=True,
            aspect_ratio="landscape",
            reference_count=2,
        )
        economy = registry.route(requirement, quality_tier="economy")
        quality = registry.route(requirement, quality_tier="quality")
        public = registry.public_catalog()
        load_agent_config.cache_clear()

    assert economy.capability.profile_id == "fast"
    assert quality.capability.profile_id == "cinema"
    assert {item["profile_id"] for item in public} == {"fast", "cinema"}
    assert "shared-secret" not in str(public)


def test_quality_profiles_adjust_costly_review_work():
    economy = apply_quality_profile({"quality": {"consistency": {"enabled": True}}}, "economy")
    quality = apply_quality_profile({}, "quality")

    assert economy["quality"]["consistency"]["video_sampling_enabled"] is False
    assert economy["quality"]["consistency"]["max_retries"] == 0
    assert quality["quality"]["consistency"]["video_sampling_enabled"] is True
    assert quality["quality"]["consistency"]["max_retries"] == 2
    assert economy["generation"]["image_candidates"] == 1
    assert quality["generation"]["image_candidates"] == 3
    assert economy["generation"]["video_candidates"] == 1
    assert quality["generation"]["video_candidates"] == 2
    assert get_quality_profile("quality").cost_multiplier == 1.6

    profiles = {item["tier"]: item for item in public_quality_profiles()}
    assert profiles["balanced"]["recommended"] is True
    assert profiles["balanced"]["image_candidates"] == 2
    assert profiles["quality"]["description"]


def test_quality_profile_preserves_explicit_consistency_settings():
    configured = {"quality": {"consistency": {
        "max_retries": 3,
        "aesthetic_threshold": 0.8,
        "temporal_threshold": 0.7,
    }}}
    economy = apply_quality_profile(configured, "economy")
    consistency = economy["quality"]["consistency"]
    assert consistency["max_retries"] == 3
    assert consistency["aesthetic_threshold"] == 0.8
    assert consistency["temporal_threshold"] == 0.7
