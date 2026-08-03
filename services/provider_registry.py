from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from domain.providers import (
    ExecutionMode,
    MediaType,
    ModelRequirement,
    ProviderCapability,
    QualityTier,
    ResumeStrategy,
)


class NoCompatibleProviderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    capability: ProviderCapability
    requested_tier: QualityTier
    reason: str
    historical_performance: dict | None = None

    def to_dict(self) -> dict:
        value = asdict(self.capability)
        for key, item in list(value.items()):
            if hasattr(item, "value"):
                value[key] = item.value
        value.update({
            "requested_tier": self.requested_tier.value,
            "reason": self.reason,
            "historical_performance": self.historical_performance,
        })
        return value


class ProviderRegistry:
    """Capability-first model catalog and deterministic automatic router."""

    def __init__(self, capabilities: Iterable[ProviderCapability] = (), *, workspace_root: str | Path | None = None) -> None:
        self._capabilities = list(capabilities)
        self._workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None

    def refresh(self) -> None:
        """Reload file-backed profiles after Settings changes without a restart."""
        if self._workspace_root is None:
            return
        current = type(self).from_workspace(self._workspace_root)
        self._capabilities = current._capabilities

    def all(self, *, enabled_only: bool = False) -> list[ProviderCapability]:
        return [item for item in self._capabilities if item.enabled or not enabled_only]

    def register(self, capability: ProviderCapability) -> None:
        key = (capability.profile_id, capability.provider_id, capability.model_id, capability.media_type)
        self._capabilities = [
            item for item in self._capabilities
            if (item.profile_id, item.provider_id, item.model_id, item.media_type) != key
        ]
        self._capabilities.append(capability)

    def route(
        self,
        requirement: ModelRequirement,
        *,
        quality_tier: QualityTier | str = QualityTier.BALANCED,
        max_cost: float | None = None,
        preferred_provider: str | None = None,
    ) -> RoutingDecision:
        tier = QualityTier(quality_tier)
        compatible = [item for item in self._capabilities if item.supports(requirement)]
        if preferred_provider:
            preferred = [item for item in compatible if item.provider_id == preferred_provider]
            if preferred:
                compatible = preferred
        if max_cost is not None:
            compatible = [
                item for item in compatible
                if item.estimated_cost is not None and item.estimated_cost <= float(max_cost)
            ]
        if not compatible:
            raise NoCompatibleProviderError(_unsupported_reason(requirement, max_cost))
        performance = self._performance()
        selected = min(
            compatible,
            key=lambda item: _route_rank(
                item, tier, _performance_for(item, performance)
            ),
        )
        selected_performance = _performance_for(selected, performance)
        base_reason = (
            "exact_quality_tier" if selected.quality_tier is tier
            else f"fallback_to_{selected.quality_tier.value}"
        )
        if selected_performance and selected_performance.get("routing_eligible"):
            base_reason += "_with_historical_performance"
        return RoutingDecision(
            capability=selected,
            requested_tier=tier,
            reason=base_reason,
            historical_performance=selected_performance,
        )

    def _performance(self) -> dict[str, dict]:
        if self._workspace_root is None:
            return {}
        from services.production_metrics import load_provider_performance

        return load_provider_performance(self._workspace_root)

    @classmethod
    def from_config(cls, config: dict) -> "ProviderRegistry":
        section = (config or {}).get("provider_registry") or {}
        capabilities = [
            _capability_from_mapping(payload)
            for payload in (section.get("models") or [])
            if isinstance(payload, dict)
        ]
        return cls(capabilities)

    @classmethod
    def from_workspace(cls, workspace_root: str | Path = ".") -> "ProviderRegistry":
        from agent_runtime.config import api_provider_from_base_url, load_agent_config, video_profiles

        config = load_agent_config(Path(workspace_root).resolve())
        image = config.get("image") if isinstance(config.get("image"), dict) else {}
        capabilities = []
        image_model = str(image.get("model") or "").strip()
        if image_model:
            image_provider = str(image.get("provider") or "").strip().lower()
            if not image_provider:
                image_provider = "seedream" if "seedream" in image_model.lower() else "nanobanana"
            capabilities.append(ProviderCapability(
                provider_id=image_provider,
                transport_id=api_provider_from_base_url(str(image.get("base_url") or "")) or None,
                model_id=image_model,
                media_type=MediaType.IMAGE,
                text_to_image=True,
                image_to_image=True,
                multi_reference=True,
                multi_character_reference=True,
                lora=bool(image.get("supports_lora", False)),
                supported_aspect_ratios=("landscape", "portrait", "square", "16:9", "9:16", "1:1"),
                max_reference_count=int(image.get("max_reference_count", 8) or 8),
                execution_mode=ExecutionMode.SYNC,
                estimated_cost=_optional_float(image.get("estimated_cost")),
                quality_tier=_quality_tier(image.get("quality_tier")),
                enabled=bool(image.get("api_key")),
            ))
        for video in video_profiles(workspace_root):
            video_model = str(video.get("model") or "").strip()
            if not video_model:
                continue
            declared = video.get("capabilities") if isinstance(video.get("capabilities"), dict) else {}
            transport = str(video.get("transport") or api_provider_from_base_url(str(video.get("base_url") or "")) or "") or None
            provider_id = str(video.get("provider") or transport or "video").strip().lower()
            capabilities.append(ProviderCapability(
                profile_id=str(video.get("profile_id") or "legacy"),
                provider_id=provider_id,
                transport_id=transport,
                model_id=video_model,
                media_type=MediaType.VIDEO,
                text_to_video=bool(declared.get("text_to_video", True)),
                image_to_video=bool(declared.get("image_to_video", True)),
                first_last_frame=bool(declared.get("first_last_frame", True)),
                multi_reference=bool(declared.get("multi_reference", True)),
                supported_aspect_ratios=tuple(video.get("supported_aspect_ratios") or
                                              ("landscape", "portrait", "square", "16:9", "9:16", "1:1")),
                supported_durations=_video_durations(video_model, provider_id, video.get("supported_durations")),
                max_reference_count=max(0, int(video.get("max_reference_count", 2) or 0)),
                execution_mode=ExecutionMode.ASYNC,
                resume_strategy=ResumeStrategy.REMOTE_TASK,
                remote_cancel=bool(video.get("remote_cancel", False)),
                estimated_cost=_optional_float(video.get("estimated_cost")),
                quality_tier=_quality_tier(video.get("quality_tier")),
                enabled=bool(video.get("enabled", True) and video.get("api_key")),
            ))
        return cls(capabilities, workspace_root=workspace_root)

    def public_catalog(self) -> list[dict]:
        return [RoutingDecision(item, item.quality_tier, "registered").to_dict()
                for item in self.all(enabled_only=True)]


def _route_rank(
    capability: ProviderCapability,
    requested: QualityTier,
    performance: dict | None = None,
) -> tuple:
    tier_rank = {QualityTier.ECONOMY: 0, QualityTier.BALANCED: 1, QualityTier.QUALITY: 2}
    cost = capability.estimated_cost if capability.estimated_cost is not None else float("inf")
    current = tier_rank[capability.quality_tier]
    target = tier_rank[requested]
    eligible = bool(performance and performance.get("routing_eligible"))
    acceptance_rate = 0.0
    effective_cost = cost
    mean_seconds = float("inf")
    if eligible:
        acceptance_rate = _optional_float(performance.get("acceptance_rate")) or 0.0
        effective_cost = _optional_float(
            performance.get("estimated_cost_per_accepted_shot")
        )
        effective_cost = cost if effective_cost is None else effective_cost
        mean_seconds = _optional_float(performance.get("mean_generation_seconds"))
        mean_seconds = float("inf") if mean_seconds is None else mean_seconds
    if requested is QualityTier.ECONOMY:
        return effective_cost, -acceptance_rate, mean_seconds, current, capability.provider_id, capability.model_id, capability.profile_id or ""
    if requested is QualityTier.QUALITY:
        return -current, -acceptance_rate, effective_cost, mean_seconds, capability.provider_id, capability.model_id, capability.profile_id or ""
    return abs(current - target), -acceptance_rate, effective_cost, mean_seconds, capability.provider_id, capability.model_id, capability.profile_id or ""


def _performance_for(
    capability: ProviderCapability, performance: dict[str, dict]
) -> dict | None:
    if capability.profile_id:
        item = performance.get(f"profile:{capability.profile_id}")
        if item:
            return item
    return performance.get(f"model:{capability.provider_id}:{capability.model_id}")


def _unsupported_reason(requirement: ModelRequirement, max_cost: float | None) -> str:
    details = [requirement.media_type.value]
    for key in (
        "image_to_image", "image_to_video", "first_last_frame",
        "multi_reference", "multi_character_reference", "provider_character_id", "lora",
    ):
        if getattr(requirement, key):
            details.append(key)
    if requirement.aspect_ratio:
        details.append(f"aspect={requirement.aspect_ratio}")
    if requirement.duration is not None:
        details.append(f"duration={requirement.duration}s")
    if requirement.reference_count:
        details.append(f"references={requirement.reference_count}")
    if max_cost is not None:
        details.append(f"max_cost={max_cost}")
    return "No enabled model supports: " + ", ".join(details)


def _capability_from_mapping(payload: dict) -> ProviderCapability:
    data = dict(payload)
    data["media_type"] = MediaType(data["media_type"])
    data["quality_tier"] = _quality_tier(data.get("quality_tier"))
    data["execution_mode"] = ExecutionMode(data.get("execution_mode", "async"))
    data["resume_strategy"] = ResumeStrategy(data.get("resume_strategy", "none"))
    for key in ("supported_aspect_ratios", "supported_durations"):
        data[key] = tuple(data.get(key) or ())
    return ProviderCapability(**data)


def _quality_tier(value) -> QualityTier:
    try:
        return QualityTier(str(value or QualityTier.BALANCED.value))
    except ValueError:
        return QualityTier.BALANCED


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    return max(0.0, float(value))


def _video_durations(model: str, provider: str, configured) -> tuple[int, ...]:
    if configured:
        return tuple(sorted({int(value) for value in configured if int(value) > 0}))
    text = f"{provider} {model}".lower()
    if "seedance" in text:
        return (5, 10)
    if "veo" in text:
        return (8,)
    if "openrouter" in text:
        return tuple(range(1, 16))
    return ()
