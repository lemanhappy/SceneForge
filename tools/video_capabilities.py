"""Video-provider capabilities and deterministic duration selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Optional


@dataclass(frozen=True)
class VideoCapabilities:
    """Duration controls exposed by one video generator.

    ``duration_parameter`` is the keyword accepted by ``generate_single_video``.
    A provider can either expose a discrete list of durations or an inclusive
    min/max range. Providers without a duration control may still declare their
    fixed output duration through ``default_duration``.
    """

    provider: str
    duration_parameter: Optional[str] = None
    supported_durations: tuple[int, ...] = ()
    min_duration: Optional[int] = None
    max_duration: Optional[int] = None
    duration_step: int = 1
    default_duration: Optional[int] = None


@dataclass(frozen=True)
class VideoDurationPlan:
    provider: str
    planned_duration_sec: float
    requested_duration_sec: Optional[int]
    duration_parameter: Optional[str]
    exact: bool
    reason: str

    def generation_kwargs(self) -> dict[str, int]:
        if self.duration_parameter is None or self.requested_duration_sec is None:
            return {}
        return {self.duration_parameter: self.requested_duration_sec}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_video_capabilities(generator: Any) -> VideoCapabilities:
    capabilities = getattr(generator, "video_capabilities", None)
    if isinstance(capabilities, VideoCapabilities):
        return capabilities
    return VideoCapabilities(provider=type(generator).__name__)


def plan_video_duration(generator: Any, planned_duration_sec: float) -> VideoDurationPlan:
    """Select the closest duration the configured backend can actually request."""

    planned = float(planned_duration_sec)
    if not math.isfinite(planned) or planned <= 0:
        raise ValueError("planned_duration_sec must be a positive finite number")

    capabilities = get_video_capabilities(generator)
    requested: Optional[int]
    reason: str

    supported = tuple(sorted(set(capabilities.supported_durations)))
    if supported:
        # On an equal-distance tie, prefer the longer clip so a performance beat
        # is not cut short.
        requested = min(supported, key=lambda value: (abs(value - planned), -value))
        reason = "exact" if _same_duration(planned, requested) else "nearest_supported"
    elif capabilities.min_duration is not None and capabilities.max_duration is not None:
        minimum = capabilities.min_duration
        maximum = capabilities.max_duration
        step = max(1, capabilities.duration_step)
        units = math.floor(((planned - minimum) / step) + 0.5)
        requested = min(maximum, max(minimum, minimum + units * step))
        reason = "exact" if _same_duration(planned, requested) else "nearest_supported"
    else:
        requested = capabilities.default_duration
        if requested is None:
            reason = "backend_unspecified"
        else:
            reason = "exact" if _same_duration(planned, requested) else "backend_fixed"

    return VideoDurationPlan(
        provider=capabilities.provider,
        planned_duration_sec=planned,
        requested_duration_sec=requested,
        duration_parameter=capabilities.duration_parameter,
        exact=requested is not None and _same_duration(planned, requested),
        reason=reason,
    )


def storyboard_duration_instruction(generator: Any) -> str:
    """Return a planning hint that keeps new shots aligned with the backend."""

    capabilities = get_video_capabilities(generator)
    supported = tuple(sorted(set(capabilities.supported_durations)))
    if supported:
        choices = ", ".join(str(value) for value in supported)
        return (
            "Video backend duration constraint: set each shot's duration_sec to "
            f"one of [{choices}] seconds. Keep every performance beat within that duration."
        )
    if capabilities.duration_parameter and capabilities.min_duration is not None and capabilities.max_duration is not None:
        return (
            "Video backend duration constraint: use whole-second duration_sec values "
            f"from {capabilities.min_duration} to {capabilities.max_duration} seconds, inclusive. "
            "Keep every performance beat within that duration."
        )
    if capabilities.default_duration is not None:
        return (
            "Video backend duration constraint: each generated shot has a fixed duration of "
            f"{capabilities.default_duration} seconds. Set duration_sec accordingly and keep "
            "every performance beat within that duration."
        )
    return ""


def _same_duration(left: float, right: float) -> bool:
    return abs(left - right) < 1e-6
