from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class QualityTier(str, Enum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    QUALITY = "quality"


class ExecutionMode(str, Enum):
    LOCAL = "local"
    SYNC = "sync"
    ASYNC = "async"


class ResumeStrategy(str, Enum):
    NONE = "none"
    REMOTE_TASK = "remote_task"


@dataclass(frozen=True, slots=True)
class ModelRequirement:
    media_type: MediaType
    text_to_image: bool = False
    image_to_image: bool = False
    text_to_video: bool = False
    image_to_video: bool = False
    first_last_frame: bool = False
    multi_reference: bool = False
    multi_character_reference: bool = False
    provider_character_id: bool = False
    lora: bool = False
    aspect_ratio: str | None = None
    duration: int | None = None
    reference_count: int = 0


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider_id: str
    model_id: str
    media_type: MediaType
    profile_id: str | None = None
    transport_id: str | None = None
    text_to_image: bool = False
    image_to_image: bool = False
    text_to_video: bool = False
    image_to_video: bool = False
    first_last_frame: bool = False
    multi_reference: bool = False
    multi_character_reference: bool = False
    provider_character_id: bool = False
    lora: bool = False
    supported_aspect_ratios: tuple[str, ...] = ()
    supported_durations: tuple[int, ...] = ()
    max_reference_count: int = 0
    execution_mode: ExecutionMode = ExecutionMode.ASYNC
    resume_strategy: ResumeStrategy = ResumeStrategy.NONE
    remote_cancel: bool = False
    estimated_cost: float | None = None
    quality_tier: QualityTier = QualityTier.BALANCED
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.model_id.strip():
            raise ValueError("provider_id and model_id cannot be empty")
        if self.max_reference_count < 0:
            raise ValueError("max_reference_count cannot be negative")
        if self.resume_strategy is ResumeStrategy.REMOTE_TASK and self.execution_mode is not ExecutionMode.ASYNC:
            raise ValueError("remote task resume requires async execution")

    def supports(self, requirement: ModelRequirement) -> bool:
        if not self.enabled or self.media_type != requirement.media_type:
            return False
        flags = (
            "text_to_image",
            "image_to_image",
            "text_to_video",
            "image_to_video",
            "first_last_frame",
            "multi_reference",
            "multi_character_reference",
            "provider_character_id",
            "lora",
        )
        if any(getattr(requirement, name) and not getattr(self, name) for name in flags):
            return False
        if requirement.aspect_ratio and self.supported_aspect_ratios:
            if requirement.aspect_ratio not in self.supported_aspect_ratios:
                return False
        if requirement.duration is not None and self.supported_durations:
            if requirement.duration not in self.supported_durations:
                return False
        return requirement.reference_count <= self.max_reference_count
